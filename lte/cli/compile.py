#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lte/cli/compile.py -- ORCHESTRATOR. Write-Model generation entry point.

REPLACES BOTH parse_graph.py AND compile_from_git.py. Those two scripts
differed in one respect -- where the Markdown bytes come from -- yet each
carried its own copy of the assembly loop. That difference is now an
injected SourceProvider; everything after source selection is one path
through lte.engine.graph_builder.

    --source disk    -> DiskSourceProvider   (was parse_graph.py)
    --source cas     -> CasSourceProvider    (was compile_from_git.py)

WHAT THIS MODULE DOES: parse argv, load config, bind the engine's
grammar-/taxonomy-taking functions into the plain callables graph_builder
expects (bind_collaborators), write through lte.io.artifact_writer, and map
the outcome to an exit code. No parsing, assembly or file writing of its own.

NO ROLE NAME IS SPELLED HERE. bind_collaborators passes the taxonomy through
and _summarize_lang reads its role table; adding a dependent role in config
changes this module not at all.

COMPILE TARGETS come from config/corpus_layout.yaml: `--scope` names a
target (or alias), which decides the partitions read, the artifact written,
the locales emitted and the source prefixes the artifact must never contain.

EXCLUSION POLICY (see graph_builder's docstring):
  default       production behaviour of compile_from_git.py: unbalanced
                fences, duplicate anchors and misplaced anchors are
                excluded and reported; the build succeeds.
  --soft-fail   additionally excludes files that cite an anchor in a
                higher-visibility scope (parse_graph.py --soft-fail).
  --strict      fails, writing nothing, if anything had to be excluded.

RUNS IN THE CONTAINER (Python 3.11): it imports lte.engine.

Exit codes: 0 written, 1 fatal or --strict failure, 2 usage.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import sys
from pathlib import Path

from lte.engine import ast_blocks, frontmatter, graph_builder, integrity, rendering, state_machine
from lte.engine.grammar import ConfigError
from lte.engine.layout import PUBLIC_EXPOSURE, LayoutError
from lte.engine.taxonomy import GRAPH_SCHEMA_VERSION
from lte.io import artifact_writer, config_reader
from lte.io.artifact_writer import ArtifactWriteError
from lte.io.corpus_reader import CorpusReadError
from lte.io.git_cas_client import GitClient, GitError
from lte.io.source_provider import CasSourceProvider, DiskSourceProvider

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
TAG = "[compile]"
# graph.json "summary" length. The legacy compilers used 160;
# lte.engine.rendering.SUMMARY_MAX_LEN is 280. Pinned here so the payload,
# and therefore graph_hash, does not change as a side effect of the move.
SUMMARY_MAX_LEN = 160


def _err(message: str) -> None:
    sys.stderr.write("%s %s\n" % (TAG, message))


def bind_collaborators(config, renderer) -> dict:
    """Binds engine functions to this config's grammar, taxonomy and layout."""
    grammar, taxonomy, layout = config.grammar, config.taxonomy, config.layout

    def title_from_text(rel_path, text):
        info = layout.classify(rel_path)
        return ast_blocks.title_from_text(grammar, text, info.document_id if info else rel_path)

    return {
        "layout": layout,
        "parse_text": functools.partial(ast_blocks.parse_text, grammar),
        "parent_anchor_of": functools.partial(integrity.parent_anchor_of, grammar),
        "render": renderer,
        "summarize": functools.partial(rendering.plain_text_summary, max_len=SUMMARY_MAX_LEN),
        "metadata_from_text": functools.partial(frontmatter.metadata_from_text,
                                                fence=grammar.frontmatter_fence),
        "normalize_status": frontmatter.normalize_status,
        "resolve_dependent_status": functools.partial(state_machine.resolve_dependent_status, grammar),
        "title_from_text": title_from_text,
        "prologue_from_text": functools.partial(ast_blocks.prologue_from_text, grammar),
        "find_duplicate_spec_ids": integrity.find_duplicate_spec_ids,
        "find_misplaced_anchors": functools.partial(integrity.find_misplaced_anchors, layout, taxonomy),
        "find_orphan_refs": integrity.find_orphan_refs,
        "find_cross_boundary_refs": functools.partial(integrity.find_cross_boundary_refs, layout, taxonomy),
        "taxonomy": taxonomy,
        "status_values": tuple(grammar.status_values),
        "document_roles": tuple(grammar.document_roles),
    }


def build_source(args, config):
    """The ONE place a source backend is chosen by name."""
    if args.source == "disk":
        return DiskSourceProvider(config.repo_root, config.layout)
    if args.repo is None:
        raise ConfigError("--source cas requires --repo <bare repo>")
    client = GitClient(git_dir=args.repo, safe_directory=True)
    if not args.repo.is_dir() or not client.is_repository():
        raise ConfigError("%s is not a git repository" % args.repo)
    commit = client.rev_parse("refs/heads/" + args.branch)
    if commit is None:
        raise ConfigError("refs/heads/%s does not exist in %s -- nothing has been ingested. "
                          "Run the ingest step first." % (args.branch, args.repo))
    return CasSourceProvider(args.repo, commit, client, config.layout)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m lte.cli.compile", description="Compile the Markdown corpus into a JSON Graph.")
    parser.add_argument("--source", choices=["disk", "cas"], default="disk")
    parser.add_argument("--repo-root", type=Path, default=None,
                        help="checkout holding the corpus and config/ (default: discovered)")
    parser.add_argument("--config-dir", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=None, help="bare repository for --source cas")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--scope", default=None, metavar="TARGET",
                        help="compile target id or alias from config/corpus_layout.yaml "
                             "(default: the first declared target)")
    parser.add_argument("--output", type=Path, default=None,
                        help="override the output path (relative paths resolve against the cwd)")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--soft-fail", action="store_true",
                        help="also exclude files that cite a private anchor from public scope")
    policy.add_argument("--strict", action="store_true",
                        help="fail, writing nothing, if any content had to be excluded")
    return parser.parse_args(argv)


