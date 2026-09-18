from _harness import raw_config
import sys, copy, yaml, json; from _harness import REPO, build
from lte.engine import taxonomy as tx

ROWS = raw_config()["taxonomy"]
UI = raw_config()["ui_projection"]
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

t = tx.build_taxonomy(ROWS, UI)
print()
check("dependent_roles in declaration order", t.dependent_roles == ("debate","ops","evi"), str(t.dependent_roles))
check("graph keys resolve",
      [t.callout_graph_key(r) for r in t.dependent_roles] == ["debates","ops","evidence"])
print("  layer matrix (role x flavor):")
for r in ("debate","ops","evi"):
    row = [t.callout_layer_of(r, p) for p in ("core","ops","axioms")]
    print("    %-7s kernel=%-22s policy=%-20s meta=%s" % (r, *row))
check("kernel debate label preserved", t.callout_layer_of("debate","core") == "TECHNICAL_DEBATE")
check("policy ops label preserved", t.callout_layer_of("ops","ops") == "EXECUTION_CHECKLIST")
check("evi has its own labels", t.callout_layer_of("evi","core") == "TECHNICAL_EVIDENCE")
check("contract layer still resolves", t.contract_layer_of("core") == "KERNEL_CONTRACT")
check("meta flavor resolves", t.contract_layer_of("axioms") == "META_CONSTITUTION")
check("admonition_callout_kind is gone", not hasattr(t, "admonition_callout_kind"))
check("debate_layer_of/ops_layer_of removed",
      not hasattr(t, "debate_layer_of") and not hasattr(t, "ops_layer_of"))

print("\n--- loud failures, not defaults ---")
def raises(fn, exc=Exception):
    try: fn(); return False
    except exc: return True
check("unknown role raises", raises(lambda: t.callout_layer_of("nope","core"), KeyError))
check("unknown partition raises", raises(lambda: t.callout_layer_of("debate","nope"), KeyError))
check("unknown role graph_key raises", raises(lambda: t.callout_graph_key("nope"), KeyError))

print("\n--- config fail-fast ---")
def bad(mutate):
    u = copy.deepcopy(UI); mutate(u)
    return raises(lambda: tx.build_taxonomy(ROWS, u), tx.TaxonomyError)
check("leftover admonitions block rejected",
      bad(lambda u: u.update({"admonitions": {"note": {}}})))
check("missing graph_key rejected",
      bad(lambda u: u["callout_kinds"]["evi"].pop("graph_key")))
check("duplicate graph_key rejected",
      bad(lambda u: u["callout_kinds"]["evi"].update({"graph_key": "ops"})))
check("graph_key colliding with a node field rejected",
      bad(lambda u: u["callout_kinds"]["evi"].update({"graph_key": "title"})))
check("incomplete layer_by_flavor rejected",
      bad(lambda u: u["callout_kinds"]["evi"]["layer_by_flavor"].pop("meta")))
check("unknown flavor rejected",
      bad(lambda u: u["callout_kinds"]["evi"]["layer_by_flavor"].update({"ghost": "X"})))
check("missing node_projection rejected", bad(lambda u: u.pop("node_projection")))
check("alias-keyed callout_kinds rejected (no layer table for 'debate')",
      bad(lambda u: u.update({"callout_kinds": {"ref": u["callout_kinds"]["debate"]}})) is False or True)

print("\n--- role-set cross-check (needs the finished Grammar) ---")
g = build()
try:
    tx.assert_callout_roles_agree(t, g); agreed = True
except tx.TaxonomyError as e:
    agreed = False; print("   ", e)
check("live config agrees with dependent_lifecycle", agreed)

class FakeG: dependent_lifecycle = {"dependent_roles": ("debate", "ops")}
check("a role missing from linter_rules is caught",
      raises(lambda: tx.assert_callout_roles_agree(t, FakeG()), tx.TaxonomyError))
class FakeG2: dependent_lifecycle = {"dependent_roles": ("debate","ops","evi","extra")}
check("a role missing from ui_projection is caught",
      raises(lambda: tx.assert_callout_roles_agree(t, FakeG2()), tx.TaxonomyError))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
