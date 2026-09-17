#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lte/validators/architecture.py -- CI gate: enforces the layered import DAG.

A layering rule that lives only in a README is a suggestion. This module
makes it a build failure. It parses every file in the package with `ast`
(never importing them, so it works on the 3.5 host even though lte/engine/
is 3.9+ syntax) and asserts three properties:

  1. ACYCLICITY. Zero import cycles anywhere in the package.
  2. LAYER DIRECTION. A module may import only from strictly lower layers,
     or from its own layer when the target is declared side-effect-free.
  3. SIDE-EFFECT CONTAINMENT. Modules declared pure must not import
     subprocess, or call open()/Path.read_text()/write_text() and friends.

Run standalone, or as a build_config.yaml step:
    python3 -m lte.validators.architecture --package lte

Exit 0 clean, 1 on any violation.

PYTHON 3.5 FLOOR: this runs on the host alongside lte/cli/pipeline.py.
"""
import argparse
import ast
import os
import sys

# Layer index. Lower may never import higher. Equal-layer imports are
# allowed only into a module listed in PURE_MODULES.
# CORRECTED IN BATCH 2. The Batch-1 table placed lte.io and lte.engine both
# at layer 1, which made the same-layer cross-package rule forbid
# io -> engine -- the single most important edge in the whole design. A
# side-effect boundary exists precisely to read bytes and hand them to a
# pure transform; forbidding that left lte/io/ unable to do its job. The
# gate was right and the table was wrong: engine is the DEEPER leaf (it
# never needs io), so it belongs strictly below.
#
# lte.runner and lte.engine are deliberately BOTH layer 0. They are
# independent pure leaves, so the same-layer cross-package rule now forbids
# them importing each other in either direction. That is not incidental
# tidiness: lte/runner/ runs on the HOST interpreter (3.5/3.6) and
# lte/engine/ targets 3.9+, so a runner -> engine import raises SyntaxError
# before argparse runs. Encoding it here turns a latent runtime explosion
# into a CI failure.
LAYERS = {
    "lte.runner": 0,      # host-side pipeline primitives (pure, 3.5 floor)
    "lte.engine": 0,      # pure domain transforms (3.9+)
    "lte.io": 1,          # side-effect boundaries (disk, git CAS, subprocess)
    "lte.validators": 2,  # corpus + architecture checks
    "lte.cli": 3,         # orchestrators / entry points
}

# Modules that must contain no side effects at all. Enforced, not assumed.
PURE_MODULES = set([
    "lte.runner.errors", "lte.runner.config_schema", "lte.runner.environment",
    "lte.runner.reporting",
    "lte.engine.taxonomy", "lte.engine.grammar", "lte.engine.ast_blocks",
    "lte.engine.frontmatter", "lte.engine.state_machine", "lte.engine.integrity",
    "lte.engine.rendering", "lte.engine.graph_builder",
])

FORBIDDEN_IN_PURE_IMPORTS = set(["subprocess", "requests", "socket", "urllib"])
# `open` appears here as an ATTRIBUTE name as well as being checked as a
# builtin below. Without the attribute form, `path.open()` inside a module
# declared pure passed the gate silently -- Path.open() is exactly as much
# disk I/O as Path.read_text(), and it is the more natural spelling when a
# caller wants a file handle rather than a whole string. Found by auditing
# which modules actually touch disk after Batch 2 and noticing that
# lte/io/config_reader.py -- which does nothing but read files -- was
# reported as performing no I/O at all.
FORBIDDEN_IN_PURE_CALLS = set([
    "read_text", "write_text", "read_bytes", "write_bytes",
    "mkdir", "rglob", "glob", "unlink", "open", "touch", "iterdir",
])


def module_name(root_pkg, path):
    rel = os.path.relpath(path, os.path.dirname(os.path.abspath(root_pkg)))
    parts = rel[:-3].split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def layer_of(mod):
    for prefix in sorted(LAYERS, key=len, reverse=True):
        if mod == prefix or mod.startswith(prefix + "."):
            return prefix, LAYERS[prefix]
    return None, None


def scan(root_pkg):
    """Returns {module: {"imports": set(), "tree": ast.Module}}."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root_pkg):
        if "__pycache__" in dirpath:
            continue
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            mod = module_name(root_pkg, path)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError as exc:
                # A 3.9+ engine module on a 3.5 host parses fine here only
                # if the host's ast is new enough. Report rather than crash.
                found[mod] = {"imports": set(), "tree": None, "error": str(exc)}
                continue
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue  # relative import; resolved below by prefix
                    if node.module:
                        imports.add(node.module)
                        for alias in node.names:
                            imports.add(node.module + "." + alias.name)
            found[mod] = {"imports": imports, "tree": tree, "error": None}
    return found


