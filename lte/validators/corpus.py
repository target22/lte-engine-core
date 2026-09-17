#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lte/validators/corpus.py -- LAYER 2 VALIDATOR. Referential integrity and
dependent-lifecycle linting over the full corpus.

Replaces scripts/verification_and_testing/validate_markdown.py. Behaviour is
preserved: same five checks, same report grouping, same exit codes (0 ok /
1 issues found / 0 under --soft), same --file scoping semantics.

ALWAYS SCANS THE FULL CORPUS -- every walk directory the corpus layout
declares (config/corpus_layout.yaml), in every scope -- regardless of which
compile target a later build will use. This is a single repo-wide lint pass,
not a target-filtered build step, and it is the one place the cross-boundary
rule is enforced at all.

The five checks:
  1. Code fence parity.
  2. Referential integrity (orphan refs, duplicate anchors), per locale.
  3. Partition placement (anchor domain vs. physical directory).
  4. Cross-boundary security rule (a file citing an anchor in a scope with a
     higher level than its own).
  5. Dependent-node lifecycle front matter -- conditional required fields on
     a .debate/.ops/.evi file's independent `status`. The only
     severity-driven check; see the loop below and
     config/linter_rules.json's dependent_lifecycle.severity.

LAYER POSITION. Imports lte.engine (integrity, state_machine) and lte.io
(config_reader, corpus_reader, build_cache). Imports nothing from lte.cli --
enforced mechanically by lte/validators/architecture.py, not merely intended.
That direction matters: lte/cli/lint.py is a thin entry point that calls
run(), so any logic living here stays reusable by the pre-commit hook and the
VS Code bridge without either of them importing an orchestrator.

PATHS COME FROM THE CORPUS LAYOUT. The walk, each file's locale, partition
placement and scope levels are all CorpusLayout answers (config.layout).
This module names no directory, no scope and no filename suffix.

SCOPE NOTE vs. the compiler's. This linter walks the layout's WHOLE walk
directories (ignore rules applied), so it reports on a file sitting in a
directory that is not a registered partition at all. Such a file joins the
per-locale checks by its NAME (layout.locale_of_name), so an anchor in it is
reported as misplaced. The compiler's own discovery only ever walks
registered partition directories, so such a file is invisible to it, not
"skipped" by any decision. If a file's errors here never appear in the
compiler's per-file table, that is why.

REPORT GROUPING. Issues are grouped by file -- useful when most of a large
error count traces to one or two problem files. This changes what is PRINTED,
not what is detected or how the exit code is computed. An issue naming
multiple files (duplicate anchor, cross-boundary) is listed under EACH of
them, since each side needs the same information to fix its own half.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lte.engine import integrity, state_machine
from lte.io import build_cache, config_reader, corpus_reader

