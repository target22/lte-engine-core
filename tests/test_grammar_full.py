import sys, json, copy, yaml, types; sys.path.insert(0, "/home/claude/final")
from lte.engine.grammar import (Grammar, ConfigError, compile_grammar,
                                validate_callout_kinds, build_alternation,
                                build_domain_alternation,
                                assert_dependent_roles_producible,
                                assert_callout_tokens_producible)
fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

RULES = json.load(open("/home/claude/v4/linter_rules.json"))
TAX = yaml.safe_load(open("/mnt/user-data/uploads/taxonomy.yaml"))
DOMAINS = [p["domain"] for p in TAX["partitions"]]
class Layout:
    locales = ("vi", "en"); locales_suffixed = True; extensions = (".md",)

g = compile_grammar(RULES, DOMAINS, layout=Layout())
print("\n--- compiled ---")
print("  anchor_id       :", g.anchor_id)
print("  kind_alternation:", g.kind_alternation)
print("  composite_id    :", g.composite_id)
print("  ref_callout     :", g.ref_callout_start.pattern[:110] + "...")
print("  subblock_suffix :", g.subblock_suffix.pattern)
print("  status_directive:", g.status_directive.pattern)
print()

print("--- requirement 1: the six legacy attributes are gone ---")
LEGACY = ["admonition_open","admonition_indent","admonition_blank",
          "admonition_trailing_anchor","admonition_ref_line","admonition_status"]
for a in LEGACY:
    check("Grammar has no .%s" % a, not hasattr(g, a))

print("\n--- requirement 2: status_directive kept and working ---")
check("status_directive present", hasattr(g, "status_directive"))
check("matches a directive line", bool(g.status_directive.match("{status: deprecated}")))
check("captures the value",
      g.status_directive.match("{status: deprecated}").group("status") == "deprecated")
check("rejects prose", not g.status_directive.match("status: deprecated"))

print("\n--- requirement 3: the four placeholders expanded BEFORE compile ---")
check("no placeholder leaked into ref_callout_start", "{" not in g.ref_callout_start.pattern)
check("kind_alt expanded", "ref|ops|evi" in g.ref_callout_start.pattern)
check("composite_id expanded into the pattern", "debate|evi|ops" in g.ref_callout_start.pattern)
check("dependent_role_alt used by composite_id", "debate|evi|ops" in g.composite_id)
check("subblock_letter_alt expanded", "c|d|p|e" in g.subblock_suffix.pattern)
for ok, s in [(True,  "> [!ref-spec-lte-01-002] t"),
              (True,  "> [!evi-spec-lte-01-002#debate-d01] t"),
              (True,  "> [!ops-spec-lte-01-002-c01] t"),
              (False, "> [!nope-spec-lte-01-002] t"),
              (False, "> [!ref-hr-onboarding-15a1] t")]:
    got = bool(g.ref_callout_start.match(s))
    check("callout %-42r -> %s" % (s, ok), got == ok)

print("\n--- requirement 4: anchor_reference_pattern resolves composites ---")
arp = g.anchor_reference_pattern()
for ok, s in [(True, "^spec-lte-01-002"), (True, "spec-lte-01-002"),
              (True, "^spec-lte-01-002#debate-d01"), (True, "spec-lte-01-002#ops-p03"),
              (True, "^spec-lte-01-002-c01"),
              (False, "^spec-lte-01-002#ref-d01"), (False, "^hr-onboarding-15a1")]:
    check("superseded_by %-32r -> %s" % (s, ok), bool(arp.match(s)) == ok)

print("\n--- diagnostics compiled onto the Grammar ---")
for a in ("loose_anchor_line","loose_ref_line","loose_admonition_open","loose_table_row"):
    check("Grammar has .%s" % a, hasattr(g, a))
check("banned admonition detected", bool(g.loose_admonition_open.match('!!! note "T"')))
check("banned table row detected", bool(g.loose_table_row.match("| a | b |")))

print("\n--- ordering regression: the CI failure cannot recur silently ---")
bad = copy.deepcopy(RULES); bad.pop("composite_id_template")
try:
    compile_grammar(bad, DOMAINS, layout=Layout()); msg = None
except ConfigError as e: msg = str(e)
check("missing composite_id_template names itself", msg and "composite_id_template" in msg, str(msg)[:90])
bad = copy.deepcopy(RULES); bad["composite_id_template"] = "(?:{anchor_id}){nope}"
try:
    compile_grammar(bad, DOMAINS, layout=Layout()); msg = None
except ConfigError as e: msg = str(e)
check("a leak in composite_id_template is caught before patterns compile",
      msg and "composite_id_template still contains" in msg, str(msg)[:90])

print("\n--- config fail-fast (callout_kinds) ---")
def rejects(mutate):
    bad = copy.deepcopy(RULES); mutate(bad["callout_kinds"])
    try:
        compile_grammar(bad, DOMAINS, layout=Layout()); return False
    except ConfigError: return True
check("missing callout_kinds rejected",
      rejects(lambda b: b.clear()))
check("alias -> unknown role rejected",
      rejects(lambda b: b["aliases"].update({"zz": "nosuchrole"})))
check("alias containing '-' rejected",
      rejects(lambda b: b["aliases"].update({"re-f": "debate"})))
check("missing suffix letter rejected",
      rejects(lambda b: b["suffix_letters"].pop("evi")))
check("duplicate suffix letter rejected",
      rejects(lambda b: b["suffix_letters"].update({"evi": "d"})))
check("alphanumeric separator rejected",
      rejects(lambda b: b.update({"separator": "x"})))
check("suffix letter outside subblock_letters rejected",
      rejects(lambda b: b.update({"subblock_letters": ["c"]})))

print("\n--- cross-checks still fire ---")
assert_dependent_roles_producible(g); assert_callout_tokens_producible(g)
check("both post-hoc cross-checks pass on the live config", True)
bad_g = Grammar(**{**{f: getattr(g, f) for f in g.__dataclass_fields__},
                   "callout_kind_aliases": {"ref": "ghost"}})
try:
    assert_callout_tokens_producible(bad_g); caught = False
except ConfigError: caught = True
check("a hand-built Grammar with a ghost role is caught", caught)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
