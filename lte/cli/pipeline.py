#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lte/cli/pipeline.py -- ORCHESTRATOR. Argument parsing, step selection,
and wiring. Holds no reusable logic of its own.

Replaces scripts/core_pipeline/task_runner.py. Behavior is unchanged: same
flags, same exit codes (0 ok / 1 fatal / 2 completed-with-non-fatal-failure
/ 130 SIGINT), same output format, same config contract. What changed is
where the logic lives.

WHAT THIS MODULE MAY DO
  * parse argv
  * select which steps to run (--only / --from)
  * construct the collaborators and pass them down
  * decide where formatted text is written
  * map results to a process exit code

WHAT THIS MODULE MAY NOT DO
  * import subprocess          (-> lte/runner/executor.py)
  * import yaml or open files  (-> lte/runner/config_io.py)
  * validate the config shape  (-> lte/runner/config_schema.py)
  * expand environment values  (-> lte/runner/environment.py)
  * build a display string     (-> lte/runner/reporting.py)

If a change here would require knowing how any of those work, the change
belongs in the leaf module, not in this file.

DEPENDENCY INJECTION, AND WHY IT IS NOT DECORATION HERE. run_pipeline()
receives its executor and its two output writers rather than reaching for
subprocess and sys.stdout directly. That is the difference between a
runner whose allow_exit_codes / halt_on_error / timeout logic can be
asserted on in milliseconds against a RecordingExecutor, and one where
verifying "exit 2 is suppressed when declared" means spawning a real shell
that exits 2. The old file was the latter, and had no test seam at all.

THIS ORCHESTRATOR DOES NOT IMPORT lte/engine/. Deliberately, and it is the
single most important boundary in this file. This process runs on the HOST
interpreter (3.5/3.6), and lte/engine/ is written against the container's
3.9+ syntax -- importing it here would raise SyntaxError before argparse
ever runs. It also orchestrates containers (`docker build`, `docker run`),
so it must stay domain-agnostic: no reference to Docker, Git, graph.json or
partitions appears anywhere below. Every piece of project knowledge lives
in config/build_config.yaml. lte/cli/compile.py is the orchestrator that
imports engine leaves; this one never does.

LEGACY-PYTHON BUILD -- PYTHON 3.5 FLOOR, package-wide for lte/runner/ and
this module:
  * no `from __future__ import annotations`            (3.7+)
  * no PEP 585 generics / PEP 604 unions in annotations (3.9/3.10)
  * no f-strings -- .format() throughout                (3.6+)
  * no subprocess.run(capture_output=/text=)            (3.7+)
  * no walrus, no dataclasses

SECURITY BOUNDARY, unchanged: commands run through the shell, so anyone who
can modify build_config.yaml executes code as whoever runs this pipeline,
including their SSH agent and push credentials. config/ has no dedicated
CODEOWNERS rule today; it falls through to the `*` catch-all.

Usage:
  python3 -m lte.cli.pipeline
  python3 -m lte.cli.pipeline --config config/build_config.yaml
  python3 -m lte.cli.pipeline --list
  python3 -m lte.cli.pipeline --dry-run
  python3 -m lte.cli.pipeline --only compile-public
  python3 -m lte.cli.pipeline --from tier1-commit
