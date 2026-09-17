# -*- coding: utf-8 -*-
"""Behavioural equivalence + test-seam demonstration for lte.cli.pipeline.

Every assertion below was IMPOSSIBLE against the monolithic task_runner.py:
verifying allow_exit_codes required a shell that really exits 2, and
verifying halt_on_error required the assertion to survive a sys.exit(1)
fired from inside run_step().
"""
import sys
from pathlib import Path

from lte.cli import pipeline
from lte.runner import config_schema, environment
from lte.runner.errors import PipelineError
from lte.runner.executor import ExecResult, RecordingExecutor

PASS = [0]
FAIL = [0]


def check(label, cond):
    if cond:
        PASS[0] += 1
        print("  PASS  {0}".format(label))
    else:
        FAIL[0] += 1
        print("  FAIL  {0}".format(label))


def sink():
    buf = []
    return buf, (lambda t: buf.append(t))


CFG = Path("config/build_config.yaml")

print("pure leaf: config_schema (no file, no yaml, no disk)")
try:
    config_schema.validate({"config_version": "9.9.9", "steps": [{}]}, "<t>")
    check("rejects unsupported config_version", False)
except PipelineError as e:
    check("rejects unsupported config_version", "config_version" in str(e))
try:
    config_schema.validate(
        {"config_version": "1.0.0",
         "steps": [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]}, "<t>")
    check("rejects duplicate step names", False)
except PipelineError as e:
    check("rejects duplicate step names", "duplicate step name" in str(e))
try:
    config_schema.validate(
        {"config_version": "1.0.0", "steps": [{"name": "a", "command": "x",
                                               "halt_on_errors": True}]}, "<t>")
    check("rejects the halt_on_errors typo", False)
except PipelineError as e:
    check("rejects the halt_on_errors typo", "unknown key" in str(e))

print("\npure leaf: environment (order-independent fixed point)")
env = environment.resolve({}, {"NESTED": "${GREETING}-world", "GREETING": "hello"}, "<t>")
check("expands regardless of declaration order", env["NESTED"] == "hello-world")
# Documents ACTUAL behavior, not the behavior the old docstring claimed.
# A mutual reference converges to a literal and is passed to the shell.
cyc = environment.resolve({}, {"A": "${B}", "B": "${A}"}, "<t>")
check("mutual reference converges to a literal (NOT an error -- see "
      "environment.MAX_EXPANSION_PASSES)", cyc == {"A": "${A}", "B": "${A}"})

print("\norchestrator with an injected executor -- zero processes spawned")
rec = RecordingExecutor()
out, emit = sink()
rc = pipeline.main(["--config", str(CFG)], executor=rec, out=emit, err=lambda t: None)
check("full 9-step run returns 0", rc == 0)
check("executed all 9 steps without spawning any", len(rec.calls) == 9)
check("shell prelude prepended to every command",
      all(c["command"].startswith("set -euo pipefail") for c in rec.calls))
check("PWD corrected to working_directory, not the caller's cwd",
      all(c["env"]["PWD"] == c["cwd"] for c in rec.calls))
check("config environment reached the steps",
      rec.calls[0]["env"]["DOCKER_IMAGE"] == "lte-cas")
check("summary table rendered", any("task_runner --" in t for t in out))

print("\npolicy: allow_exit_codes suppresses a declared non-zero")
rec = RecordingExecutor({"preflight.py": ExecResult(2, "", "warned", False)})
out, emit = sink()
errbuf, eemit = sink()
rc = pipeline.main(["--config", str(CFG)], executor=rec, out=emit, err=eemit)
check("preflight exit 2 does not fail the run", rc == 0)
check("the suppression is reported, not silent",
      any("declared acceptable" in t for t in errbuf))

print("\npolicy: an undeclared non-zero halts when halt_on_error is true")
rec = RecordingExecutor({"compile_from_git.py": ExecResult(1, "", "boom", False)})
out, emit = sink()
errbuf, eemit = sink()
rc = pipeline.main(["--config", str(CFG)], executor=rec, out=emit, err=eemit)
check("returns 1", rc == 1)
check("aborts at compile-public, later steps never run", len(rec.calls) == 4)
check("stderr tail surfaced", any("boom" in t for t in errbuf))

print("\npolicy: halt_on_error false continues but still fails the build")
rec = RecordingExecutor({"PY": ExecResult(1, "", "dashboard broke", False)})
out, emit = sink()
errbuf, eemit = sink()
rc = pipeline.main(["--config", str(CFG)], executor=rec, out=emit, err=eemit)
check("returns 2, not 0 -- CI must not go green", rc == 2)
check("all 9 steps still ran", len(rec.calls) == 9)

print("\ntimeout maps to 124 + TIMEOUT label")
rec = RecordingExecutor({"docker build": ExecResult(124, "", "", True)})
out, emit = sink()
errbuf, eemit = sink()
rc = pipeline.main(["--config", str(CFG)], executor=rec, out=emit, err=eemit)
check("timeout halts (build-image is halt_on_error: true)", rc == 1)
check("timeout reported distinctly", any("timeout" in t.lower() for t in errbuf))

print("\nstep selection")
rec = RecordingExecutor()
out, emit = sink()
pipeline.main(["--config", str(CFG), "--only", "compile-public"],
              executor=rec, out=emit, err=lambda t: None)
check("--only runs exactly one step", len(rec.calls) == 1)
rec = RecordingExecutor()
pipeline.main(["--config", str(CFG), "--from", "tier1-push"],
              executor=rec, out=lambda t: None, err=lambda t: None)
check("--from runs the tail", len(rec.calls) == 3)
errbuf, eemit = sink()
rc = pipeline.main(["--config", str(CFG), "--only", "nope"],
                   executor=RecordingExecutor(), out=lambda t: None, err=eemit)
check("--only with a bad name exits 1 and lists known steps",
      rc == 1 and "compile-public" in "".join(errbuf))

print("\n--dry-run executes nothing")
rec = RecordingExecutor()
out, emit = sink()
rc = pipeline.main(["--config", str(CFG), "--dry-run"],
                   executor=rec, out=emit, err=lambda t: None)
check("dry-run returns 0 and spawns nothing", rc == 0 and len(rec.calls) == 0)
check("dry-run shows resolved commands", any("command:" in t for t in out))

print("\n  {0} passed, {1} failed".format(PASS[0], FAIL[0]))
sys.exit(1 if FAIL[0] else 0)
