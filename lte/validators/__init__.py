# -*- coding: utf-8 -*-
"""lte.validators -- LAYER 2. Corpus, bilingual, parity and architecture gates.

May import lte.engine and lte.io; must never import lte.cli. Each module
exposes a `run(...)` returning an exit code, so the pre-commit hook and the
VS Code bridge can call the same logic the CLI does without importing an
orchestrator.

THIS FILE CONTAINS NO IMPORTS, for the same class of reason as
lte/__init__.py.

`lte/validators/architecture.py` is the DAG gate. It is step 0 of the
migration plan, it is 3.5-compatible on purpose so it can run anywhere, and
it must stay importable on a host interpreter that cannot parse
`lte/validators/corpus.py` (3.9+ syntax, and it pulls in lte.engine and
lte.io transitively). An eager re-export here would couple the gate to the
very modules it exists to police -- so the one check that must run first
would be the one most likely to fail to load at all.

Import the submodule you need:

    from lte.validators import corpus, bilingual, compiler_parity, architecture
"""

__all__ = ["architecture", "bilingual", "compiler_parity", "corpus"]
