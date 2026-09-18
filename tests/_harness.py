# -*- coding: utf-8 -*-
"""tests/_harness.py -- the one place the test suite locates the repo.

WHY THIS EXISTS. The suites were authored against a flat scratch tree and
carried it into the repo: 28 absolute paths, plus importlib loads of module
names that only existed there (`taxonomy_real.py`, `validators_draft.py`,
`io_corpus_reader.py`, `cli_compile.py`). Every one of those is a path that
resolves on exactly one machine.

`build()` was part of the same scaffolding. It was never engine API -- the
engine exposes `compile_grammar(linter_rules, known_domains, layout=...)`,
which needs three collaborators a test should not have to assemble by hand.
This module supplies `build()` for real, against the repo's own config, in
the SAME ORDER lte/io/config_reader.load() uses: taxonomy, then layout, then
grammar. That order is load-bearing -- the layout is validated against the
taxonomy's partition rows, and the grammar's filename patterns are expanded
against the layout's locales.

REPO IS DERIVED FROM __file__, never from cwd. A test invoked as
`python3 tests/test_x.py`, as `python3 -m pytest`, or from any other
directory resolves the same root.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lte.engine.grammar import compile_grammar                    # noqa: E402
from lte.engine.layout import compile_layout                      # noqa: E402
from lte.engine.taxonomy import build_taxonomy                    # noqa: E402
from lte.io import config_reader                                  # noqa: E402

GRAMMAR_LABEL = str(CONFIG / "linter_rules.json")
LAYOUT_LABEL = str(CONFIG / "corpus_layout.yaml")


def raw_config() -> dict:
    """The four parsed config documents, unvalidated.

    Goes through config_reader.read_raw() rather than yaml.safe_load() on a
    hardcoded path, so a test reads exactly what the engine reads --
    including SRKH_CONFIG_DIR if it is set.
    """
    return config_reader.read_raw(repo_root=REPO)


def build(linter_rules: dict | None = None):
    """A Grammar compiled from the repo's real config.

    Pass `linter_rules` to compile against a MUTATED rules document -- that
    is how the vocabulary-rename and fail-fast cases exercise config changes
    without writing a temp file into the working tree.
    """
    cfg = raw_config()
    rules = cfg["linter_rules"] if linter_rules is None else linter_rules
    taxonomy = build_taxonomy(cfg["taxonomy"], cfg["ui_projection"])
    layout = compile_layout(cfg["corpus_layout"], taxonomy.partitions,
                            rules["document_roles"], label=LAYOUT_LABEL)
    return compile_grammar(rules, list(taxonomy.known_domains),
                           label=GRAMMAR_LABEL, layout=layout)


def mutated_rules(mutate) -> dict:
    """A deep copy of linter_rules.json with `mutate(doc)` applied."""
    doc = copy.deepcopy(raw_config()["linter_rules"])
    mutate(doc)
    return doc


def engine_config():
    """A fully loaded EngineConfig, for tests that need layout + taxonomy."""
    return config_reader.load(repo_root=REPO)


def source_of(*parts: str) -> str:
    """Text of a repo file, for tests that assert on source structure."""
    return REPO.joinpath(*parts).read_text(encoding="utf-8")


class Reporter:
    """The pass/fail accumulator every suite shares."""

    def __init__(self) -> None:
        self.failures: list = []

    def check(self, label: str, condition, detail: str = "") -> bool:
        ok = bool(condition)
        if not ok:
            self.failures.append(label)
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", label,
                             (" -- " + detail) if detail else ""))
        return ok

    def finish(self) -> int:
        print("\n%s" % ("ALL PASS" if not self.failures
                        else "FAILURES: %s" % self.failures))
        return 1 if self.failures else 0
