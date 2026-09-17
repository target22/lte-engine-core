#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lte/validators/bilingual.py -- LAYER 2 VALIDATOR. Reports where the locale
corpora have drifted apart inside one compiled graph.

Replaces scripts/verification_and_testing/verify_bilingual_parity.py.

WHY NOTHING ELSE CATCHES THIS. Every referential-integrity check in this
pipeline is deliberately scoped to ONE language: parse_corpus() takes a
per-language glob, and find_orphan_refs()/find_duplicate_spec_ids() run over
vi and en as two independent corpora. That is correct -- a vi debate must
resolve against a vi anchor -- but it means vi and en can diverge without a
single warning. The legacy graph_lib docstring asserted the two "share
anchors by construction". Nothing verified it.

The result is not a broken build; it is two different knowledge graphs
served under one language switch. A reader toggling vi/en in the tri-pane UI
sees a different corpus, not a translation of the same one.

WHICH LOCALES. The locale set is not a constant here. run() uses, in order:
an explicit `locales` argument (--locales), the graph's own
metadata.locales (written by lte.cli.compile), or the corpus layout's
locales (config/corpus_layout.yaml). The first locale is the reference;
every other locale is compared against it. A single-locale corpus has
nothing to compare and exits 0.

REPORT-ONLY BY DEFAULT, exiting 0, because a corpus that is mid-translation
is a normal state, not a build failure. Pass --strict to exit 1 -- correct
once a partition is meant to be fully bilingual.

LAYER POSITION. Reads a COMPILED graph.json, so it needs no Grammar, no
Taxonomy, and no corpus access. Only main() reads config, and only when
neither --locales nor the graph declares a locale set. It sits in
lte/validators/ rather than lte/engine/ because it reads a file, and it
takes a path rather than a parsed dict because the artifact is its input,
not an intermediate someone else already holds.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

_DOC_PREVIEW = 8
_ANCHOR_PREVIEW = 6


def index(graph: dict, lang: str) -> dict:
    """{partition_id: {"docs": {document_id: node_count}, "anchors": set()}}

    Pure over an already-parsed graph -- no I/O, so a caller holding a graph
    in memory (the compiler, immediately after building one) can call this
    without a round-trip through disk.
    """
    out: dict = {}
    for partition in graph.get(lang, {}).get("partitions", []):
        docs: dict = {}
        anchors: set = set()
        for doc in partition.get("documents", []):
            docs[doc["document_id"]] = doc.get("node_count", 0)
            for node in doc.get("nodes", []):
                anchors.add(node["anchor_id"])
        out[partition["id"]] = {"docs": docs, "anchors": anchors}
    return out


def graph_locales(graph: dict) -> tuple | None:
    """metadata.locales as written by lte.cli.compile, or None."""
    metadata = graph.get("metadata")
    declared = metadata.get("locales") if isinstance(metadata, dict) else None
    if isinstance(declared, list) and all(isinstance(l, str) and l for l in declared):
        return tuple(declared)
    return None


def compare(graph: dict, locales: Sequence[str]) -> tuple:
    """Returns (rows, details) for the pair (locales[0], locales[1]).
    Pure; no printing, no I/O.

    Split out from the reporting below so the pre-commit hook and the
    compiler can consult parity programmatically without parsing this
    module's stdout.
    """
    left, right = locales[0], locales[1]
    a = index(graph, left)
    b = index(graph, right)

    rows = []
    details = []
    for pid in sorted(set(a) | set(b)):
        v = a.get(pid, {"docs": {}, "anchors": set()})
        e = b.get(pid, {"docs": {}, "anchors": set()})
        vd, ed = len(v["docs"]), len(e["docs"])
        vn, en_ = sum(v["docs"].values()), sum(e["docs"].values())

        only_left_docs = sorted(set(v["docs"]) - set(e["docs"]))
        only_right_docs = sorted(set(e["docs"]) - set(v["docs"]))
        only_left_anchors = sorted(v["anchors"] - e["anchors"])
        only_right_anchors = sorted(e["anchors"] - v["anchors"])
        # Same document present in BOTH, different contract count. A stronger
        # signal than a missing translation: the two files exist and
        # disagree, so this is content drift rather than pending work.
        shared_mismatch = sorted(
            (d, v["docs"][d], e["docs"][d])
            for d in set(v["docs"]) & set(e["docs"])
            if v["docs"][d] != e["docs"][d]
        )

        note = ""
        if only_left_docs or only_right_docs:
            note += " docs {0:+d}".format(ed - vd)
        if shared_mismatch:
            note += " {0} pair(s) disagree".format(len(shared_mismatch))
        if only_left_anchors or only_right_anchors:
            note += " anchors {0:+d}".format(len(e["anchors"]) - len(v["anchors"]))

        rows.append((pid, "{0}/{1}".format(vd, vn), "{0}/{1}".format(ed, en_),
                     note.strip() or "matched"))
        if note:
            details.append((pid, only_left_docs, only_right_docs, shared_mismatch,
                            only_left_anchors, only_right_anchors))
    return rows, details


