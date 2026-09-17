# -*- coding: utf-8 -*-
"""lte/engine/state_machine.py -- LEAF MODULE. The dependent-node lifecycle
state machine and the Pessimistic State Resolution Matrix.

Extracted from graph_lib's dependent-lifecycle block:
resolve_dependent_status(), check_dependent_lifecycle(),
dependent_role_of(), STATUS_STRICTNESS, INVALIDATED_STATE.

Zero I/O. Its one dependency is a Grammar value, injected -- the lifecycle
policy it enforces is config-declared (config/linter_rules.json's
dependent_lifecycle block) and arrives already validated by
grammar.validate_dependent_lifecycle().

ROLE RESOLUTION IS LAYOUT-AWARE WHEN A LAYOUT IS GIVEN. dependent_role_of(),
is_dependent_document() and check_dependent_lifecycle() accept an optional
`layout` keyword (a CorpusLayout). With it, a file's role includes its
partition's configured `default_role`, so a file in a paired directory whose
name carries no role segment is still governed as a dependent. Without it,
behaviour is the filename-only resolution these functions always had.

DEFAULT_INVALIDATED_STATE below is the only literal. It is a fallback for
a caller holding no Grammar, never the live value -- the live one comes
from config and is asserted at load time to be absent from status_values,
so it can never be authored by hand.
"""
from __future__ import annotations

from typing import Mapping

from lte.engine.grammar import Grammar

DEFAULT_INVALIDATED_STATE = "invalidated"


def _filename_role(grammar: Grammar, rel_path: str) -> str | None:
    """The role segment of the file name, if the naming pattern matches."""
    name = rel_path.rsplit("/", 1)[-1]
    match = grammar.file_naming.match(name)
    if not match:
        return None
    return match.group("role")


def dependent_role_of(grammar: Grammar, rel_path: str, layout=None) -> str | None:
    """'01-x.ops.en.md' -> 'ops'. Returns None for a primary/contract file, a
    legacy no-role file, or any name outside the convention.

    WITH A LAYOUT: returns None for a path the layout does not classify as a
    corpus file (ignored, outside every partition, non-conforming name).
    Otherwise the role is the file's role segment if it names a document
    role, else the partition's `default_role`, else the filename-pattern role
    (which also knows validation-only roles such as `evi`, never produced by
    the layout because they do not regroup documents).

    DELIBERATELY FILENAME-DRIVEN, NOT CONTENT-DRIVEN. Whether a file is
    governed by the dependent lifecycle is a declaration the author makes by
    NAMING the file, not something inferred from which blocks happen to be
    inside it. A `.contract.en.md` containing only debates is an authoring
    mistake for the linter to surface, not a reason for this function to
    start guessing.

    Uses grammar.file_naming (which knows all four roles including `evi`),
    NOT grammar.split_document (which knows only the three DOCUMENT_ROLES and
    would silently return None for every .evi file).
    """
    role = None
    if layout is not None:
        info = layout.classify(rel_path)
        if info is None:
            return None
        role = info.role
    if role is None:
        role = _filename_role(grammar, rel_path)
    return role if role in grammar.dependent_lifecycle["dependent_roles"] else None


def is_dependent_document(grammar: Grammar, rel_path: str, layout=None) -> bool:
    return dependent_role_of(grammar, rel_path, layout=layout) is not None