def _summarize_lang(scope: str, lang: str, graph: dict, taxonomy) -> str:
    """One line per locale. Dependent counts come from the ROLE TABLE.

    This function used to sum n["debates"] and n["ops"] by name -- the same
    two-role assumption that made graph_builder route every third role into
    "ops". Here it was quieter: a declared role with no hardcoded line simply
    never appeared in the summary, so `evi` entries would compile, index and
    render while the operator's only per-build readout said nothing about
    them. Driven off taxonomy.dependent_roles, a new role counts itself.
    """
    partitions = graph["partitions"]
    documents = sum(len(p["documents"]) for p in partitions)
    nodes = [n for p in partitions for d in p["documents"] for n in d["nodes"]]
    per_role = []
    for role in taxonomy.dependent_roles:
        graph_key = taxonomy.callout_graph_key(role)
        per_role.append("%d %s" % (sum(len(n.get(graph_key, ())) for n in nodes), graph_key))
    per_partition = ["%s=%ddoc/%dnode" % (p["id"], len(p["documents"]),
                                          sum(d["node_count"] for d in p["documents"]))
                     for p in partitions if p["documents"]]
    return ("%s scope=%s lang=%s: %d document(s) [%s], %d node(s), %s"
            % (TAG, scope, lang, documents, ", ".join(per_partition) or "none",
               graph_builder.total_nodes(graph), ", ".join(per_role)))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = config_reader.load(config_dir=args.config_dir, repo_root=args.repo_root)
    except (ConfigError, LayoutError, OSError) as exc:
        _err("FATAL: %s" % exc)
        return EXIT_FAILED
    layout = config.layout
    if args.scope is None:
        args.scope = layout.targets[0].id
    try:
        target = layout.target(args.scope)
    except LayoutError as exc:
        _err(str(exc))
        return EXIT_USAGE
    try:
        source = build_source(args, config)
    except (ConfigError, OSError) as exc:
        _err("FATAL: %s" % exc)
        return EXIT_FAILED
    except GitError as exc:
        _err("FATAL (git): %s" % exc)
        return EXIT_FAILED

    if target.exposure != PUBLIC_EXPOSURE:
        _err("WARNING: target %r is restricted (scopes: %s). Serve its artifact only behind "
             "an authenticated route." % (args.scope, ", ".join(target.scopes)))

    collaborators = bind_collaborators(config, rendering.build_renderer())
    diagnostics = []
    per_lang = {}
    try:
        for lang in layout.locales:
            per_lang[lang] = graph_builder.build_lang_graph(
                source.iter_documents(args.scope, lang), lang, args.scope,
                exclude_cross_boundary=args.soft_fail, diagnostics=diagnostics,
                **collaborators)
    except (CorpusReadError, UnicodeDecodeError, GitError) as exc:
        _err("FATAL: cannot read the corpus: %s" % exc)
        return EXIT_FAILED

    full_graph = graph_builder.build_full_graph(
        per_lang,
        scope=args.scope,
        locales=layout.locales,
        schema_version=GRAPH_SCHEMA_VERSION,
        status_values=config.grammar.status_values,
        invalidated_state=state_machine.invalidated_state(config.grammar),
        clock=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    errors = [d for d in diagnostics if d["level"] == "error"]
    for diag in diagnostics:
        sys.stderr.write("[%s] %s\n" % (diag["level"].upper(), diag["message"]))
    if args.strict and errors:
        _err("FAILED -- --strict and %d item(s) had to be excluded; nothing written." % len(errors))
        return EXIT_FAILED

    output = args.output or artifact_writer.artifact_path(config.repo_root, layout, args.scope)
    try:
        result = artifact_writer.write_graph(
            full_graph, output,
            accepted_scopes=layout.target_accepts(args.scope),
            restricted_prefixes=layout.restricted_prefixes(args.scope))
    except ArtifactWriteError as exc:
        _err("FATAL: %s" % exc)
        return EXIT_FAILED

    for lang in layout.locales:
        print(_summarize_lang(args.scope, lang, per_lang[lang], config.taxonomy))
    print("%s Wrote %s (%s, %s, %d total node(s)%s)." % (
        TAG, output, source.describe(), result.strategy, full_graph["metadata"]["total_nodes"],
        ", %d item(s) excluded" % len(errors) if errors else ""))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
