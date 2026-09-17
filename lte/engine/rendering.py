# -*- coding: utf-8 -*-
"""lte/engine/rendering.py -- LEAF MODULE. Markdown -> HTML and summary text.

Extracted from parse_graph.render() and parse_graph.plain_text_summary().

THIS EXTRACTION IS WHAT BREAKS THE git_cas -> parse_graph EDGE. git_cas
imported exactly three symbols from parse_graph -- render,
plain_text_summary, partitions_for_scope -- and that single import made one
orchestrator depend on another, producing the chain
compile_from_git -> git_cas -> parse_graph -> graph_lib. Two of those three
land here; partitions_for_scope lands in taxonomy. With all three demoted to
layer-1 leaves, the cycle graph_lib's GRAPH_SCHEMA_VERSION comment was
routing around cannot form, and that workaround can be deleted rather than
maintained.

THE GLOBAL MUTABLE STATE IS ALSO FIXED HERE. parse_graph held one
module-level instance and reset it per call:

    _MD = md.Markdown(extensions=["extra", "sane_lists"])
    def render(text):
        _MD.reset()
        return _MD.convert(text)

That is shared mutable state in a module otherwise treated as pure. It works
only because every caller is single-threaded and disciplined about the
reset; a missed reset leaks footnote and reference definitions from one
node's body into the next one's rendered output, which is a silent
content-corruption bug, not a crash. A Renderer instance owns its converter,
so there is no shared instance to forget to reset and no ordering
dependency between callers.
"""
from __future__ import annotations

import html as html_lib
import re

import markdown as md

SUMMARY_MAX_LEN = 280

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# Collapses ' ,' / ' .' left behind after tag stripping, e.g.
# '<em>word</em> , next' -> 'word, next'.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?)\]])")

DEFAULT_EXTENSIONS = ("extra", "sane_lists")


class Renderer:
    """Owns one markdown.Markdown converter.

    Construct one per compile and inject it -- lte/cli/compile.py already
    passes `render=` into graph_builder as a collaborator. Not thread-safe,
    and deliberately not documented as such: markdown.Markdown is itself
    stateful, so the honest answer is one Renderer per thread, not a lock
    around a shared one.
    """

    __slots__ = ("_md",)

    def __init__(self, extensions=DEFAULT_EXTENSIONS):
        self._md = md.Markdown(extensions=list(extensions))

    def render(self, text: str) -> str:
        """Markdown -> HTML.

        reset() still runs, for the reason it always did: markdown.Markdown
        accumulates footnote and link-reference definitions across convert()
        calls. What changed is that forgetting it can now only corrupt THIS
        Renderer's own output, not every caller's.
        """
        self._md.reset()
        return self._md.convert(text)

    def __call__(self, text: str) -> str:
        """So a Renderer can be passed anywhere a plain `render` callable is
        expected, including graph_builder's `render=` parameter."""
        return self.render(text)


def build_renderer(extensions=DEFAULT_EXTENSIONS) -> Renderer:
    """Factory. Exists so call sites name a function rather than a class,
    matching build_taxonomy() / compile_grammar()."""
    return Renderer(extensions)


def plain_text_summary(html: str, max_len: int = SUMMARY_MAX_LEN) -> str:
    """Strips HTML to a single-line plain-text excerpt, truncated on a word
    boundary with an ellipsis.

    Stateless and instance-free -- it needs no converter, so it stays a
    module-level function.

    Entities are unescaped AFTER tag stripping, never before: unescaping
    first would turn a literal `&lt;script&gt;` in authored prose into
    `<script>`, which _TAG_RE would then strip as though it were real markup,
    silently deleting text the author wrote.
    """
    text = _TAG_RE.sub(" ", html)
    text = html_lib.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "\u2026"
