# -*- coding: utf-8 -*-
"""lte/runner/environment.py -- leaf module: pure fixed-point expansion of
environment variable values.

PURE. Takes two mappings, returns a new mapping. Reads nothing from
os.environ itself -- the caller passes the base environment in, which is
what makes this testable with a literal three-key dict instead of having
to monkeypatch the process environment.
"""
from string import Template

from lte.runner.errors import PipelineError

# Bound on fixed-point expansion.
#
# WHAT THIS GUARD ACTUALLY CATCHES, corrected this revision. The behavior
# is carried over byte-for-byte from task_runner.resolve_environment(), but
# that function's docstring claimed it "detects reference cycles" and this
# constant's comment claimed "exceeding it means a reference cycle". Both
# overclaim, and writing the first unit test this logic has ever had is
# what surfaced it. Traced:
#
#     {"A": "${B}", "B": "${A}"}
#       pass 1 -> {"A": "${A}", "B": "${A}"}   changed=True
#       pass 2 -> unchanged                     changed=False  -> RETURNS
#
# A mutual reference reaches a FIXED POINT in two passes because
# safe_substitute leaves an unresolvable ${NAME} as a literal. The guard
# never fires; the literal "${A}" is handed to the shell, which expands it
# against the real process environment (usually to empty). What the guard
# genuinely catches is a value that keeps GROWING each pass and never
# converges.
#
# NOT FIXED HERE, on purpose. Adding real cycle detection changes runtime
# semantics -- a config that today silently resolves to a literal would
# start hard-failing the build -- and a structural refactor is the wrong
# commit for a behavior change. Filed as a follow-up; the fix is a
# reachability check over the ${NAME} references before expanding, not a
# larger pass bound.
MAX_EXPANSION_PASSES = 10


def resolve(base, declared, where):
    """Merges `declared` over `base`, expanding $VAR / ${VAR} in the VALUES
    to a fixed point.

    Fixed-point rather than a single ordered pass because dict insertion
    order is not guaranteed before Python 3.7, and this package runs on the
    host's interpreter. A single pass over an unordered mapping would
    expand `NESTED: "${GREETING}-world"` before `GREETING` was known, on
    some interpreters and not others -- an environment-dependent bug that
    surfaces as a mysteriously empty substitution. Repeating until stable
    removes declaration order from the semantics entirely.

    COMMAND STRINGS ARE NEVER EXPANDED HERE. They go to the shell verbatim,
    which does its own expansion at execution time -- that is what makes
    $(git config user.name) and ${VAR:-default} work inside a command.
    Expanding commands in Python too would mean two passes over one string
    with different rules. The split is: Python resolves the environment,
    the shell resolves the command.

    safe_substitute (not substitute) so an unresolvable reference is left
    as a literal for the shell to deal with rather than raising here -- a
    step may legitimately reference a variable that only exists at runtime.

    Raises PipelineError only when values fail to CONVERGE within
    MAX_EXPANSION_PASSES. A mutual reference does converge, to a literal --
    see that constant's comment. This function does not detect cycles, and
    the code it replaces did not either despite saying so.
    """
    env = dict(base)
    if declared is None:
        return env

    for key in declared:
        if not isinstance(key, str) or not key:
            raise PipelineError(
                "{0}: environment key {1!r} must be a non-empty string.".format(where, key)
            )
        env[key] = str(declared[key])

    declared_keys = list(declared.keys())
    for _ in range(MAX_EXPANSION_PASSES):
        changed = False
        for key in declared_keys:
            expanded = Template(env[key]).safe_substitute(env)
            if expanded != env[key]:
                env[key] = expanded
                changed = True
        if not changed:
            return env

    raise PipelineError(
        "{0}: environment values did not stabilize after {1} expansion passes -- "
        "check for a reference cycle among {2}.".format(
            where, MAX_EXPANSION_PASSES, sorted(declared_keys)
        )
    )