_USE_COLOR = sys.stdout.isatty()
_RED = "\033[31m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_DIM = "\033[2m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""


def check_fence_parity(path: Path) -> list:
    """An odd number of ``` markers means one fence is unclosed.

    Reads through corpus_reader so this module opens no file itself -- the
    layer rule is not "mostly no I/O".
    """
    lines = corpus_reader.read_document_text(path).splitlines()
    fence_count = sum(1 for line in lines if line.strip().startswith("```"))
    if fence_count % 2 != 0:
        return ["odd number of ``` fence markers ({0}) -- one code fence is "
                "unclosed".format(fence_count)]
    return []


def lang_of(layout, rel_path: str) -> str | None:
    """Locale of a repo-relative path, from its file NAME under the layout's
    filename convention; None for a name that does not conform.

    Name-based on purpose: a conforming file outside every partition
    directory still takes part in the per-locale checks, so its anchors are
    reported as misplaced instead of silently skipped.
    """
    return layout.locale_of_name(rel_path)


def run(repo_root: Path, config=None, target: Path | None = None,
        soft: bool = False, emit=print) -> int:
    """Lints the corpus. Returns the process exit code.

    `config` is an EngineConfig; loaded here when not supplied, so a caller
    that already has one (the pre-commit hook, which needs the same Grammar)
    does not pay for a second config read and cannot accidentally lint
    against a different taxonomy than it validates against.

    `emit` is injected so the VS Code bridge can collect lines instead of
    writing to a terminal.
    """
    if config is None:
        config = config_reader.load(repo_root=repo_root)
    grammar = config.grammar
    taxonomy = config.taxonomy
    layout = config.layout
    kinds = taxonomy.admonition_callout_kind

    corpus_dir = repo_root / layout.root if layout.root else repo_root
    if not corpus_dir.is_dir():
        sys.stderr.write("[validate] FATAL: {0} does not exist.\n".format(corpus_dir))
        return 1

    target_rel = None
    if target is not None:
        target = target.resolve()
        if not target.is_file():
            sys.stderr.write("[validate] FATAL: --file {0} does not exist.\n".format(target))
            return 1
        try:
            target_rel = target.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            sys.stderr.write(
                "[validate] FATAL: --file {0} is not under {1}.\n".format(target, repo_root))
            return 1

    walked = corpus_reader.walk_corpus_files(corpus_dir, layout)
    all_md_files = [path for rel, path in walked if layout.has_corpus_extension(rel)]
    rel_of = {path: rel for rel, path in walked}
    if not all_md_files:
        emit("[validate] No files with extension(s) {0} found under {1} -- nothing to "
             "validate.".format(", ".join(layout.extensions), corpus_dir))
        return 0

    cache = build_cache.load_build_cache(repo_root)
    cache_hits = 0
    cache_misses = 0
    entries: list = []

    parsed_by_path: dict = {}
    for path in all_md_files:
        nodes, callouts, was_hit = build_cache.cached_or_parse(
            grammar, kinds, path, repo_root, cache)
        parsed_by_path[path] = (nodes, callouts, was_hit)
        if was_hit:
            cache_hits += 1
        else:
            cache_misses += 1

    # --- Check 1: fence parity ----------------------------------------
    # Runs on every file, cache hit or not. It is a trivial per-file count,
    # never the expensive part caching exists for. Skipping it on a hit was
    # tried and reverted in the legacy script: it silently stopped
    # re-reporting a file's fence error the moment the file stopped
    # changing, even though the file was still just as broken -- a
    # correctness regression, not a speedup.
    fence_scope = [target] if target is not None else all_md_files
    for path in fence_scope:
        rel_str = rel_of.get(path) or corpus_reader.relative_label(path, repo_root)
        for msg in check_fence_parity(path):
            entries.append(("{0}: {1}".format(rel_str, msg), {rel_str}))

    # --- Check 5: dependent-node lifecycle front matter ----------------
    # Runs over every file regardless of --file; --file narrows only what is
    # PRINTED, same as every other check here.
    #
    # Front matter is not part of the SHA-256 parse cache at all, so this
    # runs on a cache hit too -- for the same reason the fence check does.
    #
    # Uses extract_frontmatter (RAW), never document_metadata_of. That
    # function coerces to seven rendered fields and DROPS archived_reason,
    # so feeding it here reports a correctly-tagged file as missing the very
    # field it carries. Not hypothetical: it was caught in testing when the
    # pre-commit hook used the wrong one.
    #
    # SEVERITY IS HONORED, not flattened. This validator has no severity
    # concept of its own -- every entry it collects fails the build -- so
    # only 'fail' diagnostics join `entries`. Lower severities print to
    # stderr and leave the exit code alone. Collecting a 'warn' into
    # `entries` would make the configured severity a lie.
    for path in all_md_files:
        rel_str = rel_of[path]
        raw_frontmatter = corpus_reader.extract_frontmatter(path, grammar)
        for diag in state_machine.check_dependent_lifecycle(
                grammar, rel_str, raw_frontmatter, layout=layout):
            message = "{0}: [{1}] {2}".format(rel_str, diag["source"], diag["message"])
            if diag["severity"] == "fail":
                entries.append((message, {rel_str}))
            else:
                sys.stderr.write("[{0}] {1}\n".format(diag["severity"].upper(), message))

    # --- Checks 2-4: per-locale corpus-wide integrity ------------------
    # vi and en are two INDEPENDENT corpora: a vi debate must resolve
    # against a vi anchor. Whether the two agree with each other is a
    # different question entirely -- see lte/validators/bilingual.py.
    locales = tuple(layout.locales)
    lang_by_path = {path: lang_of(layout, rel_of[path]) for path in all_md_files}
    langs = (lang_of(layout, target_rel),) if target is not None else locales
    for lang in langs:
        if lang is None:
            continue

        nodes: list = []
        callouts: list = []
        for path in all_md_files:
            if lang_by_path[path] != lang:
                continue
            n, c, _ = parsed_by_path[path]
            nodes.extend(n)
            callouts.extend(c)

        for orphan in integrity.find_orphan_refs(nodes, callouts):
            entries.append((
                "{0}:{1}: [!{2}-{3}] references '{3}', which has no matching ^{3} anchor "
                "anywhere in the '{4}' corpus".format(
                    orphan.source_file, orphan.line_no, orphan.kind, orphan.target_id, lang),
                {orphan.source_file},
            ))

        for spec_id, dupes in integrity.find_duplicate_spec_ids(nodes).items():
            locations = ", ".join("{0}:{1}".format(n.source_file, n.line_no) for n in dupes)
            entries.append((
                "duplicate anchor ^{0} defined {1} times in the '{2}' corpus -- ambiguous "
                "link target ({3})".format(spec_id, len(dupes), lang, locations),
                {n.source_file for n in dupes},
            ))

        for node, expected, actual in integrity.find_misplaced_anchors(layout, taxonomy, nodes):
            entries.append((
                "{0}:{1}: anchor ^{2} belongs under '{3}/' based on its domain prefix, but "
                "is authored under '{4}/' instead".format(
                    node.source_file, node.line_no, node.spec_id, expected, actual or "?"),
                {node.source_file},
            ))

        for violation in integrity.find_cross_boundary_refs(layout, taxonomy, nodes, callouts):
            source_scope = layout.partition_of_path(violation.source_file).scope
            target_partition = taxonomy.partition_of(violation.target_id)
            target_scope = layout.partition(target_partition).scope if target_partition else "?"
            entries.append((
                "{0}:{1}: SECURITY BOUNDARY VIOLATION -- callout [!{2}-{3}] in scope "
                "'{5}' targets '{3}', which is defined in higher-visibility scope '{6}', "
                "in the '{4}' corpus".format(
                    violation.source_file, violation.line_no, violation.kind,
                    violation.target_id, lang, source_scope, target_scope),
                {violation.source_file},
            ))

    build_cache.save_build_cache(repo_root, cache)
    cache_note = "[cache: {0} hit, {1} parsed]".format(cache_hits, cache_misses)

    if target_rel is not None:
        visible = [(msg, files) for msg, files in entries if target_rel in files]
    else:
        visible = entries

    if visible:
        by_file: dict = {}
        for msg, files in visible:
            for rel in files:
                by_file.setdefault(rel, []).append(msg)

        scope_note = " (showing only {0})".format(target_rel) if target_rel else ""
        emit("{0}[validate] FAILED -- {1} issue(s) across {2} file(s){3} {4}{5}\n".format(
            _BOLD, len(visible), len(by_file), scope_note, cache_note, _RESET))
        for rel, msgs in sorted(by_file.items()):
            unique = list(dict.fromkeys(msgs))
            emit("{0}{1}{2} {3}({4} issue{5}){6}".format(
                _RED, rel, _RESET, _DIM, len(unique), "s" if len(unique) != 1 else "", _RESET))
            for msg in unique:
                emit("  {0}-{1} {2}".format(_YELLOW, _RESET, msg))
            emit("")

        if soft:
            emit("[validate] SOFT MODE: exiting 0 despite {0} issue(s) above -- run the "
                 "compiler with --soft-fail to compile around them.".format(len(entries)))
            return 0
        return 1 if entries else 0

    if target_rel is not None and entries:
        emit("[validate] {0}: no issues involving this file ({1} issue(s) remain elsewhere "
             "in the corpus). {2}".format(target_rel, len(entries), cache_note))
        return 0 if soft else 1

    emit("[validate] OK -- {0} file(s), referential integrity, partition placement, the "
         "scope boundary rule, and dependent-node lifecycles hold for {1}. "
         "{2}".format(all_md_files.__len__(), " and ".join(locales), cache_note))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lte.validators.corpus",
        description="Lint the corpus for referential integrity and lifecycle correctness.")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--file", type=Path, default=None, help=(
        "Print only the errors involving this file. The full corpus is still parsed and "
        "checked underneath -- this changes what is printed, not what is checked."))
    parser.add_argument("--soft", action="store_true",
                        help="Report every issue found but always exit 0.")
    args = parser.parse_args(argv)

    repo_root = args.repo_root or config_reader.resolve_repo_root()
    return run(Path(repo_root), target=args.file, soft=args.soft)


if __name__ == "__main__":
    sys.exit(main())
