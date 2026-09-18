import sys, re, types
import ast as _ast
from _harness import build, raw_config, source_of
from lte.engine import ast_blocks
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

g = build()
rules = raw_config()["linter_rules"]

# ---- stub the engine modules draft.py imports -------------------------
for name, attrs in [("lte.engine.frontmatter", {"parse_text": lambda t, f=None: {}}),
                    ("lte.engine.state_machine",
                     {"is_dependent_document": lambda *a, **k: False,
                      "check_dependent_lifecycle": lambda *a, **k: []})]:
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m
lay = types.ModuleType("lte.engine.layout")
lay.normalize_rel = lambda p: p
lay.CorpusLayout = type("CorpusLayout", (), {}); lay.LayoutError = type("LayoutError", (Exception,), {})
lay.PUBLIC_EXPOSURE = "public"
sys.modules["lte.engine.layout"] = lay
import lte.engine as _e
_e.frontmatter = sys.modules["lte.engine.frontmatter"]
_e.state_machine = sys.modules["lte.engine.state_machine"]

from lte.validators import draft

print("\n--- draft.py: check_likely_typos no longer touches deleted attrs ---")
TYPO = "^legal-contracts-15a1-1501a2\n> [!refs-spec-lte-01-002] typo\n"
try:
    out = draft.check_likely_typos(g, TYPO); raised = None
except AttributeError as e:
    out = []; raised = e
check("no AttributeError on a deleted Grammar attribute", raised is None, str(raised))
check("unknown-domain anchor still warned", any("looks like an anchor" in m for _, m in out))
check("bad token warned and lists configured kinds",
      any("looks like a dependent block" in m and "ref, ops, evi".replace(", ", "") 
          or "evi" in m for _, m in out), str(out))
for n, m in out: print("     line %d: %s" % (n, m[:80]))

print("\n--- draft.py: banned constructs are ERRORS and fence-aware ---")
BAD = '!!! note "T"\n    body\n\n| a | b |\n|---|---|\n'
FENCED = '```\n!!! note "shown as a migration example"\n| a | b |\n```\n\n## Real\ntext\n\n^spec-lte-01-002\n'
# (the deeper draft assertions now live in test_draft.py)
bc = draft.check_banned_constructs(g, BAD)
check("admonition + table rows flagged", len(bc) == 3, str(len(bc)))
check("fenced legacy syntax is NOT flagged", draft.check_banned_constructs(g, FENCED) == [])
check("check_truncated_admonitions is gone", not hasattr(draft, "check_truncated_admonitions"))

print("\n--- draft.py: parse_text arity + _label ---")
src = source_of("lte", "validators", "draft.py")
check("no admonition_callout_kind left", "admonition_callout_kind" not in src)
check("no grammar.admonition_* attribute access left",
      not re.search(r"grammar\.admonition_[a-z_]+\.", src))
n, c = ast_blocks.parse_text(g, "docs/public/core/x.contract.en.md",
                             "## X\nt\n\n^spec-lte-01-002\n\n> [!evi-spec-lte-01-002] log\n> b\n")
check("_label uses the authored token", draft._label(c[0]) == "[!evi-spec-lte-01-002]", draft._label(c[0]))

print("\n--- compile.py: binding + role-blind summary ---")
csrc = source_of("lte", "cli", "compile.py")
check("parse_text bound with grammar only",
      'functools.partial(ast_blocks.parse_text, grammar),' in csrc)
check("parent_anchor_of is now injected", '"parent_anchor_of": functools.partial' in csrc)
check("no admonition_callout_kind left", "admonition_callout_kind" not in csrc)
# AST, not grep: my own docstring quotes the old expression verbatim.
_fn = [n for n in _ast.walk(_ast.parse(csrc))
       if isinstance(n, _ast.FunctionDef) and n.name == "_summarize_lang"][0]
_body = _ast.dump(_ast.Module(body=_fn.body[1:], type_ignores=[]))
check("summary body indexes no role by name",
      "'debates'" not in _body and "'ops'" not in _body)

mod = types.ModuleType("cmp"); exec(compile(csrc.split("def _summarize_lang")[1].join(
    ["def _summarize_lang", ""]).split("\ndef main")[0], "s", "exec"),
    {"TAG": "[compile]", "graph_builder": types.SimpleNamespace(total_nodes=lambda g: 1)}, mod.__dict__)
class Tax:
    dependent_roles = ("debate", "ops", "evi")
    def callout_graph_key(self, r): return {"debate":"debates","ops":"ops","evi":"evidence"}[r]
G = {"partitions": [{"id": "core", "documents": [{"node_count": 1, "nodes": [
    {"debates": [1, 2], "ops": [1], "evidence": [1, 2, 3]}]}]}]}
line = mod._summarize_lang("public", "en", G, Tax())
print("     %s" % line)
check("summary counts every declared role",
      "2 debates" in line and "1 ops" in line and "3 evidence" in line, line)

print("\n--- pre_commit.py ---")
psrc = source_of("lte", "cli", "hooks", "pre_commit.py")
check("no admonition_callout_kind left", "admonition_callout_kind" not in psrc)
check("no 'kinds' argument left in payload_lock calls",
      not re.search(r"payload_lock\.\w+\(grammar, kinds", psrc))
# The host-half docstring legitimately NAMES lte.engine while forbidding it.
_top = []
for _n in _ast.parse(psrc).body:
    if isinstance(_n, _ast.Import):
        _top += [a.name for a in _n.names]
    elif isinstance(_n, _ast.ImportFrom):
        _top.append(_n.module or "")
check("host half imports no lte.* at module level",
      not any(m.startswith("lte") for m in _top), str(_top))

print("\n--- graph_builder.py: docstring only, routing already correct ---")
gsrc = source_of("lte", "engine", "graph_builder.py")
check("contract lists the new taxonomy surface", "callout_layer_of(role, pid)" in gsrc)
check("contract no longer promises deleted methods",
      "debate_layer_of(pid), ops_layer_of(pid)" not in gsrc)
check("live routing was already role-driven",
      "taxonomy.callout_graph_key(callout.kind)" in gsrc)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