def check_dependent_lifecycle(grammar: Grammar, rel_path: str,
                              frontmatter: Mapping, layout=None) -> list:
    """Validates ONE dependent-role document's lifecycle front matter.

    Returns structured diagnostics in the same shape check_file_naming() uses
    -- {"source", "severity", "path", "message"} -- so a consumer can route
    both through one reporting path. Pure and print-free: the caller decides
    whether a diagnostic warns, fails a build, or rejects a commit.

    `frontmatter` MUST be raw front matter from frontmatter.parse_text(), NOT
    the coerced output of frontmatter.metadata_from_text(). That function
    drops every key outside its seven rendered ones, `archived_reason`
    included, and feeding it here reports a correctly-tagged file as missing
    the field it carries. This is not hypothetical -- it was caught in
    testing when the pre-commit hook used the wrong one.

    `layout` is forwarded to dependent_role_of(); see there.

    Returns [] for a non-dependent file, for a dependent file with no
    `status` at all (absence means "inherits from the parent contract", which
    stays legal), and for a status carrying no conditional requirements.

    Checks, in order:
      1. `status` is one of the declared status values.
      2. Every field conditional_requirements demands for that status is
         present and non-empty.
      3. Every enum-constrained field holds a DECLARED tag, not free text.
         `archived_reason: "we didn't need it"` is rejected: an un-enumerated
         reason cannot be aggregated, filtered, or acted on downstream, which
         is the entire point of requiring one.
      4. Every anchor-reference field parses as a real anchor id under the
         live grammar. EXISTENCE OF THE TARGET IS NOT CHECKED HERE -- this
         function sees one file and has no corpus; resolving the reference is
         integrity.find_orphan_refs()'s job, at the layer that holds the
         anchor index.
    """
    role = dependent_role_of(grammar, rel_path, layout=layout)
    if role is None:
        return []

    policy = grammar.dependent_lifecycle

    def _diag(message: str) -> dict:
        return {
            "source": policy["source"],
            "severity": policy["severity"],
            "path": rel_path,
            "message": message,
        }

    raw_status = frontmatter.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else None
    status = status or None

    if status is None:
        if raw_status is not None:
            return [_diag(
                "dependent ({0}) document declares a non-string status {1!r}; expected "
                "one of: {2}".format(role, raw_status, ", ".join(grammar.status_values)))]
        return []  # no independent status -> inherits from the parent contract

    if status not in grammar.status_values:
        return [_diag(
            "dependent ({0}) document declares status {1!r}, which is not one of: "
            "{2}".format(role, status, ", ".join(grammar.status_values)))]

    diagnostics: list = []
    anchor_re = grammar.anchor_reference_pattern()

    for field in policy["conditional_requirements"].get(status, ()):
        value = frontmatter.get(field)
        text = str(value).strip() if value is not None else ""
        if not text:
            diagnostics.append(_diag(
                "dependent ({0}) document has status: {1} but no {2!r}. status: {1} "
                "requires {3}.".format(
                    role, status, field,
                    sorted(policy["conditional_requirements"][status]))))
            continue

        allowed = policy["enum_fields"].get(field)
        if allowed is not None and text not in allowed:
            diagnostics.append(_diag(
                "dependent ({0}) document has {1}: {2!r}, which is free text. This "
                "field is a closed tag set -- use one of: {3}.".format(
                    role, field, text, ", ".join(allowed))))

        if field in policy["anchor_reference_fields"] and not anchor_re.match(text):
            diagnostics.append(_diag(
                "dependent ({0}) document has {1}: {2!r}, which is not a valid anchor "
                "id. On a dependent-role file this field is a Tier-4 ANCHOR reference "
                "(^<domain>-<...>), not the Tier-3 document_id it carries on a primary "
                "file.".format(role, field, text)))

    return diagnostics


def resolve_dependent_status(grammar: Grammar, contract_status: str | None,
                             dependent_status: str | None) -> str | None:
    """THE PESSIMISTIC STATE RESOLUTION MATRIX. Pure; no I/O, no globals.

    Computes a dependent node's effective state from its parent contract's
    status and its own declared one. The strictest constraint wins, with one
    asymmetry that is the whole point of the design.

        contract     dependent    ->  computed
        active       archived     ->  archived      (dependent is stricter)
        deprecated   active       ->  invalidated   (CASCADING FAILURE)
        deprecated   deprecated   ->  deprecated    (acknowledged)
        deprecated   archived     ->  archived      (acknowledged, stronger)
        draft        active       ->  draft         (parent stricter, no cascade)
        anything     None         ->  contract's    (inherits)

    THE ASYMMETRY: a dependent LESS restrictive than a deprecated/archived
    parent is not merely "as restrictive as its parent" -- it is
    INCONSISTENT. It actively asserts it is in force under a rule that is
    not. Collapsing that to plain `deprecated` would erase the distinction
    between a checklist correctly retired alongside its contract and one
    whose author never got the memo. The computed `invalidated` state --
    never authorable, enforced at config load -- names exactly that second
    case so a consumer can surface it as the integrity failure it is.

    WHY `None` INHERITS RATHER THAN DEFAULTING TO `active`: a dependent with
    no declared status has made no claim about itself, so there is no claim
    to contradict. Treating absence as `active` would fire the cascade on
    every pre-existing dependent file in the corpus (none of which declares a
    status) and rewrite their compiled state wholesale. This branch is what
    makes the whole matrix purely ADDITIVE against the existing corpus --
    verified empirically: with no dependent statuses declared,
    computed_status == contract_status everywhere.

    Raises KeyError on a status outside the declared values rather than
    ranking it as unknown -- same precedent as Taxonomy.layer_for(). Every
    live call site sources both arguments from validated front matter, so
    this is unreachable in the pipeline, and "unreachable" is the reason to
    make it loud rather than the reason to guess.
    """
    if dependent_status is None:
        return contract_status
    if contract_status is None:
        return dependent_status

    policy = grammar.dependent_lifecycle
    strictness = policy["strictness"]

    try:
        dependent_rank = strictness[dependent_status]
        contract_rank = strictness[contract_status]
    except KeyError as exc:
        raise KeyError(
            "{0!r} is not a declared status value (known: {1})".format(
                exc.args[0], sorted(strictness))
        ) from None

    if dependent_rank >= contract_rank:
        return dependent_status
    if contract_status in policy["invalidating_parent_states"]:
        return policy["invalidated_state"]
    return contract_status


def invalidated_state(grammar: Grammar | None = None) -> str:
    """The computed-state sentinel. Config-declared when a Grammar is
    available, falling back to the literal only for a caller that has none."""
    if grammar is None:
        return DEFAULT_INVALIDATED_STATE
    return grammar.dependent_lifecycle["invalidated_state"]
