# -*- coding: utf-8 -*-
"""lte/runner/config_schema.py -- leaf module: pure validation of an
already-parsed pipeline config mapping.

TAKES A DICT, NEVER A PATH. This is the half of the old
task_runner.load_config() that has no side effects. Reading the file is
lte/runner/config_io.py's job; this function never touches the disk, so
it is callable in a unit test against a literal dict with no tmpdir, no
fixture file, and no YAML parser involved.

The `path` argument is a DISPLAY LABEL used only to build error messages.
It is deliberately typed as a plain string rather than a Path: accepting a
Path here would invite a caller to assume this function reads it.
"""
from lte.runner.errors import PipelineError

SUPPORTED_CONFIG_VERSIONS = frozenset(["1.0.0"])

# Strict key sets. An unrecognized key is an ERROR, not something to ignore:
# `halt_on_errors: true` (plural, a plausible typo) would otherwise be
# silently dropped and the step would inherit the default, turning a step
# the operator believed was fatal into one that is not.
TOP_LEVEL_KEYS = set([
    "config_version", "working_directory", "shell_executable",
    "shell_prelude", "environment", "steps",
])
STEP_KEYS = set([
    "name", "command", "description", "halt_on_error", "environment",
    "stream", "timeout", "working_directory", "allow_exit_codes",
])

DEFAULT_HALT_ON_ERROR = True
DEFAULT_SHELL = "/bin/bash"
# Applied to every command unless the config overrides it. Defaults ON:
# without it, a multi-line command keeps running after an intermediate
# failure and reports the LAST line's exit code, so a step can fail halfway
# and still be recorded as ok -- silently defeating halt_on_error.
DEFAULT_SHELL_PRELUDE = "set -euo pipefail"


def validate(data, path="<config>"):
    """Validates a parsed config mapping. Returns it unchanged on success,
    raises PipelineError on the first problem found.

    Returns the same object rather than a copy: this is a checker, not a
    normalizer. A caller that mutated the result and expected the original
    untouched would be relying on a copy this function never promised.
    """
    if not isinstance(data, dict):
        raise PipelineError(
            "{0} must parse to a mapping, got {1}.".format(path, type(data).__name__)
        )

    version = data.get("config_version")
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise PipelineError(
            "{0}: config_version={1!r} is not supported (supported: {2}).".format(
                path, version, sorted(SUPPORTED_CONFIG_VERSIONS)
            )
        )

    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise PipelineError(
            "{0}: unknown top-level key(s) {1}.".format(path, sorted(unknown))
        )

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PipelineError("{0}: 'steps' must be a non-empty list.".format(path))

    seen = {}
    for index, step in enumerate(steps):
        _validate_step(step, index, seen, path)
    return data


def _validate_step(step, index, seen, path):
    where = "{0}: steps[{1}]".format(path, index)
    if not isinstance(step, dict):
        raise PipelineError(
            "{0}: expected a mapping, got {1}.".format(where, type(step).__name__)
        )

    unknown = set(step) - STEP_KEYS
    if unknown:
        raise PipelineError(
            "{0}: unknown key(s) {1}. Known: {2}.".format(
                where, sorted(unknown), sorted(STEP_KEYS)
            )
        )

    name = step.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PipelineError("{0}: 'name' must be a non-empty string.".format(where))
    if name in seen:
        raise PipelineError(
            "{0}: duplicate step name {1!r} (also at steps[{2}]). Names must be "
            "unique -- --only and --from select by name.".format(where, name, seen[name])
        )
    seen[name] = index

    command = step.get("command")
    if not isinstance(command, str) or not command.strip():
        raise PipelineError(
            "{0} ({1}): 'command' must be a non-empty string.".format(where, name)
        )

    timeout = step.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise PipelineError(
            "{0} ({1}): 'timeout' must be a positive number of seconds.".format(where, name)
        )

    allow = step.get("allow_exit_codes")
    if allow is not None:
        if not isinstance(allow, list) or not allow:
            raise PipelineError(
                "{0} ({1}): 'allow_exit_codes' must be a non-empty list of ints.".format(
                    where, name)
            )
        for code in allow:
            if not isinstance(code, int) or isinstance(code, bool):
                raise PipelineError(
                    "{0} ({1}): allow_exit_codes entry {2!r} is not an int.".format(
                        where, name, code)
                )

    for key in ("halt_on_error", "stream"):
        if key in step and not isinstance(step[key], bool):
            raise PipelineError(
                "{0} ({1}): '{2}' must be a boolean.".format(where, name, key)
            )

    env = step.get("environment")
    if env is not None and not isinstance(env, dict):
        raise PipelineError(
            "{0} ({1}): 'environment' must be a mapping.".format(where, name)
        )
