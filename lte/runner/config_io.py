# -*- coding: utf-8 -*-
"""lte/runner/config_io.py -- disk + YAML side-effect boundary for the
pipeline config.

The ONLY module in lte/runner/ that reads a file or imports yaml. It reads
and parses; it does NOT validate. Validation is lte/runner/config_schema's
pure job, and keeping them apart is what lets the schema be exercised
against a literal dict with no fixture file on disk.
"""
import sys

from lte.runner.errors import PipelineError

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "[task_runner] FATAL: PyYAML is not installed for this interpreter.\n"
        "[task_runner] Install it on the HOST: python3 -m pip install pyyaml\n"
        "[task_runner] (PyYAML is the only host dependency -- every "
        "project-specific step still runs inside the lte-cas image.)\n"
    )
    sys.exit(1)


def read(path):
    """Reads and YAML-parses `path`. Returns whatever it parsed to --
    including a non-mapping, which config_schema.validate() rejects. This
    function deliberately does not pre-judge the shape; one validator, one
    place."""
    if not path.is_file():
        raise PipelineError("{0} is missing.".format(path))
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle.read())
    except yaml.YAMLError as exc:
        raise PipelineError("{0} is not valid YAML: {1}".format(path, exc))
