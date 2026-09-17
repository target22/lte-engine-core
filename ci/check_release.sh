#!/usr/bin/env bash
# ci/check_release.sh -- lte-engine-core release gate (run in CI on every push
# and before `git tag`). Needs python >= 3.11 (tomllib) and git.
#   --tag   additionally require HEAD to be exactly on vX.Y.Z-LTS == version
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY=${PYTHON:-python3.11}
rc=0

"$PY" - "${1:-}" <<'PY' || rc=1
import re, subprocess, sys, tomllib

want_tag = sys.argv[1] == "--tag"
fail = []
proj = tomllib.load(open("pyproject.toml", "rb"))["project"]

# 1. version is static PEP 440 X.Y.Z; tag is vX.Y.Z-LTS
ver = proj["version"]
if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
    fail.append(f"pyproject version {ver!r} is not X.Y.Z")
if want_tag:
    tag = subprocess.run(["git", "describe", "--tags", "--exact-match"],
                         capture_output=True, text=True).stdout.strip()
    if tag != f"v{ver}-LTS":
        fail.append(f"HEAD tag {tag or '<none>'!r} != 'v{ver}-LTS'")

# 2. manifest command surface == console scripts (lte-<name>)
cmds, contract = [], None
for line in open("lte-engine.manifest"):
    tok = line.split("#", 1)[0].split()
    if tok[:1] == ["cmd"]:
        cmds.append(tok[1])
    elif tok[:1] == ["contract"]:
        contract = tok[1]
scripts = sorted(k[4:] for k in proj.get("scripts", {}) if k.startswith("lte-"))
if contract != "1":
    fail.append(f"manifest contract {contract!r} != '1'")
if sorted(cmds) != scripts:
    fail.append(f"manifest cmds {sorted(cmds)} != pyproject lte-* scripts {scripts}")

# 3. image recipe installs exactly the declared runtime deps (no tooling deps)
norm = lambda s: re.split(r"[\s<>=!~;\[]", s.strip(), maxsplit=1)[0].lower().replace("_", "-")
declared = {norm(d) for d in proj.get("dependencies", [])}
reqs = [l for l in open("requirements.txt") if l.strip() and not l.lstrip().startswith("#")]
if any(l.lstrip().startswith("-") for l in reqs):
    fail.append("requirements.txt contains pip options (-r/-e/--...)")
image = {norm(l) for l in reqs}
if image != declared:
    fail.append(f"requirements.txt {sorted(image)} != pyproject dependencies {sorted(declared)}")
if proj.get("optional-dependencies"):
    fail.append("core declares extras (tooling deps belong to lte-dev)")

for f in fail:
    print("RELEASE-GATE:", f)
sys.exit(1 if fail else 0)
PY

bash -n bin/lte || rc=1
[ "$rc" = 0 ] && echo "release gate: pass"
exit "$rc"