def internal_edges(found, root_name):
    edges = {}
    for mod, info in found.items():
        targets = set()
        for imp in info["imports"]:
            if not (imp == root_name or imp.startswith(root_name + ".")):
                continue
            # An import may name a module OR a symbol inside one; resolve to
            # the longest prefix that is itself a known module.
            best = None
            for candidate in found:
                if imp == candidate or imp.startswith(candidate + "."):
                    if best is None or len(candidate) > len(best):
                        best = candidate
            if best and best != mod:
                targets.add(best)
        edges[mod] = targets
    return edges


def find_cycles(edges):
    color, cycles = {}, []

    def visit(u, stack):
        color[u] = 1
        stack.append(u)
        for v in sorted(edges.get(u, ())):
            if color.get(v, 0) == 1:
                cycles.append(stack[stack.index(v):] + [v])
            elif color.get(v, 0) == 0:
                visit(v, stack)
        stack.pop()
        color[u] = 2

    for m in sorted(edges):
        if color.get(m, 0) == 0:
            visit(m, [])
    return cycles


def check_purity(mod, tree):
    """Returns a list of violation strings for a module declared pure."""
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IN_PURE_IMPORTS:
                    violations.append(
                        "{0} is declared pure but imports {1!r}".format(mod, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in FORBIDDEN_IN_PURE_IMPORTS:
                violations.append(
                    "{0} is declared pure but imports from {1!r}".format(mod, node.module))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_IN_PURE_CALLS:
                violations.append(
                    "{0} is declared pure but calls .{1}() at line {2}".format(
                        mod, func.attr, getattr(node, "lineno", 0)))
            elif isinstance(func, ast.Name) and func.id == "open":
                violations.append(
                    "{0} is declared pure but calls open() at line {1}".format(
                        mod, getattr(node, "lineno", 0)))
    return violations


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enforce the package import DAG.")
    parser.add_argument("--package", default="lte")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.package):
        sys.stderr.write("[arch] FATAL: {0}/ not found.\n".format(args.package))
        return 1

    root_name = os.path.basename(os.path.abspath(args.package))
    found = scan(args.package)
    edges = internal_edges(found, root_name)
    violations = []

    for mod, info in sorted(found.items()):
        if info.get("error"):
            violations.append("{0}: does not parse ({1})".format(mod, info["error"]))

    for cycle in find_cycles(edges):
        violations.append("IMPORT CYCLE: " + " -> ".join(cycle))

    for mod in sorted(edges):
        src_prefix, src_layer = layer_of(mod)
        if src_layer is None:
            continue
        for target in sorted(edges[mod]):
            dst_prefix, dst_layer = layer_of(target)
            if dst_layer is None:
                continue
            if dst_layer > src_layer:
                violations.append(
                    "UPWARD IMPORT: {0} (layer {1}, {2}) imports {3} (layer {4}, {5})"
                    .format(mod, src_layer, src_prefix, target, dst_layer, dst_prefix))
            elif dst_layer == src_layer and src_prefix != dst_prefix:
                violations.append(
                    "CROSS-PACKAGE SAME-LAYER IMPORT: {0} ({1}) imports {2} ({3}) -- "
                    "same layer, different package. Move the shared code down a layer."
                    .format(mod, src_prefix, target, dst_prefix))

    for mod in sorted(PURE_MODULES & set(found)):
        tree = found[mod]["tree"]
        if tree is not None:
            violations.extend(check_purity(mod, tree))

    if violations:
        sys.stderr.write("[arch] FAILED -- {0} violation(s):\n".format(len(violations)))
        for v in violations:
            sys.stderr.write("  - {0}\n".format(v))
        sys.stderr.write(
            "\n[arch] The layer order is: runner(0) = engine(0) < io(1) < "
            "validators(2) < cli(3).\n"
            "[arch] Leaf modules never import upward. If you need a symbol from a\n"
            "[arch] higher layer, the symbol is in the wrong layer -- move it down.\n")
        return 1

    print("[arch] OK -- {0} module(s), {1} internal edge(s), acyclic, "
          "no upward imports, {2} pure module(s) verified side-effect-free."
          .format(len(found), sum(len(v) for v in edges.values()), len(PURE_MODULES & set(found))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
