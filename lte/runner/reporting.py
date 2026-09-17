# -*- coding: utf-8 -*-
"""lte/runner/reporting.py -- leaf module: pure formatting.

EVERY FUNCTION RETURNS A STRING. Nothing here calls print() or writes to
a stream. The orchestrator decides where text goes, which is what lets a
test assert on the summary table's exact content instead of capturing
stdout and hoping.
"""
import os

STDERR_TAIL_LINES = 200


def format_step_header(index, total, name, description=None):
    lines = ["\n=== [{0}/{1}] {2} ===".format(index, total, name)]
    if description:
        lines.append("    " + " ".join(str(description).split()))
    return "\n".join(lines)


def format_failure(name, returncode, duration, stream, stdout, stderr, halt_on_error):
    """The failure report for one step. Pure -- the stderr tail is computed
    here, not truncated at the point of writing."""
    out = ["\n[task_runner] step {0!r} exited {1} after {2:.1f}s.".format(
        name, returncode, duration)]
    if stream:
        out.append("[task_runner] output was streamed above (this step sets stream: true).")
    elif stderr.strip():
        tail = stderr.rstrip().splitlines()
        if len(tail) > STDERR_TAIL_LINES:
            out.append("[task_runner] ... {0} earlier stderr line(s) elided ...".format(
                len(tail) - STDERR_TAIL_LINES))
            tail = tail[-STDERR_TAIL_LINES:]
        out.append("[task_runner] --- stderr ---")
        out.append("\n".join(tail))
        out.append("[task_runner] --- end stderr ---")
    elif not stdout.strip():
        out.append("[task_runner] the step produced no output on either stream.")

    if halt_on_error:
        out.append("[task_runner] halt_on_error is true for {0!r} -- aborting pipeline.".format(name))
    else:
        out.append("[task_runner] halt_on_error is false for {0!r} -- continuing.".format(name))
    return "\n".join(out) + "\n"


def format_allowed_nonzero(name, returncode, allow_exit_codes):
    return ("\n[task_runner] step {0!r} exited {1}, declared acceptable via "
            "allow_exit_codes={2}.\n".format(name, returncode, allow_exit_codes))


def status_label(returncode, timed_out):
    if timed_out:
        return "TIMEOUT"
    if returncode == 0:
        return "ok"
    return "FAILED({0})".format(returncode)


def format_summary(results, total_duration):
    if results:
        width = max([len(r["name"]) for r in results])
    else:
        width = 4
    lines = ["\n" + "=" * (width + 26),
             " task_runner -- {0} step(s) in {1:.1f}s".format(len(results), total_duration),
             "-" * (width + 26)]
    for r in results:
        lines.append(" {0}  {1:>7.1f}s  {2}".format(
            r["name"].ljust(width), r["duration"], r["status"]))
    lines.append("=" * (width + 26))
    return "\n".join(lines)


def format_step_list(steps, default_halt):
    lines = []
    for i, step in enumerate(steps, 1):
        halt = step.get("halt_on_error", default_halt)
        suffix = "" if halt else "   (halt_on_error: false)"
        lines.append("{0:>3}. {1}{2}".format(i, step["name"], suffix))
    return "\n".join(lines)


def format_dry_run_step(index, total, step, env, defaults, default_halt, parent_env=None):
    """`parent_env` is passed in rather than read from os.environ directly,
    so the 'which variables did the config actually change' diff is
    computed against an explicit baseline a test can supply."""
    if parent_env is None:
        parent_env = os.environ
    extra = dict((k, v) for k, v in env.items() if parent_env.get(k) != v)
    lines = ["\n=== [{0}/{1}] {2} ===".format(index, total, step["name"]),
             "  shell : {0}".format(defaults["shell_executable"]),
             "  cwd   : {0}".format(step.get("working_directory", defaults["working_directory"])),
             "  halt  : {0}".format(step.get("halt_on_error", default_halt))]
    if extra:
        lines.append("  env   : {0}".format(
            ", ".join("{0}={1}".format(k, extra[k]) for k in sorted(extra))))
    lines.append("  command:")
    for line in step["command"].rstrip().splitlines():
        lines.append("    {0}".format(line))
    return "\n".join(lines)
