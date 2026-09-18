"""
lte/validators/draft.py -- LAYER 2 VALIDATOR. Pre-ingest checks for ONE
draft file against the corpus it is about to join.

Extracted from the legacy ingest.py so lte/cli/ingest.py only routes.
Returns a DraftReport; never prints, never exits, never writes.

PATHS ARE INTERPRETED ONLY BY THE CORPUS LAYOUT (config.layout): whether a
target is a corpus file, which partition and locale it belongs to, and where
a staged draft lands. This module splits no path and builds no suffix.

HARD FAILURES (block ingest): target not a corpus file, unclosed code fence,
duplicate anchor, misplaced anchor domain, orphan reference, public->private
reference, dependent-lifecycle front matter, banned constructs (admonitions,
tables), and a zero-block file in a partition whose unindexed policy is
"fail".

DIAGNOSTIC PATTERNS COME FROM THE GRAMMAR, not from a second compile. This
module used to build its own DiagnosticPatterns from the same
`diagnostic_patterns` block lte/engine/grammar.py already compiles onto the
Grammar. Two compilations of one config block is not just wasted work: they
are two places a future pattern can be added to, and the pre-ingest gate
silently disagreeing with the corpus linter about what counts as a banned
construct is the exact failure a draft gate exists to prevent.

WARNINGS (never block): file-naming convention, and anchor/ref lines that
look intended but do not match the grammar (grammar.loose_anchor_line /
grammar.loose_ref_line).
These are heuristics by design; the real grammar is the only arbiter of what
is valid.

BANNED CONSTRUCTS ARE ERRORS, NOT WARNINGS. Admonition openers and Markdown
table rows are not heuristics -- the grammar definitely will not accept
them, so a draft carrying one ingests clean and compiles empty. That is the
one class of finding here where being lenient loses content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from lte.engine import ast_blocks, frontmatter, integrity, state_machine
from lte.engine.grammar import ConfigError, Grammar
from lte.engine.layout import normalize_rel as layout_normalize

FENCE_MARKER = "```"
NON_BLOCKING_SEVERITIES = frozenset({"info", "warn", "warning"})


@dataclass(frozen=True)
class DraftReport:
    target: str
    errors: tuple
    warnings: tuple
    node_count: int
    callout_count: int

    @property
    def ok(self) -> bool:
        return not self.errors


# ------------------------------------------------------------ small helpers

def target_from_draft_path(layout, draft_path: str) -> str | None:
    """A draft under the layout's staging directory -> its corpus path."""
    return layout.staging_target(str(draft_path))


def _diag(diag) -> tuple:
    if isinstance(diag, Mapping):
        return (str(diag.get("severity", "fail")).lower(), str(diag.get("message", diag)),
                diag.get("line"))
    return (str(getattr(diag, "severity", "fail")).lower(),
            str(getattr(diag, "message", diag)), getattr(diag, "line", None))


def _where(obj) -> str:
    if isinstance(obj, tuple) and obj:
        obj = obj[0]
    source = getattr(obj, "source_file", "?")
    line = getattr(obj, "line_no", None)
    return "%s:%s" % (source, line) if line else str(source)


def _label(obj) -> str:
    if isinstance(obj, tuple) and obj:
        obj = obj[0]
    if hasattr(obj, "spec_id"):
        return "^" + obj.spec_id
    if hasattr(obj, "target_id"):
        # The AUTHORED token, falling back to the internal role. The old
        # default here was the literal "ref", which would mislabel every
        # dependent block once the alias table stopped containing it.
        token = getattr(obj, "token", "") or getattr(obj, "kind", "?")
        return "[!%s-%s]" % (token, obj.target_id)
    return str(obj)


def _outside_fences(text: str):
    """(line_no, line) pairs, skipping fenced code blocks."""
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(FENCE_MARKER):
            inside = not inside
            continue
        if not inside:
            yield number, line


# ------------------------------------------------------------------ checks

