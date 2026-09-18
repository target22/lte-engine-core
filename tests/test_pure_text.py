from _harness import mutated_rules
import sys, json, collections; from _harness import REPO, build, mutated_rules
from lte.engine.grammar import validate_callout_kinds, ConfigError
from lte.engine import ast_blocks
 
g = build()
fails = []
def check(label, cond, detail=""):
    fails.append(label) if not cond else None
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- " + detail) if detail else ""))

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
nodes, callouts = ast_blocks.parse_text(g, "docs/public/core/01-price.contract.en.md", MD)
check("one SpecNode from the bare anchor", len(nodes) == 1)
check("SpecNode id + title", nodes[0].spec_id == "spec-lte-01-002" and nodes[0].title == "Price bounds")
check("four dependents parsed", len(callouts) == 4, str(len(callouts)))
print()
for c in callouts:
    print("     token=%-4s kind=%-7s node_id=%-32s %r" % (c.token, c.kind, c.node_id, c.title))
print()
check("alias ref -> internal role debate", callouts[0].kind == "debate")
check("alias ops -> internal role ops", callouts[2].kind == "ops")
check("alias evi -> internal role evi", callouts[3].kind == "evi")
check("composite key #1", callouts[0].node_id == "spec-lte-01-002#debate-d01")
check("ordinal increments per (parent, role)", callouts[1].node_id == "spec-lte-01-002#debate-d02")
check("ops uses its own letter + own counter", callouts[2].node_id == "spec-lte-01-002#ops-p01")
check("evi uses its own letter + own counter", callouts[3].node_id == "spec-lte-01-002#evi-e01")
check("every composite id validates", all(g.anchor_reference_pattern().match(c.node_id) for c in callouts))
check("authored token preserved", [c.token for c in callouts] == ["ref","ref","ops","evi"])

print("\n--- hierarchical suffix targeting (spec 3) ---")
SUF = "## Clause\ntext\n\n^spec-lte-01-002-c01\n\n> [!ref-spec-lte-01-002-c01] On clause 1\n> body\n"
n2, c2 = ast_blocks.parse_text(g, "docs/public/core/x.contract.en.md", SUF)
check("-c01 suffix parses as an anchor", len(n2) == 1 and n2[0].spec_id == "spec-lte-01-002-c01")
check("a ref can target the suffixed anchor", len(c2) == 1 and c2[0].target_id == "spec-lte-01-002-c01")
check("child of a suffixed parent", c2[0].node_id == "spec-lte-01-002-c01#debate-d01")

print("\n--- Grammar 2 is gone ---")
G2 = '!!! note "Price bounds"\n\n    text\n\n    ^spec-lte-01-002\n'
n3, c3 = ast_blocks.parse_text(g, "docs/public/core/x.contract.en.md", G2)
check("admonitions parse to nothing", (len(n3), len(c3)) == (0, 0), "%d/%d" % (len(n3), len(c3)))
check("no admonition attrs remain on Grammar",
      not any(a.startswith("admonition") for a in vars(g)))

print("\n--- vocabulary is genuinely configurable ---")
# Mutated IN MEMORY. The previous version wrote renamed.json into the
# caller's cwd -- a test that litters the working tree, and that silently
# read a different file depending on where it was invoked from.
g2 = build(mutated_rules(lambda doc: doc["callout_kinds"].__setitem__(
    "aliases", collections.OrderedDict(
        [("cite", "debate"), ("sops", "ops"), ("logs", "evi")]))))
RE = "## X\nt\n\n^spec-lte-01-002\n\n> [!sops-spec-lte-01-002] Renamed checklist\n> - [ ] a\n"
n4, c4 = ast_blocks.parse_text(g2, "docs/public/core/x.contract.en.md", RE)
check("renamed alternation recompiled (ties keep declaration order)", g2.kind_alternation == "cite|sops|logs", g2.kind_alternation)
RE = "## X\nt\n\n^spec-lte-01-002\n\n> [!sops-spec-lte-01-002] Renamed checklist\n> - [ ] a\n"
n4, c4 = ast_blocks.parse_text(g2, "docs/public/core/x.contract.en.md", RE)
check("renamed alternation recompiled (ties keep declaration order)", g2.kind_alternation == "cite|sops|logs", g2.kind_alternation)
check("'sops' token accepted with zero code change", len(c4) == 1)
check("still maps to internal role 'ops'", c4 and c4[0].kind == "ops")
check("composite id unchanged by the rename", c4 and c4[0].node_id == "spec-lte-01-002#ops-p01")
n5, c5 = ast_blocks.parse_text(g2, "docs/public/core/x.contract.en.md",
                               "> [!ops-spec-lte-01-002] old token\n> b\n")
check("the retired 'ops' token no longer parses", len(c5) == 0)

print("\n--- config fail-fast ---")
for label, blk in [
    ("alias -> unknown role rejected",
     {"aliases": {"ref": "nosuchrole"}, "suffix_letters": {"debate": "d"}}),
    ("alias containing '-' rejected",
     {"aliases": {"re-f": "debate"}, "suffix_letters": {"debate": "d"}}),
    ("missing suffix letter rejected",
     {"aliases": {"ref": "debate"}, "suffix_letters": {}}),
    ("duplicate suffix letter rejected",
     {"aliases": {"ref": "debate", "ops": "ops"}, "suffix_letters": {"debate": "d", "ops": "d"}}),
    ("alphanumeric separator rejected",
     {"aliases": {"ref": "debate"}, "suffix_letters": {"debate": "d"}, "separator": "x"}),
]:
    try:
        validate_callout_kinds(blk, ["debate","ops","evi"], "t.json"); ok = False
    except ConfigError:
        ok = True
    check(label, ok)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
