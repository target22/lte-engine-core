# -*- coding: utf-8 -*-
"""lte/engine/integrity.py -- LEAF MODULE. Pure corpus integrity checks.

The four find_* functions from graph_lib, plus check_file_naming() and
unindexed_severity() which belong to the same diagnostic family.

TAXONOMY IS INJECTED, NOT REACHED FOR. In graph_lib these functions read
node.partition / node.scope, properties that consulted module-level
DOMAIN_TO_PARTITION and PARTITION_SCOPE maps built by import-time file
reads. SpecNode here keeps only `domain` (a pure string split); resolving a
domain to a partition or scope needs config, so the two functions that need
it take a Taxonomy parameter. That keeps SpecNode free of any upward
dependency and this module free of import-time I/O.

THE CORPUS LAYOUT IS INJECTED THE SAME WAY. Which partition directory a file
sits in, which scope that directory belongs to, which scope levels may cite
which, and what a conforming filename looks like are all answers from the
CorpusLayout (lte/engine/layout.py, same layer). No function here splits a
path, names a scope, or tests for a file extension. The functions that need
the layout take it as their FIRST parameter.

Every function is pure and print-free: the caller decides whether a finding
warns, fails a build, or rejects a commit.
"""
from __future__ import annotations

from typing import Sequence

from lte.engine.ast_blocks import RefCallout, SpecNode
from lte.engine.grammar import Grammar
from lte.engine.layout import CorpusLayout
from lte.engine.taxonomy import Taxonomy


def find_orphan_refs(nodes: Sequence[SpecNode],
                     callouts: Sequence[RefCallout]) -> list:
    """Every callout whose target_id has no matching anchor node anywhere in
    the given SAME-LANGUAGE corpus.

    DELIBERATELY CORPUS-WIDE, not partition- or scope-scoped: a debate is
    free to reference an anchor in a different partition than its own file
    lives in. Whether a file in one scope may reference an anchor in another
    is a separate question -- see find_cross_boundary_refs().

    Needs no taxonomy: this is pure set membership over anchor ids.
    """
    known_ids = {n.spec_id for n in nodes}
    return [c for c in callouts if c.target_id not in known_ids]


def find_duplicate_spec_ids(nodes: Sequence[SpecNode]) -> dict:
    """anchor_id -> [nodes] for every anchor id used more than once.

    A duplicate anchor is ambiguous at link time, equally across partitions
    and scopes -- it fails the build regardless of where the collision sits.
    """
    by_id: dict = {}
    for node in nodes:
        by_id.setdefault(node.spec_id, []).append(node)
    return {sid: ns for sid, ns in by_id.items() if len(ns) > 1}


def find_cross_boundary_refs(layout: CorpusLayout, taxonomy: Taxonomy,
                             nodes: Sequence[SpecNode],
                             callouts: Sequence[RefCallout]) -> list:
    """Every callout authored in a file whose scope has a LOWER level than the
    scope of the anchor it cites (config/corpus_layout.yaml, scopes[].level).

    THE ONE DIRECTION OF SCOPE-CROSSING TREATED AS A HARD VIOLATION. A file
    readable at a lower visibility level that references a higher-level
    anchor id discloses that the restricted decision exists and roughly what
    it is about -- the callout's own title and body text, AUTHORED IN THE
    LOWER-LEVEL FILE, is not itself restricted just because its target is.

    Flagged here rather than relied upon to disappear: a compile target that
    does not read the higher-level scope already drops such a callout from
    its artifact. But "the compiled artifact happens not to show it" is not
    the same guarantee as "this was never written down in a lower-level
    file" -- source control history, local clones, and anyone reading the
    raw Markdown still see it.

    The reverse direction (a higher-level file citing a lower-level anchor)
    is NOT a violation and is not checked.

    SCOPE RESOLUTION. The callout's scope is the scope of the partition
    directory its file sits in. The cited anchor's scope is the scope of the
    partition that actually defines it when that node is in `nodes`,
    otherwise the partition its domain maps to. A callout outside every
    partition directory, or citing an unknown domain, is not a boundary
    question (find_orphan_refs() reports the latter).

    No allowlist mechanism exists to mark a specific crossing as
    reviewed-and-intentional. This check is unconditional.
    """
    defined_scope: dict = {}
    for node in nodes:
        spec = layout.partition_of_path(node.source_file)
        if spec is not None:
            defined_scope.setdefault(node.spec_id, spec.scope)

    violations: list = []
    for callout in callouts:
        source = layout.partition_of_path(callout.source_file)
        if source is None:
            continue
        target_scope = defined_scope.get(callout.target_id)
        if target_scope is None:
            target_partition = taxonomy.partition_of(callout.target_id)
            if target_partition is None:
                continue
            target_scope = layout.partition(target_partition).scope
        if layout.is_boundary_violation(source.scope, target_scope):
            violations.append(callout)
    return violations


