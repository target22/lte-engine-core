"""
lte/engine/retrieval.py -- LEAF MODULE. Pure retrieval over a compiled
read-model: flattening, tokenisation, Okapi BM25, relational expansion and
prompt assembly.

Extracted from the monolithic scripts/core_pipeline/ask_lte.py so that
lte/cli/ask.py is a router only. No I/O: the graph arrives as an already
parsed mapping (lte.io.graph_reader) and the prompt leaves as a string.

GRAPH SHAPE CONSUMED (graph_builder output):

    graph[lang].partitions[].documents[].nodes[].{debates,ops}[]

Key lookups use documented fallbacks (`document_id` then `id`, `metadata`
then the entry itself) so a renamed field degrades to an empty string
instead of a KeyError deep inside a scoring loop.
"""

from __future__ import annotations

import abc
import html
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Sequence

BM25_K1 = 1.5
BM25_B = 0.75
TITLE_WEIGHT = 2          # title tokens are counted this many times
KIND_CONTRACT = "contract"
KIND_DEBATE = "debate"
KIND_OPS = "ops"
ALL_KINDS = (KIND_CONTRACT, KIND_DEBATE, KIND_OPS)
ENTRY_LIST_KEYS = ((KIND_DEBATE, "debates"), (KIND_OPS, "ops"))
NOT_IN_FORCE = frozenset({"deprecated", "archived", "invalidated", "ast_non_compliant"})

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_ANCHOR_LIKE_RE = re.compile(r"\^?([a-z][a-z0-9]*(?:-[a-z0-9]+)+)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


class RetrievalError(ValueError):
    """The graph cannot be used for this query (scope, language, shape)."""


# ------------------------------------------------------------------ units

@dataclass(frozen=True)
class RetrievalUnit:
    """One addressable Tier-4 unit, flattened out of the nested graph.

    parent_anchor is the anchor the unit hangs off: itself for a contract,
    the target contract for a debate/ops entry.
    """

    uid: str
    kind: str
    anchor_id: str
    parent_anchor: str
    layer: str
    title: str
    text: str
    source_file: str
    partition: str
    document_id: str
    document_title: str
    status: str
    contract_status: str
    computed_status: str
    invalidated: bool

    def effective_status(self) -> str:
        """computed_status (Pessimistic State Resolution) wins when present."""
        if self.invalidated:
            return self.computed_status or "invalidated"
        return self.computed_status or self.status or "unknown"

    def citation(self) -> str:
        if self.kind == KIND_CONTRACT:
            return "^" + self.anchor_id
        return "%s -> ^%s" % (self.kind, self.parent_anchor)


@dataclass(frozen=True)
class Hit:
    unit: RetrievalUnit
    score: float
    reason: str


@dataclass(frozen=True)
class PromptEnvelope:
    system_instructions: str
    hits: tuple
    query: str
    graph_meta: Mapping


# ------------------------------------------------------------- text utils

def strip_html(value: str) -> str:
    """Rendered bodies are HTML; retrieval scores prose, not markup."""
    text = _TAG_RE.sub(" ", value or "")
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()


def tokenize(text: str) -> list:
    """NFC-normalised, casefolded `\\w+` tokens (Vietnamese-safe)."""
    return _WORD_RE.findall(unicodedata.normalize("NFC", text or "").casefold())


def anchor_tokens(anchor_id: str) -> list:
    """'spec-lte-01-001' -> ['spec-lte-01-001', 'spec', 'lte', '01', '001']."""
    if not anchor_id:
        return []
    whole = anchor_id.casefold()
    return [whole] + [part for part in whole.split("-") if part]


def anchors_in(text: str) -> list:
    """Anchor-shaped substrings of a query, caret optional, in order."""
    seen, out = set(), []
    for match in _ANCHOR_LIKE_RE.finditer(text or ""):
        value = match.group(1).casefold()
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _str(mapping: Mapping, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# ------------------------------------------------------------ graph access

def declared_scope(graph: Mapping) -> str | None:
    metadata = graph.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("scope"), str):
        return metadata["scope"]
    value = graph.get("scope")
    return value if isinstance(value, str) else None


def _contains_restricted_source(value, prefixes: tuple) -> bool:
    if not prefixes:
        return False
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if item.startswith(prefixes):
                return True
        elif isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def check_graph_scope(graph: Mapping, allow_restricted: bool, restricted_targets,
                      restricted_prefixes) -> str:
    """
    Returns the graph's declared target; raises when a restricted graph is
    refused. `restricted_targets` are the layout's non-public target names
    (ids and aliases); `restricted_prefixes` are the source prefixes no
    public artifact may contain, used when a graph declares no target or an
    unknown one.
    """
    scope = declared_scope(graph)
    restricted = (scope in set(restricted_targets) if scope is not None else False)
    if not restricted and _contains_restricted_source(graph, tuple(p for p in restricted_prefixes if p)):
        restricted = True
    if restricted and not allow_restricted:
        raise RetrievalError(
            "graph target %r is restricted; pass --internal to query it" % (scope or "undeclared")
        )
    return scope or ("restricted" if restricted else "unknown")


def languages(graph: Mapping) -> list:
    return sorted(k for k, v in graph.items()
                  if isinstance(v, Mapping) and isinstance(v.get("partitions"), list))


def flatten(graph: Mapping, lang: str, kinds: Sequence[str] = ALL_KINDS) -> list:
    """Deterministic graph-order walk into a flat unit list."""
    lang_graph = graph.get(lang)
    if not isinstance(lang_graph, Mapping):
        raise RetrievalError(
            "language %r not present in graph (available: %s)"
            % (lang, ", ".join(languages(graph)) or "none")
        )
    wanted = set(kinds)
    units = []
    for partition in lang_graph.get("partitions") or []:
        partition_id = _str(partition, "id", "partition")
        for document in partition.get("documents") or []:
            doc_meta = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
            document_id = _str(document, "document_id", "id")
            document_title = _str(document, "title", "document_title")
            doc_status = _str(doc_meta, "status") or _str(document, "status")
            doc_source = _str(document, "source_file")
            for node in document.get("nodes") or []:
                anchor = _str(node, "anchor_id")
                if not anchor:
                    continue
                node_status = _str(node, "status", "effective_status") or doc_status
                if KIND_CONTRACT in wanted:
                    units.append(RetrievalUnit(
                        uid=anchor, kind=KIND_CONTRACT, anchor_id=anchor,
                        parent_anchor=anchor, layer=_str(node, "layer"),
                        title=_str(node, "title"),
                        text=strip_html(_str(node, "body", "body_html")),
                        source_file=_str(node, "source_file") or doc_source,
                        partition=partition_id, document_id=document_id,
                        document_title=document_title, status=node_status,
                        contract_status=node_status,
                        computed_status=_str(node, "computed_status"),
                        invalidated=False,
                    ))
                for kind, key in ENTRY_LIST_KEYS:
                    if kind not in wanted:
                        continue
                    for ordinal, entry in enumerate(node.get(key) or []):
                        units.append(RetrievalUnit(
                            uid="%s#%s-%d" % (anchor, kind, ordinal), kind=kind,
                            anchor_id=anchor, parent_anchor=anchor,
                            layer=_str(entry, "layer"), title=_str(entry, "title"),
                            text=strip_html(_str(entry, "body", "body_html")),
                            source_file=_str(entry, "source_file"),
                            partition=partition_id, document_id=document_id,
                            document_title=document_title,
                            status=_str(entry, "dependent_status", "status"),
                            contract_status=_str(entry, "contract_status") or node_status,
                            computed_status=_str(entry, "computed_status"),
                            invalidated=bool(entry.get("invalidated")),
                        ))
    return units


# -------------------------------------------------------------- retrieval

class Retriever(abc.ABC):
    """The swap contract. Implementations must be deterministic."""

    @abc.abstractmethod
    def retrieve(self, query: str, limit: int) -> list: ...

    @abc.abstractmethod
    def by_anchor(self, anchor_id: str): ...

    @abc.abstractmethod
    def children_of(self, anchor_id: str) -> list: ...

    @abc.abstractmethod
    def parent_of(self, unit: RetrievalUnit): ...


class InMemoryRetriever(Retriever):
    """Okapi BM25 over the flattened read-model, built per invocation."""

    def __init__(self, units: Sequence[RetrievalUnit], seed_kinds: Sequence[str] = ALL_KINDS):
        self.units = list(units)
        self.seed_kinds = frozenset(seed_kinds)
        self._contracts = {}
        self._children = {}
        for unit in self.units:
            if unit.kind == KIND_CONTRACT:
                self._contracts.setdefault(unit.anchor_id.casefold(), unit)
            else:
                self._children.setdefault(unit.parent_anchor.casefold(), []).append(unit)
        self._build_index()

    @staticmethod
    def _field_tokens(unit: RetrievalUnit) -> list:
        return (tokenize(unit.title) * TITLE_WEIGHT
                + anchor_tokens(unit.anchor_id)
                + tokenize(unit.text))

    def _build_index(self) -> None:
        self._tf, self._len, self._df = [], [], {}
        for unit in self.units:
            counts = {}
            tokens = self._field_tokens(unit)
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self._tf.append(counts)
            self._len.append(len(tokens))
            for token in counts:
                self._df[token] = self._df.get(token, 0) + 1
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0

    def _idf(self, token: str) -> float:
        n, df = len(self.units), self._df.get(token, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Sequence[str], doc_index: int) -> float:
        counts, length = self._tf[doc_index], self._len[doc_index]
        norm = 1.0 - BM25_B + BM25_B * (length / self._avg if self._avg else 0.0)
        total = 0.0
        for token in query_tokens:
            tf = counts.get(token, 0)
            if tf:
                total += self._idf(token) * tf * (BM25_K1 + 1) / (tf + BM25_K1 * norm)
        return total

    def retrieve(self, query: str, limit: int) -> list:
        """
        Anchors named in the query come first (explicit references ignore
        seed_kinds); BM25 seeds follow, restricted to seed_kinds. The index
        itself always holds every kind, so expand() can still attach a
        parent contract to a debate seed.
        """
        hits, seen = [], set()
        for anchor in anchors_in(query):
            unit = self.by_anchor(anchor)
            if unit is not None and unit.uid not in seen:
                seen.add(unit.uid)
                hits.append(Hit(unit, float("inf"), "anchor"))
        tokens = list(dict.fromkeys(tokenize(query)))
        for anchor in anchors_in(query):
            if anchor not in tokens:
                tokens.append(anchor)
        scored = []
        for index, unit in enumerate(self.units):
            if unit.uid in seen or unit.kind not in self.seed_kinds:
                continue
            value = self.score(tokens, index)
            if value > 0:
                scored.append((-value, index))
        scored.sort()
        for negative, index in scored:
            if len(hits) >= limit:
                break
            hits.append(Hit(self.units[index], -negative, "bm25"))
        return hits[:max(limit, 0)]

    def by_anchor(self, anchor_id: str):
        return self._contracts.get((anchor_id or "").lstrip("^").casefold())

    def children_of(self, anchor_id: str) -> list:
        return list(self._children.get((anchor_id or "").casefold(), ()))

    def parent_of(self, unit: RetrievalUnit):
        if unit.kind == KIND_CONTRACT:
            return None
        return self.by_anchor(unit.parent_anchor)


class VectorDBRetriever(Retriever):
    """Swap point for dense retrieval. Deliberately not implemented."""

    def __init__(self, units: Sequence[RetrievalUnit], seed_kinds: Sequence[str] = ALL_KINDS,
                 endpoint: str | None = None, collection: str | None = None):
        self.units, self.seed_kinds = list(units), frozenset(seed_kinds)
        self.endpoint, self.collection = endpoint, collection

    def _unsupported(self):
        raise NotImplementedError(
            "VectorDBRetriever is an interface placeholder; use --retriever inmemory"
        )

    def retrieve(self, query, limit):
        self._unsupported()

    def by_anchor(self, anchor_id):
        self._unsupported()

    def children_of(self, anchor_id):
        self._unsupported()

    def parent_of(self, unit):
        self._unsupported()


RETRIEVERS = {"inmemory": InMemoryRetriever, "vector": VectorDBRetriever}


def build_retriever(name: str, units: Sequence[RetrievalUnit],
                    seed_kinds: Sequence[str] = ALL_KINDS) -> Retriever:
    try:
        factory = RETRIEVERS[name]
    except KeyError:
        raise RetrievalError(
            "unknown retriever %r (expected one of %s)" % (name, ", ".join(sorted(RETRIEVERS)))
        ) from None
    return factory(units, seed_kinds=seed_kinds)


def expand(retriever: Retriever, seeds: Sequence[Hit], max_units: int) -> list:
    """
    Adds relational neighbours, directionally:

      dependent seed -> its parent contract is inserted BEFORE it, because a
                        debate or checklist is ungrounded without the rule
                        it annotates;
      contract seed  -> its dependents are appended AFTER it, only while the
                        max_units budget remains.

    Order-stable and de-duplicated by uid.
    """
    out, seen = [], set()

    def add(hit: Hit) -> bool:
        if hit.unit.uid in seen or len(out) >= max_units:
            return False
        seen.add(hit.unit.uid)
        out.append(hit)
        return True

    for hit in seeds:
        if len(out) >= max_units:
            break
        if hit.unit.kind == KIND_CONTRACT:
            add(hit)
            for child in retriever.children_of(hit.unit.anchor_id):
                add(Hit(child, 0.0, "child-of:" + hit.unit.anchor_id))
        else:
            parent = retriever.parent_of(hit.unit)
            if parent is not None:
                add(Hit(parent, 0.0, "parent-of:" + hit.unit.uid))
            add(hit)
    return out


# ---------------------------------------------------------------- prompts

SYSTEM_INSTRUCTIONS = (
    "You are answering questions about a compiled relational governance "
    "knowledge graph. Use ONLY the numbered context blocks below. Cite every "
    "claim with the anchor shown in its block, e.g. [^spec-lte-01-001]. A "
    "block whose status is deprecated, archived, invalidated or "
    "ast_non_compliant is NOT in force: say so explicitly instead of applying "
    "it. CONTRACT blocks are normative; DEBATE blocks record trade-offs; OPS "
    "blocks are checklists that implement a contract. If the context does not "
    "answer the question, reply that the graph does not contain the answer."
)


def render_block(hit: Hit, index: int, show_scores: bool = False) -> str:
    unit = hit.unit
    status = unit.effective_status()
    flag = "  [NOT IN FORCE]" if status in NOT_IN_FORCE else ""
    lines = [
        "[%d] %s %s | status: %s%s | layer: %s"
        % (index, unit.kind.upper(), unit.citation(), status, flag, unit.layer or "-"),
        "title: %s" % (unit.title or "-"),
        "source: %s (document: %s)" % (unit.source_file or "-", unit.document_title or unit.document_id or "-"),
    ]
    if show_scores:
        score = "anchor-match" if math.isinf(hit.score) else "%.4f" % hit.score
        lines.append("score: %s (%s)" % (score, hit.reason))
    lines.append(unit.text or "(empty body)")
    return "\n".join(lines)


def assemble_prompt(envelope: PromptEnvelope, show_scores: bool = False) -> str:
    meta = envelope.graph_meta
    header = ", ".join("%s=%s" % (k, meta[k]) for k in sorted(meta) if meta[k] not in (None, ""))
    blocks = [render_block(hit, i, show_scores) for i, hit in enumerate(envelope.hits, 1)]
    return "\n\n".join([
        "## SYSTEM\n" + envelope.system_instructions,
        "## GRAPH\n" + (header or "(no metadata)"),
        "## CONTEXT\n" + ("\n\n".join(blocks) if blocks else "(no matching units)"),
        "## QUESTION\n" + envelope.query.strip(),
    ]) + "\n"


def graph_meta(graph: Mapping, lang: str, scope: str) -> dict:
    metadata = graph.get("metadata") if isinstance(graph.get("metadata"), Mapping) else {}
    return {
        "lang": lang,
        "scope": scope,
        "schema_version": metadata.get("schema_version") or graph.get("schema_version"),
        "generated_at": metadata.get("generated_at") or graph.get("generated_at"),
        "graph_hash": metadata.get("graph_hash"),
    }
