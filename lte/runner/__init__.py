# -*- coding: utf-8 -*-
"""lte.runner -- LAYER 0. Host-side pipeline primitives.

Public surface for lte/cli/pipeline.py. Everything here is either pure or a
declared side-effect boundary; nothing reaches into a higher layer.

PYTHON 3.5 FLOOR APPLIES TO THIS FILE TOO. It executes on the host before
any runner submodule does, so a 3.6+ construct here (an f-string, a variable
annotation) breaks the pipeline exactly as surely as one inside
executor.py. No annotations, no f-strings below.

MUST NEVER IMPORT lte.engine. Both packages are layer 0, so
lte/validators/architecture.py rejects an import in either direction --
which is the mechanical expression of the interpreter split described in
lte/__init__.py. If you need a domain transform in a pipeline step, the step
runs it in the CONTAINER via a command in config/build_config.yaml; it does
not import it here.

Eager re-export is safe in this package (unlike in lte/__init__.py) because
every module below targets the same 3.5 floor.
"""
from lte.runner.config_io import read as read_pipeline_config
from lte.runner.config_schema import (
    DEFAULT_HALT_ON_ERROR,
    DEFAULT_SHELL,
    DEFAULT_SHELL_PRELUDE,
    SUPPORTED_CONFIG_VERSIONS,
    validate as validate_pipeline_config,
)
from lte.runner.environment import MAX_EXPANSION_PASSES, resolve as resolve_environment
from lte.runner.errors import PipelineError
from lte.runner.executor import (
    TIMEOUT_RETURNCODE,
    ExecResult,
    RecordingExecutor,
    SubprocessExecutor,
)

# `reporting` is exported as a MODULE, not flattened. Its functions are all
# named format_* and several (format_summary, format_step_list) would read as
# generic utilities once detached from their namespace, inviting use from
# outside the runner. Keeping the module qualifier makes the owning layer
# visible at every call site.
from lte.runner import reporting

__all__ = [
    "PipelineError",
    "read_pipeline_config",
    "validate_pipeline_config",
    "resolve_environment",
    "ExecResult",
    "SubprocessExecutor",
    "RecordingExecutor",
    "TIMEOUT_RETURNCODE",
    "SUPPORTED_CONFIG_VERSIONS",
    "DEFAULT_HALT_ON_ERROR",
    "DEFAULT_SHELL",
    "DEFAULT_SHELL_PRELUDE",
    "MAX_EXPANSION_PASSES",
    "reporting",
]