def check_target_path(layout, grammar: Grammar, target: str) -> tuple:
    """Errors when `target` is not a corpus file under the layout; naming warnings."""
    errors, warnings = [], []
    try:
        normalized = layout_normalize(target)
    except ValueError as exc:
        return ["%s: %s" % (target, exc)], warnings
    if normalized != target:
        return ["%s: target must be a normalized repo-relative path (%s)" % (target, normalized)], warnings
    if layout.is_ignored(target):
        return ["%s: target matches a layout ignore rule and would never be compiled" % target], warnings
    spec = layout.partition_of_path(target)
    if spec is None:
        errors.append("%s: not inside any partition directory (known: %s)"
                      % (target, ", ".join(layout.partition_prefix(p.id) or "." for p in layout.partitions)))
        return errors, warnings
    info = layout.classify(target)
    if info is None:
        suffixes = ", ".join("*" + x for x in layout.listing_suffixes())
        errors.append("%s: filename does not follow the corpus convention (%s)" % (target, suffixes))
        return errors, warnings
    for diag in integrity.check_file_naming(layout, grammar, target):
        severity, message, _ = _diag(diag)
        bucket = warnings if severity in NON_BLOCKING_SEVERITIES else errors
        bucket.append("%s: %s" % (target, message))
    return errors, warnings


def check_fence_parity(text: str) -> list:
    last_open, inside = None, False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(FENCE_MARKER):
            inside = not inside
            last_open = number if inside else last_open
    if inside:
        return [(last_open, "code fence opened here is never closed")]
    return []


def check_likely_typos(grammar: Grammar, text: str) -> list:
    """Lines that look like a token but match no compiled pattern.

    The 4-space dedent is gone with Grammar 2: nothing indents a token any
    more, so a line is tested exactly as authored. The two admonition
    fallbacks (admonition_trailing_anchor, admonition_ref_line) are gone for
    the harder reason -- those attributes no longer exist on Grammar, so this
    function raised AttributeError on the first anchor-shaped line it saw.
    """
    found = []
    for number, line in _outside_fences(text):
        anchor = grammar.loose_anchor_line.match(line)
        if anchor and not grammar.spec_anchor.match(line.strip()):
            found.append((number, "'^%s' looks like an anchor but does not match the grammar "
                                  "(domain must be one of the configured partition domains)"
                          % anchor.group(1)))
        ref = grammar.loose_ref_line.match(line)
        if ref and not grammar.ref_callout_start.match(line):
            found.append((number, "'[!%s]' looks like a dependent block but does not match "
                                  "[!<kind>-<anchor-id>] for any configured kind (%s)"
                          % (ref.group(1), ", ".join(sorted(grammar.callout_kind_aliases)))))
    return found


def check_banned_constructs(grammar: Grammar, text: str) -> list:
    """Admonition openers and Markdown table rows -- pure-text violations.

    REPLACES check_truncated_admonitions(), which read six Grammar
    attributes that no longer exist and whose whole purpose was warning that
    a `note` block would not compile to a node. No admonition compiles to
    anything now, so the warning is obsolete and the construct is banned.

    A HARD ERROR HERE, not a warning, matching lte/validators/corpus.py's
    Check 6. Everything else this module warns about is a heuristic that the
    real grammar may still accept; this one is the opposite -- the grammar
    definitely will not accept it, and the file would ingest clean and
    compile empty.

    Fence-aware, unlike the corpus linter's line-based version: a draft is
    one file a human is actively editing, and code samples showing the old
    syntax are exactly what someone writes while migrating.
    """
    found = []
    for number, line in _outside_fences(text):
        if grammar.loose_admonition_open.match(line):
            found.append((number, "admonition blocks are not parsed and compile to nothing; "
                                  "use a heading with a bare ^anchor, or a top-level "
                                  "'>' dependent block"))
        elif grammar.loose_table_row.match(line):
            found.append((number, "Markdown tables are banned in node bodies; express "
                                  "tabular rules as separately anchored sub-blocks"))
    return found


# ------------------------------------------------------------- aggregate

