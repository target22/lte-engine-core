# -*- coding: utf-8 -*-
"""lte.cli.hooks -- LAYER 3. Git hook entry points.

INTENTIONALLY EXPORTS NOTHING -- same rules as lte/cli/__init__.py.

Hooks are a distinct subpackage rather than more modules in lte/cli/ because
they are invoked by GIT, not by a person or by build_config.yaml. That
difference is operational, not cosmetic:

  * A hook's exit code is a veto on a commit, not a build result. The
    pre-commit guardrail returning 1 rejects the commit outright.
  * A hook runs on the HOST interpreter, in whatever environment the
    committer's shell provides, with no guarantee the container is running.
    lte/cli/hooks/pre_commit.py therefore holds the 3.5 floor and re-execs
    into Docker for anything needing lte.engine.
  * A hook must never block on a prompt or write to stdout expecting a
    reader; git captures both.

Installed by symlink from .git/hooks/pre-commit. The symlink target is a
shim that runs `python3 -m lte.cli.hooks.pre_commit` from the repo root, so
the -m requirement in lte/cli/__init__.py holds here too.
"""
