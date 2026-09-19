"""
lte/cli/ingest.py -- ORCHESTRATOR. Write-path entry point into the Git CAS.

REPLACES scripts/core_pipeline/ingest.py AND batch_sync.py. Two modes:

  single  python -m lte.cli.ingest DRAFT --repo R [--target <corpus path>]
          Validates one draft against the CAS branch tip it joins
          (lte.validators.draft), then commits it as a single-file change.

  batch   python -m lte.cli.ingest --batch [CORPUS_DIR] --repo R
          REPLACES batch_sync.py, behaviour for behaviour. Walks the
          layout's scope directories under CORPUS_DIR (default: the
          layout root), classifies every file as
          ADDED / UPDATED / UNCHANGED / SKIPPED against the branch tip,
          validates the changed files against the CAS corpus
          (lte.validators.draft.validate_batch), and commits them in ONE
          commit. APPEND-ONLY: a file deleted from disk is NOT deleted from
          the CAS (Axiom IV) unless --mirror-deletions is given. A
          non-UTF-8 file is SKIPPED with a warning instead of aborting the
          batch. Prints the `[ingest] SUMMARY_JSON:` line the pipeline
          dashboard reads. The working-tree corpus lint
          (lte.validators.corpus) is an ADDITIONAL gate that batch_sync.py
          did not have; --no-lint skips it and keeps exact parity.

--dry-run validates and reports what would be written, and writes nothing.

Where the corpus lives, which files belong to it and where a staged draft
lands are answers from config/corpus_layout.yaml (config.layout).

DIAGNOSTIC PATTERNS ARE NOT ASSEMBLED HERE. This module used to build a
DiagnosticPatterns object per call and hand it to the validator; that object
was a second compilation of a config block lte/engine/grammar.py already
compiles onto the Grammar, and it reached the validator by a path an
orchestrator had no business knowing about. The validator now reads
grammar.loose_* directly off the Grammar it already receives via `config`.

Exit codes: 0 ingested / nothing to do, 1 rejected or git failure,
2 usage or configuration error. Holds no domain logic: every check lives in
lte.validators, every git call in lte.io.git_cas_client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from lte.engine.grammar import ConfigError
from lte.engine.layout import LayoutError
from lte.io import config_reader, corpus_reader
from lte.io.corpus_reader import CorpusReadError
from lte.io.git_cas_client import GitClient, GitError
from lte.validators import corpus as corpus_validator
from lte.validators import draft as draft_validator

EXIT_OK, EXIT_REJECTED, EXIT_USAGE = 0, 1, 2
DEFAULT_BRANCH = "main"
TAG = "[ingest]"
SUMMARY_PREFIX = TAG + " SUMMARY_JSON:"


def _out(message: str) -> None:
    print("%s %s" % (TAG, message))


def _err(message: str) -> None:
    print("%s %s" % (TAG, message), file=sys.stderr)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m lte.cli.ingest",
        description="Validate Markdown and commit it into the bare Git CAS repository.")
    parser.add_argument("draft", nargs="?", type=Path,
                        help="draft file (single mode); its path under drafts/ implies --target")
    parser.add_argument("--batch", nargs="?", const="", metavar="CORPUS_DIR",
                        help="sync the whole corpus in one commit (default: the layout root)")
    parser.add_argument("--repo", required=True, type=Path, help="bare CAS repository")
    parser.add_argument("--target", help="repo-relative corpus path of the destination")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--message", help="commit message")
    parser.add_argument("--author-name")
    parser.add_argument("--author-email")
    parser.add_argument("--repo-root", type=Path, help="override SRKH_REPO_ROOT discovery")
    parser.add_argument("--no-lint", action="store_true",
                        help="batch mode: skip the extra working-tree corpus lint "
                             "(changed files are still validated, as batch_sync.py did)")
    parser.add_argument("--mirror-deletions", action="store_true",
                        help="batch mode: also delete CAS files no longer on disk "
                             "(off by default: the CAS is append-only)")
    parser.add_argument("--verbose", action="store_true",
                        help="batch mode: list UNCHANGED files individually")
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    args = parser.parse_args(argv)
    if (args.draft is None) == (args.batch is None):
        parser.error("give exactly one of DRAFT or --batch")
    if args.batch is not None and args.target:
        parser.error("--target applies to single-draft mode only")
    if args.batch is None and (args.mirror_deletions or args.no_lint or args.verbose):
        parser.error("--mirror-deletions, --no-lint and --verbose apply to --batch only")
    return args


def resolve_identity(args, repo_root: Path) -> tuple:
    local = GitClient(cwd=repo_root)
    name = (args.author_name or os.environ.get("GIT_AUTHOR_NAME")
            or local.config_value("user.name"))
    email = (args.author_email or os.environ.get("GIT_AUTHOR_EMAIL")
             or local.config_value("user.email"))
    if not name or not email:
        raise ConfigError("no author identity: pass --author-name/--author-email "
                          "or set git config user.name/user.email")
    return name, email


def open_cas(repo: Path) -> GitClient:
    client = GitClient(git_dir=repo, safe_directory=True)
    if not repo.is_dir() or not client.is_repository():
        raise ConfigError("%s is not a git repository (create it with: "
                          "git init --bare %s)" % (repo, repo))
    return client


def run_single(args, config, cas: GitClient) -> int:
    layout = config.layout
    draft_path = args.draft.resolve()
    target = args.target or draft_validator.target_from_draft_path(layout, args.draft.as_posix())
    if not target:
        _err("cannot infer --target: %s is not under a %r staging directory"
             % (args.draft, layout.staging_root))
        return EXIT_USAGE
    draft_text = corpus_reader.read_document_text(draft_path)

    tip = cas.rev_parse("refs/heads/" + args.branch)
    paths = cas.list_paths(tip, layout.root_prefix(), layout.listing_suffixes()) if tip else []
    corpus = cas.read_texts(tip, paths).items() if tip else []

    report = draft_validator.validate_draft(config, draft_text, target, corpus)

    for line in report.warnings:
        _out("WARN  " + line)
    for line in report.errors:
        _err("FAIL  " + line)
    if not report.ok:
        _err("REJECTED -- %d error(s); nothing written" % len(report.errors))
        return EXIT_REJECTED

    summary = "%s (%d node(s), %d callout(s))" % (target, report.node_count, report.callout_count)
    if args.dry_run:
        _out("DRY RUN -- valid; would commit %s to %s@%s" % (summary, args.repo, args.branch))
        return EXIT_OK

    name, email = resolve_identity(args, config.repo_root)
    result = cas.commit_paths(
        args.branch, {target: draft_text.encode("utf-8")},
        message=args.message or "ingest: %s" % target,
        author_name=name, author_email=email)
    if not result.changed:
        _out("UNCHANGED -- %s already matches %s" % (target, args.branch))
    else:
        _out("COMMITTED %s -> %s@%s" % (summary, args.branch, result.commit[:12]))
    return EXIT_OK


def _summary(added, updated, unchanged, skipped, commit, started) -> str:
    return SUMMARY_PREFIX + json.dumps({
        "added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped,
        "commit": commit, "duration_s": round(time.monotonic() - started, 2),
    })


def run_batch(args, config, cas: GitClient) -> int:
    started = time.monotonic()
    repo_root, layout = config.repo_root, config.layout
    corpus_dir = Path(args.batch or layout.root or ".")
    corpus_dir = (corpus_dir if corpus_dir.is_absolute() else repo_root / corpus_dir).resolve()
    if not corpus_dir.is_dir():
        _err("corpus directory not found: %s" % corpus_dir)
        return EXIT_USAGE

    if args.no_lint:
        _out("corpus lint gate skipped (--no-lint); changed files are still validated")
    elif corpus_validator.run(repo_root, config=config) != 0:
        _err("REJECTED -- corpus lint failed; nothing written")
        return EXIT_REJECTED

    ref = "refs/heads/" + args.branch
    tip = cas.rev_parse(ref)
    existing = cas.blob_ids(tip, layout.root_prefix(), layout.listing_suffixes()) if tip else {}
    existing = {rel: oid for rel, oid in existing.items()
                if layout.parse_filename(rel.rsplit("/", 1)[-1]) and not layout.is_ignored(rel)}

    # Classify. Paths are always rooted at layout.root, whatever the local
    # directory is called. A name that does not follow the filename
    # convention is SKIPPED; a conforming name outside every partition is a
    # candidate, and validation rejects it (batch_sync.py behaviour).
    texts, skipped, warnings = {}, [], []
    for rel, path in corpus_reader.walk_corpus_files(corpus_dir, layout):
        if layout.parse_filename(path.name) is None:
            skipped.append(rel)
            continue
        try:
            texts[rel] = corpus_reader.read_document_text(path)
        except CorpusReadError as exc:
            skipped.append(rel)
            warnings.append("%s: not valid UTF-8 (%s); skipped, not ingested this run" % (rel, exc))

    ordered = sorted(texts)
    local_ids = dict(zip(ordered, cas.hash_blobs([texts[r].encode("utf-8") for r in ordered], write=False)))
    added = [r for r in ordered if r not in existing]
    updated = [r for r in ordered if r in existing and existing[r] != local_ids[r]]
    unchanged = [r for r in ordered if r in existing and existing[r] == local_ids[r]]
    removed = sorted(set(existing) - set(texts)) if args.mirror_deletions else []
    candidates = {r: texts[r] for r in added + updated}

    for rel in added:
        print("[ADDED]     %s" % rel)
    for rel in updated:
        print("[UPDATED]   %s" % rel)
    if args.verbose:
        for rel in unchanged:
            print("[UNCHANGED] %s" % rel)
    else:
        print("[UNCHANGED] %d file(s) -- content already matches the current commit." % len(unchanged))
    for rel in sorted(skipped):
        print("[SKIPPED]   %s" % rel)
    for rel in removed:
        print("[DELETED]   %s" % rel)

    if candidates:
        corpus = cas.read_texts(tip, sorted(existing)).items() if tip else []
        report = draft_validator.validate_batch(config, candidates, corpus)
        warnings += list(report.warnings)
        for line in warnings:
            print("[WARN] %s" % line, file=sys.stderr)
        if not report.ok:
            _err("REJECTED -- %d issue(s). Nothing was written to %s; the working tree is untouched."
                 % (len(report.errors), args.repo))
            for line in report.errors:
                print("  - %s" % line, file=sys.stderr)
            return EXIT_REJECTED
    else:
        for line in warnings:
            print("[WARN] %s" % line, file=sys.stderr)

    if not candidates and not removed:
        _out("Nothing to commit (%d unchanged, %d skipped). No commit created."
             % (len(unchanged), len(skipped)))
        print(_summary(0, 0, len(unchanged), len(skipped), tip, started))
        return EXIT_OK

    if args.dry_run:
        _out("DRY RUN -- validation passed for %d added + %d updated file(s)%s. Nothing committed."
             % (len(added), len(updated),
                ", %d deletion(s)" % len(removed) if removed else ""))
        return EXIT_OK

    name, email = resolve_identity(args, repo_root)
    message = args.message or (
        "batch sync: %d added, %d updated (%d unchanged, %d skipped)"
        % (len(added), len(updated), len(unchanged), len(skipped)))
    result = cas.commit_paths(
        args.branch, {rel: text.encode("utf-8") for rel, text in candidates.items()},
        message=message, author_name=name, author_email=email, removals=removed)
    commit = result.commit
    _out("OK -- committed %s on '%s' (%d added, %d updated, %d unchanged, %d skipped%s)."
         % ((commit or "")[:12], args.branch, len(added), len(updated), len(unchanged), len(skipped),
            ", %d deleted" % len(result.removed) if result.removed else ""))
    print(_summary(len(added), len(updated), len(unchanged), len(skipped), commit, started))
    return EXIT_OK


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = config_reader.load(repo_root=args.repo_root)
    except (ConfigError, LayoutError, OSError) as exc:
        _err("CONFIG ERROR: %s" % exc)
        return EXIT_USAGE
    try:
        cas = open_cas(args.repo.resolve())
        if args.batch is not None:
            return run_batch(args, config, cas)
        return run_single(args, config, cas)
    except ConfigError as exc:
        _err("CONFIG ERROR: %s" % exc)
        return EXIT_USAGE
    except (CorpusReadError, FileNotFoundError) as exc:
        _err("READ ERROR: %s" % exc)
        return EXIT_REJECTED
    except GitError as exc:
        _err("GIT ERROR: %s" % exc)
        return EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