def find_misplaced_anchors(layout: CorpusLayout, taxonomy: Taxonomy,
                           nodes: Sequence[SpecNode]) -> list:
    """Nodes whose anchor DOMAIN prefix doesn't match the partition directory
    they are physically defined in.

    A scope mismatch is a partition mismatch too: each partition directory
    belongs to exactly one scope, so comparing directories covers both.

    Returns (node, expected_dir, actual_dir) triples, repo-relative, for a
    readable message. `expected_dir` is the partition directory the layout
    declares for the anchor's domain. `actual_dir` is the partition directory
    the file sits in; for a file outside every partition directory it is the
    file's own parent directory (None for a file at the repository root). A domain outside the
    taxonomy cannot reach this function at all (the compiled anchor pattern
    would not have matched it), so `expected_dir` is always real.
    """
    mismatches: list = []
    for node in nodes:
        expected_partition = taxonomy.partition_of(node.spec_id)
        if expected_partition is None:
            continue  # unreachable given the compiled anchor pattern; kept defensive
        expected = layout.partition(expected_partition).path

        spec = layout.partition_of_path(node.source_file)
        if spec is not None:
            actual = spec.path
        elif "/" in node.source_file:
            actual = node.source_file.rsplit("/", 1)[0]
        else:
            actual = None

        if actual != expected:
            mismatches.append((node, expected, actual))
    return mismatches


def check_file_naming(layout: CorpusLayout, grammar: Grammar, rel_path: str) -> list:
    """Validates ONE repo-relative path against the configured file-naming
    pattern. Returns structured diagnostics; empty when the name conforms or
    the layout ignores the path.

    Returns dicts rather than (line_no, message) tuples: a filename violation
    has no line number, and a `source` discriminator lets a consumer route
    naming diagnostics separately from content ones.

    DELIBERATELY NOT FATAL BY DEFAULT: the role segment is optional (every
    pre-convention document omits it), so the only real problems are a
    missing or unrecognized locale, an unrecognized role segment, or a name
    the discovery rules could never match anyway.
    """
    if layout.is_ignored(rel_path):
        return []
    name = rel_path.rsplit("/", 1)[-1]
    if grammar.file_naming.match(name):
        return []

    extension = next((e for e in sorted(layout.extensions, key=len, reverse=True)
                      if name.endswith(e)), None)
    if extension is None:
        detail = "extension is not one of {0}".format(list(layout.extensions))
    else:
        stem_parts = name[:-len(extension)].split(".")
        role_index = -1
        detail = None
        if layout.locales_suffixed:
            locale = stem_parts[-1] if len(stem_parts) > 1 else ""
            if locale not in layout.locales:
                detail = (
                    "locale segment {0!r} is not one of {1} -- file discovery only "
                    "recognizes those, so this file would never be compiled".format(
                        locale, list(layout.locales)))
            role_index = -2
        if detail is None and len(stem_parts) > -role_index and \
                stem_parts[role_index] not in grammar.file_naming_roles:
            detail = "role segment {0!r} is not one of {1}".format(
                stem_parts[role_index], list(grammar.file_naming_roles))
        if detail is None:
            detail = "does not match {0}".format(grammar.file_naming.pattern)

    return [{
        "source": grammar.file_naming_source,
        "severity": grammar.file_naming_severity,
        "path": rel_path,
        "message": "filename does not follow the configured convention: {0}".format(detail),
    }]


def unindexed_severity(grammar: Grammar, partition_id: str) -> str:
    """'info' | 'warn' | 'fail' for a zero-contract file in this partition."""
    policy = grammar.unindexed_policy
    return policy.get("by_partition", {}).get(partition_id, policy["default"])


def check_all(layout: CorpusLayout, taxonomy: Taxonomy, grammar: Grammar,
              nodes: Sequence[SpecNode], callouts: Sequence[RefCallout]) -> list:
    """Runs every corpus-wide check and returns flat diagnostic strings.

    A convenience aggregator for lte/cli/compile.py, which wants one
    pass/fail answer. Callers needing structured results call the individual
    functions -- this one deliberately flattens to text and is not a
    substitute for them.
    """
    problems: list = []

    for callout in find_orphan_refs(nodes, callouts):
        problems.append(
            "{0}:{1}: [!ref-{2}] targets an anchor that exists nowhere in this "
            "corpus".format(callout.source_file, callout.line_no, callout.target_id))

    for anchor_id, duplicates in sorted(find_duplicate_spec_ids(nodes).items()):
        where = ", ".join(
            "{0}:{1}".format(n.source_file, n.line_no) for n in duplicates)
        problems.append(
            "^{0} is defined {1} times ({2}) -- an anchor id must be unique "
            "corpus-wide".format(anchor_id, len(duplicates), where))

    for callout in find_cross_boundary_refs(layout, taxonomy, nodes, callouts):
        problems.append(
            "{0}:{1}: file references ^{2}, which is defined in a higher-visibility "
            "scope".format(callout.source_file, callout.line_no, callout.target_id))

    for node, expected, actual in find_misplaced_anchors(layout, taxonomy, nodes):
        problems.append(
            "{0}:{1}: ^{2} belongs in {3} but is authored in {4}".format(
                node.source_file, node.line_no, node.spec_id, expected, actual or "?"))

    return problems
