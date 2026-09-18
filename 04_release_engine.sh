#!/usr/bin/env bash
# 04_release_engine.sh -- Day 2 / Task 3a: cut a new LTS release of the engine.
#
# Run from any directory AFTER the fix is merged into main of /srv/src/lte-engine-core:
#   bash /srv/src/lte-day2/04_release_engine.sh 1.0.1   # bug fix          -> v1.0.1-LTS
#   bash /srv/src/lte-day2/04_release_engine.sh 1.1.0   # additive feature -> v1.1.0-LTS
#
# Refuses: dirty tree, not on main, main != origin/main, existing tag,
# non-increasing version, missing python3.11. Any failure before the push
# restores pyproject.toml and removes the local commit/tag.
set -euo pipefail

CORE="/srv/src/lte-engine-core"
PY="python3.11"

step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 1 ] || die "usage: bash /srv/src/lte-day2/04_release_engine.sh <X.Y.Z>   e.g. 1.0.1"
VERSION="$1"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z, got: $VERSION"
TAG="v$VERSION-LTS"

VENV="$(mktemp -d /tmp/lte-release-venv.XXXXXX)"
BASE_SHA=""
PUSHED=0
on_exit() {
    rc=$?
    rm -rf "$VENV"
    if [ "$rc" -ne 0 ] && [ -n "$BASE_SHA" ] && [ "$PUSHED" -eq 0 ]; then
        set +e
        printf '\n!! release aborted -- restoring %s to %s\n' "$CORE" "$BASE_SHA" >&2
        cd "$CORE" && git tag -d "$TAG" >/dev/null 2>&1
        git reset -q --hard "$BASE_SHA"
        rm -rf lte_engine_core.egg-info build
    fi
    exit "$rc"
}
trap on_exit EXIT

# ---------------------------------------------------------------------------
step "0/6  preconditions"
command -v git >/dev/null 2>&1 || die "git not found"
command -v "$PY" >/dev/null 2>&1 || die "$PY not found (release tests and the gate need >=3.11)"
cd "$CORE"
[ -z "$(git status --porcelain)" ] || die "$CORE is not pristine (git status is not empty)"
[ "$(git symbolic-ref --short -q HEAD || true)" = "main" ] || die "not on main"
git fetch -q --tags origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "local main != origin/main -- pull/push first"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    die "$TAG already exists"
fi
CURRENT="$(sed -n 's/^version = "\([0-9.]*\)"$/\1/p' pyproject.toml)"
[ -n "$CURRENT" ] || die "cannot read version from pyproject.toml"
HIGHEST="$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -1)"
if [ "$VERSION" = "$CURRENT" ] || [ "$HIGHEST" != "$VERSION" ]; then
    die "version must increase: current $CURRENT, requested $VERSION"
fi
PREV_TAG="$(git describe --tags --abbrev=0 --match 'v*-LTS' 2>/dev/null || echo none)"
BASE_SHA="$(git rev-parse HEAD)"
echo "   $CURRENT -> $VERSION   (previous release: $PREV_TAG)"
if [ "$PREV_TAG" != "none" ]; then
    [ -n "$(git rev-list "$PREV_TAG..HEAD")" ] || die "no commits since $PREV_TAG -- nothing to release"
    git log --oneline "$PREV_TAG..HEAD"
fi

# ---------------------------------------------------------------------------
step "1/6  bump pyproject.toml"
sed -i "s/^version = \"$CURRENT\"\$/version = \"$VERSION\"/" pyproject.toml
grep -qx "version = \"$VERSION\"" pyproject.toml || die "version bump failed"

# ---------------------------------------------------------------------------
step "2/6  install into a throwaway venv and run the test suites"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q -e .
for t in tests/test_*.py; do
    [ -e "$t" ] || continue
    echo "   $t"
    SRKH_REPO_ROOT="$(pwd -P)" "$VENV/bin/python" "$t"
done
rm -rf lte_engine_core.egg-info build

# ---------------------------------------------------------------------------
step "3/6  release gate (pre-tag)"
bash -n bin/lte
PYTHON="$PY" bash ci/check_release.sh

# ---------------------------------------------------------------------------
step "4/6  commit + tag $TAG"
git commit -q -am "Release $TAG"
git tag -a "$TAG" -m "LTE engine $VERSION (LTS)"
PYTHON="$PY" bash ci/check_release.sh --tag

# ---------------------------------------------------------------------------
step "5/6  push"
git push origin refs/heads/main "refs/tags/$TAG"
PUSHED=1

# ---------------------------------------------------------------------------
step "6/6  done"
cat << EOF
   released $TAG ($(git rev-parse --short HEAD)) to $(git config --get remote.origin.url)
   rebuild : lte self build   (in each downstream after its bump; no-op if the image recipe is unchanged)
   bump    : bash /srv/src/lte-day2/03_bump_engine.sh /home/SRKH $TAG
             bash /srv/src/lte-day2/03_bump_engine.sh /srv/src/tarahos $TAG
EOF
