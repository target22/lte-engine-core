# -*- coding: utf-8 -*-
"""lte/io/build_cache.py -- SIDE-EFFECT BOUNDARY. The exclusive owner of
.cache/build_state.json and of file hashing.

Extracted from the legacy graph_lib.py: CACHE_VERSION, build_cache_path,
compute_file_hash, load_build_cache, save_build_cache, cached_or_parse.

WHAT THE CACHE IS AND IS NOT. It memoizes the expensive
read-and-regex-parse step for files whose bytes are unchanged. It does NOT
memoize any judgement about validity. Callers MUST still run every
corpus-wide check -- duplicate, orphan, misplaced, cross-boundary -- over
the FULL combined node/callout set on every run, cache hit or not. A cache
hit means "these bytes parsed to this AST before", never "this file was
fine before".

CACHE_VERSION STAYS AT 2. The decomposition changed where the parser lives,
not what it emits: lte/engine/ast_blocks.parse_text() was verified in Batch
1 to produce byte-identical SpecNode/RefCallout output to the legacy
parse_file() for the same input, across all 12 compiled patterns. A
gratuitous bump would discard every cache entry in every working tree and
CI runner for no behavioural reason. Bump it only when the SERIALIZED SHAPE
below changes -- a new SpecNode field, a different key layout -- because
that is what a stale entry would deserialize wrongly.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from lte.engine.ast_blocks import RefCallout, SpecNode
from lte.engine.grammar import Grammar
# Submodule import, NOT `from lte.io import corpus_reader`.
#
# The `from`-form names the PACKAGE as the import source, so it creates a
# build_cache -> lte.io edge. Combined with lte/io/__init__.py importing
# build_cache, that is a genuine cycle -- caught by
# lte/validators/architecture.py the moment the package __init__ was added:
#
#     IMPORT CYCLE: lte.io -> lte.io.build_cache -> lte.io
#
# It also re-enters a partially-initialised package at runtime: while
# lte/io/__init__.py is still executing, `from lte.io import corpus_reader`
# asks for an attribute on a module object that does not have it yet.
# CPython happens to recover by falling back to a submodule import, but
# relying on that is relying on an implementation detail to paper over a
# cycle. Addressing the module directly removes both problems.
import lte.io.corpus_reader as corpus_reader

CACHE_VERSION = 2
CACHE_RELATIVE_PATH = (".cache", "build_state.json")


def build_cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*CACHE_RELATIVE_PATH)


def empty_cache() -> dict:
    """A fresh, valid cache document. Returned by every load() failure path
    so a caller never has to distinguish 'missing' from 'corrupt' from
    'stale' -- all three mean the same thing operationally: parse everything
    this run."""
    return {"version": CACHE_VERSION, "files": {}}


def compute_file_hash(path: Path) -> str:
    """SHA-256 of the file's raw BYTES, not its decoded text.

    Bytes, deliberately: hashing decoded text would miss an encoding-level
    change (a BOM appearing, CRLF becoming LF) that alters the file on disk
    without altering its decoded content. The cache's job is "are these the
    same bytes I parsed last time", and only a byte hash answers that.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_build_cache(repo_root: Path) -> dict:
    """Reads .cache/build_state.json, or returns a fresh cache.

    Three distinct failure modes all collapse to empty_cache(), and none of
    them is an error worth reporting: a missing file (first run), unreadable
    or malformed JSON (an interrupted write), and a version mismatch. The
    last one matters most -- entries written under a different schema must
    not be trusted, because deserializing a stale shape via SpecNode(**n)
    would either raise deep inside a corpus walk or, worse, succeed and
    silently populate a field with the wrong meaning.
    """
    path = build_cache_path(repo_root)
    if not path.is_file():
        return empty_cache()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return empty_cache()
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return empty_cache()
    if not isinstance(data.get("files"), dict):
        return empty_cache()
    return data


def save_build_cache(repo_root: Path, cache: dict) -> None:
    """Writes the cache, creating .cache/ if needed.

    WRITTEN VIA A TEMP FILE AND AN ATOMIC RENAME. The legacy version wrote
    in place, so an interrupted run (Ctrl-C during a long compile, a killed
    container) could leave a truncated JSON document on disk. That was
    survivable only because load_build_cache() swallows a JSONDecodeError --
    the cost was a silent full reparse on the next run, with no indication
    why. os.replace() is atomic on POSIX and on Windows, so a reader now
    sees either the old cache or the new one, never a half-written one.
    """
    import os
    import tempfile

    path = build_cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cache, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".build_state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, str(path))
    except BaseException:
        # Best-effort cleanup. A leftover .tmp is harmless (load only ever
        # opens build_state.json), but leaving one per interrupted run would
        # accumulate.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def cached_or_parse(grammar: Grammar, callout_kind_by_type, path: Path,
                    repo_root: Path, cache: dict) -> tuple:
    """Returns (nodes, callouts, was_cache_hit).

    Reuses a file's stored parse output ONLY when its current SHA-256 matches
    the cached one. On a miss, parses through lte/io/corpus_reader.py and
    records the result.

    THE CACHE MUTATES `cache` IN PLACE and does not write it. The caller owns
    persistence via save_build_cache(), because a caller that aborts midway
    -- the linter hitting a fatal read error, say -- should not leave a
    partially-updated cache claiming files were validated in a run that never
    finished.

    `SpecNode(**n)` round-trips because status_override carries a default;
    an entry written before that field existed still deserializes. That
    safety net is why CACHE_VERSION exists as well: the default handles one
    added optional field, the version handles anything larger.
    """
    rel = corpus_reader.relative_label(path, repo_root)
    current_hash = compute_file_hash(path)
    entry = cache["files"].get(rel)

    if entry is not None and entry.get("sha256") == current_hash:
        nodes = [SpecNode(**n) for n in entry["nodes"]]
        callouts = [RefCallout(**c) for c in entry["callouts"]]
        return nodes, callouts, True

    nodes, callouts = corpus_reader.parse_file(
        grammar, callout_kind_by_type, path, repo_root)
    cache["files"][rel] = {
        "sha256": current_hash,
        "last_validated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "nodes": [asdict(n) for n in nodes],
        "callouts": [asdict(c) for c in callouts],
    }
    return nodes, callouts, False


def prune_missing(cache: dict, present_rel_paths) -> int:
    """Drops entries for files no longer in the corpus. Returns the count.

    Not called automatically. The legacy cache grew monotonically -- a
    deleted or renamed document's entry stayed forever, so build_state.json
    accumulated dead weight proportional to churn rather than to corpus size.
    Left opt-in rather than wired into save_build_cache() because a caller
    that parsed only a SUBSET of the corpus (the VS Code bridge lints one
    file) would otherwise prune every entry it simply did not look at.
    """
    present = set(present_rel_paths)
    stale = [rel for rel in cache["files"] if rel not in present]
    for rel in stale:
        del cache["files"][rel]
    return len(stale)