def _preview(items, limit) -> str:
    return ", ".join(items[:limit]) + ("..." if len(items) > limit else "")


def _report_pair(graph_path: Path, graph: dict, left: str, right: str, emit) -> int:
    """Prints one pair's table and details. Returns the number of diverging partitions."""
    rows, details = compare(graph, (left, right))

    emit("[parity] {0}: {1} vs {2}".format(graph_path, left, right))
    emit("{0:<16} {1:>12} {2:>12}   {3}".format(
        "partition", "{0} doc/node".format(left), "{0} doc/node".format(right), "divergence"))
    emit("-" * 72)
    for pid, lcol, rcol, note in rows:
        emit("{0:<16} {1:>12} {2:>12}   {3}".format(pid, lcol, rcol, note))

    if not details:
        emit("-" * 72)
        emit("[parity] {0} and {1} are in full parity.".format(left, right))
        return 0

    emit("-" * 72)
    for pid, only_l_docs, only_r_docs, mismatch, only_l_anchors, only_r_anchors in details:
        emit("\n{0}:".format(pid))
        if only_r_docs:
            emit("  {0}-only documents ({1}): {2}".format(
                right, len(only_r_docs), _preview(only_r_docs, _DOC_PREVIEW)))
        if only_l_docs:
            emit("  {0}-only documents ({1}): {2}".format(
                left, len(only_l_docs), _preview(only_l_docs, _DOC_PREVIEW)))
        for doc, lcount, rcount in mismatch:
            emit("  sibling pair disagrees: {0}  {1}={2} node(s), {3}={4} node(s)".format(
                doc, left, lcount, right, rcount))
        if only_r_anchors:
            emit("  {0}-only anchors ({1}): {2}".format(
                right, len(only_r_anchors), _preview(only_r_anchors, _ANCHOR_PREVIEW)))
        if only_l_anchors:
            emit("  {0}-only anchors ({1}): {2}".format(
                left, len(only_l_anchors), _preview(only_l_anchors, _ANCHOR_PREVIEW)))
    emit("\n[parity] {0} partition(s) diverge between {1} and {2}.".format(
        len(details), left, right))
    return len(details)


def run(graph_path: Path, strict: bool = False, locales: Sequence[str] | None = None,
        emit=print) -> int:
    """Compares every locale against the first one.

    `locales` None means: take metadata.locales from the graph. A caller
    with neither (a graph compiled before metadata.locales existed) must
    pass them; main() supplies the corpus layout's.
    """
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write("[parity] FATAL: cannot read {0}: {1}\n".format(graph_path, exc))
        return 1

    locales = tuple(locales) if locales else graph_locales(graph)
    if not locales:
        sys.stderr.write(
            "[parity] FATAL: {0} declares no metadata.locales; pass --locales.\n".format(
                graph_path))
        return 1
    missing = [l for l in locales if l not in graph]
    if missing:
        sys.stderr.write("[parity] FATAL: {0} has no top-level key for locale(s) {1}.\n".format(
            graph_path, ", ".join(missing)))
        return 1
    if len(locales) < 2:
        emit("[parity] {0}: single-locale graph ({1}); nothing to compare.".format(
            graph_path, locales[0]))
        return 0

    reference = locales[0]
    diverging = 0
    for other in locales[1:]:
        diverging += _report_pair(graph_path, graph, reference, other, emit)

    if not diverging:
        return 0
    emit("[parity] A 'sibling pair disagrees' line is the stronger signal: both files")
    emit("[parity] exist and their contract sets differ, so this is content drift rather")
    emit("[parity] than a pending translation.")
    if strict:
        emit("[parity] --strict: exiting 1.")
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lte.validators.bilingual",
        description="Report locale divergence inside one compiled graph.")
    parser.add_argument("--check-graph", type=Path, required=True, metavar="GRAPH_JSON")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 when any divergence is found (default: report, exit 0).")
    parser.add_argument("--locales", default=None,
                        help="comma list, reference first (default: the graph's "
                             "metadata.locales, else config/corpus_layout.yaml)")
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args(argv)

    locales = tuple(l.strip() for l in args.locales.split(",") if l.strip()) if args.locales else None
    if locales is None:
        try:
            graph = json.loads(args.check_graph.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            graph = {}
        if graph_locales(graph) is None:
            from lte.engine.grammar import ConfigError
            from lte.io import config_reader
            try:
                locales = config_reader.load(repo_root=args.repo_root).layout.locales
            except ConfigError as exc:
                sys.stderr.write("[parity] FATAL: {0}\n".format(exc))
                return 1
    return run(args.check_graph, strict=args.strict, locales=locales)


if __name__ == "__main__":
    sys.exit(main())
