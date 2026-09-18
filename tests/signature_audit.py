#!/usr/bin/env python3
"""Arity audit for the pure-text refactor.

The DEAD-NAME gate from Round 6 catches references to things that no longer
exist. It cannot catch a call to something that still exists with a
DIFFERENT SIGNATURE -- parse_text(grammar, kinds, rel, text) is four valid
positional args to a function that now takes three, and nothing flags it
until it runs.

Reads Call nodes only, so docstrings and comments cannot trip it.
"""
import ast
import pathlib
import sys

# function name -> (positional arity after the refactor, what was removed)
EXPECTED = {
    "parse_text":               (3, "callout_kind_by_type"),
    "parse_file":               (3, "callout_kind_by_type"),
    "cached_or_parse":          (4, "callout_kind_by_type"),
    "parse_image":              (3, "kinds"),
    "reference_map":            (2, "kinds"),
    "node_bodies":              (3, "kinds"),
    "check_payload_lock":       (6, "kinds"),
    "effective_statuses":       (3, "kinds"),
    "newly_invalidating":       (4, "kinds"),
    "resolution_of":            (5, "kinds"),
    "check_atomic_deprecation": (5, "kinds"),
    "build_taxonomy":           (2, None),
}
GONE = {"parse_corpus", "check_truncated_admonitions",
        "debate_layer_of", "ops_layer_of", "admonition_callout_kind"}


def audit(roots):
    hits = []
    for root in roots:
        for path in sorted(pathlib.Path(root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                hits.append("%s: does not parse: %s" % (path, exc))
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else (
                        fn.id if isinstance(fn, ast.Name) else None)
                    if name in GONE:
                        hits.append("%s:%d: calls %s(), which was deleted"
                                    % (path, node.lineno, name))
                    elif name in EXPECTED:
                        want, removed = EXPECTED[name]
                        got = len(node.args)
                        if got > want:
                            hits.append(
                                "%s:%d: %s() called with %d positional arg(s), expected %d%s"
                                % (path, node.lineno, name, got, want,
                                   " -- drop the %s argument" % removed if removed else ""))
                elif isinstance(node, ast.Attribute) and node.attr in GONE:
                    # Only when it is NOT the callee of a Call already
                    # reported above -- ast.walk visits both nodes.
                    hits.append("%s:%d: references .%s, which was deleted"
                                % (path, node.lineno, node.attr))
    # A deleted name used as a call yields both a Call hit and an Attribute
    # hit at the same line. Keep the Call one; it is the more specific.
    seen, deduped = set(), []
    for hit in hits:
        key = hit.rsplit(":", 1)[0]
        if key in seen and "references ." in hit:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


if __name__ == "__main__":
    roots = sys.argv[1:] or ["lte", "tests"]
    found = audit([r for r in roots if pathlib.Path(r).is_dir()])
    print("\n".join(found) or "clean -- no stale call sites")
    sys.exit(1 if found else 0)
