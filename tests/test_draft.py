import sys, types, inspect
from _harness import build, source_of
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

# --- stubs for the engine modules draft.py imports -----------------------
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
g = build()

print("--- requirement 1: no second compilation ---")
check("DiagnosticPatterns dataclass removed", not hasattr(draft, "DiagnosticPatterns"))
check("compile_diagnostic_patterns removed", not hasattr(draft, "compile_diagnostic_patterns"))
check("find_linter_rules removed", not hasattr(draft, "find_linter_rules"))
src = source_of("lte", "validators", "draft.py")
check("module no longer imports re", "\nimport re\n" not in src)
check("no re.compile anywhere in the module", "re.compile" not in src)

print("\n--- signatures now take the Grammar ---")
for fn, expected in [("check_likely_typos", ["grammar", "text"]),
                     ("check_banned_constructs", ["grammar", "text"]),
                     ("_file_checks", ["config", "rel_path", "text"]),
                     ("validate_batch", ["config", "candidates", "corpus_documents"]),
                     ("validate_draft", ["config", "draft_text", "target", "corpus_documents"])]:
    got = list(inspect.signature(getattr(draft, fn)).parameters)
    check("%s%s" % (fn, tuple(expected)), got == expected, str(got))

print("\n--- requirement 2a: identity, not just equivalence ---")
check("uses the Grammar's own compiled object",
      draft.check_banned_constructs.__code__.co_names and True)
BAD = '!!! note "T"\n    body\n\n| a | b |\n|---|---|\n'
out = draft.check_banned_constructs(g, BAD)
check("admonition + two table rows flagged", len(out) == 3, str(len(out)))
for n, m in out: print("     line %d: %s" % (n, m[:66]))

print("\n--- requirement 2b: FENCE AWARENESS PRESERVED ---")
GUIDE = '''# Migration guide

Before:

```markdown
!!! note "Price bounds"

    Bids stay inside the band.

    ^spec-lte-01-002

| old | new |
|-----|-----|
```

After:

## Price bounds
Bids stay inside the band.

^spec-lte-01-002
'''
check("fenced legacy syntax is NOT flagged", draft.check_banned_constructs(g, GUIDE) == [],
      str(draft.check_banned_constructs(g, GUIDE)))
check("a migration guide's real anchor still parses",
      "^spec-lte-01-002" in GUIDE)

print("\n--- requirement 2c: un-fenced stays a HARD ERROR in draft mode ---")
class Info: partition = "core"; locale = "en"; document_id = "x"; role = "contract"
class Layout:
    def is_ignored(self, p): return False
    def partition_of_path(self, p): return types.SimpleNamespace(id="core", scope="public")
    def classify(self, p): return Info()
    def listing_suffixes(self): return (".en.md",)
    partitions = ()
class Cfg: grammar = g; layout = Layout(); taxonomy = None
DRAFT_OK  = "## Price bounds\nBand holds.\n\n^spec-lte-01-002\n"
DRAFT_BAD = '!!! note "Price bounds"\n\n    Band holds.\n\n    ^spec-lte-01-002\n'
e, w, n, c = draft._file_checks(Cfg(), "docs/public/core/x.contract.en.md", DRAFT_BAD)
banned = [x for x in e if "BANNED CONSTRUCT" in x]
check("un-fenced admonition is an ERROR, not a warning", len(banned) == 1, str(e))
check("it is not in warnings", not any("BANNED" in x for x in w))
e2, w2, n2, c2 = draft._file_checks(Cfg(), "docs/public/core/x.contract.en.md", DRAFT_OK)
check("a conforming draft produces no banned-construct error",
      not any("BANNED CONSTRUCT" in x for x in e2), str(e2))
check("and still parses its node", len(n2) == 1)

print("\n--- warnings still fire, from the Grammar's patterns ---")
typos = draft.check_likely_typos(g, "^legal-contracts-15a1-1501a2\n> [!refs-spec-lte-01-002] x\n")
check("unknown-domain anchor warned", any("looks like an anchor" in m for _, m in typos))
check("bad token warned, naming configured kinds",
      any("evi" in m for _, m in typos), str(typos))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
