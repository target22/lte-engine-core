"""
REPLACEMENT FUNCTIONS for lte/engine/integrity.py (layout-driven).

Replace the bodies of find_misplaced_anchors, find_cross_boundary_refs and
check_file_naming with these. Each gains `layout` as its FIRST parameter;
find_orphan_refs, find_duplicate_spec_ids, unindexed_severity and check_all
are unchanged apart from check_all passing `layout` through. integrity.py
stays pure: `layout` is an lte.engine.layout.CorpusLayout (same layer, same
package), and nothing here touches the filesystem.

Callers already updated in this delivery: lte/cli/compile.py,
lte/validators/draft.py. Callers to update: lte/validators/corpus.py,
lte/graph_lib.py (the shim; or leave it until step 7 deletes it).
"""

from __future__ import annotations

from typing import Sequence


def find_misplaced_anchors(layout, taxonomy, nodes: Sequence) -> list:
    """
    (node, expected_partition, actual_partition) for every node whose anchor
    domain maps to a different partition than the directory it is defined in.

    `actual` comes from layout.partition_of_path(); it is None for a file
    outside every partition directory. The old implementation indexed
    'docs/<scope>/<partition>/' by position.
    """
    out = []
    for node in nodes:
        expected = taxonomy.partition_of(node.spec_id)
        spec = layout.partition_of_path(node.source_file)
        actual = spec.id if spec is not None else None
        if expected != actual:
            out.append((node, expected, actual))
    return out


def find_cross_boundary_refs(layout, taxonomy, nodes: Sequence, callouts: Sequence) -> list:
    """
    Every callout whose own file sits in a LOWER-level scope than the anchor
    it cites. The old implementation hardcoded the single case
    "public file -> private anchor".

    The cited anchor's scope is the scope of the partition that actually
    defines it when that node is in `nodes`; otherwise the partition its
    domain maps to. A callout outside every partition, or citing an unknown
    domain, is not a boundary question (find_orphan_refs reports the latter).
    """
    home = {}
    for node in nodes:
        spec = layout.partition_of_path(node.source_file)
        if spec is not None:
            home.setdefault(node.spec_id, spec.scope)
    out = []
    for callout in callouts:
        source = layout.partition_of_path(callout.source_file)
        if source is None:
            continue
        target_scope = home.get(callout.target_id)
        if target_scope is None:
            target_pid = taxonomy.partition_of(callout.target_id)
            if target_pid is None:
                continue
            target_scope = layout.partition(target_pid).scope
        if layout.is_boundary_violation(source.scope, target_scope):
            out.append(callout)
    return out


def check_file_naming(layout, grammar, rel_path: str) -> list:
    """
    Structured diagnostics ({severity, source, path, message}) for one
    repo-relative path; empty when the name conforms.

    Conformance is the layout's filename convention, plus the one rule the
    old file_naming pattern added on top of it: the slug contains no '.',
    except for a trailing validation-only role segment such as '.evi'
    (linter_rules.json#file_naming.roles) that is not a document role.
    Paths outside every partition, or ignored by the layout, are not
    naming questions and return [].
    """
    if layout.is_ignored(rel_path) or layout.partition_of_path(rel_path) is None:
        return []
    name = rel_path.rsplit("/", 1)[-1]
    parsed = layout.parse_filename(name)
    problem = None
    if parsed is None:
        problem = "filename does not follow the corpus convention (%s)" % ", ".join(
            "*" + s for s in layout.listing_suffixes())
    else:
        slug = parsed[0]
        if "." in slug:
            head, _, tail = slug.rpartition(".")
            if "." in head or tail not in set(grammar.file_naming_roles):
                problem = ("slug %r contains '.'; only a role segment (%s) may follow the slug"
                           % (slug, ", ".join(grammar.file_naming_roles)))
    if problem is None:
        return []
    return [{
        "severity": grammar.file_naming_severity,
        "source": grammar.file_naming_source,
        "path": rel_path,
        "message": problem,
    }]
