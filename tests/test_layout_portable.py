"""
tests/test_layout_portable.py -- corpus-layout integration test.

Loads the repository's own config/ and the two example deployments under
config/examples/ through lte.io.config_reader.load(), then checks layout
classification, grammar expansion, taxonomy delegation, integrity rules and
dependent-role resolution against each of them. No corpus, no git.

Run from the repository root (inside the lte-cas container):

    python tests/test_layout_portable.py

Exit code 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import copy
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from lte.engine import integrity, state_machine  # noqa: E402
from lte.engine.grammar import ConfigError, compile_grammar  # noqa: E402
from lte.engine.layout import LayoutError, compile_glob, compile_layout  # noqa: E402
from lte.io import config_reader  # noqa: E402

CFG = ROOT / "config"
EXAMPLES = CFG / "examples"
RESULTS = {"pass": 0, "fail": 0}

# The split_document pattern the engine compiled before locales and
# extensions moved into corpus_layout.yaml. The default layout must still
# produce exactly these bytes.
LEGACY_SPLIT_DOCUMENT = r"^(?P<slug>.+)\.(?P<role>contract|debate|ops)\.(?P<lang>vi|en)\.md$"


def check(label, cond, extra=""):
    RESULTS["pass" if cond else "fail"] += 1
    if not cond:
        print("FAIL %s %s" % (label, extra))


def load_deployment(directory: Path, workdir: Path):
    """config_reader.load() over an example directory plus the repo's
    linter_rules.json, copied into a scratch config dir."""
    target = workdir / directory.name
    target.mkdir()
    for name in ("taxonomy.yaml", "ui_projection.yaml", "corpus_layout.yaml"):
        shutil.copy(directory / name, target / name)
    shutil.copy(CFG / "linter_rules.json", target / "linter_rules.json")
    return config_reader.load(config_dir=target, repo_root=ROOT)


def node(spec_id, source_file):
    return SimpleNamespace(spec_id=spec_id, source_file=source_file, line_no=1)


def callout(target_id, source_file):
    return SimpleNamespace(target_id=target_id, source_file=source_file, line_no=1, kind="ref")


work = Path(tempfile.mkdtemp(prefix="lte-layout-test-"))
DEFAULT = config_reader.load(config_dir=CFG, repo_root=ROOT)
A = load_deployment(EXAMPLES / "governance-os", work)
B = load_deployment(EXAMPLES / "personal-os", work)

# ---- default layout: the conventions the engine had before the layout ------
L, G, T = DEFAULT.layout, DEFAULT.grammar, DEFAULT.taxonomy
check("default split_document bytes", G.split_document.pattern == LEGACY_SPLIT_DOCUMENT,
      G.split_document.pattern)
check("default grammar locales", G.file_naming_locales == L.locales == ("vi", "en"))
expected = {
    "docs/public/core/lte/01-pricing.contract.en.md": ("core", "en", "contract", "01-pricing", ("lte",)),
    "docs/public/core/lte/01-pricing.debate.vi.md": ("core", "vi", "debate", "01-pricing", ("lte",)),
    "docs/public/core/legacy.en.md": ("core", "en", None, "legacy", ()),
    "docs/public/core/x/01-x.evi.en.md": ("core", "en", None, "01-x.evi", ("x",)),
    "docs/private/core-internal/sec/a/b.en.md": ("core-internal", "en", None, "b", ("sec", "a")),
    "docs/public/core/README.md": None,
    "docs/public/core/x.fr.md": None,
    "docs/private/core/stray.en.md": None,
}
for rel, want in expected.items():
    info = L.classify(rel)
    got = None if info is None else (info.partition, info.locale, info.role, info.document_id, info.tractate)
    check("default classify %s" % rel, got == want, (got, want))
public_rows = [p for p in T.partitions if p[4] == L.target("public").scopes[0]]
check("default public rows", T.partitions_for_scope("public", layout=L) == public_rows)
check("default internal rows", T.partitions_for_scope("all", layout=L) == list(T.partitions))
check("default partition_dir", T.partition_dir(Path("/r"), "core-internal", layout=L)
      == Path("/r/docs/private/core-internal"))
check("default scope_of_source_file", T.scope_of_source_file("docs/private/axioms/x.en.md", layout=L) == "private")
check("default leak prefixes", L.restricted_prefixes("public") == ("docs/private/",))
check("default artifacts", (L.artifact_path("public"), L.artifact_path("all")) ==
      ("public/data/graph.json", "public/internal-data/graph-internal.json"))
check("default naming ok", integrity.check_file_naming(L, G, "docs/public/core/01-x.debate.en.md") == [])
check("default naming locale", "locale segment 'fr'" in
      integrity.check_file_naming(L, G, "docs/public/core/01-x.fr.md")[0]["message"])
check("default naming role typo", "role segment 'contarct'" in
      integrity.check_file_naming(L, G, "docs/public/core/01-x.contarct.en.md")[0]["message"])
nodes = [node("coreint-sec-001", "docs/private/core-internal/sec.en.md"),
         node("adr-mis-001", "docs/public/core/mis.en.md"),
         node("spec-stray-001", "docs/private/core/stray.en.md")]
calls = [callout("coreint-sec-001", "docs/public/core/xb.en.md"),
         callout("spec-x-001", "docs/private/core-internal/sec.en.md")]
check("default boundary", [c.source_file for c in integrity.find_cross_boundary_refs(L, T, nodes, calls)]
      == ["docs/public/core/xb.en.md"])
check("default misplaced", [(n.spec_id, e, a) for n, e, a in integrity.find_misplaced_anchors(L, T, nodes)] == [
    ("adr-mis-001", "docs/public/adr", "docs/public/core"),
    ("spec-stray-001", "docs/public/core", "docs/private/core")])
check("default evi role", state_machine.dependent_role_of(G, "docs/public/core/x.evi.en.md", layout=L) == "evi")

# ---- Deployment A: paired directories ---------------------------------------
LA, GA, TA = A.layout, A.grammar, A.taxonomy
pol = LA.classify("governance-os/left-pane/hr/leave.en.md")
deb = LA.classify("governance-os/right-pane/hr/leave.en.md")
check("A pairing", pol.group_key == deb.group_key == ("policy", ("hr",), "leave"))
check("A default role", deb.role == "debate")
check("A emitted", LA.emitted_partitions_for_target("public") == ["policy"])
check("A dependent via default_role",
      state_machine.dependent_role_of(GA, "governance-os/right-pane/hr/leave.en.md", layout=LA) == "debate")
check("A no layout -> filename only",
      state_machine.dependent_role_of(GA, "governance-os/right-pane/hr/leave.en.md") is None)
check("A lifecycle via default_role", state_machine.check_dependent_lifecycle(
    GA, "governance-os/right-pane/hr/leave.en.md", {"status": "archived", "archived_reason": "nope"},
    layout=LA) != [])
check("A readme ignored", LA.classify("governance-os/left-pane/README.en.md") is None)
check("A locales order", GA.file_naming_locales == ("en", "vi"))

# ---- Deployment B: no locale suffix, directory-less scopes ------------------
LB, GB, TB = B.layout, B.grammar, B.taxonomy
v = LB.classify("personal-os/01_Philosophy/ethics/values.md")
check("B unsuffixed", (v.locale, v.document_id, v.tractate) == ("en", "values", ("ethics",)))
check("B role segment", LB.classify("personal-os/01_Philosophy/ethics/values.debate.md").role == "debate")
check("B .markdown", LB.classify("personal-os/02_Infrastructure/stack.markdown") is not None)
check("B grammar none-mode locale group", GB.file_naming.match("values.md").group("locale") == "")
check("B grammar split", GB.split_document.match("values.debate.markdown").group("slug") == "values")
check("B naming ok", integrity.check_file_naming(LB, GB, "personal-os/01_Philosophy/values.md") == [])
for ignored in ("personal-os/00_Inbox-Raw/idea.md", "personal-os/todo.md",
                "personal-os/01_Philosophy/.obsidian/app.md", "personal-os/01_Philosophy/README.md"):
    check("B ignored %s" % ignored, LB.is_ignored(ignored))
check("B nested .md not ignored", not LB.is_ignored("personal-os/01_Philosophy/x/y.md"))
check("B site rows", [p[0] for p in TB.partitions_for_scope("site", layout=LB)]
      == ["philosophy", "infrastructure", "releases"])
check("B leak prefixes", LB.restricted_prefixes("site") == ("personal-os/03_Evidence/",))
check("B scope of evidence", TB.scope_of_source_file("personal-os/03_Evidence/a.md", layout=LB) == "vault")
b_nodes = [node("evid-2020-001", "personal-os/03_Evidence/2020/audit.md")]
b_calls = [callout("evid-2020-001", "personal-os/04_Releases/v1.md"),
           callout("evid-2020-001", "personal-os/03_Evidence/2021/x.md")]
check("B boundary open->vault only",
      [c.source_file for c in integrity.find_cross_boundary_refs(LB, TB, b_nodes, b_calls)]
      == ["personal-os/04_Releases/v1.md"])

# ---- globs --------------------------------------------------------------------
for pattern, path, want in [("*.md", "todo.md", True), ("*.md", "a/b.md", False),
                            ("**/README*", "README.md", True), ("x/**", "x/a/b", True),
                            ("a?c", "a/c", False)]:
    check("glob %s ~ %s" % (pattern, path), bool(compile_glob(pattern).match(path)) == want)

# ---- fail-fast configuration --------------------------------------------------
BASE = yaml.safe_load((CFG / "corpus_layout.yaml").read_text(encoding="utf-8"))
ROLES = G.document_roles


def rejects(label, mutate, needle):
    data = copy.deepcopy(BASE)
    mutate(data)
    try:
        compile_layout(data, T.partitions, ROLES)
        check("rejects " + label, False, "accepted")
    except LayoutError as exc:
        check("rejects " + label, needle in str(exc), str(exc))


rejects("version", lambda d: d.update(layout_version="9"), "layout_version")
rejects("undeclared scope", lambda d: d["scopes"].pop(), "is not declared here")
rejects("overlapping scopes", lambda d: d["scopes"][1].update(path="public/x"), "overlap")
rejects("stray partition", lambda d: d["partitions"].update(nosuch={}), "not in config/taxonomy.yaml")
rejects("public target reads restricted", lambda d: d["targets"][0]["scopes"].append("private"), "above level")
rejects("alias collision", lambda d: d["targets"][0].update(aliases=["all"]), "declared twice")
rejects("bad extension", lambda d: d["filenames"].update(extensions=["md"]), "look like")

raw_rules = config_reader.read_raw(CFG, ROOT)["linter_rules"]
try:
    compile_grammar(raw_rules, T.known_domains)
    check("grammar requires layout", False, "accepted")
except ConfigError as exc:
    check("grammar requires layout", "layout" in str(exc), str(exc))
stale = copy.deepcopy(raw_rules)
stale["file_naming"]["locales"] = ["vi", "en"]
try:
    compile_grammar(stale, T.known_domains, layout=L)
    check("grammar rejects file_naming.locales", False, "accepted")
except ConfigError as exc:
    check("grammar rejects file_naming.locales", "corpus_layout.yaml" in str(exc), str(exc))

shutil.rmtree(work, ignore_errors=True)
print("layout portable tests: %(pass)d passed, %(fail)d failed" % RESULTS)
sys.exit(1 if RESULTS["fail"] else 0)