"""

import sys

# Checked before anything else is imported or parsed at runtime. Written in
# syntax valid all the way back to Python 2, so the message survives even on
# an interpreter far older than this script targets.
if sys.version_info < (3, 5):
    sys.stderr.write(
        "[task_runner] FATAL: needs Python 3.5 or newer "
        "(subprocess.run); this interpreter is %d.%d.\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.stderr.write(
        "[task_runner] This script must run on the HOST, not in the "
        "container. If the host Python is too old, install a newer one "
        "or run the steps in config/build_config.yaml by hand.\n"
    )
    sys.exit(1)

import argparse
import os
import time
from pathlib import Path

from lte.runner import config_io, config_schema, environment, reporting
from lte.runner.errors import PipelineError
from lte.runner.executor import SubprocessExecutor


def _stdout(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _stderr(text):
    sys.stderr.write(text)
    sys.stderr.flush()


def run_step(step, index, total, defaults, executor, out, err):
    """Executes one step and returns its result record.

    Reads as a sequence of delegations: resolve the environment (leaf),
    format a header (leaf), run the command (injected executor), format the
    outcome (leaf). The only judgement this function makes itself is the
    allow_exit_codes / halt_on_error policy, which is genuinely
    orchestration -- it decides whether the PIPELINE continues, which no
    leaf module is positioned to know.
    """
    name = step["name"]
    halt_on_error = step.get("halt_on_error", config_schema.DEFAULT_HALT_ON_ERROR)
    # Exit codes this step declares as success. Exists because a tool can
    # legitimately use a non-zero code for a non-failure -- preflight.py
    # returns 2 for "warnings only, safe to continue". Without this key the
    # runner's only options are "any non-zero aborts" or halt_on_error:
    # false, and the latter would also swallow a genuine exit 1.
    allow_exit_codes = step.get("allow_exit_codes", [0])
    stream = step.get("stream", False)
    timeout = step.get("timeout")

    env = environment.resolve(
        defaults["environment"], step.get("environment"), "step {0!r}".format(name)
    )
    cwd = Path(step.get("working_directory", defaults["working_directory"]))
    # $PWD must describe where this step actually runs, not where the
    # operator invoked the runner from.
    env["PWD"] = str(cwd)

    prelude = defaults["shell_prelude"]
    command = "{0}\n{1}".format(prelude, step["command"]) if prelude else step["command"]

    out(reporting.format_step_header(index, total, name, step.get("description")))

    started = time.monotonic()
    result = executor.run(
        command=command, env=env, cwd=cwd,
        shell_executable=defaults["shell_executable"],
        timeout=timeout, stream=stream,
    )
    duration = time.monotonic() - started

    if result.timed_out:
        err("[task_runner] step {0!r} exceeded its {1}s timeout.\n".format(name, timeout))
    if not stream and result.stdout.strip():
        out(result.stdout.rstrip())

    returncode = result.returncode
    if returncode in allow_exit_codes and returncode != 0:
        # Declared-acceptable non-zero: report it, do not fail on it. The
        # step's own stderr has already explained what it means.
        err(reporting.format_allowed_nonzero(name, returncode, allow_exit_codes))
        returncode = 0

    if returncode != 0:
        err(reporting.format_failure(
            name, returncode, duration, stream,
            result.stdout, result.stderr, halt_on_error,
        ))
        if halt_on_error:
            raise _HaltPipeline()

    return {
        "name": name,
        "status": reporting.status_label(returncode, result.timed_out),
        "duration": duration,
        "returncode": returncode,
    }


class _HaltPipeline(Exception):
    """Internal control signal for halt_on_error.

    An exception rather than the old code's direct sys.exit(1) from inside
    run_step(). A leaf-adjacent helper calling sys.exit() makes the function
    untestable -- you cannot assert "this step halts the pipeline" without
    the assertion killing the test process. Raising lets run_pipeline()
    own process termination, which is an orchestrator's job.
    """


def select_steps(steps, only, from_step):
    """Pure step selection. Raises PipelineError naming the known steps when
    a selector matches nothing -- a typo in --only should say which names
    exist, not silently run zero steps and report success."""
    names = [s["name"] for s in steps]
    if only:
        if only not in names:
            raise PipelineError("no step named {0!r}. Known: {1}".format(only, names))
        return [s for s in steps if s["name"] == only]
    if from_step:
        if from_step not in names:
            raise PipelineError("no step named {0!r}. Known: {1}".format(from_step, names))
        return steps[names.index(from_step):]
    return steps


def build_defaults(config, config_path, repo_root):
    """Assembles the per-run defaults every step inherits.

    Parent environment is INHERITED, not replaced: PATH, HOME,
    SSH_AUTH_SOCK and git's credential helpers must all survive or any step
    that pushes will fail confusingly.

    PWD is the one inherited value that must be CORRECTED rather than passed
    through. It holds the CALLER's cwd, but every step runs in
    working_directory. A config value such as
        DOCKER_RUN: 'docker run -v "$PWD":/app ...'
    is expanded in Python, so an uncorrected PWD would bake the caller's
    directory into the mount -- running from /tmp would mount /tmp as /app
    and every containerized step would operate on an empty tree, with no
    error, just missing files.
    """
    working_directory = Path(
        os.path.realpath(str(repo_root / config.get("working_directory", ".")))
    )
    if not working_directory.is_dir():
        raise PipelineError(
            "working_directory {0} is not a directory.".format(working_directory)
        )

    base_env = dict(os.environ)
    base_env["PWD"] = str(working_directory)
    env = environment.resolve(
        base_env, config.get("environment"), "{0}: environment".format(config_path)
    )
    return {
        "environment": env,
        "working_directory": working_directory,
        "shell_executable": config.get("shell_executable", config_schema.DEFAULT_SHELL),
        "shell_prelude": config.get("shell_prelude", config_schema.DEFAULT_SHELL_PRELUDE),
    }


def run_pipeline(steps, defaults, executor, out, err):
    """Executes the selected steps. Returns the process exit code.

    A non-halting step that failed still means the pipeline did not fully
    succeed. Returning 0 here would let CI go green on a partially broken
    run -- halt_on_error: false means "keep going", not "pretend it passed".
    """
    started = time.monotonic()
    results = []
    total = len(steps)
    halted = False
    for i, step in enumerate(steps, 1):
        try:
            results.append(run_step(step, i, total, defaults, executor, out, err))
        except _HaltPipeline:
            results.append({
                "name": step["name"], "status": "FAILED(halted)",
                "duration": 0.0, "returncode": 1,
            })
            halted = True
            break

    out(reporting.format_summary(results, time.monotonic() - started))

    if halted:
        return 1
    for r in results:
        if r["returncode"] != 0:
            err("[task_runner] completed with non-fatal step failure(s) -- "
                "see summary above.\n")
            return 2
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="lte.cli.pipeline", description="Declarative pipeline executor.")
    parser.add_argument("--config", type=Path, default=Path("config/build_config.yaml"))
    parser.add_argument("--list", action="store_true", help="Print the step names and exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print each resolved command without executing anything.")
    parser.add_argument("--only", metavar="NAME", help="Run exactly one step, by name.")
    parser.add_argument("--from", dest="from_step", metavar="NAME",
                        help="Start at this step and run to the end.")
    return parser.parse_args(argv)


def main(argv=None, executor=None, out=None, err=None):
    """Entry point. The three optional collaborators exist for tests; the
    production path supplies the real ones."""
    args = parse_args(argv)
    executor = executor or SubprocessExecutor()
    out = out or _stdout
    err = err or _stderr

    try:
        raw = config_io.read(args.config)
        config = config_schema.validate(raw, str(args.config))
        # working_directory resolves against the CONFIG FILE's parent's
        # parent, not the caller's cwd -- so the runner behaves identically
        # from anywhere, matching the Bash original's `cd "$(dirname "$0")/.."`.
        repo_root = args.config.resolve().parent.parent
        defaults = build_defaults(config, args.config, repo_root)
        steps = select_steps(config["steps"], args.only, args.from_step)
    except PipelineError as exc:
        err("[task_runner] FATAL: {0}\n".format(exc))
        return 1

    if args.list:
        out(reporting.format_step_list(steps, config_schema.DEFAULT_HALT_ON_ERROR))
        return 0

    if args.dry_run:
        out("[task_runner] DRY RUN -- {0} step(s), nothing will be executed.".format(len(steps)))
        for i, step in enumerate(steps, 1):
            env = environment.resolve(
                defaults["environment"], step.get("environment"), step["name"])
            out(reporting.format_dry_run_step(
                i, len(steps), step, env, defaults,
                config_schema.DEFAULT_HALT_ON_ERROR))
        return 0

    return run_pipeline(steps, defaults, executor, out, err)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `--list | head` closes the pipe early. Exit quietly instead of
        # tracebacking; redirect stdout to devnull first so the
        # interpreter's own flush-on-exit does not raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        # A full pipeline run takes minutes; Ctrl-C is expected, not an
        # internal error. 130 is the conventional SIGINT exit code.
        sys.stderr.write("\n[task_runner] interrupted by user.\n")
        sys.exit(130)