def _file_checks(config, rel_path: str, text: str) -> tuple:
    """Checks that need only the file itself. Returns (errors, warnings, nodes, callouts)."""
    grammar, layout = config.grammar, config.layout
    errors, warnings = check_target_path(layout, grammar, rel_path)
    errors += ["%s:%d: %s" % (rel_path, n, m) for n, m in check_fence_parity(text)]
    warnings += ["%s:%d: %s" % (rel_path, n, m) for n, m in check_likely_typos(grammar, text)]
    errors += ["%s:%d: BANNED CONSTRUCT -- %s" % (rel_path, n, m)
               for n, m in check_banned_constructs(grammar, text)]

    nodes, callouts = ast_blocks.parse_text(grammar, rel_path, text)

    if state_machine.is_dependent_document(grammar, rel_path, layout=layout):
        fm = frontmatter.parse_text(text, grammar.frontmatter_fence)
        for diag in state_machine.check_dependent_lifecycle(grammar, rel_path, fm, layout=layout):
            severity, message, line = _diag(diag)
            entry = "%s:%s: %s" % (rel_path, line or 1, message)
            (warnings if severity in NON_BLOCKING_SEVERITIES else errors).append(entry)

    info = layout.classify(rel_path)
    if not nodes and not callouts and info is not None:
        severity = integrity.unindexed_severity(grammar, info.partition)
        message = ("%s: no AST blocks found; the document compiles as ast_non_compliant "
                   "(unindexed policy for %s: %s)" % (rel_path, info.partition, severity))
        (errors if severity == "fail" else warnings).append(message)
    return errors, warnings, list(nodes), list(callouts)


def validate_batch(config, candidates: Mapping,
                   corpus_documents: Iterable) -> DraftReport:
    """
    Validates every changed file at once against the corpus it joins.

    `candidates` is {rel_path: text}. `corpus_documents` is the (rel_path,
    text) set at the branch tip; every path in `candidates` is excluded from
    it, because the candidate replaces it. Corpus-wide checks run PER
    LOCALE -- the vi and en files of one document legitimately share anchor
    ids -- and only findings that involve a changed file are reported, so a
    pre-existing problem elsewhere never blocks an unrelated change. Two new
    files that reference each other validate against each other.
    """
    taxonomy, grammar, layout = config.taxonomy, config.grammar, config.layout
    changed = frozenset(candidates)
    errors, warnings = [], []
    node_count = callout_count = 0
    parsed = {}
    for rel_path in sorted(candidates):
        e, w, nodes, callouts = _file_checks(config, rel_path, candidates[rel_path])
        errors += e
        warnings += w
        parsed[rel_path] = (nodes, callouts)
        node_count += len(nodes)
        callout_count += len(callouts)

    corpus_by_locale = {}
    for rel_path, text in corpus_documents:
        if rel_path in changed or text is None:
            continue
        locale = layout.locale_of(rel_path)
        if locale is not None:
            corpus_by_locale.setdefault(locale, []).append((rel_path, text))

    for locale in sorted({layout.locale_of(r) for r in changed} - {None}):
        draft_nodes, draft_callouts = [], []
        for rel_path in sorted(changed):
            if layout.locale_of(rel_path) == locale:
                draft_nodes += parsed[rel_path][0]
                draft_callouts += parsed[rel_path][1]
        corpus_nodes = []
        for rel_path, text in sorted(corpus_by_locale.get(locale, [])):
            corpus_nodes += ast_blocks.parse_text(grammar, rel_path, text)[0]
        all_nodes = corpus_nodes + draft_nodes

        for anchor, group in sorted(integrity.find_duplicate_spec_ids(all_nodes).items()):
            if any(n.source_file in changed for n in group):
                errors.append("duplicate anchor ^%s in the '%s' corpus: %s"
                              % (anchor, locale, ", ".join(_where(n) for n in group)))
        for item in integrity.find_misplaced_anchors(layout, taxonomy, draft_nodes):
            errors.append("%s: anchor %s domain does not match its partition directory"
                          % (_where(item), _label(item)))
        for item in integrity.find_orphan_refs(all_nodes, draft_callouts):
            errors.append("%s: %s points to no anchor in the '%s' corpus"
                          % (_where(item), _label(item), locale))
        for item in integrity.find_cross_boundary_refs(layout, taxonomy, all_nodes, draft_callouts):
            errors.append("%s: %s cites an anchor in a higher-visibility scope (boundary violation)"
                          % (_where(item), _label(item)))

    target = next(iter(candidates)) if len(candidates) == 1 else "%d file(s)" % len(candidates)
    return DraftReport(target=target, errors=tuple(errors), warnings=tuple(warnings),
                       node_count=node_count, callout_count=callout_count)


def validate_draft(config, draft_text: str, target: str,
                   corpus_documents: Iterable) -> DraftReport:
    """
    `config` is an lte.io.config_reader.EngineConfig. `corpus_documents` is
    the (rel_path, text) set the draft joins -- typically the CAS branch
    tip. The document currently stored at `target` is excluded: the draft
    replaces it.
    """
    return validate_batch(config, {target: draft_text}, corpus_documents)
