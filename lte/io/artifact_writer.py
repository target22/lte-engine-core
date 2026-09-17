"""
lte/io/artifact_writer.py -- SIDE-EFFECT BOUNDARY. The exclusive owner of
writes to compiled read-model artifacts.

Nothing here builds, reorders or normalizes a graph, and nothing here knows
where an artifact lives or which sources it may contain: the compile
target's artifact path, its accepted names and its restricted source
prefixes all come from the CorpusLayout (config/corpus_layout.yaml) through
the caller. This module serializes deterministically, refuses a target
mismatch or a leak, and puts the bytes on disk.

THE LEAK GATE. A write is refused if ANY string value in the payload starts
with one of `restricted_prefixes` -- the directory prefixes of every scope
the target does not read. Target selection at listing time is the primary
control; this is the second, independent one, and it is schema-agnostic on
purpose: it does not depend on which keys graph_builder emits.

WHY THE DEFAULT WRITE PRESERVES THE INODE. docker-compose.yml bind-mounts
each artifact as a SINGLE FILE into the nginx containers. A single-file bind
mount pins the inode that existed at container start, so the classic
temp-file + os.replace() pattern would leave nginx serving the stale graph
until a restart. The default strategy therefore validates and serializes
first, then rewrites the existing file in place with one write() + fsync.
The trade-off is a torn-read window during that single write; mounting the
parent DIRECTORY instead removes it and lets callers pass
preserve_inode=False for a true atomic replace.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_FILE_MODE = 0o644  # nginx workers run as a non-root uid
_LEAK_REPORT_LIMIT = 5


class ArtifactWriteError(OSError):
    """The payload was refused, or the bytes could not be written."""


@dataclass(frozen=True)
class WriteResult:
    path: Path
    size: int
    sha256: str
    strategy: str  # "in_place" | "atomic_replace" | "created" | "unchanged"


def artifact_path(repo_root: Path, layout, target: str) -> Path:
    """The target's artifact file, from the layout, under repo_root."""
    return Path(repo_root) / layout.artifact_path(target)


def serialize(graph: Mapping, indent: int | None = 2) -> bytes:
    """
    Deterministic bytes for a graph mapping.

    Key order is preserved, NOT sorted: graph_builder already emits a fixed
    order, and sorting would reorder every key relative to the legacy
    artifact for no semantic gain. No trailing newline: the legacy
    compilers wrote json.dumps() output verbatim, and byte parity with
    them is a migration gate. allow_nan=False because NaN is not JSON and
    the browser's JSON.parse rejects it.
    """
    text = json.dumps(graph, ensure_ascii=False, indent=indent, allow_nan=False)
    return text.encode("utf-8")


def declared_scope(graph: Mapping) -> str | None:
    metadata = graph.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("scope"), str):
        return metadata["scope"]
    value = graph.get("scope")
    return value if isinstance(value, str) else None


def find_restricted_sources(value, restricted_prefixes: Sequence[str], location: str = "$",
                            limit: int = _LEAK_REPORT_LIMIT) -> list:
    """Locations of string values that start with a restricted source prefix."""
    prefixes = tuple(p for p in restricted_prefixes if p)
    if not prefixes:
        return []
    found, stack = [], [(location, value)]
    while stack and len(found) < limit:
        where, item = stack.pop()
        if isinstance(item, str):
            if item.startswith(prefixes):
                found.append("%s = %r" % (where, item))
        elif isinstance(item, Mapping):
            for key in reversed(list(item)):
                stack.append(("%s.%s" % (where, key), item[key]))
        elif isinstance(item, (list, tuple)):
            for index in range(len(item) - 1, -1, -1):
                stack.append(("%s[%d]" % (where, index), item[index]))
    return found


def assert_writable(graph: Mapping, accepted_scopes: Sequence[str],
                    restricted_prefixes: Sequence[str]) -> None:
    """
    accepted_scopes      every name of the target being written (id + aliases)
    restricted_prefixes  source prefixes that must not appear in this artifact
    """
    if not isinstance(graph, Mapping):
        raise ArtifactWriteError("graph payload must be a mapping, got %s" % type(graph).__name__)
    accepted = tuple(accepted_scopes)
    if not accepted:
        raise ArtifactWriteError("no target name given for the write")
    declared = declared_scope(graph)
    if declared is not None and declared not in accepted:
        raise ArtifactWriteError(
            "target mismatch: payload declares %r but the write targets %r" % (declared, accepted[0])
        )
    leaks = find_restricted_sources(graph, restricted_prefixes)
    if leaks:
        raise ArtifactWriteError(
            "refusing write for %r: payload references restricted sources:\n  %s"
            % (accepted[0], "\n  ".join(leaks))
        )


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return  # not supported on every platform/filesystem
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_in_place(path: Path, payload: bytes) -> None:
    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _write_atomic(path: Path, payload: bytes, mode: int) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)  # mkstemp creates 0600; nginx could not read it
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


def write_graph(graph: Mapping, path: Path, *, accepted_scopes: Sequence[str],
                restricted_prefixes: Sequence[str] = (), indent: int | None = 2,
                preserve_inode: bool = True) -> WriteResult:
    """
    Validates, serializes and writes one artifact.

    Every refusal happens before the target file is opened, so a rejected
    payload never truncates the artifact currently being served.
    """
    assert_writable(graph, accepted_scopes, restricted_prefixes)
    try:
        payload = serialize(graph, indent=indent)
    except (TypeError, ValueError) as exc:
        raise ArtifactWriteError("graph payload is not JSON-serializable: %s" % exc) from exc
    digest = hashlib.sha256(payload).hexdigest()
    path = Path(path)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        if exists and not path.is_file():
            raise ArtifactWriteError("artifact path exists and is not a regular file: %s" % path)
        if exists and path.read_bytes() == payload:
            return WriteResult(path, len(payload), digest, "unchanged")
        if exists and preserve_inode:
            _write_in_place(path, payload)
            strategy = "in_place"
        else:
            mode = stat.S_IMODE(path.stat().st_mode) if exists else DEFAULT_FILE_MODE
            _write_atomic(path, payload, mode)
            strategy = "atomic_replace" if exists else "created"
    except ArtifactWriteError:
        raise
    except OSError as exc:
        raise ArtifactWriteError("cannot write %s: %s" % (path, exc)) from exc
    return WriteResult(path, len(payload), digest, strategy)
