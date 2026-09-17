# -*- coding: utf-8 -*-
"""lte/engine/grammar.py -- LEAF MODULE. Pure AST grammar compilation.

Extracted from scripts/graph_lib.py's regex block (lines 191-268, 337-372)
and the VALIDATION half of scripts/config_loader.py.

=======================================================================
THE ONE PLACE THIS EXTRACTION DEPARTS FROM THE BRIEF, AND WHY
=======================================================================

The brief says: "Extract ... the full contents of config_loader.py" into a
module with "ZERO side effects. Absolutely no open(), no Path.read_text()."

Those two instructions cannot both be satisfied. config_loader.py's entire
job is reading three files:

    _read_json(GRAMMAR_PATH)      -> open() + json.load
    _read_yaml(TAXONOMY_PATH)     -> open() + yaml.safe_load
    _read_yaml(UI_PROJECTION_PATH)-> open() + yaml.safe_load

and it resolves CONFIG_DIR from the SRKH_REPO_ROOT / SRKH_CONFIG_DIR
environment variables, which is environment-dependent behavior on top of
the disk access. Moving that verbatim into a leaf module declared pure
would make the purity claim false on the first import, and
lte/validators/architecture.py -- which is already wired as the first CI
step and already lists this module in PURE_MODULES -- would fail the build
immediately and correctly.

RESOLUTION: config_loader.py is SPLIT along the seam it already has.

    lte/io/config_reader.py   reads + parses     -> raw dicts   (impure, layer 1)
    lte/engine/grammar.py     validates + compiles-> Grammar    (pure,   layer 1)

Every validation rule, every placeholder-leak check, every named-group
assertion and every fail-fast message from config_loader is preserved
below, operating on an already-parsed mapping instead of a path. Nothing
is dropped; only the three read calls move. The orchestrator wires them:

    raw = config_reader.read_all()                    # lte/io
    grammar = grammar.compile_grammar(raw.linter_rules, known_domains)

This is the same treatment the codebase already applied to
extract_frontmatter -> extract_frontmatter_from_text, for the same reason.
=======================================================================

LOCALES AND EXTENSIONS COME FROM THE CORPUS LAYOUT. compile_grammar()
takes the compiled CorpusLayout (lte/engine/layout.py) and derives the
{locale_sep}, {locale_alt} and {extension_pattern} substitutions from it.
config/linter_rules.json no longer declares a locale list, and no pattern
template contains a literal locale or `.md`. The layout is duck-typed here
(`locales`, `locale_mode`, `extensions`); this module does not import it.

Imports: stdlib + `re` + `yaml` only. `yaml` is a parser library, not an
I/O library -- it is imported here for nothing but safe_load over strings
in lte/engine/frontmatter.py's sibling use; this module does not call it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

SUPPORTED_CONFIG_VERSION = "1.0.0"


class ConfigError(ValueError):
    """A config document is missing, malformed, or internally inconsistent.

    Raised at COMPILE time (when a Grammar is constructed), never lazily at
    first use. A half-loaded grammar silently narrows what counts as a valid
    anchor and then reports the resulting empty corpus as a legitimate one.
    """


# ----------------------------------------------------------------------
# Domain alternation -- shared by every anchor-bearing pattern
# ----------------------------------------------------------------------

def build_extension_pattern(extensions: Sequence[str]) -> str:
    """'\\.md' for one extension; '(?:\\.md|\\.markdown)' for several.

    A single extension is emitted unwrapped so the default layout expands
    split_document to the exact bytes it compiled to before extensions were
    configurable.
    """
    if not extensions:
        raise ConfigError("the corpus layout declares no file extensions.")
    escaped = [re.escape(e) for e in sorted(extensions, key=len, reverse=True)]
    if len(escaped) == 1:
        return escaped[0]
    return "(?:" + "|".join(escaped) + ")"


def layout_substitutions(layout) -> dict:
    """The three filename placeholders, from a compiled CorpusLayout.

    Suffix mode:  locale_sep='\\.', locale_alt='vi|en'
    None mode:    locale_sep='',    locale_alt=''   -> an EMPTY locale group,
                  so patterns that require a named locale group still have one.
    """
    if layout.locales_suffixed:
        locale_sep = re.escape(".")
        locale_alt = "|".join(re.escape(l) for l in layout.locales)
    else:
        locale_sep = ""
        locale_alt = ""
    return {
        "locale_sep": locale_sep,
        "locale_alt": locale_alt,
        "extension_pattern": build_extension_pattern(layout.extensions),
    }


def build_domain_alternation(domains: Sequence[str], sort: str = "length_desc") -> str:
    """Builds the `(?:a|b|c)` alternation substituted into {anchor_id}.

    LONGEST-FIRST MATTERS FOR PORTABILITY, not for CPython. With domains
    `adr` and `adrint`, a leftmost-first engine matches only `adr` against
    the input `adrint-001`, leaving `int-001` to fail the rest of the
    pattern -- every internal-ADR anchor would silently stop parsing.
    Python's own `re` backtracks through all alternatives regardless, so
    this is defensive here; it IS load-bearing for any future port (Go's
    RE2 is leftmost-first on alternation).

    TIES KEEP DECLARATION ORDER, not alphabetical order. `sorted(key=len,
    reverse=True)` is stable, so `spec` and `exec` (both length 4) come out
    in config/taxonomy.yaml's row order. An earlier draft here tie-broke
    alphabetically on the reasoning that it made the pattern byte-stable --
    but a stable sort is already byte-stable, and the alphabetical variant
    silently emitted `exec|spec` where every prior build emitted
    `spec|exec`. Semantically identical, but it churns the compiled pattern
    against every artifact built before this refactor for no gain. Caught
    by diffing the compiled regexes against the legacy module.

    RETURNS AN UNWRAPPED ALTERNATION (`a|b|c`), not `(?:a|b|c)`. The
    templates in config/linter_rules.json already wrap the substitution
    site as `(?:{domain_alt})`; wrapping here too produces `(?:(?:a|b|c))`,
    which is again semantically identical and again a gratuitous byte-level
    difference from every previously compiled pattern.
    """
    if not domains:
        raise ConfigError("cannot build a domain alternation from an empty domain list.")
    for domain in domains:
        if not domain or not isinstance(domain, str):
            raise ConfigError("domain {0!r} is not a non-empty string.".format(domain))
    if sort == "length_desc":
        ordered = sorted(domains, key=len, reverse=True)
    elif sort == "as_declared":
        ordered = list(domains)
    else:
        raise ConfigError(
            "domain_alternation_sort={0!r} is not one of ['length_desc', "
            "'as_declared'].".format(sort))
    return "|".join(re.escape(d) for d in ordered)


# ----------------------------------------------------------------------
# The compiled grammar value object
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Grammar:
    """Every compiled pattern and grammar table the AST parser needs.

    TWO GRAMMARS SUPPORTED SIMULTANEOUSLY, on purpose:
      * Grammar 1 -- heading + standalone-anchor-line + top-level blockquote
      * Grammar 2 -- MkDocs-Material indented admonitions with deeper
        hierarchical anchor ids (^domain-tractate-chapter-node)
    Both produce identical SpecNode/RefCallout objects; ast_blocks.parse_text
    dispatches per-block on whichever opening token it sees.
    """

    # raw string fragments, needed by modules that build further patterns
    anchor_id: str
    domain_alternation: str

    # --- Grammar 1: heading-based --------------------------------------
    spec_anchor: re.Pattern
    heading: re.Pattern
    ref_callout_start: re.Pattern
    blockquote_line: re.Pattern

    # --- Grammar 2: admonition-based -----------------------------------
    admonition_open: re.Pattern
    admonition_indent: re.Pattern
    admonition_blank: re.Pattern
    admonition_trailing_anchor: re.Pattern
    # The spec defines only ONE link token, `[!ref-...]`, for BOTH
    # admonition types -- unlike Grammar 1's separate `[!ref-...]` /
    # `[!ops-...]` tokens. Whether a match becomes a debate or a checklist
    # callout is decided by the ENCLOSING ADMONITION TYPE (see
    # Taxonomy.admonition_callout_kind), never by the token text.
    admonition_ref_line: re.Pattern
    # Optional per-contract status override: a `note` block's body may carry
    # one standalone `{status: <value>}` line. The value is captured RAW and
    # validated by the caller against status_values, exactly mirroring how
    # document-level front matter `status` is handled.
    admonition_status: re.Pattern

    # --- Document structure --------------------------------------------
    # Exactly one leading '#' -- a true H1 only, deliberately distinct from
    # `heading` (which matches #{1,6} for spec-block detection), since a
    # document's title must come from its OWN single H1, not from whichever
    # heading level happens to precede the next anchor.
    document_h1: re.Pattern
    split_document: re.Pattern
    file_naming: re.Pattern
    # Declared in config alongside every other pattern. An earlier draft of
    # this module hardcoded `^---\\s*$` on the reasoning that the YAML front
    # matter fence is fixed by convention rather than by this project's
    # grammar. That reasoning was wrong on the facts: linter_rules.json
    # declares `frontmatter_fence`, so hardcoding it would have created a
    # second source of truth that silently ignores the config.
    frontmatter_fence: re.Pattern

    # --- Tables ---------------------------------------------------------
    document_roles: tuple
    status_values: tuple
    file_naming_roles: tuple
    # The layout's locale ids, copied here so consumers holding only a
    # Grammar see the same set the patterns were compiled with.
    file_naming_locales: tuple
    file_naming_severity: str
    file_naming_source: str
    payload_lock: Mapping
    unindexed_policy: Mapping
    dependent_lifecycle: Mapping

    def anchor_reference_pattern(self) -> re.Pattern:
        """A bare anchor id, caret optional -- for validating a frontmatter
        scalar such as `superseded_by: ^spec-lte-01-002`.

        Built from the SAME config-derived anchor_id fragment the two
        authoring grammars use, never a second hand-written anchor pattern:
        adding a partition widens this automatically. `spec_anchor` cannot be
        reused directly because it is a whole-LINE matcher that REQUIRES the
        caret, and a YAML scalar is neither a line nor caret-bearing by
        convention.
        """
        return re.compile(r"^\^?(?P<id>" + self.anchor_id + r")$")


# ----------------------------------------------------------------------
# Validation + compilation
# ----------------------------------------------------------------------

def _require_version(data: Mapping, label: str) -> None:
    version = data.get("config_version")
    if version != SUPPORTED_CONFIG_VERSION:
        raise ConfigError(
            "{0}: config_version={1!r} is not supported (expected {2!r}).".format(
                label, version, SUPPORTED_CONFIG_VERSION))


def _str_tuple(data: Mapping, key: str, label: str) -> tuple:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError("{0}: {1} must be a non-empty list.".format(label, key))
    if not all(isinstance(v, str) and v for v in value):
        raise ConfigError("{0}: {1} must contain only non-empty strings.".format(label, key))
    return tuple(value)


def _compile(name: str, template: str, substitutions: Mapping[str, str],
             required_groups: Sequence[str], required_group_count: int | None,
             label: str) -> re.Pattern:
    """Expands placeholders in one pattern template, compiles it, and asserts
    its capture-group contract.

    THE PLACEHOLDER-LEAK CHECK IS NOT PARANOIA. A template retaining an
    unexpanded `{anchor_id}` compiles cleanly as a literal brace sequence and
    then matches nothing at all -- the corpus parses to zero nodes and the
    pipeline reports an empty-but-valid document set rather than a broken
    grammar. That failure is invisible without this check.

    THE NAMED-GROUP CHECK IS THE SAME CLASS OF BUG. A dropped `?P<id>`
    compiles fine and raises IndexError deep inside a corpus walk, or worse,
    silently shifts positional group numbering.
    """
    if not isinstance(template, str) or not template:
        raise ConfigError("{0}: pattern {1!r} must be a non-empty string.".format(label, name))

    expanded = template
    for placeholder, replacement in substitutions.items():
        expanded = expanded.replace("{" + placeholder + "}", replacement)

    leaked = re.findall(r"\{([a-z_]+)\}", expanded)
    if leaked:
        raise ConfigError(
            "{0}: pattern {1!r} still contains unexpanded placeholder(s) {2} after "
            "substitution. Known placeholders: {3}. An unexpanded placeholder compiles "
            "to a literal and silently matches nothing.".format(
                label, name, sorted(set(leaked)), sorted(substitutions)))

    try:
        compiled = re.compile(expanded)
    except re.error as exc:
        raise ConfigError(
            "{0}: pattern {1!r} does not compile: {2}\n  expanded: {3}".format(
                label, name, exc, expanded))

    missing = [g for g in required_groups if g not in compiled.groupindex]
    if missing:
        raise ConfigError(
            "{0}: pattern {1!r} is missing required named group(s) {2} after expansion "
            "(has: {3}). Call sites read these by name.".format(
                label, name, missing, sorted(compiled.groupindex)))

    if required_group_count is not None and compiled.groups != required_group_count:
        raise ConfigError(
            "{0}: pattern {1!r} has {2} capture group(s), expected {3}. Call sites read "
            "some of these positionally.".format(
                label, name, compiled.groups, required_group_count))

    return compiled


def compile_grammar(linter_rules: Mapping, known_domains: Sequence[str],
                    label: str = "config/linter_rules.json", *, layout=None) -> Grammar:
    """Validates config/linter_rules.json and compiles it into a Grammar.

    `linter_rules` is the already-parsed mapping; `known_domains` comes from
    the Taxonomy; `layout` is the compiled CorpusLayout. Nothing is read from
    disk here.

    Templates carry {anchor_id} / {role_alt} / {domain_alt} placeholders
    expanded against `known_domains`, so adding a partition to
    config/taxonomy.yaml widens the accepted anchor grammar automatically, in
    exactly one place. {locale_sep} / {locale_alt} / {extension_pattern} are
    expanded against `layout` the same way.

    `layout` is keyword-only and REQUIRED in practice: without it the
    filename patterns have no locale set and no extension to compile
    against. lte/io/config_reader.load() always supplies it.
    """
    if not isinstance(linter_rules, Mapping):
        raise ConfigError("{0}: must parse to a mapping.".format(label))
    _require_version(linter_rules, label)
    if layout is None:
        raise ConfigError(
            "{0}: compile_grammar() needs the compiled corpus layout (layout=...). "
            "Locales and file extensions are declared in config/corpus_layout.yaml; "
            "lte.io.config_reader.load() passes it.".format(label))

    sort = linter_rules.get("domain_alternation_sort", "length_desc")
    domain_alt = build_domain_alternation(list(known_domains), sort)

    anchor_template = linter_rules.get("anchor_id_template")
    if not isinstance(anchor_template, str) or not anchor_template:
        raise ConfigError("{0}: anchor_id_template must be a non-empty string.".format(label))
    # The template is `(?:{domain_alt})(?:-[A-Za-z0-9]+)+` -- it supplies its
    # own non-capturing group, which is why build_domain_alternation returns
    # an unwrapped alternation.
    anchor_id = anchor_template.replace("{domain_alt}", domain_alt)
    if "{" in anchor_id:
        raise ConfigError(
            "{0}: anchor_id_template still contains a placeholder after expansion: "
            "{1!r}".format(label, anchor_id))

    document_roles = _str_tuple(linter_rules, "document_roles", label)
    status_values = _str_tuple(linter_rules, "status_values", label)
    role_alt = "|".join(re.escape(r) for r in document_roles)

    file_naming = linter_rules.get("file_naming")
    if not isinstance(file_naming, Mapping):
        raise ConfigError("{0}: file_naming must be a mapping.".format(label))
    naming_roles = _str_tuple(file_naming, "roles", label + ".file_naming")
    if "locales" in file_naming:
        raise ConfigError(
            "{0}.file_naming: 'locales' is no longer read here. The locale set is "
            "declared once, in config/corpus_layout.yaml (filenames.locale); remove "
            "this key so the two declarations cannot drift.".format(label))
    naming_locales = tuple(layout.locales)

    substitutions = {
        "anchor_id": anchor_id,
        "domain_alt": domain_alt,
        "role_alt": role_alt,
        # The config declares this as {file_role_alt}, distinct from
        # {role_alt}: file_naming.roles is the SUPERSET the filename pattern
        # accepts (it includes 'evi', which is not a mergeable
        # document_role), while role_alt covers only the three roles that
        # regroup into one Tier-3 document. Using one for the other would
        # make .evi files unnameable or make them look mergeable.
        "file_role_alt": "|".join(re.escape(r) for r in naming_roles),
    }
    substitutions.update(layout_substitutions(layout))

    patterns = linter_rules.get("patterns")
    if not isinstance(patterns, Mapping):
        raise ConfigError("{0}: 'patterns' must be a mapping.".format(label))

    required_groups: Mapping = linter_rules.get("required_named_groups", {})
    required_counts: Mapping = linter_rules.get("required_group_counts", {})

    def build(name: str) -> re.Pattern:
        if name not in patterns:
            raise ConfigError(
                "{0}: patterns.{1} is not declared. Every pattern the AST parser "
                "dispatches on must exist; a missing one cannot be defaulted.".format(
                    label, name))
        return _compile(
            name, patterns[name], substitutions,
            required_groups.get(name, ()), required_counts.get(name), label)

    naming_pattern_src = file_naming.get("pattern")
    if not isinstance(naming_pattern_src, str) or not naming_pattern_src:
        raise ConfigError("{0}.file_naming: 'pattern' must be a non-empty string.".format(label))
    naming_compiled = _compile(
        "file_naming.pattern", naming_pattern_src, substitutions,
        tuple(file_naming.get("required_named_groups", ("slug", "role", "locale"))),
        None, label)

    severity = file_naming.get("severity", "warn")
    if severity not in ("info", "warn", "fail"):
        raise ConfigError(
            "{0}.file_naming: severity={1!r} is not one of ['info', 'warn', "
            "'fail'].".format(label, severity))

    unindexed = linter_rules.get("unindexed_policy", {})
    if not isinstance(unindexed, Mapping) or "default" not in unindexed:
        raise ConfigError(
            "{0}: unindexed_policy must be a mapping with a 'default' key.".format(label))

    return Grammar(
        anchor_id=anchor_id,
        domain_alternation=domain_alt,
        spec_anchor=build("spec_anchor"),
        heading=build("heading"),
        ref_callout_start=build("ref_callout_start"),
        blockquote_line=build("blockquote_line"),
        admonition_open=build("admonition_open"),
        admonition_indent=build("admonition_indent"),
        admonition_blank=build("admonition_blank"),
        admonition_trailing_anchor=build("admonition_trailing_anchor"),
        admonition_ref_line=build("admonition_ref_line"),
        admonition_status=build("admonition_status"),
        document_h1=build("document_h1"),
        split_document=build("split_document"),
        file_naming=naming_compiled,
        frontmatter_fence=build("frontmatter_fence"),
        document_roles=document_roles,
        status_values=status_values,
        file_naming_roles=naming_roles,
        file_naming_locales=naming_locales,
        file_naming_severity=severity,
        file_naming_source=file_naming.get("diagnostic_source", "file-naming-convention"),
        payload_lock=linter_rules.get("payload_lock", {}),
        unindexed_policy=unindexed,
        dependent_lifecycle=validate_dependent_lifecycle(
            linter_rules.get("dependent_lifecycle"), status_values, label),
    )


def validate_dependent_lifecycle(block: Mapping | None, status_values: Sequence[str],
                                 label: str) -> Mapping:
    """Validates the dependent-node lifecycle policy consumed by
    lte/engine/state_machine.py.

    `status_values` is passed IN rather than re-read, so there is exactly one
    parse of the closed status lexicon and no way for this to validate
    against a different copy than the Grammar actually holds.

    Fail-fast contract:
      * strictness_order must be a PERMUTATION of status_values -- not a
        subset, not a superset. A missing entry leaves a legal status
        unrankable and resolve_dependent_status() would have to guess a rank
        for it per-node at compile time.
      * invalidating_parent_states must be a subset of status_values.
      * invalidated_state must NOT be in status_values -- it is COMPUTED,
        never authorable. If an author could write `status: invalidated` by
        hand, the matrix output would be indistinguishable from a claim.
      * every conditional_requirements key must be a real status.
    """
    if not isinstance(block, Mapping):
        raise ConfigError("{0}: 'dependent_lifecycle' must be an object.".format(label))

    def _lst(key: str) -> tuple:
        value = block.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError(
                "{0}: dependent_lifecycle.{1} must be a list of non-empty strings.".format(
                    label, key))
        return tuple(value)

    roles = _lst("dependent_roles")
    reasons = _lst("archived_reasons")
    anchor_fields = _lst("anchor_reference_fields")
    strictness = _lst("strictness_order")
    invalidating = _lst("invalidating_parent_states")

    known = set(status_values)
    if set(strictness) != known or len(strictness) != len(known):
        raise ConfigError(
            "{0}: dependent_lifecycle.strictness_order must be a permutation of "
            "status_values. Got {1}, expected the same members as {2}. Every legal "
            "status needs exactly one rank.".format(label, list(strictness), sorted(known)))

    unknown = sorted(set(invalidating) - known)
    if unknown:
        raise ConfigError(
            "{0}: dependent_lifecycle.invalidating_parent_states names {1}, absent "
            "from status_values {2}.".format(label, unknown, sorted(known)))

    invalidated_state = block.get("invalidated_state")
    if not isinstance(invalidated_state, str) or not invalidated_state:
        raise ConfigError(
            "{0}: dependent_lifecycle.invalidated_state must be a string.".format(label))
    if invalidated_state in known:
        raise ConfigError(
            "{0}: dependent_lifecycle.invalidated_state={1!r} is also in status_values. "
            "It is a COMPUTED state and must not be authorable, or a hand-written "
            "status would be indistinguishable from a resolved one.".format(
                label, invalidated_state))

    requirements = block.get("conditional_requirements", {})
    if not isinstance(requirements, Mapping):
        raise ConfigError(
            "{0}: dependent_lifecycle.conditional_requirements must be a mapping.".format(label))
    unknown_req = sorted(set(requirements) - known)
    if unknown_req:
        raise ConfigError(
            "{0}: dependent_lifecycle.conditional_requirements keys {1} are not in "
            "status_values {2}.".format(label, unknown_req, sorted(known)))
    normalized_req = {}
    for status, fields in requirements.items():
        if not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields):
            raise ConfigError(
                "{0}: dependent_lifecycle.conditional_requirements.{1} must be a list "
                "of non-empty field names.".format(label, status))
        normalized_req[status] = tuple(fields)

    enum_fields = block.get("enum_fields", {})
    if not isinstance(enum_fields, Mapping):
        raise ConfigError(
            "{0}: dependent_lifecycle.enum_fields must be a mapping.".format(label))
    enums = {}
    for field, list_name in enum_fields.items():
        if list_name != "archived_reasons":
            raise ConfigError(
                "{0}: dependent_lifecycle.enum_fields.{1} names list {2!r}, which is "
                "not declared. Known: ['archived_reasons'].".format(label, field, list_name))
        enums[field] = reasons

    severity = block.get("severity", "fail")
    if severity not in ("info", "warn", "fail"):
        raise ConfigError(
            "{0}: dependent_lifecycle.severity={1!r} is not one of ['info', 'warn', "
            "'fail'].".format(label, severity))

    unknown_roles = sorted(set(roles) - set(_roles_hint(block)))
    del unknown_roles  # cross-checked against file_naming.roles by the caller

    return {
        "dependent_roles": roles,
        "archived_reasons": reasons,
        "conditional_requirements": normalized_req,
        "enum_fields": enums,
        "anchor_reference_fields": anchor_fields,
        "strictness": {status: rank for rank, status in enumerate(strictness)},
        "strictness_order": strictness,
        "invalidating_parent_states": frozenset(invalidating),
        "invalidated_state": invalidated_state,
        "severity": severity,
        "source": block.get("diagnostic_source", "dependent-lifecycle"),
    }


def _roles_hint(block: Mapping) -> tuple:
    return tuple(block.get("dependent_roles", ()))


def assert_dependent_roles_producible(grammar: Grammar) -> None:
    """Cross-check: every dependent role must be one the filename grammar can
    actually produce.

    Separate from compile_grammar() because it needs the FINISHED Grammar --
    file_naming.roles is validated there, dependent_roles here. A dependent
    role the filename pattern cannot emit is dead config that looks live: no
    file could ever carry it, so the lifecycle rules keyed on it would never
    fire and nobody would notice.
    """
    unknown = sorted(set(grammar.dependent_lifecycle["dependent_roles"])
                     - set(grammar.file_naming_roles))
    if unknown:
        raise ConfigError(
            "dependent_lifecycle.dependent_roles names role(s) {0} that "
            "file_naming.roles cannot produce (known: {1}). No file could ever "
            "carry them.".format(unknown, list(grammar.file_naming_roles)))
