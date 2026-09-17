# -*- coding: utf-8 -*-
"""lte.engine -- LAYER 0. Pure domain transforms.

Zero side effects across every module: no open(), no read_text(), no
subprocess, no environment lookups. Enforced, not asserted --
lte/validators/architecture.py lists each module below in PURE_MODULES and
fails the build on a violation.

MUST NEVER IMPORT lte.io, lte.validators OR lte.cli. Same-layer
cross-package imports (lte.runner) are rejected too; see lte/__init__.py for
the interpreter-version reason that one matters.

TWO NAMES ARE DELIBERATELY NOT FLATTENED. Both ast_blocks and frontmatter
export a function called `parse_text`, and they parse different things:

    ast_blocks.parse_text(grammar, kinds, rel_path, text) -> (nodes, callouts)
    frontmatter.parse_text(text, fence=None)              -> dict

Re-exporting either as a bare `parse_text` would make the shorter name a
coin flip at the call site, and the two have incompatible signatures, so the
mistake surfaces as a TypeError several frames away from the import that
caused it. They are reachable only as `ast_blocks.parse_text` /
`frontmatter.parse_text`. Aliasing them to invented names
(`parse_ast_text`, `parse_frontmatter_text`) was considered and rejected:
inventing a second name for a function that already has a clear qualified
one adds vocabulary without removing ambiguity.

Both modules are therefore exported as MODULES, alongside the unambiguous
types and functions.
"""
from lte.engine import ast_blocks, frontmatter, integrity, rendering, state_machine
from lte.engine.ast_blocks import (
    RefCallout,
    SpecNode,
    document_id_from_name,
    document_role_from_name,
    prologue_from_text,
    title_from_text,
)
from lte.engine.grammar import (
    ConfigError,
    Grammar,
    assert_dependent_roles_producible,
    build_domain_alternation,
    compile_grammar,
)
from lte.engine.rendering import Renderer, build_renderer, plain_text_summary
from lte.engine.state_machine import (
    check_dependent_lifecycle,
    dependent_role_of,
    invalidated_state,
    is_dependent_document,
    resolve_dependent_status,
)
from lte.engine.taxonomy import (
    GRAPH_SCHEMA_VERSION,
    Taxonomy,
    TaxonomyError,
    build_taxonomy,
    domain_of,
    validate_ui_projection,
)

__all__ = [
    # submodules (see the parse_text note above)
    "ast_blocks",
    "frontmatter",
    "integrity",
    "rendering",
    "state_machine",
    # taxonomy
    "Taxonomy",
    "TaxonomyError",
    "build_taxonomy",
    "validate_ui_projection",
    "domain_of",
    "GRAPH_SCHEMA_VERSION",
    # grammar
    "Grammar",
    "ConfigError",
    "compile_grammar",
    "build_domain_alternation",
    "assert_dependent_roles_producible",
    # AST
    "SpecNode",
    "RefCallout",
    "title_from_text",
    "prologue_from_text",
    "document_id_from_name",
    "document_role_from_name",
    # state machine
    "resolve_dependent_status",
    "check_dependent_lifecycle",
    "dependent_role_of",
    "is_dependent_document",
    "invalidated_state",
    # rendering
    "Renderer",
    "build_renderer",
    "plain_text_summary",
]
