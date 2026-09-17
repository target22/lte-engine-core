# -*- coding: utf-8 -*-
"""Verification suite for Batch 2: lte/io/ and lte/validators/."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from lte.io import build_cache, config_reader, corpus_reader
from lte.validators import bilingual, corpus as corpus_validator

P=[0]; F=[0]
def check(l, c, x=""):
    if c: P[0]+=1; print("  PASS  {0}".format(l))
    else: F[0]+=1; print("  FAIL  {0} {1}".format(l, x))

ROOT = Path(".").resolve()
cfg = config_reader.load(repo_root=ROOT)
g, tax = cfg.grammar, cfg.taxonomy
KINDS = tax.admonition_callout_kind
CONTRACT = ROOT/"docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md"
OPS      = ROOT/"docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md"

print("io/config_reader -- the read half of legacy config_loader")
check("loads taxonomy + grammar together", len(tax.partitions)==8 and len(g.status_values)==5)
check("SRKH_CONFIG_DIR is honored",
      config_reader.resolve_config_dir(ROOT).name in ("config",))
raw = config_reader.read_raw(repo_root=ROOT)
check("read_raw exposes unvalidated mappings for testing",
      isinstance(raw["linter_rules"], dict) and "patterns" in raw["linter_rules"])
bad = json.loads(json.dumps(raw["linter_rules"])); bad["config_version"]="9.9.9"
from lte.engine.grammar import ConfigError, compile_grammar
try:
    compile_grammar(bad, list(tax.known_domains)); check("bad config_version raises", False)
except ConfigError: check("bad config_version raises at load, not lazily", True)

print("\nio/corpus_reader -- the exclusive disk boundary")
fm = corpus_reader.extract_frontmatter(OPS, g)
check("extract_frontmatter returns RAW keys (archived_reason survives)",
      fm.get("archived_reason")=="orphaned_dependency")
md = corpus_reader.document_metadata_of(OPS, g)
check("document_metadata_of DROPS archived_reason (the documented trap)",
      "archived_reason" not in md and md["status"]=="archived")
nodes, callouts = corpus_reader.parse_file(g, KINDS, CONTRACT, ROOT)
check("parse_file finds both contracts", len(nodes)==2)
check("source_file is the repo-relative POSIX label",
      nodes[0].source_file=="docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md")
check("extract_document_title reads the true H1",
      corpus_reader.extract_document_title(g, CONTRACT)=="State Resolution Kernel")
check("extract_prologue stops at the first block",
      corpus_reader.extract_prologue(g, CONTRACT)=="Prologue prose.")
n2, c2 = corpus_reader.parse_corpus(g, KINDS, ROOT/"docs", ROOT, "*.en.md")
check("parse_corpus walks the whole tree", len(n2)==2 and len(c2)==2)
check("discover_files is SORTED (hash stability across machines)",
      corpus_reader.discover_files(ROOT/"docs","*.md")
      == sorted(corpus_reader.discover_files(ROOT/"docs","*.md")))
docs = list(corpus_reader.iter_documents(ROOT/"docs", ROOT, ("*.en.md",)))
check("iter_documents yields (rel_path, text) SourceProvider pairs",
      len(docs)==3 and all(isinstance(a,str) and isinstance(b,str) for a,b in docs))
# undecodable file -> raises, does NOT silently compile to zero nodes
bad_bytes = ROOT/"docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md"
bad_bytes.write_bytes(b"\xff\xfe\x00bad")
try:
    corpus_reader.read_document_text(bad_bytes); check("undecodable file raises", False)
except corpus_reader.CorpusReadError: check("undecodable file raises, not silently empty", True)
bad_bytes.unlink()
check("relative_label falls back for a path outside the repo",
      corpus_reader.relative_label(Path("/tmp/x.md"), ROOT)=="/tmp/x.md")

print("\nio/build_cache")
check("CACHE_VERSION held at 2 (parser output verified identical in Batch 1)",
      build_cache.CACHE_VERSION==2)
check("hash is over BYTES", build_cache.compute_file_hash(CONTRACT)
      == __import__("hashlib").sha256(CONTRACT.read_bytes()).hexdigest())
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    check("missing cache -> fresh", build_cache.load_build_cache(tmp)=={"version":2,"files":{}})
    (tmp/".cache").mkdir(); (tmp/".cache/build_state.json").write_text("{ truncated")
    check("corrupt cache -> fresh, no raise", build_cache.load_build_cache(tmp)["files"]=={})
    (tmp/".cache/build_state.json").write_text(json.dumps({"version":1,"files":{"a":{}}}))
    check("stale version -> fresh (never deserialize a foreign shape)",
          build_cache.load_build_cache(tmp)["files"]=={})
    c = build_cache.empty_cache()
    n,cl,hit = build_cache.cached_or_parse(g, KINDS, CONTRACT, ROOT, c)
    check("first parse is a miss", hit is False and len(n)==2)
    n,cl,hit = build_cache.cached_or_parse(g, KINDS, CONTRACT, ROOT, c)
    check("second is a hit and round-trips SpecNode", hit is True and len(n)==2)
    check("round-tripped node equals freshly parsed",
          [(x.spec_id,x.title,x.content,x.status_override) for x in n]
          == [(x.spec_id,x.title,x.content,x.status_override) for x in nodes])
    build_cache.save_build_cache(tmp, c)
    check("save writes atomically and reloads", build_cache.load_build_cache(tmp)["files"].keys()==c["files"].keys())
    check("no stray .tmp files left behind",
          [p.name for p in (tmp/".cache").iterdir() if p.suffix==".tmp"]==[])
    removed = build_cache.prune_missing(c, [])
    check("prune_missing drops orphaned entries", removed==1 and c["files"]=={})

print("\nvalidators/corpus")
out=[]
rc = corpus_validator.run(ROOT, config=cfg, emit=out.append)
check("clean corpus exits 0", rc==0)
check("reports the locale set from config", any("vi and en" in l for l in out))
# inject a lifecycle violation
OPS_BAK = OPS.read_text()
OPS.write_text(OPS_BAK.replace("archived_reason: orphaned_dependency",
                               "archived_reason: we just felt like it"))
out=[]; rc = corpus_validator.run(ROOT, config=cfg, emit=out.append)
check("free-text archived_reason fails the build", rc==1)
check("diagnostic names the closed tag set", any("closed tag set" in l for l in out))
out=[]; rc = corpus_validator.run(ROOT, config=cfg, soft=True, emit=out.append)
check("--soft reports but exits 0", rc==0 and any("SOFT MODE" in l for l in out))
out=[]; rc = corpus_validator.run(ROOT, config=cfg, target=CONTRACT, emit=out.append)
check("--file scopes PRINTING but not checking (still exits 1)", rc==1)
check("--file says issues remain elsewhere", any("remain elsewhere" in l for l in out))
OPS.write_text(OPS_BAK)
# orphan ref
ORPH = ROOT/"docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md"
ORPH_BAK = ORPH.read_text()
ORPH.write_text(ORPH_BAK.replace("spec-lte-01-001","spec-lte-99-999"))
out=[]; rc = corpus_validator.run(ROOT, config=cfg, emit=out.append)
check("orphan ref fails the build", rc==1 and any("no matching" in l for l in out))
ORPH.write_text(ORPH_BAK)
# fence parity
FEN = ROOT/"docs/public/adr/platform-infrastructure-audit/01-fact-check-baseline.en.md"
FEN.write_text("# T\n\n```python\nunclosed\n")
out=[]; rc = corpus_validator.run(ROOT, config=cfg, emit=out.append)
check("unclosed fence fails the build", rc==1 and any("unclosed" in l for l in out))
FEN.unlink()
out=[]; rc = corpus_validator.run(ROOT, config=cfg, emit=out.append)
check("corpus clean again after cleanup", rc==0)

print("\nvalidators/bilingual")
GRAPH = {"vi":{"partitions":[{"id":"core","documents":[
            {"document_id":"01-a","node_count":2,"nodes":[{"anchor_id":"spec-a-1-1"},{"anchor_id":"spec-a-1-2"}]}]}]},
         "en":{"partitions":[{"id":"core","documents":[
            {"document_id":"01-a","node_count":1,"nodes":[{"anchor_id":"spec-a-1-1"}]},
            {"document_id":"01-b","node_count":1,"nodes":[{"anchor_id":"spec-b-1-1"}]}]}]}}
rows, details = bilingual.compare(GRAPH)
check("compare() is pure over a parsed graph", len(rows)==1 and len(details)==1)
pid, ovd, oed, mism, ova, oea = details[0]
check("detects en-only document", oed==["01-b"])
check("detects sibling pair disagreement (the stronger signal)",
      mism==[("01-a",2,1)])
check("detects vi-only anchor", ova==["spec-a-1-2"])
gp = ROOT/"_g.json"; gp.write_text(json.dumps(GRAPH))
out=[]; check("report-only exits 0 by default", bilingual.run(gp, emit=out.append)==0)
check("--strict exits 1", bilingual.run(gp, strict=True, emit=lambda x: None)==1)
same = {"vi":GRAPH["vi"], "en":GRAPH["vi"]}
gp.write_text(json.dumps(same))
out=[]; check("full parity reports parity", bilingual.run(gp, emit=out.append)==0
              and any("full parity" in l for l in out))
gp.unlink()

print("\nscripts/core_pipeline/graph_lib.py shim -- legacy arity preserved")
r = subprocess.run([sys.executable, "-c", """
import sys; sys.path.insert(0,'scripts/core_pipeline/')
from pathlib import Path
import graph_lib as gl
assert len(gl.PARTITIONS)==8, gl.PARTITIONS
assert gl.PARTITIONS[0][0]=='core'
assert gl.KNOWN_DOMAINS[0]=='spec'
assert gl.GRAPH_SCHEMA_VERSION=='1.0.0'
assert gl.CACHE_VERSION==2
assert gl.SPEC_ANCHOR_RE.match('^spec-lte-01-001')
p=Path('docs/public/ops/dep/02-rollout.ops.en.md')
assert gl.extract_frontmatter(p)['archived_reason']=='orphaned_dependency'
assert 'archived_reason' not in gl.document_metadata_of(p)
n,c = gl.parse_file(Path('docs/public/core/sdc-spec-engine/01-ingress-boundary.vi.md'), Path('.'))
assert len(n)==2, n
assert gl.extract_document_title(Path('docs/public/core/sdc-spec-engine/01-ingress-boundary.vi.md'))=='State Resolution Kernel'
assert gl.extract_prologue(Path('docs/public/core/sdc-spec-engine/01-ingress-boundary.vi.md'))=='Prologue prose.'
cache = gl.load_build_cache(Path('.'))
nn,cc,hit = gl.cached_or_parse(Path('docs/public/core/sdc-spec-engine/01-ingress-boundary.vi.md'), Path('.'), cache)
assert len(nn)==2
assert gl.find_orphan_refs(n,c)==[]
assert gl.find_misplaced_anchors(n)==[]
assert gl.find_cross_boundary_refs(n,c)==[]
assert gl.check_dependent_lifecycle('a/x.ops.en.md',{'status':'archived'})
assert gl.resolve_dependent_status('deprecated','active')=='invalidated'
assert gl.contract_layer_of('core')=='KERNEL_CONTRACT'
assert gl.document_id_of(Path('01-x.contract.en.md'))=='01-x'
assert gl.INVALIDATED_STATE=='invalidated'
print('SHIM_OK')
"""], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
check("all legacy names resolve at legacy arity", b"SHIM_OK" in r.stdout,
      r.stderr.decode()[-400:])

print("\n  {0} passed, {1} failed".format(P[0], F[0]))
sys.exit(1 if F[0] else 0)
