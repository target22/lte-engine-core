import sys, functools, types; from _harness import REPO, build
from lte.engine import ast_blocks, integrity, graph_builder

g = build()
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

# ---- minimal collaborators (real code under test: graph_builder, integrity) ----
DEP_ROLES = ("debate", "ops", "evi")
GRAPH_KEY = {"debate": "debates", "ops": "ops", "evi": "evidence"}
class Tax:
    dependent_roles = DEP_ROLES
    labels = {"core": "Core"}; codeowners = {"core": "@core"}; partition_scope = {"core": "public"}
    def contract_layer_of(self, pid): return "KERNEL_CONTRACT"
    def callout_graph_key(self, role): return GRAPH_KEY[role]
    def callout_layer_of(self, role, pid):
        return {"debate": "TECHNICAL_DEBATE", "ops": "OPERATIONAL_EXTENSIONS",
                "evi": "EVIDENCE_LOG"}[role]
Info = types.SimpleNamespace
class Layout:
    locales = ("en",)
    def classify(self, rel):
        doc_id = rel.split("/")[-1].split(".")[0]
        return Info(locale="en", partition="core", document_id=doc_id,
                    tractate_path="", role=None, group_key=("core", (), doc_id))
    def partitions_for_target(self, s): return ["core"]
    def emitted_partitions_for_target(self, s): return ["core"]

MD = """# Pricing

## Price bounds
Bids stay inside the band.

^spec-lte-01-002

## Clause one
Detail.

^spec-lte-01-002-c01

> [!ref-spec-lte-01-002] Why not the median
> Mean keeps tails.

> [!ops-spec-lte-01-002] Pre-deploy
> - [ ] verify

> [!evi-spec-lte-01-002] Incident log
> Held at 3 sigma.

> [!ref-spec-lte-01-002#debate-d01] Rebuttal to the median debate
> Targets a composite id.

> [!ref-spec-lte-01-002-c01] On clause one
> Targets an authored sub-anchor.
"""
REL = "docs/public/core/01-price.contract.en.md"
diagnostics = []
graph = graph_builder.build_lang_graph(
    [(REL, MD)], "en", "public",
    layout=Layout(),
    parse_text=functools.partial(ast_blocks.parse_text, g),
    parent_anchor_of=functools.partial(integrity.parent_anchor_of, g),
    render=lambda md: md, summarize=lambda h: h[:40],
    metadata_from_text=lambda t: {"status": "active"},
    normalize_status=lambda r: r,
    resolve_dependent_status=lambda c, d: d or c,
    title_from_text=lambda rel, t: "Pricing",
    prologue_from_text=lambda t: "",
    find_duplicate_spec_ids=integrity.find_duplicate_spec_ids,
    find_misplaced_anchors=lambda nodes: [],
    find_orphan_refs=integrity.find_orphan_refs,
    find_cross_boundary_refs=lambda n, c: [],
    taxonomy=Tax(), status_values=("draft","pending","active","deprecated","archived"),
    document_roles=("contract","axiom","debate","ops"), diagnostics=diagnostics)

nodes = {n["anchor_id"]: n for p in graph["partitions"] for d in p["documents"] for n in d["nodes"]}
print()
for aid, n in sorted(nodes.items()):
    print("  node %-24s parent=%-18s debates=%d ops=%d evidence=%d"
          % (aid, n["parent_anchor"], len(n["debates"]), len(n["ops"]), len(n["evidence"])))
print()
check("no orphan diagnostics emitted", not [d for d in diagnostics if d["code"] == "orphan_ref"],
      str([d["message"] for d in diagnostics if d["code"] == "orphan_ref"]))
check("three roles route to three distinct arrays",
      all(k in nodes["spec-lte-01-002"] for k in ("debates", "ops", "evidence")))
check("evi no longer swallowed into ops", len(nodes["spec-lte-01-002"]["evidence"]) == 1)
check("ops array holds only the ops callout", len(nodes["spec-lte-01-002"]["ops"]) == 1)
check("evi layer is its own", nodes["spec-lte-01-002"]["evidence"][0]["layer"] == "EVIDENCE_LOG")
check("authored sub-anchor gets a parent edge",
      nodes["spec-lte-01-002-c01"]["parent_anchor"] == "spec-lte-01-002")
check("top-level contract has no parent", nodes["spec-lte-01-002"]["parent_anchor"] is None)

deb = nodes["spec-lte-01-002"]["debates"]
check("composite-targeting ref resolved to the contract", len(deb) == 2, str(len(deb)))
check("precise composite target preserved",
      any(e["targets"] == "spec-lte-01-002#debate-d01" for e in deb))
check("sub-anchor ref attached to the sub-anchor node",
      len(nodes["spec-lte-01-002-c01"]["debates"]) == 1)
check("sub-anchor ref kept its exact target",
      nodes["spec-lte-01-002-c01"]["debates"][0]["targets"] == "spec-lte-01-002-c01")

idx = graph["anchor_index"]
check("composite child id is indexed", "spec-lte-01-002#debate-d01" in idx)
check("indexed child names its parent",
      idx.get("spec-lte-01-002#debate-d01", {}).get("parent_anchor") == "spec-lte-01-002")
check("indexed sub-anchor names its parent",
      idx.get("spec-lte-01-002-c01", {}).get("parent_anchor") == "spec-lte-01-002")

print("\n--- negative: no invented parents ---")
known = {"spec-lte-01-002": 1}
check("unknown-prefix sub-anchor stays top-level",
      integrity.parent_anchor_of(g, "spec-other-99-001-c01", known) is None)
check("undeclared suffix letter is not a suffix",
      integrity.parent_anchor_of(g, "spec-lte-01-002-x99", known) is None)
check("declared suffix with a real prefix resolves",
      integrity.parent_anchor_of(g, "spec-lte-01-002-c01", known) == "spec-lte-01-002")
check("composite key splits on the separator",
      integrity.parent_anchor_of(g, "spec-lte-01-002#ops-p01", known) == "spec-lte-01-002")

print("\n--- orphan still fires for a genuinely missing target ---")
n2, c2 = ast_blocks.parse_text(g, REL, "> [!ref-spec-nope-01-001] x\n> y\n")
check("truly missing anchor is an orphan", len(integrity.find_orphan_refs([], c2)) == 1)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
