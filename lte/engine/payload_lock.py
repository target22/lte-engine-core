"""
lte/engine/payload_lock.py -- LEAF MODULE. Pure predicates behind the
pre-commit write-path guardrails. Operates on (path, text) images that the
caller already read from HEAD and from the index; never touches Git.

Two guardrails:

1. REF-GATED PAYLOAD LOCK (config: linter_rules.json#payload_lock).
   A contract node with at least one inbound dependent callout in the same
   locale is locked: its body must stay byte-identical and its anchor must
   keep existing. Any configured kind locks -- the token is an alias
   (linter_rules.json#callout_kinds.aliases), so this module matches on
   callout.target_id and never on the token text. In a document holding a locked node, front matter keys outside
   `mutable_frontmatter_keys` are locked as well. Unreferenced documents and
   zero-node (ast_non_compliant) documents stay fully mutable.

2. ATOMIC DEPRECATION GUARDRAIL (config: linter_rules.json#dependent_lifecycle).
   A commit that moves a referenced contract INTO an invalidating parent
   state must reconcile every dependent in the SAME commit: the dependent
   file is deleted, drops the callout, or is itself moved to an
   invalidating state with lifecycle front matter that passes
   state_machine.check_dependent_lifecycle().

WHAT A COMPOSITE TARGET LOCKS. reference_map keys on callout.target_id, and
a callout may now cite a composite child key (`<parent>#<role>-<letter><nn>`)
rather than a contract anchor. Such a key is not in node_bodies(), so it
locks nothing -- deliberately: the payload lock protects CONTRACT bodies,
and a debate citing another debate does not make that debate immutable. An
authored sub-anchor (`^spec-...-c01`) is a real contract node and locks
exactly like any other.

REFERENCES ARE LOCALE-SCOPED: an en debate locks the en contract, not the
vi one. The locale comes from the injected CorpusLayout, never from a
hand-built suffix. A path the layout does not classify is not a corpus file
and is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from lte.engine import ast_blocks, frontmatter, state_machine
from lte.engine.grammar import Grammar

NON_BLOCKING_SEVERITIES = frozenset({"info", "warn", "warning"})


@dataclass(frozen=True)
class Change:
    """A staged change. Mirrors lte.io.git_cas_client.StagedChange."""

    status: str
    path: str
    old_path: str


def locale_of(layout, rel_path: str) -> str | None:
    return layout.locale_of(rel_path)


def invalidating_states(grammar: Grammar) -> frozenset:
    return frozenset(grammar.dependent_lifecycle.get("invalidating_parent_states") or ())


def parse_image(grammar: Grammar, rel_path: str, text: str) -> tuple:
    """The single parse call in this module.

    A one-line pass-through to ast_blocks.parse_text() now that `kinds` is
    gone, and kept as one anyway: six call sites below go through it, so the
    next signature change is one edit here rather than six. That is the only
    thing it buys, and it is worth stating so nobody mistakes it for a seam
    that does work.
    """
    return ast_blocks.parse_text(grammar, rel_path, text)


def reference_map(grammar: Grammar, images: Mapping, *, layout) -> dict:
    """{(locale, anchor_id): {referencing paths}} across a whole corpus image."""
    refs = {}
    for rel_path in sorted(images):
        text = images[rel_path]
        locale = locale_of(layout, rel_path)
        if text is None or locale is None:
            continue
        _, callouts = parse_image(grammar, rel_path, text)
        for callout in callouts:
            refs.setdefault((locale, callout.target_id), set()).add(rel_path)
    return refs


def node_bodies(grammar: Grammar, rel_path: str, text: str) -> dict:
    nodes, _ = parse_image(grammar, rel_path, text)
    return {node.spec_id: node.content for node in nodes}


def _locked_frontmatter(grammar: Grammar, text: str, mutable_keys: Sequence[str]) -> dict:
    data = frontmatter.parse_text(text, grammar.frontmatter_fence)
    return {k: v for k, v in data.items() if k not in set(mutable_keys)}


def check_payload_lock(grammar: Grammar, change: Change,
                       head_text: str | None, index_text: str | None,
                       references: Mapping, mutable_keys: Sequence[str], *, layout) -> list:
    """Violation strings for one staged M/D/R change. Empty when compliant."""
    locale = locale_of(layout, change.old_path)
    if head_text is None or locale is None:
        return []
    head_nodes = node_bodies(grammar, change.old_path, head_text)
    locked = sorted(a for a in head_nodes if references.get((locale, a)))
    if not locked:
        return []

    def referrers(anchor: str) -> str:
        return ", ".join(sorted(references[(locale, anchor)]))

    if change.status == "D" or index_text is None:
        return ["%s: file deleted, but it defines locked node(s) %s (referenced by: %s)"
                % (change.old_path, ", ".join("^" + a for a in locked),
                   "; ".join(referrers(a) for a in locked))]

    violations = []
    index_nodes = node_bodies(grammar, change.path, index_text)
    for anchor in locked:
        if anchor not in index_nodes:
            violations.append(
                "%s: locked node ^%s was removed or its anchor renamed (referenced by: %s)"
                % (change.path, anchor, referrers(anchor)))
        elif index_nodes[anchor] != head_nodes[anchor]:
            violations.append(
                "%s: body of locked node ^%s changed; a referenced contract body is "
                "immutable (referenced by: %s)" % (change.path, anchor, referrers(anchor)))

    head_fm = _locked_frontmatter(grammar, head_text, mutable_keys)
    index_fm = _locked_frontmatter(grammar, index_text, mutable_keys)
    changed = sorted(k for k in set(head_fm) | set(index_fm) if head_fm.get(k) != index_fm.get(k))
    if changed:
        violations.append(
            "%s: front matter key(s) %s changed in a document with locked nodes; only %s "
            "may change" % (change.path, ", ".join(changed), ", ".join(mutable_keys) or "(none)"))
    return violations


def effective_statuses(grammar: Grammar, rel_path: str, text: str) -> dict:
    """{anchor_id: status | None}; an inline {status: x} overrides front matter."""
    nodes, _ = parse_image(grammar, rel_path, text)
    doc_status = frontmatter.normalize_status(
        frontmatter.parse_text(text, grammar.frontmatter_fence).get("status"))
    allowed = set(grammar.status_values)
    out = {}
    for node in nodes:
        status = frontmatter.normalize_status(node.status_override) or doc_status
        out[node.spec_id] = status if status in allowed else None
    return out


def newly_invalidating(grammar: Grammar, changes: Sequence,
                       head_images: Mapping, index_images: Mapping, *, layout) -> dict:
    """{(locale, anchor): defining index path} for contracts entering an invalidating state."""
    states = invalidating_states(grammar)
    out = {}
    for change in changes:
        if change.status not in ("M", "R"):
            continue
        head_text = head_images.get(change.old_path)
        index_text = index_images.get(change.path)
        if head_text is None or index_text is None:
            continue
        before = effective_statuses(grammar, change.old_path, head_text)
        after = effective_statuses(grammar, change.path, index_text)
        locale = locale_of(layout, change.path)
        if locale is None:
            continue
        for anchor, status in after.items():
            if status in states and before.get(anchor) not in states:
                out[(locale, anchor)] = change.path
    return out


def _diagnostic_parts(diag) -> tuple:
    if isinstance(diag, Mapping):
        return str(diag.get("severity", "fail")), str(diag.get("message", diag))
    severity = getattr(diag, "severity", "fail")
    return str(severity), str(getattr(diag, "message", diag))


def resolution_of(grammar: Grammar, anchor: str, dependent_path: str,
                  changes_by_head_path: Mapping, index_images: Mapping, *, layout) -> str | None:
    """None when the dependent is reconciled in this commit, else the reason."""
    states = invalidating_states(grammar)
    expected = "/".join(sorted(states))
    change = changes_by_head_path.get(dependent_path)
    if change is None:
        return ("not staged: stage it with status %s (plus lifecycle fields) or remove "
                "its [!ref-%s] callout" % (expected, anchor))
    if change.status == "D":
        return None
    text = index_images.get(change.path)
    if text is None:
        return "staged, but its index image could not be read"
    _, callouts = parse_image(grammar, change.path, text)
    if not any(c.target_id == anchor for c in callouts):
        return None
    fm = frontmatter.parse_text(text, grammar.frontmatter_fence)
    status = frontmatter.normalize_status(fm.get("status"))
    if status not in states:
        return ("still references ^%s with status %r; expected %s"
                % (anchor, status, expected))
    failing = [msg for sev, msg in map(_diagnostic_parts,
                                       state_machine.check_dependent_lifecycle(grammar, change.path, fm,
                                                                               layout=layout))
               if sev.lower() not in NON_BLOCKING_SEVERITIES]
    if failing:
        return "lifecycle front matter invalid: " + "; ".join(failing)
    return None


def check_atomic_deprecation(grammar: Grammar, transitions: Mapping,
                             head_references: Mapping, changes_by_head_path: Mapping,
                             index_images: Mapping, *, layout) -> list:
    """One diagnostic block per contract whose invalidation leaves dependents behind."""
    blocks = []
    for (locale, anchor) in sorted(transitions, key=lambda k: (k[0] or "", k[1])):
        defining_path = transitions[(locale, anchor)]
        dependents = sorted(p for p in head_references.get((locale, anchor), ())
                            if p != defining_path)
        unresolved = []
        for dependent in dependents:
            reason = resolution_of(grammar, anchor, dependent,
                                   changes_by_head_path, index_images, layout=layout)
            if reason:
                unresolved.append("    - %s: %s" % (dependent, reason))
        if unresolved:
            blocks.append(
                "%s: ^%s enters an invalidating state, but %d dependent(s) are not "
                "reconciled in this commit:\n%s"
                % (defining_path, anchor, len(unresolved), "\n".join(unresolved)))
    return blocks
