# -*- coding: utf-8 -*-
"""lte -- the Sovereign Relational Knowledge Engine package.

    lte.runner      layer 0  host-side pipeline primitives (pure, 3.5 floor)
    lte.engine      layer 0  pure domain transforms (3.9+)
    lte.io          layer 1  side-effect boundaries (disk, Git CAS, subprocess)
    lte.validators  layer 2  corpus, bilingual, parity and architecture gates
    lte.cli         layer 3  orchestrators / entry points

THIS FILE MUST CONTAIN NO IMPORTS. That is a hard constraint, not a
stylistic preference, and it is the single most load-bearing line in this
package's layout.

`lte/runner/` targets the HOST interpreter (Python 3.5/3.6 -- the project's
host runs 3.6.8) while `lte/engine/` targets the container's 3.11 and uses
`from __future__ import annotations`, PEP 604 unions and variable
annotations throughout. A package `__init__` executes before ANY submodule
beneath it, so a single eager `from lte.engine import ...` here would drag
engine modules into every host-side import. Measured:

    # with an eager lte/__init__.py
    >>> import lte.runner.config_schema
    also loaded: ['lte.engine', 'lte.engine.taxonomy']

On the 3.6.8 host that is a SyntaxError raised at import time -- before
argparse runs, before any error handler exists, with a traceback pointing at
a file the operator never asked for. `python3 -m lte.cli.pipeline` would die
on a machine where every line of code it actually needs is valid.

The same reasoning is why `lte/validators/__init__.py` is import-free:
`lte/validators/architecture.py` is the first CI step and is itself
3.5-compatible, so it must remain importable without pulling in
`lte/validators/corpus.py`, which is not.

Verified mechanically: `python3 -m lte.validators.architecture --package lte`
enforces the layering, and `check_py_compat.py --target 3.5` enforces the
host floor over `lte/runner/` and `lte/validators/architecture.py`. Neither
catches an eager import HERE, because the violation is a runtime import
graph rather than a static one -- which is precisely why it is written down.
"""

__version__ = "2.0.0-migration"
