"""
lte/io/graph_reader.py -- SIDE-EFFECT BOUNDARY. Reads one compiled read-model
artifact from disk. The read-side counterpart of artifact_writer.

Fail-fast on every branch: a missing file, undecodable bytes, malformed JSON
or a non-object top level all raise GraphReadError with the path. Scope
policy (may this caller read an internal graph?) is NOT decided here; that
is lte.engine.retrieval.check_graph_scope(), which is pure.
"""

from __future__ import annotations

import json
from pathlib import Path

from lte.io.artifact_writer import artifact_path


class GraphReadError(OSError):
    """The artifact is missing or is not a JSON object."""


def default_graph_path(repo_root: Path, layout, target: str) -> Path:
    return artifact_path(repo_root, layout, target)


def read_graph(path: Path) -> dict:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise GraphReadError(
            "graph artifact not found: %s (compile it first: python -m lte.cli.compile)" % path
        ) from None
    except OSError as exc:
        raise GraphReadError("cannot read %s: %s" % (path, exc)) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GraphReadError("%s is not valid UTF-8 JSON: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise GraphReadError("%s: top level must be a JSON object" % path)
    return data
