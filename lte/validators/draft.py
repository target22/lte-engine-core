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
reference, dependent-lifecycle front matter, and a zero-block file in a
partition whose unindexed policy is "fail".

WARNINGS (never block): file-naming convention, anchor/ref lines that look
intended but do not match the grammar (config: diagnostic_patterns), and
`note` admonitions that will not compile to a node. These are heuristics by
design; the real grammar is the only arbiter of what is valid.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class DiagnosticPatterns:
    loose_anchor_line: re.Pattern
    loose_ref_line: re.Pattern


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
        return "[!%s-%s]" % (getattr(obj, "kind", "ref"), obj.target_id)
    return str(obj)


def compile_diagnostic_patterns(linter_rules: Mapping) -> DiagnosticPatterns:
    """Compiles the quarantined `diagnostic_patterns` block, fail-fast."""
    block = linter_rules.get("diagnostic_patterns")
    counts = linter_rules.get("diagnostic_required_group_counts") or {}
    if not isinstance(block, Mapping):
        raise ConfigError("linter_rules.json: missing diagnostic_patterns block")
    compiled = {}
    for name in ("loose_anchor_line", "loose_ref_line"):
        template = block.get(name)
        if not isinstance(template, str):
            raise ConfigError("linter_rules.json: diagnostic_patterns.%s missing" % name)
        try:
            pattern = re.compile(template)
        except re.error as exc:
            raise ConfigError("diagnostic_patterns.%s does not compile: %s" % (name, exc)) from exc
        expected = counts.get(name)
        if expected is not None and pattern.groups != expected:
            raise ConfigError("diagnostic_patterns.%s: expected %d group(s), found %d"
                              % (name, expected, pattern.groups))
        compiled[name] = pattern
    return DiagnosticPatterns(**compiled)


def find_linter_rules(raw_config: Mapping) -> Mapping:
    """Locates the parsed linter_rules document inside config_reader.read_raw()."""
    for value in raw_config.values():
        if isinstance(value, Mapping) and "diagnostic_patterns" in value:
            return value
    raise ConfigError("no parsed config document carries diagnostic_patterns")


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


def check_likely_typos(grammar: Grammar, patterns: DiagnosticPatterns, text: str) -> list:
    found = []
    for number, line in _outside_fences(text):
        body = line[4:] if line.startswith("    ") else line
        anchor = patterns.loose_anchor_line.match(line)
        if anchor and not (grammar.spec_anchor.match(body)
                           or grammar.admonition_trailing_anchor.match(body)):
            found.append((number, "'^%s' looks like an anchor but does not match the grammar "
                                  "(domain must be one of the configured partition domains)"
                          % anchor.group(1)))
        ref = patterns.loose_ref_line.match(line)
        if ref and not (grammar.ref_callout_start.match(body)
                        or grammar.admonition_ref_line.match(body)):
            found.append((number, "'[!%s]' looks like a reference but does not match "
                                  "[!ref-<anchor-id>]" % ref.group(1)))
    return found


def check_truncated_admonitions(grammar: Grammar, text: str) -> list:
    lines = text.splitlines()
    outside = {n for n, _ in _outside_fences(text)}
    found, i = [], 0
    while i < len(lines):
        opener = grammar.admonition_open.match(lines[i]) if (i + 1) in outside else None
        if not opener:
            i += 1
            continue
        j, has_anchor = i + 1, False
        while j < len(lines) and (grammar.admonition_blank.match(lines[j])
                                  or grammar.admonition_indent.match(lines[j])):
            indented = grammar.admonition_indent.match(lines[j])
            if indented and grammar.admonition_trailing_anchor.match(indented.group("rest")):
                has_anchor = True
            j += 1
        if opener.group("type") == "note" and not has_anchor:
            message = ('note block "%s" has no trailing ^anchor in its indented body and '
                       'will not compile to a contract node' % opener.group("title"))
            if j < len(lines):
                message += ("; the body ends at line %d (first non-blank line indented fewer "
                            "than 4 spaces)" % (j + 1))
            found.append((i + 1, message))
        i = max(j, i + 1)
    return found


# ------------------------------------------------------------- aggregate

def _file_checks(config, rel_path: str, text: str, patterns: DiagnosticPatterns) -> tuple:
    """Checks that need only the file itself. Returns (errors, warnings, nodes, callouts)."""
    grammar, layout = config.grammar, config.layout
    taxonomy = config.taxonomy
    errors, warnings = check_target_path(layout, grammar, rel_path)
    errors += ["%s:%d: %s" % (rel_path, n, m) for n, m in check_fence_parity(text)]
    warnings += ["%s:%d: %s" % (rel_path, n, m) for n, m in check_likely_typos(grammar, patterns, text)]
    warnings += ["%s:%d: %s" % (rel_path, n, m) for n, m in check_truncated_admonitions(grammar, text)]

    nodes, callouts = ast_blocks.parse_text(grammar, taxonomy.admonition_callout_kind, rel_path, text)

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


def validate_batch(config, candidates: Mapping, corpus_documents: Iterable,
                   patterns: DiagnosticPatterns) -> DraftReport:
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
    kinds = taxonomy.admonition_callout_kind
    changed = frozenset(candidates)
    errors, warnings = [], []
    node_count = callout_count = 0
    parsed = {}
    for rel_path in sorted(candidates):
        e, w, nodes, callouts = _file_checks(config, rel_path, candidates[rel_path], patterns)
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
            corpus_nodes += ast_blocks.parse_text(grammar, kinds, rel_path, text)[0]
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
                   corpus_documents: Iterable, patterns: DiagnosticPatterns) -> DraftReport:
    """
    `config` is an lte.io.config_reader.EngineConfig. `corpus_documents` is
    the (rel_path, text) set the draft joins -- typically the CAS branch
    tip. The document currently stored at `target` is excluded: the draft
    replaces it.
    """
    return validate_batch(config, {target: draft_text}, corpus_documents, patterns)
