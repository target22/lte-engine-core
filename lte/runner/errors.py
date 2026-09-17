# -*- coding: utf-8 -*-
"""lte/runner/errors.py -- leaf module: the runner's exception taxonomy.

ABSOLUTE LEAF. Zero imports, including stdlib. Everything else in
lte/runner/ may import this; this imports nothing, so it can never
participate in a cycle by construction.

PYTHON 3.5 FLOOR applies to this entire package -- see lte/cli/pipeline.py.
"""


class PipelineError(RuntimeError):
    """The config is malformed. Distinct from a step failing at runtime.

    A step that exits non-zero is a RESULT, carried in an ExecResult and
    reported through the summary. A PipelineError means the pipeline could
    not be constructed at all. Conflating the two is how a config typo ends
    up reported as a build failure of whatever step happened to run first.
    """
