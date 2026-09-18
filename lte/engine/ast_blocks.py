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
    """One Tier-4 dependent (Foreign Key) block.

    `kind` is the INTERNAL dependent role -- the closed set
    dependent_lifecycle.dependent_roles declares, which graph.json, the
    frontend and the state machine are all keyed on. `token` is the alias the
    author actually typed, kept only for diagnostics and round-tripping.
    Renaming an alias in config therefore never changes `kind` or `node_id`.

    NOT Literal[...] any more: the legal values come from config at load
    time, so an enumeration here would be a second, stale source of truth.
    """

    kind: str
    target_id: str
    title: str
    content: str
    source_file: str
    line_no: int
    # Composite Primary Key: <parent_anchor><sep><role>-<letter><ordinal>,
    # e.g. spec-lte-01-002#debate-d01. Minted by parse_text from the
    # config's separator and suffix letter -- never spelled in this module.
    node_id: str = ""
    # The authored alias ('ref', 'cite', ...). Defaulted so a build-cache
    # entry written before this field existed still deserializes.
    token: str = ""

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


def parse_text(grammar: Grammar, rel_path: str, text: str) -> tuple:
    """THE AST parser. Takes text and a repo-relative path label, never a Path.

    Two structural entities, one dispatch:
      * PRIMARY KEY  -- a heading, free prose, terminated by a line holding
        only `^<anchor>`. Emits a SpecNode.
      * FOREIGN KEY  -- a top-level blockquote opened by `> [!<token>-<parent>]`.
        Emits a RefCallout whose `kind` is the internal role the config maps
        `<token>` to, and whose `node_id` is a composite child key.

    No token string is written here. The alias set, the role it resolves to,
    the composite separator and the suffix letter all arrive on `grammar`.

    Returns (nodes, callouts).
    """
    lines = text.splitlines()
    nodes: list = []
    callouts: list = []

    current_heading = None
    current_heading_line = 0
    buffer: list = []
    # (parent_anchor, role) -> count so far, in document order. Deterministic:
    # byte-identical input yields byte-identical composite keys.
    ordinals: dict = {}

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

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
            # Optional per-contract status override: one standalone
            # `{status: ...}` line anywhere in the block. Captured RAW and
            # validated by the caller against status_values, exactly as the
            # document-level front-matter `status` is.
            #
            # CARRIED OVER FROM GRAMMAR 2, NOT NEW. parse_admonition_block()
            # scanned a note block's body for this line; deleting that
            # function took the feature with it, leaving `status_directive`
            # compiled in config and read by nothing, while
            # graph_builder.effective_node_status() and
            # payload_lock.effective_statuses() both kept asking for an
            # override that could no longer exist.
            status_override, kept = None, []
            for buffered in buffer:
                directive = (grammar.status_directive.match(buffered.strip())
                             if status_override is None else None)
                if directive:
                    status_override = directive.group("status")
                    continue  # whole line consumed, never rendered as prose
                kept.append(buffered)
            content = "\n".join(kept).strip()
            nodes.append(SpecNode(
                spec_id=spec_id, title=current_heading, content=content,
                source_file=rel_path, line_no=current_heading_line,
                status_override=status_override,
            ))
            buffer = []
            i += 1
            continue

        callout_match = grammar.ref_callout_start.match(line)
        if callout_match:
            token = callout_match.group("kind")
            # Alias -> internal role. The pattern only admits configured
            # tokens, so this lookup cannot miss; .get would hide a config
            # drift between the compiled alternation and the alias table.
            kind = grammar.callout_kind_aliases[token]
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
            seq = ordinals[(target_id, kind)] = ordinals.get((target_id, kind), 0) + 1
            callouts.append(RefCallout(
                kind=kind, target_id=target_id, title=title,
                content="\n".join(body_lines).strip(),
                source_file=rel_path, line_no=callout_line_no,
                node_id="{0}{1}{2}-{3}{4:02d}".format(
                    target_id, grammar.composite_separator, kind,
                    grammar.callout_suffix_letters[kind], seq),
                token=token,
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
    """Raw Markdown of whatever sits BEFORE the first top-level dependent
    blockquote.

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
        if grammar.ref_callout_start.match(line):
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
