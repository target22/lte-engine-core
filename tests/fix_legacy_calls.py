#!/usr/bin/env python3
"""Removes the deleted second positional argument from pure-text call sites.

WHY A CODEMOD AND NOT A DIFF. The argument to drop is always positional
index 1, but its TEXT varies (`KINDS`, `kinds`, `taxonomy.admonition_callout_kind`,
`config.taxonomy.admonition_callout_kind`) and the call may span lines. This
locates the argument by parsing, then splices the SOURCE TEXT between the end
of arg 0 and the end of arg 1 -- so comments, line breaks and formatting
everywhere else are untouched. ast.unparse() would have reformatted the
whole file.

Idempotent: a call already at the correct arity is left alone.

NOT AUTO-FIXED, reported instead: calls to deleted functions and references
to deleted attributes. Those need a human decision about what replaces them,
and silently deleting a line from a test would remove an assertion.
"""
import argparse
import ast
import pathlib
import sys

# name -> positional arity AFTER the refactor
ARITY = {
    "parse_text": 3, "parse_file": 3, "cached_or_parse": 4, "parse_image": 3,
    "reference_map": 2, "node_bodies": 3, "check_payload_lock": 6,
    "effective_statuses": 3, "newly_invalidating": 4, "resolution_of": 5,
    "check_atomic_deprecation": 5,
}
DELETED_CALLS = {"parse_corpus", "check_truncated_admonitions"}
DELETED_ATTRS = {"admonition_callout_kind", "debate_layer_of", "ops_layer_of",
                 "admonition_open", "admonition_indent", "admonition_blank",
                 "admonition_trailing_anchor", "admonition_ref_line",
                 "admonition_status"}


def _offset(lines, lineno, col):
    """(1-based lineno, 0-based utf8 col) -> absolute index into the source."""
    return sum(len(l) for l in lines[:lineno - 1]) + col


def _callee(node):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def process(path, apply_changes):
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    cuts, manual = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name in DELETED_CALLS:
                manual.append("%s:%d: calls %s(), deleted -- replace by hand"
                              % (path, node.lineno, name))
            elif name in ARITY and len(node.args) == ARITY[name] + 1:
                first, second = node.args[0], node.args[1]
                cuts.append((
                    _offset(lines, first.end_lineno, first.end_col_offset),
                    _offset(lines, second.end_lineno, second.end_col_offset),
                    "%s:%d: %s()" % (path, node.lineno, name)))
            elif name in ARITY and len(node.args) > ARITY[name] + 1:
                manual.append("%s:%d: %s() has %d positional args, expected %d -- "
                              "too far off to fix mechanically"
                              % (path, node.lineno, name, len(node.args), ARITY[name]))
        elif isinstance(node, ast.Attribute) and node.attr in DELETED_ATTRS:
            manual.append("%s:%d: references .%s, deleted -- remove the binding"
                          % (path, node.lineno, node.attr))

    if cuts and apply_changes:
        out = src
        for start, end, _ in sorted(cuts, reverse=True):   # right to left
            out = out[:start] + out[end:]
        path.write_text(out, encoding="utf-8")
    return [c[2] for c in cuts], manual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        p = pathlib.Path(p)
        files += sorted(p.rglob("*.py")) if p.is_dir() else [p]

    fixed_all, manual_all = [], []
    for path in files:
        fixed, manual = process(path, args.apply)
        fixed_all += fixed
        manual_all += manual

    verb = "removed arg from" if args.apply else "would remove arg from"
    for line in fixed_all:
        print("  %s %s" % (verb, line))
    for line in manual_all:
        print("  MANUAL  %s" % line)
    if not fixed_all and not manual_all:
        print("  nothing to do")
    if manual_all:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
