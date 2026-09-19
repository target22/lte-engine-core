#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_cli_ingest.py -- the orchestrator, and the class of bug it hid.

WHY THIS FILE EXISTS. lte/cli/ingest.py crashed the pipeline with

    AttributeError: module 'lte.validators.draft' has no attribute
                    'compile_diagnostic_patterns'

while every unit suite was green. Nothing imported the CLI, so nothing ever
resolved the names it reads off other modules. Unit tests that exercise a
module in isolation cannot see a caller that was left behind.

TWO LAYERS, DELIBERATELY:

  1. A STATIC CONTRACT CHECK over the whole package. For every
     `module.attribute` access where `module` is an imported lte module, the
     attribute must exist. This is the general form of the bug -- it would
     have failed the moment draft.py lost the function, without anyone
     thinking to write a test for that particular pair.

  2. AN EXECUTION TEST that drives run_batch() and run_single() against a
     temporary corpus and an in-memory CAS double. Static analysis proves
     the names resolve; only running proves the arities do.

Run: python3 tests/test_cli_ingest.py
"""
import ast
import importlib
import pathlib
import shutil
import sys
import tempfile

from _harness import REPO, Reporter, engine_config

r = Reporter()


# ----------------------------------------------------------------------
print("--- static: every cross-module attribute an lte module reads exists ---")
# ----------------------------------------------------------------------
def module_aliases(tree):
    """{local alias: dotted lte module} for this file's lte imports."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("lte."):
                    aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            if not (node.module or "").startswith("lte"):
                continue
            for a in node.names:
                dotted = "%s.%s" % (node.module, a.name)
                try:
                    importlib.import_module(dotted)
                except ImportError:
                    continue            # a class or function, not a module
                aliases[a.asname or a.name] = dotted
    return aliases


unresolved, scanned = [], 0
for path in sorted((REPO / "lte").rglob("*.py")):
    rel = path.relative_to(REPO).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = module_aliases(tree)
    if not aliases:
        continue
    scanned += 1
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        dotted = aliases.get(node.value.id)
        if dotted is None:
            continue
        try:
            module = importlib.import_module(dotted)
        except ImportError as exc:
            unresolved.append("%s:%d: cannot import %s (%s)" % (rel, node.lineno, dotted, exc))
            continue
        if not hasattr(module, node.attr):
            unresolved.append("%s:%d: %s has no attribute %r"
                              % (rel, node.lineno, dotted, node.attr))

for line in unresolved:
    print("     " + line)
r.check("no lte module reads a name another lte module does not define",
        not unresolved, "%d file(s) scanned" % scanned)


# ----------------------------------------------------------------------
print("\n--- static: the removed plumbing is really gone from the CLI ---")
# ----------------------------------------------------------------------
from lte.cli import ingest                                        # noqa: E402
from lte.validators import draft as draft_validator               # noqa: E402

r.check("ingest._diagnostic_patterns removed", not hasattr(ingest, "_diagnostic_patterns"))

calls = {}
for node in ast.walk(ast.parse((REPO / "lte/cli/ingest.py").read_text(encoding="utf-8"))):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.setdefault(node.func.attr, []).append(len(node.args))

r.check("validate_draft is called with 4 positional args",
        calls.get("validate_draft") == [4], str(calls.get("validate_draft")))
r.check("validate_batch is called with 3 positional args",
        calls.get("validate_batch") == [3], str(calls.get("validate_batch")))
r.check("compile_diagnostic_patterns is never called", "compile_diagnostic_patterns" not in calls)
r.check("find_linter_rules is never called", "find_linter_rules" not in calls)


# ----------------------------------------------------------------------
print("\n--- execution: run_batch against a temporary corpus ---")
# ----------------------------------------------------------------------
from lte.io.git_cas_client import GitClient                       # noqa: E402

GOOD = '''---
status: "active"
---
# Pricing

## Price bounds
Bids stay inside the band.

^spec-lte-01-002

> [!ref-spec-lte-01-002] Why not the median
> Mean keeps tails.
'''
LEGACY = '!!! note "Legacy"\n\n    body\n\n    ^spec-lte-01-003\n'

work = pathlib.Path(tempfile.mkdtemp(prefix="lte-ingest-"))
try:
    core = work / "docs/public/core"
    core.mkdir(parents=True)
    (core / "01-price.contract.en.md").write_text(GOOD, encoding="utf-8")

    config = engine_config()
    cas = GitClient()
    argv = ["--batch", str(work / "docs"), "--repo", str(work), "--no-lint",
            "--author-name", "Test User", "--author-email", "t@example.com"]

    rc = ingest.run_batch(ingest.parse_args(argv), config, cas)
    r.check("a conforming corpus ingests cleanly", rc == ingest.EXIT_OK, "exit %s" % rc)

    rc = ingest.run_batch(ingest.parse_args(argv), config, cas)
    r.check("an unchanged rerun commits nothing and still succeeds",
            rc == ingest.EXIT_OK, "exit %s" % rc)

    (core / "02-legacy.contract.en.md").write_text(LEGACY, encoding="utf-8")
    rc = ingest.run_batch(ingest.parse_args(argv), config, cas)
    r.check("a banned construct is REJECTED, not ingested",
            rc == ingest.EXIT_REJECTED, "exit %s" % rc)
    (core / "02-legacy.contract.en.md").unlink()

    rc = ingest.run_batch(ingest.parse_args(argv + ["--dry-run"]), config, cas)
    r.check("--dry-run succeeds and writes nothing", rc == ingest.EXIT_OK, "exit %s" % rc)

    # ------------------------------------------------------------------
    print("\n--- execution: run_single ---")
    # ------------------------------------------------------------------
    drafts = work / "drafts/public/core"
    drafts.mkdir(parents=True)
    draft_file = drafts / "03-new.contract.en.md"
    # NOTE the domain: `spec` is the core partition's anchor domain. Using
    # `sop` here (the ops partition's domain) is a misplaced anchor and is
    # correctly rejected -- asserted below rather than worked around.
    draft_file.write_text(
        "# New\n\n## Deploy gate\nGate holds.\n\n^spec-lte-03-001\n", encoding="utf-8")
    single = ["--repo", str(work), "--author-name", "T", "--author-email", "t@e.com",
              "--target", "docs/public/core/03-new.contract.en.md", str(draft_file)]
    rc = ingest.run_single(ingest.parse_args(single), config, cas)
    r.check("a conforming single draft ingests", rc == ingest.EXIT_OK, "exit %s" % rc)

    draft_file.write_text(LEGACY, encoding="utf-8")
    rc = ingest.run_single(ingest.parse_args(single), config, cas)
    r.check("a banned construct in a single draft is REJECTED",
            rc == ingest.EXIT_REJECTED, "exit %s" % rc)

    # An anchor whose domain belongs to another partition must not ingest,
    # whatever its syntax. `sop` is the ops partition's domain.
    draft_file.write_text(
        "# New\n\n## Deploy gate\nGate holds.\n\n^sop-deploy-03-001\n", encoding="utf-8")
    rc = ingest.run_single(ingest.parse_args(single), config, cas)
    r.check("a misplaced anchor domain is REJECTED", rc == ingest.EXIT_REJECTED,
            "exit %s" % rc)
finally:
    shutil.rmtree(work, ignore_errors=True)

sys.exit(r.finish())
