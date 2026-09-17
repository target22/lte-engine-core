# -*- coding: utf-8 -*-
"""lte/engine/ast_blocks.py -- LEAF MODULE. Pure AST block extraction.

parse_text() is git_cas.parse_text() promoted to canonical. That function
was already described in its own docstring as a "line-for-line port of
graph_lib.parse_file()'s body, with its one disk read and its one
disk-relative computation replaced by parameters" -- which is exactly the
shape a leaf module needs, so the port becomes the implementation and
parse_file() becomes a thin wrapper in lte/io/corpus_reader.py. The
duplication is resolved by deletion, not by a third copy.

title_from_text() and prologue_from_text() are new text cores extracted
from graph_lib.extract_document_title() and extract_prologue(), whose only
impure line in each case was a leading `path.read_text(encoding="utf-8")`.

GRAMMAR IS INJECTED, NEVER IMPORTED. Every regex arrives as a Grammar
value. This module therefore has no import-time config dependency and no
module-level mutable state.

KNOWN LIMITATION, carried forward deliberately: this is a line-scanner, not
a fence-aware parser. Do not put a bare-anchor-shaped, heading-shaped, or
callout-opener-shaped line inside a ``` code fence within a block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lte.engine.grammar import Grammar

# Fallback only. The live fence comes from grammar.frontmatter_fence, which
# config/linter_rules.json declares alongside every other pattern. This
# literal exists for a caller holding no Grammar and must never diverge
# from the config -- prologue_from_text() below prefers the injected one.
FALLBACK_FRONTMATTER_FENCE_RE = re.compile(r"^---\s*$")


@dataclass
class SpecNode:
    """One Tier-4 anchored rule statement."""

    spec_id: str
    title: str
    content: str
    source_file: str
    line_no: int
    # Axiom IV, per-contract status. Raw, UNVALIDATED capture of an optional
    # `{status: ...}` line inside a `note` block (Grammar 2 only; a Grammar-1
    # heading/anchor node never sets this). None means "no override" -- NOT
    # "status is unset": an unoverridden contract still inherits its
    # document's front-matter status, it just has no NODE-level one.
    # Defaulted so a build-cache entry written before this field existed
    # still deserializes via SpecNode(**n).
    status_override: str | None = None

    @property
    def domain(self) -> str:
        """Pure string split; needs no taxonomy. Resolving this to a
        partition or scope DOES need one -- see Taxonomy.partition_of() /
        scope_of(), which integrity.py calls with an injected taxonomy
        rather than this class reaching upward for one."""
        return self.spec_id.split("-", 1)[0]


@dataclass
class RefCallout:
    """One Tier-4 inbound reference: a Debate ('ref') or Checklist ('ops')."""

    kind: Literal["ref", "ops"]
    target_id: str
    title: str
    content: str
    source_file: str
    line_no: int

    @property
    def source_scope(self) -> str | None:
        """Scope inferred from the callout's OWN file path
        (docs/<scope>/<partition>/...), independent of what it targets.

        Pure path-string parsing, no filesystem access. None if source_file
        doesn't look like a docs/<scope>/... path at all -- defensive; every
        real corpus file does.
        """
        parts = Path(self.source_file).parts
        if len(parts) > 1 and parts[0] == "docs" and parts[1] in ("public", "private"):
            return parts[1]
        return None


def collect_admonition_body(grammar: Grammar, lines: list, start: int) -> tuple:
    """From index `start` (the line right after a matched admonition opener),
    collects every following line that is blank or indented >=4 spaces,
    dedenting the indented ones by exactly 4 spaces. Stops at the first
    non-blank, under-indented line or EOF.

    Trims leading and trailing blank lines: mkdocs tolerates either around
    the real content, and keeping them would add noise to the stored body.

    Returns (dedented_body_lines, next_index_to_resume_at).
    """
    body: list = []
    j = start
    n = len(lines)
    while j < n:
        line = lines[j]
        if grammar.admonition_blank.match(line):
            body.append("")
            j += 1
            continue
        indent_match = grammar.admonition_indent.match(line)
        if not indent_match:
            break
        body.append(indent_match.group("rest"))
        j += 1
    while body and body[0] == "":
        body.pop(0)
    while body and body[-1] == "":
        body.pop()
    return body, j


def parse_admonition_block(grammar: Grammar, callout_kind_by_type, block_type: str,
                           title: str, open_line_no: int, body: list,
                           rel_path: str) -> tuple:
    """Turns one already-collected, already-dedented admonition body into
    either a SpecNode (block_type == 'note') or a RefCallout (block_type in
    ('warning', 'tip')) -- or NEITHER.

    Returning (None, None) is a valid, silently-skipped state, never a raised
    error: this is a line-scanner applying a grammar, not a schema validator.
    lte/validators/corpus.py is the layer that turns "no anchor found" into a
    build failure if that is ever wanted, not this function.

    `callout_kind_by_type` is Taxonomy.admonition_callout_kind, passed in
    rather than imported so this module stays free of any taxonomy
    dependency.
    """
    if block_type == "note":
        anchor_id = None
        anchor_idx = None
        for idx in range(len(body) - 1, -1, -1):
            stripped = body[idx].strip()
            standalone = grammar.spec_anchor.match(stripped)
            if standalone:
                anchor_id = standalone.group("id")
                anchor_idx = idx
                body[idx] = None  # the whole line WAS the anchor
                break
            trailing = grammar.admonition_trailing_anchor.match(body[idx])
            if trailing:
                anchor_id = trailing.group("id")
                anchor_idx = idx
                body[idx] = trailing.group("pre")
                break
        if anchor_id is None:
            return None, None  # unanchored note: valid prose, silently unindexed

        # Optional per-contract status override: a standalone
        # `{status: ...}` line anywhere else in the body. Scanned and
        # stripped the same way the anchor line is (whole line consumed, not
        # left behind as rendered prose), but as an INDEPENDENT pass -- it is
        # not positionally tied to the anchor the way Grammar 1's trailing
        # style is.
        status_override = None
        for idx, line in enumerate(body):
            if line is None or idx == anchor_idx:
                continue
            status_match = grammar.admonition_status.match(line.strip())
            if status_match:
                status_override = status_match.group("status")
                body[idx] = None
                break

        content = "\n".join(l for l in body if l is not None).strip()
        return (
            SpecNode(
                spec_id=anchor_id, title=title, content=content,
                source_file=rel_path, line_no=open_line_no,
                status_override=status_override,
            ),
            None,
        )

    # 'warning' -> ref/debate callout, 'tip' -> ops/checklist callout. Both
    # use the SAME `[!ref-<id>]` token -- the admonition type, not the token
    # text, decides kind.
    ref_idx = None
    target_id = None
    ref_title = ""
    for idx, line in enumerate(body):
        m = grammar.admonition_ref_line.match(line)
        if m:
            ref_idx = idx
            target_id = m.group("id")
            ref_title = m.group("title").strip()
            break
    if target_id is None:
        # No [!ref-<id>] link: valid but unlinked, silently skipped. No
        # positional or proximity guessing at an intended target -- same
        # no-heuristics stance as unanchored heading sections.
        return None, None

    remainder = [line for idx, line in enumerate(body) if idx != ref_idx]
    content = "\n".join(remainder).strip()
    # A KeyError here means an admonition type reached the grammar's
    # admonition_open pattern without a matching ui_projection entry --
    # exactly when this should fail loudly. `note` never reaches this line.
    kind = callout_kind_by_type[block_type]
    return None, RefCallout(
        kind=kind, target_id=target_id, title=ref_title, content=content,
        source_file=rel_path, line_no=open_line_no,
    )


def parse_text(grammar: Grammar, callout_kind_by_type, rel_path: str,
               text: str) -> tuple:
    """THE AST parser. Takes text and a repo-relative path label, never a Path.

    Dispatches per-line across both grammars:
      * a Grammar-2 admonition opener consumes its whole indented block
      * a Grammar-1 heading arms a buffer that a later standalone anchor line
        closes into a SpecNode
      * a Grammar-1 top-level `> [!ref-...]` / `> [!ops-...]` blockquote
        becomes a RefCallout

    Returns (nodes, callouts).
    """
    lines = text.splitlines()
    nodes: list = []
    callouts: list = []

    current_heading = None
    current_heading_line = 0
    buffer: list = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        admonition_match = grammar.admonition_open.match(line)
        if admonition_match:
            open_line_no = i + 1
            body, next_i = collect_admonition_body(grammar, lines, i + 1)
            node, callout = parse_admonition_block(
                grammar, callout_kind_by_type,
                admonition_match.group("type"), admonition_match.group("title"),
                open_line_no, body, rel_path,
            )
            if node is not None:
                nodes.append(node)
            if callout is not None:
                callouts.append(callout)
            i = next_i
            buffer = []
            current_heading = None
            continue

        heading_match = grammar.heading.match(line)
        if heading_match:
            current_heading = heading_match.group(2)
            current_heading_line = i + 1
            buffer = []
            i += 1
            continue

        anchor_match = grammar.spec_anchor.match(line.strip())
        if anchor_match and current_heading is not None:
            spec_id = anchor_match.group("id")
            content = "\n".join(buffer).strip()
            nodes.append(SpecNode(
                spec_id=spec_id, title=current_heading, content=content,
                source_file=rel_path, line_no=current_heading_line,
            ))
            buffer = []
            i += 1
            continue

        callout_match = grammar.ref_callout_start.match(line)
        if callout_match:
            kind = callout_match.group("kind")
            target_id = callout_match.group("id")
            title = callout_match.group("title").strip()
            callout_line_no = i + 1
            body_lines: list = []
            j = i + 1
            while j < n:
                bq_match = grammar.blockquote_line.match(lines[j])
                if not bq_match:
                    break
                body_lines.append(bq_match.group(1))
                j += 1
            callouts.append(RefCallout(
                kind=kind, target_id=target_id, title=title,
                content="\n".join(body_lines).strip(),
                source_file=rel_path, line_no=callout_line_no,
            ))
            i = j
            buffer = []
            continue

        if current_heading is not None:
            buffer.append(line)
        i += 1

    return nodes, callouts


def title_from_text(grammar: Grammar, text: str, fallback: str) -> str:
    """The file's first true H1 ('# ...', not '## ...') heading text -- the
    Tier-3 document title.

    Text core of graph_lib.extract_document_title(), whose only impure line
    was the leading read. `fallback` replaces that function's internal
    document_id_of(path) call: computing a document id needs a Path, and
    reaching for one here would reintroduce the filesystem coupling this
    extraction exists to remove. The caller (lte/io/corpus_reader.py) already
    has the Path and passes the id in.

    Falls back rather than raising: every real document has an H1, and a
    malformed file should degrade to a readable label, not crash the whole
    graph build.
    """
    for line in text.splitlines():
        match = grammar.document_h1.match(line)
        if match:
            return match.group(1)
    return fallback


def prologue_from_text(grammar: Grammar, text: str) -> str:
    """Raw Markdown of whatever sits BEFORE the first recognized AST block --
    a Grammar-2 admonition opener or a Grammar-1 top-level ref/ops callout.

    A leading YAML front matter fence and the document's own true H1 title
    line are stripped first, since both are surfaced elsewhere (front matter
    is not rendered; the H1 is already document.title).

    This exists so a document that legitimately parses to zero nodes -- a
    draft with only prose, or a file whose blocks all fail to anchor -- still
    has something for a human to read instead of a blank pane. If the file
    never opens a recognized block, the prologue is the entire remaining
    document.

    Deliberately independent of parse_text(): it does not participate in the
    SHA-256 parse cache, so it can never desync from nodes/callouts. It is
    also NOT anchor-aware and makes no validity judgement -- it delimits
    "before the first block token", nothing more.
    """
    lines = text.splitlines()
    i = 0
    n = len(lines)

    fence = getattr(grammar, "frontmatter_fence", FALLBACK_FRONTMATTER_FENCE_RE)
    if n and fence.match(lines[0]):
        j = 1
        while j < n and not fence.match(lines[j]):
            j += 1
        i = j + 1 if j < n else n  # skip the closing '---' fence too

    while i < n and lines[i].strip() == "":
        i += 1
    if i < n and grammar.document_h1.match(lines[i]):
        i += 1

    collected: list = []
    while i < n:
        line = lines[i]
        if grammar.admonition_open.match(line) or grammar.ref_callout_start.match(line):
            break
        collected.append(line)
        i += 1

    while collected and collected[0].strip() == "":
        collected.pop(0)
    while collected and collected[-1].strip() == "":
        collected.pop()
    return "\n".join(collected)


def document_id_from_name(grammar: Grammar, name: str) -> str:
    """'01-price-bounds.contract.en.md' -> '01-price-bounds'.

    Text core of graph_lib.document_id_of(), taking a bare filename instead
    of a Path so it stays free of filesystem types.

    THE SPLIT-FILE LINE IS LOAD-BEARING: <slug>.contract/.debate/.ops all
    resolve to the SAME document_id. That is what lets graph_builder regroup
    up-to-three sibling files back into one Tier-3 document, by grouping on
    this return value exactly as it grouped on document_id before split-file
    authoring existed.

    This is the FILENAME stem only, not the intermediate tractate
    subdirectory -- a tractate subfolder is a physical grouping convenience,
    not part of document identity.
    """
    split_match = grammar.split_document.match(name)
    if split_match:
        return split_match.group("slug")
    for locale in grammar.file_naming_locales:
        suffix = ".{0}.md".format(locale)
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name[:-3] if name.endswith(".md") else name


def document_role_from_name(grammar: Grammar, name: str) -> str | None:
    """'01-x.contract.en.md' -> 'contract'. '01-x.en.md' -> None.

    A None role is a complete, self-contained, single-file document. It is
    never "missing" a role; it simply predates the convention and is treated
    as its own primary file.
    """
    match = grammar.split_document.match(name)
    return match.group("role") if match else None
