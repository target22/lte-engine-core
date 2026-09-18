# -*- coding: utf-8 -*-
"""lte/io/config_reader.py -- SIDE-EFFECT BOUNDARY. Reads and parses the
four config documents, then hands the raw mappings to lte/engine/ to
validate and compile.

    config/taxonomy.yaml        -> lte.engine.taxonomy.build_taxonomy
    config/ui_projection.yaml   -> (same)
    config/corpus_layout.yaml   -> lte.engine.layout.compile_layout
    config/linter_rules.json    -> lte.engine.grammar.compile_grammar

COMPILE ORDER IS FORCED: taxonomy, then layout, then grammar. The layout is
validated against the taxonomy's partition rows, and the grammar's filename
patterns are expanded against the layout's locales and extensions.

THAT ORDER LEAVES A GAP, closed at the bottom of load(). Because taxonomy is
built first, it cannot see linter_rules.json; because the grammar is
compiled last, it is handed only the taxonomy's domains. Any rule spanning
both files therefore has no natural home in either validator and lives as an
explicit post-hoc assertion here -- see the two calls after compile_grammar.

This is the READ half of the legacy scripts/config_loader.py. Batch 1 moved
its validation half into lte/engine/grammar.py and lte/engine/taxonomy.py
and promised this module; here it is. Between them, every rule and every
fail-fast message from config_loader survives -- only the three `open()`
calls and the environment-variable path resolution live here.

WHY THE ENVIRONMENT LOOKUP IS AN I/O CONCERN. config_loader resolved
CONFIG_DIR from SRKH_REPO_ROOT / SRKH_CONFIG_DIR. That is environment-
dependent behavior, which is a side effect in exactly the sense the layer
rule cares about: the same call returns different data on two machines.
Keeping it here means lte/engine/ stays a function of its arguments alone.

PATHS ARE NEVER RELATIVE LITERALS. A relative Path("config/...") resolves
against the CALLER's cwd, so the VS Code bridge and any invocation from
outside the repo root would fail to find the file -- or silently find a
different one. Resolution goes through repo_root, always.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from lte.engine.grammar import ConfigError, Grammar, compile_grammar
from lte.engine.layout import CorpusLayout, LayoutError, compile_layout
from lte.engine.taxonomy import (Taxonomy, TaxonomyError,
                                 assert_callout_roles_agree, build_taxonomy)

TAXONOMY_FILENAME = "taxonomy.yaml"
GRAMMAR_FILENAME = "linter_rules.json"
UI_PROJECTION_FILENAME = "ui_projection.yaml"
LAYOUT_FILENAME = "corpus_layout.yaml"


def resolve_repo_root(explicit: Path | None = None) -> Path:
    """SRKH_REPO_ROOT wins, then an explicit argument, then a walk upward
    from this file until a directory containing both config/ and lte/ (or
    the legacy scripts/) is found.

    The walk exists because this package is mid-migration: `lte/` and
    `scripts/` coexist, and a fixed number of .parent hops breaks silently
    -- resolving to a directory OUTSIDE the repo rather than erroring -- the
    moment a module moves between nesting levels.
    """
    from_env = os.environ.get("SRKH_REPO_ROOT")
    if from_env:
        return Path(from_env)
    if explicit is not None:
        return Path(explicit)
    here = Path(__file__).resolve().parent
    for candidate in [here] + list(here.parents):
        if (candidate / "config").is_dir() and (
            (candidate / "lte").is_dir() or (candidate / "scripts").is_dir()
        ):
            return candidate
    raise ConfigError(
        "cannot locate a repo root containing config/ alongside lte/ or scripts/. "
        "Set SRKH_REPO_ROOT explicitly."
    )


def resolve_config_dir(repo_root: Path | None = None) -> Path:
    """SRKH_CONFIG_DIR wins, else <repo_root>/config."""
    from_env = os.environ.get("SRKH_CONFIG_DIR")
    if from_env:
        return Path(from_env)
    return resolve_repo_root(repo_root) / "config"


def _read_json(path: Path) -> Mapping:
    if not path.is_file():
        raise ConfigError("{0} is missing.".format(path))
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise ConfigError("{0} is not valid JSON: {1}".format(path, exc))
    except OSError as exc:
        raise ConfigError("cannot read {0}: {1}".format(path, exc))


def _read_yaml(path: Path) -> object:
    if not path.is_file():
        raise ConfigError("{0} is missing.".format(path))
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle.read())
    except yaml.YAMLError as exc:
        raise ConfigError("{0} is not valid YAML: {1}".format(path, exc))
    except OSError as exc:
        raise ConfigError("cannot read {0}: {1}".format(path, exc))


@dataclass(frozen=True)
class EngineConfig:
    """The compiled, validated taxonomy, grammar and corpus layout every
    engine call site needs, plus the roots they were resolved from.

    Built once per process and injected downstream. Holding them together
    matters because a Grammar is only meaningful against the Taxonomy whose
    domains widened its anchor pattern -- handing a call site one without
    the other invites compiling a corpus against a grammar that does not
    know half its partitions. The layout belongs to the same set for the
    same reason: it was validated against this taxonomy, and this grammar's
    filename patterns were compiled against its locales.
    """

    taxonomy: Taxonomy
    grammar: Grammar
    layout: CorpusLayout
    repo_root: Path
    config_dir: Path


def read_raw(config_dir: Path | None = None, repo_root: Path | None = None) -> dict:
    """The four parsed documents, unvalidated. Exposed separately so a test
    can exercise lte/engine/'s validators against a mutated mapping without
    writing a temp file."""
    root = resolve_repo_root(repo_root)
    cfg = Path(config_dir) if config_dir is not None else resolve_config_dir(root)
    taxonomy_raw = _read_yaml(cfg / TAXONOMY_FILENAME)
    return {
        "taxonomy": (taxonomy_raw.get("partitions")
                     if isinstance(taxonomy_raw, dict) else taxonomy_raw),
        "ui_projection": _read_yaml(cfg / UI_PROJECTION_FILENAME),
        "linter_rules": _read_json(cfg / GRAMMAR_FILENAME),
        "corpus_layout": _read_yaml(cfg / LAYOUT_FILENAME),
        "repo_root": root,
        "config_dir": cfg,
    }


def load(config_dir: Path | None = None, repo_root: Path | None = None) -> EngineConfig:
    """Read, validate, compile. Raises ConfigError at LOAD time, never lazily
    at first use.

    Fail-fast here is the deliberate inverse of how this pipeline treats
    malformed AUTHORED CONTENT (front matter and prologue degrade to empty
    rather than failing the build). A bad .md file is an authoring mistake
    that must not take down the corpus; a bad config file is an operator
    mistake that must not be compiled around, because a half-loaded grammar
    silently narrows what counts as a valid anchor and then reports the
    resulting empty corpus as a legitimate one.
    """
    raw = read_raw(config_dir, repo_root)
    try:
        taxonomy = build_taxonomy(raw["taxonomy"], raw["ui_projection"])
    except TaxonomyError as exc:
        raise ConfigError("{0}: {1}".format(raw["config_dir"] / TAXONOMY_FILENAME, exc)) from exc

    linter_rules = raw["linter_rules"]
    document_roles = linter_rules.get("document_roles") if isinstance(linter_rules, Mapping) else None
    if not isinstance(document_roles, list) or not all(
            isinstance(r, str) and r for r in document_roles):
        raise ConfigError("{0}: document_roles must be a list of non-empty strings.".format(
            raw["config_dir"] / GRAMMAR_FILENAME))
    try:
        layout = compile_layout(
            raw["corpus_layout"], taxonomy.partitions, document_roles,
            label=str(raw["config_dir"] / LAYOUT_FILENAME))
    except LayoutError as exc:
        # One error type for every configuration failure: callers catch
        # ConfigError and nothing else.
        raise ConfigError(str(exc)) from exc

    grammar = compile_grammar(
        linter_rules, list(taxonomy.known_domains),
        label=str(raw["config_dir"] / GRAMMAR_FILENAME),
        layout=layout,
    )
    # Cross-checks that need the FINISHED Grammar, which is why they run
    # here and not inside either validator. COMPILE ORDER IS FORCED --
    # taxonomy, then layout, then grammar -- so build_taxonomy() cannot see
    # linter_rules.json at all, and compile_grammar() is handed only the
    # taxonomy's domains. Anything spanning both files lands in this gap.
    from lte.engine.grammar import assert_dependent_roles_producible

    # 1. A dependent role the filename pattern cannot emit is dead config
    #    that looks live.
    assert_dependent_roles_producible(grammar)

    # 2. The two config files each declare a dependent role set --
    #    ui_projection.yaml's callout_kinds keys and linter_rules.json's
    #    dependent_lifecycle.dependent_roles -- and neither can see the
    #    other at its own validation time. A role in only one of them is
    #    either a KeyError mid-corpus-walk or an array emitted empty on
    #    every node forever, so the disagreement is caught here or not at
    #    all.
    #
    #    WRAPPED, not allowed to propagate. assert_callout_roles_agree
    #    raises TaxonomyError; load()'s contract, stated at the top of this
    #    module and relied on by lte/cli/compile.py's except clause, is that
    #    every configuration failure surfaces as ConfigError. An unwrapped
    #    TaxonomyError here would escape that handler and print a traceback
    #    instead of the FATAL line every other config error produces.
    try:
        assert_callout_roles_agree(taxonomy, grammar)
    except TaxonomyError as exc:
        raise ConfigError("{0} vs {1}: {2}".format(
            raw["config_dir"] / UI_PROJECTION_FILENAME,
            raw["config_dir"] / GRAMMAR_FILENAME, exc)) from exc

    return EngineConfig(
        taxonomy=taxonomy, grammar=grammar, layout=layout,
        repo_root=raw["repo_root"], config_dir=raw["config_dir"],
    )
