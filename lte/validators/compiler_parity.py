#!/usr/bin/env python3
"""
lte/validators/compiler_parity.py -- LAYER 2 VALIDATOR. Asserts that the
DISK source and the CAS source produce the same read-model for the same
corpus.

WHY THIS EXISTS. The two compilers are independent implementations of one
shape. graph_lib.py's own extract_frontmatter_from_text() docstring
records what that costs: "Two implementations of one rule is how the
`supersedes` field came to be emitted by the disk compiler and silently
absent from the CAS compiler that actually runs in production." It
happened again with the Task-3 dependent lifecycle -- `dependent_status`
and `computed_status` landed in parse_graph.py and were missing from
git_cas.py's parallel copy of the same assembly loop.

Both compilers write to the SAME two output paths (public/data/graph.json
and public/internal-data/graph-internal.json), so a consumer cannot tell
which one produced the file it is reading. A field present in one and
absent from the other is therefore invisible in the artifact until
something downstream reads `undefined`. A DRY refactor would be the real
fix; until one happens, this is the check that makes the drift loud.

WHAT IS COMPARED. metadata.graph_hash is computed over {"vi","en"} only
(both compilers), so an identical hash means the entire compiled payload
matches byte-for-byte -- that single comparison is the strongest
assertion available and is checked first. When it differs, the per-field
walk below localises WHERE, instead of reporting only that two 130 KB
files are not equal.

metadata itself is compared by KEY SET, not by value: graph_hash and
generated_at legitimately differ per run.

LAYER POSITION. Invokes the two compile entry points as SUBPROCESSES rather
than importing lte.cli -- a validator importing an orchestrator would be an
upward import, which lte/validators/architecture.py rejects. Shelling out is
not a workaround for the rule; it is the honest shape of the check, which
tests the compilers as the pipeline actually invokes them, argv and all.
`subprocess` is therefore legitimate here and this module is deliberately
absent from architecture.py's PURE_MODULES list.

USAGE
    python3 -m lte.validators.compiler_parity \\
        --repo /path/to/repo.git --branch main --scope public

Exit 0 on parity, 1 on any divergence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

def _resolve_repo_root() -> Path:
    """SRKH_REPO_ROOT wins; otherwise walk up until a directory holding
    both config/ and scripts/ is found. A fixed number of .parent hops
    breaks the moment this file moves between scripts/ and
    scripts/verification_and_testing/ -- and that move is mid-flight in
    this repo (README_20260914_scripts.md), so it would break silently,
    by resolving to a directory OUTSIDE the repo rather than erroring."""
    import os
    override = os.environ.get("SRKH_REPO_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    for candidate in [here] + list(here.parents):
        if (candidate / "config").is_dir() and (candidate / "scripts").is_dir():
            return candidate
    return here.parent


REPO_ROOT = _resolve_repo_root()

# Every key both compilers must emit on a dependent (debate/ops) entry.
# Listed explicitly rather than derived from whichever side happens to be
# richer -- deriving it would make the check pass whenever BOTH compilers
# forget a field, which is the case it most needs to catch.
REQUIRED_ENTRY_KEYS = (
    "title", "body", "source_file", "layer",
    "contract_status", "dependent_status", "computed_status", "invalidated",
)
REQUIRED_NODE_KEYS = ("anchor_id", "title", "layer", "body", "status", "debates", "ops")
REQUIRED_METADATA_KEYS = (
    "graph_hash", "generated_at", "total_nodes", "schema_version",
    "status_values", "invalidated_state",
)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def flatten_entries(graph: dict) -> dict[tuple, dict]:
    """Keyed on (lang, anchor_id, kind, ordinal) -- a stable identity that
    survives partition/document reordering, so a genuine content
    difference is never masked by an ordering one, and vice versa."""
    out: dict[tuple, dict] = {}
    for lang in ("vi", "en"):
        for partition in graph.get(lang, {}).get("partitions", []):
            for document in partition.get("documents", []):
                for node in document.get("nodes", []):
                    for kind in ("debates", "ops"):
                        for index, entry in enumerate(node.get(kind, []) or []):
                            out[(lang, node["anchor_id"], kind, index)] = entry
    return out


def flatten_nodes(graph: dict) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for lang in ("vi", "en"):
        for partition in graph.get(lang, {}).get("partitions", []):
            for document in partition.get("documents", []):
                for node in document.get("nodes", []):
                    out[(lang, node["anchor_id"])] = node
    return out


def compare(disk: dict, cas: dict) -> list[str]:
    failures: list[str] = []

    disk_meta, cas_meta = disk.get("metadata", {}), cas.get("metadata", {})
    for name, meta in (("disk", disk_meta), ("cas", cas_meta)):
        missing = [k for k in REQUIRED_METADATA_KEYS if k not in meta]
        if missing:
            failures.append("{} compiler metadata is missing {}".format(name, missing))
    only_disk = sorted(set(disk_meta) - set(cas_meta))
    only_cas = sorted(set(cas_meta) - set(disk_meta))
    if only_disk:
        failures.append("metadata keys present only in the DISK output: {}".format(only_disk))
    if only_cas:
        failures.append("metadata keys present only in the CAS output: {}".format(only_cas))
    for key in ("schema_version", "status_values", "invalidated_state", "total_nodes"):
        if key in disk_meta and key in cas_meta and disk_meta[key] != cas_meta[key]:
            failures.append("metadata.{}: disk={!r} cas={!r}".format(key, disk_meta[key], cas_meta[key]))

    disk_nodes, cas_nodes = flatten_nodes(disk), flatten_nodes(cas)
    if set(disk_nodes) != set(cas_nodes):
        for key in sorted(set(disk_nodes) ^ set(cas_nodes))[:10]:
            side = "DISK" if key in disk_nodes else "CAS"
            failures.append("node ^{} ({}) exists only in the {} output".format(key[1], key[0], side))
    for key in sorted(set(disk_nodes) & set(cas_nodes)):
        for field in REQUIRED_NODE_KEYS:
            a, b = disk_nodes[key].get(field, "<missing>"), cas_nodes[key].get(field, "<missing>")
            if field in ("debates", "ops"):
                a, b = len(a or []), len(b or [])
            if a != b:
                failures.append("node ^{} .{}: disk={!r} cas={!r}".format(key[1], field, a, b))

    disk_entries, cas_entries = flatten_entries(disk), flatten_entries(cas)
    if set(disk_entries) != set(cas_entries):
        for key in sorted(set(disk_entries) ^ set(cas_entries))[:10]:
            side = "DISK" if key in disk_entries else "CAS"
            failures.append("{} entry #{} under ^{} exists only in the {} output"
                            .format(key[2], key[3], key[1], side))
    for key in sorted(set(disk_entries) & set(cas_entries)):
        a, b = disk_entries[key], cas_entries[key]
        for field in REQUIRED_ENTRY_KEYS:
            if field not in a or field not in b:
                side = "DISK" if field not in a else "CAS"
                failures.append("{} entry under ^{}: {!r} ABSENT from the {} output"
                                .format(key[2], key[1], field, side))
                continue
            if a[field] != b[field]:
                failures.append("{} entry under ^{} .{}: disk={!r} cas={!r}"
                                .format(key[2], key[1], field, a[field], b[field]))

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lte.validators.compiler_parity",
        description=__doc__.strip().splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True, help="Bare Git repository the CAS compiler reads.")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--scope", default="public", choices=["public", "internal", "all"])
    parser.add_argument("--module", default="lte.cli.compile",
                        help="Module path of the compile entry point (default: lte.cli.compile).")
    parser.add_argument("--scripts", type=Path, default=None,
                        help=(
                            "LEGACY ESCAPE HATCH: a directory holding parse_graph.py and "
                            "compile_from_git.py, used instead of --module. Exists so this "
                            "gate can run against the pre-migration tree during steps 2-6 of "
                            "the plan, which is exactly when it is most needed -- it is the "
                            "check that proves the collapse in step 5 was correct."))
    args = parser.parse_args(argv)

    # Temp dir UNDER the repo root, not in /tmp: parse_graph.py prints its
    # output path relative to REPO_ROOT, and both compilers resolve config
    # relative to it. Cleaned up on exit either way.
    with tempfile.TemporaryDirectory(dir=str(REPO_ROOT)) as tmp:
        disk_path = Path(tmp) / "disk.json"
        cas_path = Path(tmp) / "cas.json"

        disk_scope = "public" if args.scope == "public" else "internal"
        if args.scripts is not None:
            disk_cmd = [sys.executable, args.scripts / "parse_graph.py",
                        "--scope", disk_scope, "--output", disk_path]
        else:
            disk_cmd = [sys.executable, "-m", args.module, "--source", "disk",
                        "--scope", disk_scope, "--output", disk_path]
        proc = run(disk_cmd, cwd=REPO_ROOT)
        if proc.returncode != 0 or not disk_path.is_file():
            sys.stderr.write("[parity] FATAL: the disk compiler failed.\n")
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return 1

        if args.scripts is not None:
            cas_cmd = [sys.executable, args.scripts / "compile_from_git.py",
                       "--repo", args.repo, "--branch", args.branch,
                       "--scope", args.scope, "--output", cas_path]
        else:
            cas_cmd = [sys.executable, "-m", args.module, "--source", "cas",
                       "--repo", args.repo, "--branch", args.branch,
                       "--scope", args.scope, "--output", cas_path]
        proc = run(cas_cmd, cwd=REPO_ROOT)
        if proc.returncode != 0 or not cas_path.is_file():
            sys.stderr.write("[parity] FATAL: the CAS compiler failed.\n")
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return 1

        disk = json.loads(disk_path.read_text(encoding="utf-8"))
        cas = json.loads(cas_path.read_text(encoding="utf-8"))

    # The single strongest assertion, first. graph_hash digests {"vi","en"}
    # only, so an identical hash means the whole payload matches.
    if disk["metadata"]["graph_hash"] == cas["metadata"]["graph_hash"]:
        extra = compare(disk, cas)
        metadata_only = [f for f in extra if f.startswith("metadata")]
        if not metadata_only:
            print("[parity] OK -- disk and CAS compilers produced an identical "
                  "read-model (graph_hash {}).".format(disk["metadata"]["graph_hash"][:12]))
            return 0
        sys.stderr.write("[parity] FAILED -- payloads match but metadata diverges:\n")
        for failure in metadata_only:
            sys.stderr.write("  - {}\n".format(failure))
        return 1

    failures = compare(disk, cas)
    sys.stderr.write(
        "[parity] FAILED -- graph_hash differs (disk {} / cas {}); {} specific divergence(s):\n"
        .format(disk["metadata"]["graph_hash"][:12], cas["metadata"]["graph_hash"][:12], len(failures))
    )
    for failure in failures[:40]:
        sys.stderr.write("  - {}\n".format(failure))
    if len(failures) > 40:
        sys.stderr.write("  ... and {} more\n".format(len(failures) - 40))
    if not failures:
        sys.stderr.write(
            "  (no field-level difference found -- the divergence is in a key this\n"
            "   check does not yet compare. Widen REQUIRED_*_KEYS above.)\n"
        )
    sys.stderr.write(
        "\n[parity] These two compilers write the SAME output paths, so whichever ran\n"
        "[parity] last wins and the difference is invisible in the artifact. Port the\n"
        "[parity] change to BOTH, or collapse them onto one graph_builder.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
