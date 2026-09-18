import sys, types, inspect
from _harness import build
from lte.engine import ast_blocks
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

# stubs for the two engine modules payload_lock imports
import yaml
fm = types.ModuleType("lte.engine.frontmatter")
def _fm_parse(text, fence=None):
    lines = text.splitlines()
    if not lines or not fence.match(lines[0]): return {}
    end = next((i for i in range(1, len(lines)) if fence.match(lines[i])), None)
    return yaml.safe_load("\n".join(lines[1:end])) or {} if end else {}
fm.parse_text = _fm_parse
fm.normalize_status = lambda r: r.strip().lower() if isinstance(r, str) and r.strip() else None
sm = types.ModuleType("lte.engine.state_machine")
sm.check_dependent_lifecycle = lambda *a, **k: []
for n, m in (("lte.engine.frontmatter", fm), ("lte.engine.state_machine", sm)):
    sys.modules[n] = m
import lte.engine as _e; _e.frontmatter, _e.state_machine = fm, sm

from lte.engine import payload_lock as pl

g = build()
class Layout:
    def locale_of(self, rel): return "en" if rel.endswith(".en.md") else None

print("--- signatures ---")
for fn, expected in [
    ("parse_image", ["grammar","rel_path","text"]),
    ("reference_map", ["grammar","images","layout"]),
    ("node_bodies", ["grammar","rel_path","text"]),
    ("check_payload_lock", ["grammar","change","head_text","index_text","references","mutable_keys","layout"]),
    ("effective_statuses", ["grammar","rel_path","text"]),
    ("newly_invalidating", ["grammar","changes","head_images","index_images","layout"]),
    ("resolution_of", ["grammar","anchor","dependent_path","changes_by_head_path","index_images","layout"]),
    ("check_atomic_deprecation", ["grammar","transitions","head_references","changes_by_head_path","index_images","layout"]),
]:
    got = list(inspect.signature(getattr(pl, fn)).parameters)
    check("%s%s" % (fn, tuple(expected)), got == expected, str(got))

print("\n--- behaviour: references lock, composite targets do not ---")
C = '''---
status: "active"
version: "1.0.0"
---
## Price bounds
Bids stay inside the band.

^spec-lte-01-002

## Unreferenced rule
Free to change.

^sop-deploy-03-001
'''
D = '''---
status: "active"
---
## Why not the median
Mean keeps tails.

> [!ref-spec-lte-01-002] cites the contract
> body

> [!evi-spec-lte-01-002#debate-d01] cites a DEBATE, not a contract
> body
'''
CP, DP = "docs/public/core/01-p.contract.en.md", "docs/public/core/01-p.debate.en.md"
head = {CP: C, DP: D}
refs = pl.reference_map(g, head, layout=Layout())
print("   reference_map keys:")
for k, v in sorted(refs.items()): print("     %-45s <- %s" % (str(k), sorted(v)))
check("contract anchor is referenced", ("en", "spec-lte-01-002") in refs)
check("composite target keyed separately", ("en", "spec-lte-01-002#debate-d01") in refs)
check("unreferenced anchor absent", ("en", "sop-deploy-03-001") not in refs)

MUT = ("version",)
ch = pl.Change("M", CP, CP)
v = pl.check_payload_lock(g, ch, C, C.replace("Bids stay inside the band.", "Bids may drift."),
                          refs, MUT, layout=Layout())
check("locked body change is blocked", len(v) == 1 and "body of locked node" in v[0], str(v))
v = pl.check_payload_lock(g, ch, C, C.replace("Free to change.", "Changed freely."),
                          refs, MUT, layout=Layout())
check("unreferenced node stays mutable", v == [], str(v))
v = pl.check_payload_lock(g, ch, C, C.replace('version: "1.0.0"', 'version: "1.1.0"'),
                          refs, MUT, layout=Layout())
check("mutable frontmatter key allowed", v == [], str(v))
v = pl.check_payload_lock(g, ch, C, C.replace('status: "active"', 'status: "draft"'),
                          refs, MUT, layout=Layout())
check("locked frontmatter key blocked", len(v) == 1 and "front matter key" in v[0], str(v))

print("\n--- the status directive drives atomic deprecation again ---")
DEP = C.replace("Bids stay inside the band.\n", "Bids stay inside the band.\n\n{status: deprecated}\n")
st = pl.effective_statuses(g, CP, DEP)
print("   effective_statuses:", st)
check("per-node override is honoured", st["spec-lte-01-002"] == "deprecated", str(st))
check("sibling node keeps the document status", st["sop-deploy-03-001"] == "active", str(st))
trans = pl.newly_invalidating(g, [ch], {CP: C}, {CP: DEP}, layout=Layout())
check("transition detected from the directive alone",
      trans.get(("en", "spec-lte-01-002")) == CP, str(trans))
check("sibling did NOT transition", ("en", "sop-deploy-03-001") not in trans)
blocks = pl.check_atomic_deprecation(g, trans, refs, {}, {}, layout=Layout())
check("unreconciled dependent is reported", len(blocks) == 1 and DP in blocks[0], str(blocks))

print("\n--- regression guard: without the directive there is no transition ---")
check("undeprecated corpus produces no transitions",
      pl.newly_invalidating(g, [ch], {CP: C}, {CP: C}, layout=Layout()) == {})

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
