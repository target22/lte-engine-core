# -*- coding: utf-8 -*-
"""lte/engine/grammar.py -- LEAF MODULE. Pure AST grammar compilation.

Extracted from scripts/graph_lib.py's regex block and the VALIDATION half of
scripts/config_loader.py.

=======================================================================
THE ONE PLACE THIS EXTRACTION DEPARTS FROM THE BRIEF, AND WHY
=======================================================================

The brief says: "Extract ... the full contents of config_loader.py" into a
module with "ZERO side effects. Absolutely no open(), no Path.read_text()."

Those two instructions cannot both be satisfied. config_loader.py's entire
job is reading three files, and it resolves CONFIG_DIR from the
SRKH_REPO_ROOT / SRKH_CONFIG_DIR environment variables, which is
environment-dependent behavior on top of the disk access. Moving that
verbatim into a leaf module declared pure would make the purity claim false
on the first import, and lte/validators/architecture.py -- already wired as
the first CI step and already listing this module in PURE_MODULES -- would
fail the build immediately and correctly.

RESOLUTION: config_loader.py is SPLIT along the seam it already has.

    lte/io/config_reader.py   reads + parses     -> raw dicts   (impure, layer 1)
    lte/engine/grammar.py     validates + compiles-> Grammar    (pure,   layer 1)

Every validation rule, every placeholder-leak check, every named-group
assertion and every fail-fast message from config_loader is preserved below,
operating on an already-parsed mapping instead of a path. Nothing is
dropped; only the three read calls move.

=======================================================================
ONE GRAMMAR, AS OF v1.1.0
=======================================================================

The MkDocs-admonition grammar is GONE. Six patterns went with it --
admonition_open, admonition_indent, admonition_blank,
admonition_trailing_anchor, admonition_ref_line, admonition_status -- and
so did the Grammar fields that carried them. A corpus file using `!!! note`
now parses to zero nodes and is reported as a banned construct by
lte/validators/corpus.py and lte/validators/draft.py.

`admonition_status` is the ONE survivor, renamed `status_directive`. It was
never admonition machinery: it carries the optional per-contract
`{status: ...}` override, which ast_blocks.parse_text() scans for in a
heading block's buffered body. Deleting it with the rest would have silently
removed a feature that lte/engine/payload_lock.py and
lte/engine/graph_builder.py both still read.

=======================================================================
TWO VOCABULARIES, DELIBERATELY SEPARATE
=======================================================================

A dependent block is authored as `> [!<token>-<anchor>]`. The TOKEN is a
user-facing alias declared in linter_rules.json#callout_kinds.aliases and is
freely renameable. It resolves to an internal ROLE -- the closed set
dependent_lifecycle.dependent_roles declares, which graph.json's per-node
arrays, the frontend and the state machine are all keyed on.

Only the alias set feeds {kind_alt}, the pattern's token alternation. Only
the role set feeds {dependent_role_alt}, which composite child ids are built
from. Renaming `ref` to `cite` therefore changes what authors type and
changes nothing in any compiled artifact. No role name and no token string
is spelled anywhere in this module.

LOCALES AND EXTENSIONS COME FROM THE CORPUS LAYOUT. compile_grammar() takes
the compiled CorpusLayout (lte/engine/layout.py) and derives {locale_sep},
{locale_alt} and {extension_pattern} from it. config/linter_rules.json
declares no locale list, and no pattern template contains a literal locale
or `.md`. The layout is duck-typed here (`locales`, `locales_suffixed`,
`extensions`); this module does not import it.

Imports: stdlib + `re` only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

SUPPORTED_CONFIG_VERSION = "1.0.0"

# Fields graph_builder writes onto a contract node dict. A callout role's
# graph_key must not collide with one of these (validated in
# lte/engine/taxonomy.py), and they are listed here only so the two modules
# cannot drift on what "reserved" means.
RESERVED_NODE_FIELDS = frozenset(
    {"anchor_id", "title", "layer", "body", "status", "parent_anchor"})


class ConfigError(ValueError):
    """A config document is missing, malformed, or internally inconsistent.

    Raised at COMPILE time (when a Grammar is constructed), never lazily at
    first use. A half-loaded grammar silently narrows what counts as a valid
    anchor and then reports the resulting empty corpus as a legitimate one.
    """


# ----------------------------------------------------------------------
# Alternations -- shared by every anchor- and token-bearing pattern
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


def build_alternation(values: Sequence[str], sort: str = "length_desc",
                      what: str = "value") -> str:
    """Builds the `a|b|c` alternation substituted into a pattern template.

    LONGEST-FIRST MATTERS FOR PORTABILITY, not for CPython. With domains
    `adr` and `adrint`, a leftmost-first engine matches only `adr` against
    the input `adrint-001`, leaving `int-001` to fail the rest of the
    pattern -- every internal-ADR anchor would silently stop parsing.
    Python's own `re` backtracks through all alternatives regardless, so
    this is defensive here; it IS load-bearing for any future port (Go's RE2
    is leftmost-first on alternation). The same reasoning applies verbatim
    to callout tokens: `ops` and `opslog` would collide the same way.

    TIES KEEP DECLARATION ORDER, not alphabetical order. `sorted(key=len,
    reverse=True)` is stable, so `spec` and `exec` (both length 4) come out
    in config/taxonomy.yaml's row order. An earlier draft tie-broke
    alphabetically on the reasoning that it made the pattern byte-stable --
    but a stable sort is already byte-stable, and the alphabetical variant
    silently emitted `exec|spec` where every prior build emitted
    `spec|exec`. Semantically identical, but it churns the compiled pattern
    against every artifact built before the refactor for no gain.

    RETURNS AN UNWRAPPED ALTERNATION (`a|b|c`), not `(?:a|b|c)`. The
    templates in config/linter_rules.json already wrap the substitution site
    as `(?:{domain_alt})`; wrapping here too produces `(?:(?:a|b|c))`, again
    semantically identical and again a gratuitous byte-level difference from
    every previously compiled pattern.
    """
    if not values:
        raise ConfigError(
            "cannot build a {0} alternation from an empty list.".format(what))
    for value in values:
        if not value or not isinstance(value, str):
            raise ConfigError("{0} {1!r} is not a non-empty string.".format(what, value))
    if sort == "length_desc":
        ordered = sorted(values, key=len, reverse=True)
    elif sort == "as_declared":
        ordered = list(values)
    else:
        raise ConfigError(
            "{0}_alternation_sort={1!r} is not one of ['length_desc', "
            "'as_declared'].".format(what, sort))
    return "|".join(re.escape(v) for v in ordered)


def build_domain_alternation(domains: Sequence[str], sort: str = "length_desc") -> str:
    """Domain-specific wrapper around build_alternation().

    Kept as a named function because callers outside this module import it
    by name; the general form exists because callout tokens, dependent roles
    and sub-block letters need the identical treatment.
    """
    return build_alternation(domains, sort, "domain")


# ----------------------------------------------------------------------
# The compiled grammar value object
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Grammar:
    """Every compiled pattern and grammar table the AST parser needs.

    ONE GRAMMAR, TWO STRUCTURAL ENTITIES:
      * PRIMARY KEY (standalone) -- a heading, prose, terminated by a line
        holding only `^<anchor>`. Emits a SpecNode.
      * FOREIGN KEY (dependent)  -- a top-level blockquote opened by
        `> [!<token>-<parent>]`. Emits a RefCallout whose kind is the
        internal role `callout_kind_aliases` maps the token to.
    """

    # --- raw string fragments, for modules that build further patterns ---
    anchor_id: str
    domain_alternation: str
    # Token alternation actually compiled into ref_callout_start. Exposed so
    # a diagnostic can tell an author which kinds are configured without
    # re-deriving it.
    kind_alternation: str
    # Expanded composite-child-id fragment: <anchor><sep><role>-<letter><nn>.
    composite_id: str

    # --- Primary key -----------------------------------------------------
    spec_anchor: re.Pattern
    heading: re.Pattern

    # --- Foreign key -----------------------------------------------------
    # The token carries the kind: `[!ref-...]`, `[!ops-...]`, `[!evi-...]`,
    # or whatever the alias table declares. Nothing about the enclosing
    # block decides it -- there is no enclosing block any more.
    ref_callout_start: re.Pattern
    blockquote_line: re.Pattern

    # --- Hierarchy -------------------------------------------------------
    # Trailing sub-block suffix (`-c01`, `-d03`). Built from a CLOSED letter
    # set, which is what keeps `^spec-lte-01-002-x99` from being read as a
    # child of `^spec-lte-01-002`. See integrity.parent_anchor_of(): a match
    # here is necessary but not sufficient -- the remaining prefix must also
    # exist as a real anchor.
    subblock_suffix: re.Pattern

    # --- Per-contract status override ------------------------------------
    # One standalone `{status: <value>}` line inside a heading block. The
    # value is captured RAW and validated by the caller against
    # status_values, exactly mirroring how document-level front matter
    # `status` is handled. Formerly `admonition_status`; it was never
    # admonition machinery.
    status_directive: re.Pattern

    # --- Document structure ----------------------------------------------
    # Exactly one leading '#' -- a true H1 only, deliberately distinct from
    # `heading` (which matches #{1,6} for spec-block detection), since a
    # document's title must come from its OWN single H1, not from whichever
    # heading level happens to precede the next anchor.
    document_h1: re.Pattern
    split_document: re.Pattern
    file_naming: re.Pattern
    # Declared in config alongside every other pattern. An earlier draft
    # hardcoded `^---\\s*$` on the reasoning that the YAML front matter fence
    # is fixed by convention rather than by this project's grammar. That was
    # wrong on the facts: linter_rules.json declares `frontmatter_fence`, so
    # hardcoding it would have created a second source of truth that
    # silently ignores the config.
    frontmatter_fence: re.Pattern

    # --- Near-miss diagnostics -------------------------------------------
    # Deliberately PERMISSIVE, and never used to parse anything. These match
    # a line TRYING to be a token but failing the real grammar, which is the
    # only way an unknown anchor domain or a banned construct becomes
    # visible instead of compiling to zero nodes. Consumed by
    # lte/engine/integrity.py; lte/validators/draft.py compiles its own copy
    # for the pre-ingest path.
    loose_anchor_line: re.Pattern
    loose_ref_line: re.Pattern
    loose_admonition_open: re.Pattern
    loose_table_row: re.Pattern

    # --- Tables -----------------------------------------------------------
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

    # --- Callout vocabulary ----------------------------------------------
    # authored token -> internal dependent role. ast_blocks.parse_text()
    # indexes this directly (not .get) because ref_callout_start only admits
    # configured tokens: a miss would mean the compiled alternation and this
    # table had drifted, which is a crash worth having.
    callout_kind_aliases: Mapping
    # internal role -> single letter used in a composite child id.
    callout_suffix_letters: Mapping
    # The one character joining a parent anchor to its child key. Validated
    # to be outside [A-Za-z0-9-], or a composite id would be
    # indistinguishable from a plain anchor.
    composite_separator: str

    def anchor_reference_pattern(self) -> re.Pattern:
        """A bare anchor id OR a composite child id, caret optional -- for
        validating a front-matter scalar such as
        `superseded_by: ^spec-lte-01-002` or
        `superseded_by: ^spec-lte-01-002#debate-d01`.

        Built from the SAME config-derived fragments the authoring grammar
        uses, never a second hand-written anchor pattern: adding a partition
        or a dependent role widens this automatically. `spec_anchor` cannot
        be reused directly because it is a whole-LINE matcher that REQUIRES
        the caret, and a YAML scalar is neither a line nor caret-bearing by
        convention.

        COMPOSITE FIRST in the alternation. Python's `re` backtracks either
        way, but a leftmost-first engine would match the bare-anchor prefix
        and then fail on the separator -- the same RE2 precaution as
        build_alternation()'s length_desc ordering.
        """
        return re.compile(
            r"^\^?(?P<id>" + self.composite_id + r"|" + self.anchor_id + r")$")


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


def validate_callout_kinds(block: Mapping | None, dependent_roles: Sequence[str],
                           label: str) -> dict:
    """Validates the configurable callout vocabulary.

    Returns a normalized mapping with keys: aliases, suffix_letters,
    subblock_letters, separator, sort.

    Fail-fast contract, each rule guarding a failure that is otherwise
    silent:
      * an alias VALUE must be a declared dependent_role -- an alias
        pointing at a role the lifecycle engine never heard of parses fine
        and then never fires;
      * an alias TOKEN must be [A-Za-z0-9]+ -- it sits directly before the
        anchor's '-' separator, so a '-' inside it makes the callout pattern
        ambiguous and a regex metacharacter makes it wrong;
      * every aliased role needs a suffix letter, and the letters must be
        pairwise distinct, or two roles' composite child ids collide;
      * suffix_letters must be a SUBSET of subblock_letters, or a minted
        composite key would not be recognizable as a sub-anchor by
        integrity.parent_anchor_of();
      * the separator must be outside [A-Za-z0-9-], or a composite id is
        indistinguishable from a plain anchor.
    """
    if not isinstance(block, Mapping):
        raise ConfigError(
            "{0}: 'callout_kinds' must be a mapping. Without it there is no token "
            "vocabulary and no dependent block could parse.".format(label))

    aliases = block.get("aliases")
    if not isinstance(aliases, Mapping) or not aliases:
        raise ConfigError(
            "{0}.callout_kinds: 'aliases' must be a non-empty mapping of authored "
            "token -> internal dependent role.".format(label))

    known_roles = set(dependent_roles)
    for token, role in aliases.items():
        if not isinstance(token, str) or not token:
            raise ConfigError(
                "{0}.callout_kinds.aliases: token {1!r} is not a non-empty "
                "string.".format(label, token))
        if re.search(r"[^A-Za-z0-9]", token):
            raise ConfigError(
                "{0}.callout_kinds.aliases: token {1!r} contains a character outside "
                "[A-Za-z0-9]. The token sits directly before the anchor's '-' "
                "separator; a '-' or regex metacharacter there makes the callout "
                "pattern ambiguous.".format(label, token))
        if role not in known_roles:
            raise ConfigError(
                "{0}.callout_kinds.aliases[{1!r}] = {2!r} is not one of "
                "dependent_lifecycle.dependent_roles {3}. An alias must map to a role "
                "the lifecycle engine already knows.".format(
                    label, token, role, sorted(known_roles)))

    subblock_letters = block.get("subblock_letters")
    if not isinstance(subblock_letters, list) or not subblock_letters or not all(
            isinstance(x, str) and len(x) == 1 and x.isalpha() for x in subblock_letters):
        raise ConfigError(
            "{0}.callout_kinds: 'subblock_letters' must be a non-empty list of single "
            "ASCII letters. Narrowing this set is what keeps an anchor whose last "
            "segment merely looks like '-c01' from being read as a child.".format(label))

    suffix_letters = block.get("suffix_letters")
    if not isinstance(suffix_letters, Mapping):
        raise ConfigError(
            "{0}.callout_kinds: 'suffix_letters' must be a mapping of role -> "
            "letter.".format(label))
    missing = sorted(set(aliases.values()) - set(suffix_letters))
    if missing:
        raise ConfigError(
            "{0}.callout_kinds.suffix_letters has no letter for role(s) {1}. Every "
            "aliased role needs one or its composite child ids cannot be "
            "minted.".format(label, missing))
    by_letter: dict = {}
    for role, letter in suffix_letters.items():
        if not isinstance(letter, str) or not re.fullmatch(r"[A-Za-z]", letter):
            raise ConfigError(
                "{0}.callout_kinds.suffix_letters[{1!r}] = {2!r} must be a single "
                "ASCII letter.".format(label, role, letter))
        if letter in by_letter:
            raise ConfigError(
                "{0}.callout_kinds.suffix_letters: {1!r} is used by both {2!r} and "
                "{3!r}. Their composite child ids would collide.".format(
                    label, letter, by_letter[letter], role))
        by_letter[letter] = role
    stray = sorted(set(suffix_letters.values()) - set(subblock_letters))
    if stray:
        raise ConfigError(
            "{0}.callout_kinds: suffix_letters uses {1}, absent from subblock_letters "
            "{2}. A minted composite key would then not be recognizable as a "
            "sub-anchor.".format(label, stray, subblock_letters))

    separator = block.get("separator", "#")
    if not isinstance(separator, str) or len(separator) != 1 or \
            re.fullmatch(r"[A-Za-z0-9-]", separator):
        raise ConfigError(
            "{0}.callout_kinds.separator={1!r} must be exactly one character outside "
            "[A-Za-z0-9-], or a composite id is indistinguishable from a plain "
            "anchor.".format(label, separator))

    return {
        "aliases": dict(aliases),
        "suffix_letters": dict(suffix_letters),
        "subblock_letters": list(subblock_letters),
        "separator": separator,
        "sort": block.get("kind_alternation_sort", "length_desc"),
    }


def compile_grammar(linter_rules: Mapping, known_domains: Sequence[str],
                    label: str = "config/linter_rules.json", *, layout=None) -> Grammar:
    """Validates config/linter_rules.json and compiles it into a Grammar.

    `linter_rules` is the already-parsed mapping; `known_domains` comes from
    the Taxonomy; `layout` is the compiled CorpusLayout. Nothing is read from
    disk here.

    ORDER IS LOAD-BEARING and is the reason this function reads top to
    bottom rather than assembling Grammar(...) inline:

      1. dependent_lifecycle is validated FIRST, into a local. It supplies
         the role set that validate_callout_kinds() checks aliases against.
         (It used to be validated inside the Grammar(...) call itself, which
         made it unavailable to anything that had to run before.)
      2. callout_kinds is validated next, yielding the alias table.
      3. EVERY placeholder -- including {kind_alt}, {composite_sep},
         {dependent_role_alt} and {subblock_letter_alt} -- goes into
         `substitutions` BEFORE the first build() call. A placeholder added
         after that point expands in no pattern and _compile()'s leak check
         fires with a bare list of names, which is exactly how a
         partially-applied edit to this function presents.
      4. {composite_id} is expanded from the three fragments above and added
         to the same dict, so ref_callout_start can accept either a plain
         anchor or a composite child id.

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

    # --- 1. tables --------------------------------------------------------
    document_roles = _str_tuple(linter_rules, "document_roles", label)
    status_values = _str_tuple(linter_rules, "status_values", label)

    # Hoisted out of the Grammar(...) call: validate_callout_kinds() below
    # needs the role set this produces.
    lifecycle = validate_dependent_lifecycle(
        linter_rules.get("dependent_lifecycle"), status_values, label)

    kinds = validate_callout_kinds(
        linter_rules.get("callout_kinds"), lifecycle["dependent_roles"], label)

    # --- 2. fragments -----------------------------------------------------
    domain_sort = linter_rules.get("domain_alternation_sort", "length_desc")
    domain_alt = build_alternation(list(known_domains), domain_sort, "domain")

    anchor_template = linter_rules.get("anchor_id_template")
    if not isinstance(anchor_template, str) or not anchor_template:
        raise ConfigError("{0}: anchor_id_template must be a non-empty string.".format(label))
    # The template is `(?:{domain_alt})(?:-[A-Za-z0-9]+)+` -- it supplies its
    # own non-capturing group, which is why build_alternation returns an
    # unwrapped alternation.
    anchor_id = anchor_template.replace("{domain_alt}", domain_alt)
    if "{" in anchor_id:
        raise ConfigError(
            "{0}: anchor_id_template still contains a placeholder after expansion: "
            "{1!r}".format(label, anchor_id))

    kind_alt = build_alternation(list(kinds["aliases"]), kinds["sort"], "kind")
    # Composite child ids are keyed on the internal ROLE, not the authored
    # token: renaming an alias must not rewrite every stored child id.
    dependent_role_alt = build_alternation(
        sorted(set(kinds["aliases"].values())), "length_desc", "role")
    # Single letters have no length to sort by, so declaration order.
    subblock_letter_alt = build_alternation(
        kinds["subblock_letters"], "as_declared", "letter")

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

    # --- 3. substitutions, COMPLETE before any build() --------------------
    substitutions = {
        "anchor_id": anchor_id,
        "domain_alt": domain_alt,
        "role_alt": "|".join(re.escape(r) for r in document_roles),
        # The config declares this as {file_role_alt}, distinct from
        # {role_alt}: file_naming.roles is the SUPERSET the filename pattern
        # accepts (it includes 'evi', which is not a mergeable
        # document_role), while role_alt covers only the roles that regroup
        # into one Tier-3 document. Using one for the other would make .evi
        # files unnameable or make them look mergeable.
        "file_role_alt": "|".join(re.escape(r) for r in naming_roles),
        "kind_alt": kind_alt,
        "dependent_role_alt": dependent_role_alt,
        "subblock_letter_alt": subblock_letter_alt,
        "composite_sep": re.escape(kinds["separator"]),
    }
    substitutions.update(layout_substitutions(layout))

    composite_template = linter_rules.get("composite_id_template")
    if not isinstance(composite_template, str) or not composite_template:
        raise ConfigError(
            "{0}: composite_id_template must be a non-empty string. Dependent blocks "
            "carry a composite primary key and it cannot be defaulted.".format(label))
    composite_id = composite_template
    for placeholder, replacement in substitutions.items():
        composite_id = composite_id.replace("{" + placeholder + "}", replacement)
    leaked = re.findall(r"\{([a-z_]+)\}", composite_id)
    if leaked:
        raise ConfigError(
            "{0}: composite_id_template still contains unexpanded placeholder(s) {1} "
            "after substitution. Known placeholders: {2}.".format(
                label, sorted(set(leaked)), sorted(substitutions)))
    # Added LAST, and only after it is fully expanded: ref_callout_start
    # substitutes {composite_id} and would otherwise inherit a leak.
    substitutions["composite_id"] = composite_id

    # --- 4. patterns ------------------------------------------------------
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

    diagnostics: Mapping = linter_rules.get("diagnostic_patterns", {})
    diagnostic_counts: Mapping = linter_rules.get("diagnostic_required_group_counts", {})

    def build_diagnostic(name: str) -> re.Pattern:
        if name not in diagnostics:
            raise ConfigError(
                "{0}: diagnostic_patterns.{1} is not declared. Near-miss detection "
                "cannot be defaulted: without it a malformed token or a banned "
                "construct compiles to zero nodes with no diagnostic at all.".format(
                    label, name))
        return _compile(name, diagnostics[name], substitutions, (),
                        diagnostic_counts.get(name), label)

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

    # --- 5. assemble ------------------------------------------------------
    return Grammar(
        anchor_id=anchor_id,
        domain_alternation=domain_alt,
        kind_alternation=kind_alt,
        composite_id=composite_id,
        spec_anchor=build("spec_anchor"),
        heading=build("heading"),
        ref_callout_start=build("ref_callout_start"),
        blockquote_line=build("blockquote_line"),
        subblock_suffix=build("subblock_suffix"),
        status_directive=build("status_directive"),
        document_h1=build("document_h1"),
        split_document=build("split_document"),
        file_naming=naming_compiled,
        frontmatter_fence=build("frontmatter_fence"),
        loose_anchor_line=build_diagnostic("loose_anchor_line"),
        loose_ref_line=build_diagnostic("loose_ref_line"),
        loose_admonition_open=build_diagnostic("loose_admonition_open"),
        loose_table_row=build_diagnostic("loose_table_row"),
        document_roles=document_roles,
        status_values=status_values,
        file_naming_roles=naming_roles,
        file_naming_locales=naming_locales,
        file_naming_severity=severity,
        file_naming_source=file_naming.get("diagnostic_source", "file-naming-convention"),
        payload_lock=linter_rules.get("payload_lock", {}),
        unindexed_policy=unindexed,
        dependent_lifecycle=lifecycle,
        callout_kind_aliases=kinds["aliases"],
        callout_suffix_letters=kinds["suffix_letters"],
        composite_separator=kinds["separator"],
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

    dependent_roles is returned unvalidated against any other list HERE, on
    purpose: it is cross-checked twice downstream, against
    file_naming.roles by assert_dependent_roles_producible() and against
    ui_projection.yaml by taxonomy.assert_callout_roles_agree(). Both need a
    finished object this function runs too early to have.
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


def assert_callout_tokens_producible(grammar: Grammar) -> None:
    """Cross-check: every authored token must resolve to a role the lifecycle
    engine declares.

    validate_callout_kinds() already enforces this at compile time, so this
    exists for the same reason assert_dependent_roles_producible() does --
    it is cheap, it runs against the finished object, and it catches a
    Grammar assembled by anything other than compile_grammar() (a test
    fixture, a future loader) before that Grammar reaches a corpus walk.
    """
    declared = set(grammar.dependent_lifecycle["dependent_roles"])
    unknown = sorted(set(grammar.callout_kind_aliases.values()) - declared)
    if unknown:
        raise ConfigError(
            "callout_kinds.aliases map to role(s) {0} that "
            "dependent_lifecycle.dependent_roles does not declare (known: {1}).".format(
                unknown, sorted(declared)))
