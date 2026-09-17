# -*- coding: utf-8 -*-
"""lte/engine/frontmatter.py -- LEAF MODULE. Pure YAML front matter parsing
and metadata coercion.

Extracted from graph_lib.extract_frontmatter_from_text(),
normalize_status() and document_metadata_from_text(). Those three were
ALREADY the pure text cores -- graph_lib's extract_frontmatter() and
document_metadata_of() are one-line Path-taking wrappers over them, and
their own docstring records why the split exists:

    "git_cas.py previously carried its own copy of this function and of
     document_metadata_of() [...] Two implementations of one rule is how
     the `supersedes` field came to be emitted by the disk compiler and
     silently absent from the CAS compiler that actually runs in
     production."

So this extraction moves code that was already shaped correctly. The
wrappers go to lte/io/corpus_reader.py.

`yaml` is imported for safe_load over a STRING. It is a parser, not an I/O
library -- no file is opened here, which is why lte/validators/architecture
lists this module as pure and the gate passes.

MALFORMED CONTENT DEGRADES, IT DOES NOT RAISE. Every failure path below
returns {} rather than propagating. That is the deliberate inverse of how
lte/engine/grammar.py treats a malformed CONFIG file: a bad .md file is an
authoring mistake that must not take down the corpus, while a bad config
file is an operator mistake that must not be compiled around.
"""
from __future__ import annotations

import re

import yaml

# Fallback only. config/linter_rules.json declares `frontmatter_fence`, so
# the live pattern is grammar.frontmatter_fence; parse_text() accepts it via
# the optional `fence` parameter. This literal is the default for the many
# callers that have no Grammar in hand and must never diverge from config.
FALLBACK_FRONTMATTER_FENCE_RE = re.compile(r"^---\s*$")

# The document-level keys the compiler renders. A key outside this set is
# parsed but NOT carried into metadata_from_text()'s output -- see that
# function's docstring for the one place that distinction has bitten.
RENDERED_KEYS = ("status", "author", "date", "version", "tags",
                 "supersedes", "superseded_by")


def parse_text(text: str, fence=None) -> dict:
    """THE front matter parser. Takes text, not a Path.

    Returns {} for: no leading fence, an unterminated fence, invalid YAML,
    or a document whose front matter parses to a non-mapping (e.g. a bare
    list). Each of those is an authoring mistake in one file, not a reason
    to fail the corpus.
    """
    fence = fence or FALLBACK_FRONTMATTER_FENCE_RE
    lines = text.splitlines()
    if not lines or not fence.match(lines[0]):
        return {}
    end = None
    for i in range(1, len(lines)):
        if fence.match(lines[i]):
            end = i
            break
    if end is None:
        return {}
    raw = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_status(raw: object) -> str | None:
    """Case/whitespace-normalizes a front matter `status` for comparison
    against a Grammar's status_values.

    Returns None for a missing value or a non-string one (a YAML
    `status: 42` typo). NEVER guesses at the "closest" valid value for
    something unrecognized -- same no-fuzzy-matching rule as anchor and
    domain resolution.

    NORMALIZES ONLY; VALIDATION IS THE CALLER'S JOB. Whether the result is
    actually one of the declared status values is checked at the call site,
    which knows whether an unrecognized value should warn, fall back, or
    fail.
    """
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    return normalized or None


def metadata_from_text(text: str, fence=None) -> dict:
    """Coerces front matter into the seven document-level fields the
    compiler renders.

    THIS FUNCTION DROPS EVERY OTHER KEY, and that has already caused a real
    bug worth naming here. `archived_reason` -- required by the dependent
    lifecycle in lte/engine/state_machine.py -- is NOT in RENDERED_KEYS, so
    feeding this function's output to check_dependent_lifecycle() reports a
    correctly-tagged file as missing the very field it carries. Callers
    validating lifecycle front matter must use parse_text() above, which
    returns everything. The two functions look interchangeable and are not.
    """
    fm = parse_text(text, fence)

    tags_raw = fm.get("tags")
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [tags_raw.strip()]
    else:
        tags = []

    def _scalar(key: str) -> str | None:
        value = fm.get(key)
        if value is None:
            return None
        as_text = str(value).strip()
        return as_text or None

    return {
        "status": normalize_status(fm.get("status")),
        "author": _scalar("author"),
        "date": _scalar("date"),
        "version": _scalar("version"),
        "tags": tags,
        # Document lifecycle (Tier 3, not Tier 4). Both carry a document_id,
        # NOT an anchor id -- superseding is a document-level relation,
        # [!ref-...] is a node-level one. Different namespaces; do not
        # resolve these against anchor_index.
        #
        # NOTE THE COLLISION with the dependent lifecycle: on a
        # .debate/.ops/.evi file, `superseded_by` carries an ANCHOR id, not a
        # document_id. Which namespace applies is decided by the file's role
        # segment. state_machine.check_dependent_lifecycle() validates the
        # anchor form; this function only ever produces the document form.
        "supersedes": _scalar("supersedes"),
        "superseded_by": _scalar("superseded_by"),
    }
