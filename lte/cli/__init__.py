# -*- coding: utf-8 -*-
"""lte.cli -- LAYER 3. Orchestrators. Entry points only.

INTENTIONALLY EXPORTS NOTHING. No imports, no __all__ with symbols, no
convenience aliases.

An orchestrator is defined in this architecture as a module that nothing
inside lte/ imports. Re-exporting one here would make `lte.cli` an import
target and invite `from lte.cli import compile` from a validator or an io
module -- an upward import that lte/validators/architecture.py rejects, but
only after someone has already written it. Keeping this file empty removes
the affordance rather than relying on the gate to catch its misuse.

There is a second, harder reason. `lte/cli/pipeline.py` runs on the HOST
(3.5/3.6) and `lte/cli/compile.py` runs in the CONTAINER (3.11). They are
siblings that must never load together. An eager re-export of both would
make `python3 -m lte.cli.pipeline` parse compile.py on the host and fail
with a SyntaxError -- the exact trap documented in lte/__init__.py,
reproduced one level down.

Entry points are invoked by module path, always with -m from the repo root:

    python3 -m lte.cli.pipeline --list          # host
    python  -m lte.cli.compile --source disk    # container
    python3 -m lte.cli.hooks.pre_commit         # host

`-m` is required, not stylistic: `python lte/cli/compile.py` puts
`lte/cli/` on sys.path instead of the repo root, so `from lte.engine import
...` fails.
"""
