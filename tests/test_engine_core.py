#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_engine_core.py -- grammar compilation and the pure-text AST.

Covers lte/engine/grammar.py and lte/engine/ast_blocks.py: placeholder
expansion, the configurable callout vocabulary, both structural entities,
composite child keys, hierarchical suffixes, and the per-contract status
directive.

Run: python3 tests/test_engine_core.py
"""
import collections
import re
import sys

from _harness import Reporter, build, mutated_rules

from lte.engine import ast_blocks
from lte.engine.grammar import ConfigError, validate_callout_kinds

r = Reporter()
g = build()
REL = "docs/public/core/01-price.contract.en.md"


# ----------------------------------------------------------------------
print("--- grammar compiles from the repo's own config ---")
# ----------------------------------------------------------------------
print("  anchor_id       :", g.anchor_id)
print("  kind_alternation:", g.kind_alternation)
print("  composite_id    :", g.composite_id)

# A bare "{" in a pattern is legal -- heading carries #{1,6} and
# status_directive carries a literal \{status:. The leak _compile() guards
# against is a PLACEHOLDER shape, so match that, not any brace.
_LEAK = re.compile(r"\{[a-z_]+\}")
r.check("no placeholder leaked into any compiled pattern",
        not [p.pattern for p in (
            g.spec_anchor, g.heading, g.ref_callout_start, g.subblock_suffix,
            g.status_directive, g.split_document, g.file_naming,
            g.document_h1, g.frontmatter_fence, g.blockquote_line)
            if _LEAK.search(p.pattern)])
r.check("kind_alt expanded into ref_callout_start",
        g.kind_alternation in g.ref_callout_start.pattern)
r.check("composite_id expanded into ref_callout_start",
        g.composite_id in g.ref_callout_start.pattern)

# The six Grammar-2 patterns are gone; status_directive is the one survivor.
for attr in ("admonition_open", "admonition_indent", "admonition_blank",
             "admonition_trailing_anchor", "admonition_ref_line", "admonition_status"):
    r.check("Grammar has no .%s" % attr, not hasattr(g, attr))
r.check("status_directive survives the purge", hasattr(g, "status_directive"))
for attr in ("loose_anchor_line", "loose_ref_line",
             "loose_admonition_open", "loose_table_row"):
    r.check("diagnostics compiled onto the Grammar: .%s" % attr, hasattr(g, attr))


# ----------------------------------------------------------------------
print("\n--- two structural entities, one pass ---")
# ----------------------------------------------------------------------
MD = '''---
status: "active"
---
# Pricing chapter

Intro prose that becomes the prologue.

## Price bounds
Bids stay inside the **published** band.

^spec-lte-01-002

> [!ref-spec-lte-01-002] Why not the median
> The mean keeps tail information.

> [!ref-spec-lte-01-002] Why not a trimmed mean
> Second debate on the same parent.

> [!ops-spec-lte-01-002] Pre-deploy
> - [ ] verify band

> [!evi-spec-lte-01-002] Incident 2026-04-02
> Band held at 3 sigma.
'''
nodes, callouts = ast_blocks.parse_text(g, REL, MD)
for c in callouts:
    print("     token=%-4s role=%-7s node_id=%-30s %r" % (c.token, c.kind, c.node_id, c.title))

r.check("primary key: one SpecNode from the bare anchor", len(nodes) == 1)
r.check("node id and title", nodes[0].spec_id == "spec-lte-01-002"
        and nodes[0].title == "Price bounds")
r.check("foreign keys: four dependents", len(callouts) == 4, str(len(callouts)))
r.check("alias ref resolves to internal role debate", callouts[0].kind == "debate")
r.check("alias ops resolves to internal role ops", callouts[2].kind == "ops")
r.check("alias evi resolves to internal role evi", callouts[3].kind == "evi")
r.check("authored token preserved alongside the role",
        [c.token for c in callouts] == ["ref", "ref", "ops", "evi"])

r.check("composite key minted for the first debate",
        callouts[0].node_id == "spec-lte-01-002#debate-d01")
r.check("ordinal increments per (parent, role)",
        callouts[1].node_id == "spec-lte-01-002#debate-d02")
r.check("ops has its own letter and its own counter",
        callouts[2].node_id == "spec-lte-01-002#ops-p01")
r.check("evi has its own letter and its own counter",
        callouts[3].node_id == "spec-lte-01-002#evi-e01")
r.check("every composite key validates as an anchor reference",
        all(g.anchor_reference_pattern().match(c.node_id) for c in callouts))

# Determinism: byte-identical input must yield byte-identical keys.
again_nodes, again_callouts = ast_blocks.parse_text(g, REL, MD)
r.check("a second parse of identical bytes yields identical keys",
        [c.node_id for c in again_callouts] == [c.node_id for c in callouts])


# ----------------------------------------------------------------------
print("\n--- hierarchical sub-anchors ---")
# ----------------------------------------------------------------------
SUF = ("## Clause\ntext\n\n^spec-lte-01-002-c01\n\n"
       "> [!ref-spec-lte-01-002-c01] On clause 1\n> body\n")
n2, c2 = ast_blocks.parse_text(g, REL, SUF)
r.check("a -c01 suffix parses as an anchor in its own right",
        len(n2) == 1 and n2[0].spec_id == "spec-lte-01-002-c01")
r.check("a dependent can target the suffixed anchor",
        len(c2) == 1 and c2[0].target_id == "spec-lte-01-002-c01")
r.check("and gets its own composite child key",
        c2[0].node_id == "spec-lte-01-002-c01#debate-d01")

n3, c3 = ast_blocks.parse_text(
    g, REL, "> [!ref-spec-lte-01-002#debate-d01] cites a debate\n> body\n")
r.check("a dependent may cite a composite key",
        len(c3) == 1 and c3[0].target_id == "spec-lte-01-002#debate-d01")


# ----------------------------------------------------------------------
print("\n--- per-contract status directive ---")
# ----------------------------------------------------------------------
DIR = ("## Price bounds\nBand holds.\n\n{status: deprecated}\n\nMore prose.\n\n"
       "^spec-lte-01-002\n\n## Other\nNo override.\n\n^sop-deploy-03-001\n")
n4, _ = ast_plain = ast_blocks.parse_text(g, REL, DIR)
r.check("directive captured as status_override", n4[0].status_override == "deprecated")
r.check("the directive line is stripped from the rendered body",
        "{status" not in n4[0].content, n4[0].content)
r.check("a sibling node in the same file is unaffected",
        n4[1].status_override is None)


# ----------------------------------------------------------------------
print("\n--- Grammar 2 is gone ---")
# ----------------------------------------------------------------------
G2 = '!!! note "Price bounds"\n\n    text\n\n    ^spec-lte-01-002\n'
n5, c5 = ast_blocks.parse_text(g, REL, G2)
r.check("an admonition parses to nothing", (len(n5), len(c5)) == (0, 0),
        "%d/%d" % (len(n5), len(c5)))
r.check("an unknown anchor domain parses to nothing",
        ast_blocks.parse_text(g, REL, "## X\nt\n\n^hr-onboarding-15a1-0930a1\n") == ([], []))


# ----------------------------------------------------------------------
print("\n--- the vocabulary is genuinely configurable ---")
# ----------------------------------------------------------------------
g2 = build(mutated_rules(lambda doc: doc["callout_kinds"].__setitem__(
    "aliases", collections.OrderedDict(
        [("cite", "debate"), ("sops", "ops"), ("logs", "evi")]))))
RE = ("## X\nt\n\n^spec-lte-01-002\n\n"
      "> [!sops-spec-lte-01-002] Renamed checklist\n> - [ ] a\n")
_, c6 = ast_blocks.parse_text(g2, REL, RE)
r.check("a renamed token is accepted with zero code change", len(c6) == 1)
r.check("it still resolves to the internal role 'ops'", c6 and c6[0].kind == "ops")
r.check("the composite key is unchanged by the rename",
        c6 and c6[0].node_id == "spec-lte-01-002#ops-p01")
_, c7 = ast_blocks.parse_text(g2, REL, "> [!ops-spec-lte-01-002] retired\n> b\n")
r.check("the retired token no longer parses", len(c7) == 0)


# ----------------------------------------------------------------------
print("\n--- config fail-fast ---")
# ----------------------------------------------------------------------
def rejects(mutate):
    try:
        build(mutated_rules(mutate))
        return False
    except ConfigError:
        return True


r.check("alias mapping to an undeclared role",
        rejects(lambda d: d["callout_kinds"]["aliases"].update({"zz": "nosuchrole"})))
r.check("alias containing a hyphen",
        rejects(lambda d: d["callout_kinds"]["aliases"].update({"re-f": "debate"})))
r.check("missing suffix letter",
        rejects(lambda d: d["callout_kinds"]["suffix_letters"].pop("evi")))
r.check("duplicate suffix letter",
        rejects(lambda d: d["callout_kinds"]["suffix_letters"].update({"evi": "d"})))
r.check("alphanumeric composite separator",
        rejects(lambda d: d["callout_kinds"].update({"separator": "x"})))
r.check("suffix letter outside subblock_letters",
        rejects(lambda d: d["callout_kinds"].update({"subblock_letters": ["c"]})))
r.check("missing composite_id_template",
        rejects(lambda d: d.pop("composite_id_template")))
r.check("a leaked placeholder in composite_id_template",
        rejects(lambda d: d.update({"composite_id_template": "(?:{anchor_id}){nope}"})))

# validate_callout_kinds is reachable directly, for callers building a
# Grammar by other means.
try:
    validate_callout_kinds({"aliases": {"ref": "debate"}, "suffix_letters": {},
                            "subblock_letters": ["d"]}, ["debate"], "t.json")
    direct = False
except ConfigError:
    direct = True
r.check("validate_callout_kinds rejects a bad block directly", direct)

sys.exit(r.finish())
