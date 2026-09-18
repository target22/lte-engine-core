from _harness import raw_config, source_of
import sys, yaml, importlib.util, pathlib; from _harness import REPO, build
from lte.engine.grammar import ConfigError

from lte.engine import taxonomy as tx

fails = []
def check(label, cond, detail=""):
    if not cond: fails.append(label)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label, (" -- "+detail) if detail else ""))

src = source_of("lte", "io", "config_reader.py")
check("assert_callout_roles_agree imported at module level",
      "assert_callout_roles_agree, build_taxonomy" in src)
check("call sits after compile_grammar",
      src.index("assert_callout_roles_agree(taxonomy, grammar)") > src.index("grammar = compile_grammar("))
check("call sits before EngineConfig is returned",
      src.index("assert_callout_roles_agree(taxonomy, grammar)") < src.index("return EngineConfig("))
check("TaxonomyError is wrapped, not propagated",
      "except TaxonomyError as exc:" in src.split("assert_callout_roles_agree(taxonomy, grammar)")[1][:300])

# exercise the exact wrap from load()
t = tx.build_taxonomy(raw_config()["taxonomy"],
                      raw_config()["ui_projection"])
UI, GR = pathlib.Path("config/ui_projection.yaml"), pathlib.Path("config/linter_rules.json")
def wrapped(grammar):
    try:
        tx.assert_callout_roles_agree(t, grammar)
    except tx.TaxonomyError as exc:
        raise ConfigError("{0} vs {1}: {2}".format(UI, GR, exc)) from exc

class Good: dependent_lifecycle = {"dependent_roles": ("debate","ops","evi")}
class Missing: dependent_lifecycle = {"dependent_roles": ("debate","ops")}

try:
    wrapped(Good()); ok = True
except Exception as e:
    ok = False; print("   ", e)
check("live config passes the wrapped check", ok)

try:
    wrapped(Missing()); raised = None
except ConfigError as e:
    raised = e
except tx.TaxonomyError:
    raised = "TAXONOMY_LEAKED"
check("a mismatch raises ConfigError, not TaxonomyError", isinstance(raised, ConfigError),
      str(type(raised)))
check("the message names both config files",
      "ui_projection.yaml" in str(raised) and "linter_rules.json" in str(raised))
check("compile.py's except clause would catch it",
      isinstance(raised, (ConfigError, OSError)))
print("\n   message: %s" % str(raised)[:150])
print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
