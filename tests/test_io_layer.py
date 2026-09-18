import sys, types, pathlib, tempfile, shutil, atexit
from _harness import build
from lte.engine import ast_blocks

# stub the two engine modules corpus_reader imports beyond ast_blocks
fm = types.ModuleType("lte.engine.frontmatter")
fm.parse_text = lambda text, fence=None: {}
fm.metadata_from_text = lambda text, fence=None: {}
sys.modules["lte.engine.frontmatter"] = fm
import lte.engine as _e; _e.frontmatter = fm

from lte.io import corpus_reader as cr

g = build()

# The fixture corpus is CREATED HERE, not pointed at. It used to reference a
# scratch directory that existed on one machine; a test that depends on
# out-of-tree state is not a test, it is a coincidence.
ROOT = pathlib.Path(tempfile.mkdtemp(prefix="lte-io-"))
atexit.register(shutil.rmtree, ROOT, True)
P = ROOT / "docs/public/core/01-price.contract.en.md"
P.parent.mkdir(parents=True, exist_ok=True)
P.write_text("""---
status: "active"
---
# Pricing

## Price bounds
Bids stay inside the **published** band.

^spec-lte-01-002

> [!ref-spec-lte-01-002] Why not the median
> The mean keeps tail information.

> [!ops-spec-lte-01-002] Pre-deploy
> - [ ] verify band

> [!evi-spec-lte-01-002] Incident log
> Band held at 3 sigma.
""", encoding="utf-8")
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

import inspect
check("parse_file arity is 3", list(inspect.signature(cr.parse_file).parameters) == ["grammar","path","repo_root"],
      str(list(inspect.signature(cr.parse_file).parameters)))
check("parse_corpus deleted (no callers)", not hasattr(cr, "parse_corpus"))

nodes, callouts = cr.parse_file(g, P, ROOT)
print()
for n in nodes: print("     node    ^%s  %r" % (n.spec_id, n.title))
for c in callouts: print("     callout token=%-4s kind=%-7s node_id=%s" % (c.token, c.kind, c.node_id))
print()
check("one node parsed through the io layer", len(nodes) == 1)
check("three dependents parsed", len(callouts) == 3, str(len(callouts)))
check("composite keys survive the io layer",
      [c.node_id for c in callouts] == ["spec-lte-01-002#debate-d01",
                                        "spec-lte-01-002#ops-p01",
                                        "spec-lte-01-002#evi-e01"])
check("source_file is the repo-relative label",
      nodes[0].source_file == "docs/public/core/01-price.contract.en.md", nodes[0].source_file)

docs = list(cr.iter_documents(ROOT / "docs/public/core", ROOT, ["*.en.md"]))
check("iter_documents is the surviving discovery path", len(docs) == 1, str(len(docs)))

check("title falls back through document_id_from_name",
      cr.extract_document_title(g, P) == "Pricing")
check("no callout_kind_by_type left in any signature",
      "callout_kind_by_type" not in "".join(
          str(inspect.signature(getattr(cr, f)))
          for f in ("parse_file", "extract_document_title", "extract_prologue")))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
