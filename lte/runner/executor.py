# -*- coding: utf-8 -*-
"""lte/runner/executor.py -- THE process side-effect boundary.

This is the ONLY module in lte/runner/ that imports `subprocess`. Every
other module in the package is a pure transformation over data. That is
not a stylistic arrangement: it is what makes the orchestrator in
lte/cli/pipeline.py testable at all. Before this split, verifying that
`allow_exit_codes: [0, 2]` actually suppressed exit 2 required spawning a
real shell that exits 2.

DEPENDENCY INJECTION SEAM. lte/cli/pipeline.py never constructs a
SubprocessExecutor by name in its step loop -- it receives an executor and
calls .run() on it. Swapping in RecordingExecutor yields a full pipeline
run with zero processes spawned.

An implementation must satisfy exactly this contract:

    run(command, env, cwd, shell_executable, timeout, stream) -> ExecResult

  * NEVER raises for a non-zero exit. A failing step is data, returned in
    ExecResult.returncode, not control flow. Only a genuinely broken
    invocation (missing shell binary) may raise.
  * A timeout is reported as ExecResult(timed_out=True, returncode=124) --
    the coreutils timeout(1) convention -- never as an exception.
  * With stream=True, stdout/stderr are inherited by the parent and the
    returned ExecResult carries empty strings for both. Callers must not
    treat that emptiness as "the step was silent".
"""
import subprocess
from collections import namedtuple

ExecResult = namedtuple("ExecResult", ["returncode", "stdout", "stderr", "timed_out"])

TIMEOUT_RETURNCODE = 124


def _as_text(value):
    """TimeoutExpired.stdout/.stderr come back as bytes or str depending on
    how the child was configured; normalize without assuming either."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class SubprocessExecutor(object):
    """The real executor. Runs commands through a shell."""

    def run(self, command, env, cwd, shell_executable, timeout, stream):
        try:
            if stream:
                # Output goes straight to this process's terminal. Nothing is
                # captured, so on failure there is no payload to re-print --
                # it is already on screen. Used for steps whose silence would
                # be indistinguishable from a hang (docker build, git push).
                proc = subprocess.run(
                    command, shell=True, executable=shell_executable,
                    env=env, cwd=str(cwd), timeout=timeout,
                )
                return ExecResult(proc.returncode, "", "", False)

            # capture_output= and text= are both 3.7+; these are the 3.5
            # spellings of the same thing.
            proc = subprocess.run(
                command, shell=True, executable=shell_executable,
                env=env, cwd=str(cwd), timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            return ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "", False)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                TIMEOUT_RETURNCODE,
                _as_text(getattr(exc, "stdout", None)),
                _as_text(getattr(exc, "stderr", None)),
                True,
            )


class RecordingExecutor(object):
    """Test double. Records every invocation, spawns nothing.

    `results` maps a command substring to an ExecResult, so a test can make
    one specific step fail without affecting the others. Anything unmatched
    succeeds silently.
    """

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def run(self, command, env, cwd, shell_executable, timeout, stream):
        self.calls.append({
            "command": command, "cwd": str(cwd), "timeout": timeout,
            "stream": stream, "env": env,
        })
        for needle in self.results:
            if needle in command:
                return self.results[needle]
        return ExecResult(0, "", "", False)
