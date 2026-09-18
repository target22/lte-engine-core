# -*- coding: utf-8 -*-
"""lte/io/corpus_reader.py -- SIDE-EFFECT BOUNDARY. The exclusive owner of
disk reads over the corpus tree.

Extracted from the legacy graph_lib.py's Path-taking wrappers:
extract_frontmatter, document_metadata_of, parse_file,
extract_document_title, extract_prologue.

parse_corpus() was extracted alongside them and then never called by
anything: discovery went through iter_layout_documents() instead. Removed
rather than maintained through the pure-text signature change.

THE SHAPE OF THIS MODULE IS THE WHOLE POINT. Every function below is the
same three steps: resolve a path label, read bytes to text, hand the text to
a pure function in lte/engine/. There is no parsing logic here at all. If a
change to this module requires knowing what a `> [!ref-...]` block looks
like, the change belongs in lte/engine/ast_blocks.py instead.

EXACTLY ONE read call exists in this module -- read_document_text() below.
Every wrapper routes through it. That is deliberate: a second inlined
read_text() would be a second place to get encoding handling wrong, and
encoding is precisely where this pipeline's "absence is absence, not a build
failure" rule and its fail-fast rule collide. See that function's docstring.

DISCOVERY IS LAYOUT-DRIVEN. Which directories are walked, which files count
as corpus files and which locale each one has are CorpusLayout answers
(lte/engine/layout.py, config/corpus_layout.yaml). This module builds no
directory name and no filename suffix of its own; see the Discovery section.

GRAMMAR IS INJECTED. The engine functions take a Grammar; so do these. The
legacy signatures had no such parameter because graph_lib built its regexes
at import time via a file read -- the behavior this whole decomposition
exists to remove. scripts/ is deprecated and its shim is gone; the Grammar
parameter is now the only way these functions learn the token language.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from lte.engine import ast_blocks, frontmatter
from lte.engine.grammar import Grammar


class CorpusReadError(OSError):
    """A corpus file exists but could not be read as UTF-8 text."""


def relative_label(path: Path, repo_root: Path) -> str:
    """The repo-relative POSIX path used as a node's `source_file`.

    Falls back to the absolute path rather than raising when `path` is not
    under `repo_root`. A caller linting a file outside the repo (the VS Code
    bridge does exactly this on a scratch buffer) should get a usable label,
    not a ValueError from relative_to() -- the label is diagnostic text, not
    an identity key.
    """
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def read_document_text(path: Path) -> str:
    """THE read call. Every other function in this module routes through it.

    Raises CorpusReadError on an undecodable file rather than degrading to
    "". This is the one place the pipeline's two error philosophies meet, so
    the choice is worth stating: a malformed front matter BLOCK degrades to
    {} because the rest of the file is still valid content, but a file whose
    BYTES are not UTF-8 has no recoverable content at all -- silently
    treating it as empty would report a file full of rules as contributing
    zero nodes, which is indistinguishable from a file that genuinely has
    none.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusReadError(
            "{0} is not valid UTF-8 ({1}). A corpus file that cannot be decoded has no "
            "recoverable content; fix the encoding rather than letting it compile to "
            "zero nodes.".format(path, exc)
        ) from None
    except OSError as exc:
        raise CorpusReadError("cannot read {0}: {1}".format(path, exc)) from None


# ----------------------------------------------------------------------
# Front matter
# ----------------------------------------------------------------------

def extract_frontmatter(path: Path, grammar: Grammar | None = None) -> dict:
    """Parse the leading `---`/`---` YAML front matter block into a dict.

    Returns {} for a file with no fence, a block that is not valid YAML, or
    one that parses to something other than a mapping -- never raises for
    content reasons. This mirrors every other "absence is absence" field in
    the pipeline (prologue, summary, codeowner).

    Uses PyYAML's safe_load, not a hand-rolled key:value regex. Unlike the
    anchor/callout grammar -- a closed token language this project owns
    end-to-end -- front matter is genuine YAML with its own quoting,
    escaping, multi-line and list rules. "Never fuzzy-match" means using a
    real parser for a real grammar, not regexing around one.

    RETURNS RAW FRONT MATTER, every key intact. Use this, not
    document_metadata_of(), when validating dependent-lifecycle fields: that
    function drops `archived_reason`, which is exactly how a correctly-tagged
    file came to be reported as missing the field it carried.

    Deliberately independent of parse_file()/cached_or_parse(): no SHA-256
    cache participation, no CACHE_VERSION bump, always computed fresh. Cost
    is bounded by the front matter block's own size -- always small, always
    at the very top of the file -- not by file size.
    """
    fence = grammar.frontmatter_fence if grammar is not None else None
    return frontmatter.parse_text(read_document_text(path), fence)


def document_metadata_of(path: Path, grammar: Grammar | None = None) -> dict:
    """Coerces front matter into the seven document-level fields the frontend
    renders (status, author, date, version, tags, supersedes, superseded_by).

    Does NOT validate `status` against the declared status values -- that
    belongs to the compiler, the same place every other soft-fail WARN
    already lives, not scattered into this otherwise print-free module.
    `status` here is only case/whitespace-normalized.

    Coercion rules, each independently defensive against a wrong YAML shape
    (a hand-typed file WILL eventually have `tags:` written as a bare string)
    rather than raising on something the author didn't intend:
      - tags: a list -> stringified/trimmed/empty-filtered items; a non-empty
        bare string -> a single-item list (a human writing `tags: solo-tag`
        means one tag, not "please crash"); anything else -> [].
      - author/date/version: any non-empty scalar -> str(...).strip(); empty,
        None, or missing -> None.

    DROPS EVERY OTHER KEY, `archived_reason` included. This is the wrong
    function for lifecycle validation -- use extract_frontmatter() above.
    """
    fence = grammar.frontmatter_fence if grammar is not None else None
    return frontmatter.metadata_from_text(read_document_text(path), fence)


# ----------------------------------------------------------------------
# AST blocks
# ----------------------------------------------------------------------

def parse_file(grammar: Grammar, path: Path, repo_root: Path) -> tuple:
    """Parses one Markdown file into its anchor nodes and dependent callouts.

    The read happens here; the dispatch happens in
    lte/engine/ast_blocks.parse_text(). Returns (nodes, callouts).

    `callout_kind_by_type` is gone from this signature. It carried
    Taxonomy.admonition_callout_kind -- the map from an admonition type to a
    callout kind -- and with Grammar 2 removed the enclosing block no longer
    decides anything. A dependent block's role now comes from the configured
    alias table, which reaches the parser on the Grammar it already takes.
    """
    rel_path = relative_label(path, repo_root)
    return ast_blocks.parse_text(grammar, rel_path, read_document_text(path))


def extract_document_title(grammar: Grammar, path: Path) -> str:
    """The file's first true H1 ('# ...', not '## ...') -- the Tier-3 title.

    Falls back to the document id derived from the filename when the file has
    no H1. Defensive: every real document here has one, and a malformed file
    should degrade to a readable label rather than crash the whole graph
    build over one bad file.

    The fallback is computed HERE, not inside the engine: deriving a document
    id needs a filename, and reaching for one inside a pure function would
    reintroduce exactly the filesystem coupling this split removes.
    """
    fallback = ast_blocks.document_id_from_name(grammar, path.name)
    return ast_blocks.title_from_text(grammar, read_document_text(path), fallback)


def extract_prologue(grammar: Grammar, path: Path) -> str:
    """Raw Markdown of whatever sits BEFORE the first recognized AST block.

    Exists so a document that legitimately parses to zero nodes -- a draft
    with only prose, or a file whose blocks all fail to anchor -- still has
    something for a human to read instead of a blank pane.

    Deliberately independent of parse_file()/cached_or_parse(): it does not
    participate in the SHA-256 parse cache and needs no CACHE_VERSION bump,
    so it can never desync from nodes/callouts.
    """
    return ast_blocks.prologue_from_text(grammar, read_document_text(path))


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def discover_files(directory: Path, glob: str) -> list:
    """Every path matching `glob` under `directory`, recursively, sorted.

    SORTED IS LOAD-BEARING, not tidiness. rglob() yields in filesystem order,
    which differs between machines and between filesystems. The compiled
    graph's partition/document ordering drives the left nav, and
    metadata.graph_hash digests the whole payload -- an unsorted walk makes
    the hash differ between two machines compiling identical bytes, which
    would make lte/validators/compiler_parity.py report a false divergence.
    """
    return sorted(directory.rglob(glob))


def iter_documents(directory: Path, repo_root: Path, globs: Sequence[str]) -> Iterable:
    """(rel_path, text) pairs for every file matching one of `globs` under
    `directory` -- the SourceProvider contract that
    lte/engine/graph_builder.py consumes.

    `globs` is required: take it from CorpusLayout.discovery_globs(). There
    is no default, so no caller can silently fall back to a fixed locale set.
    Prefer iter_layout_documents(), which also applies the layout's
    partition membership and ignore rules.
    """
    for glob in globs:
        for path in discover_files(directory, glob):
            yield relative_label(path, repo_root), read_document_text(path)


def discover_layout_files(repo_root: Path, layout, partition_ids,
                          locale: str | None = None) -> list:
    """Sorted repo-relative POSIX paths of corpus files in the given
    partitions, as the layout classifies them.

    Walks each partition directory once. A file counts only if
    layout.classify() places it in THAT partition (so ignore rules and the
    filename convention apply) and, when `locale` is given, in that locale.
    Sorted as plain strings, which is Git tree order: the disk and CAS
    sources therefore present files in the same sequence.
    """
    repo_root = Path(repo_root)
    found = set()
    for partition_id in partition_ids:
        base = repo_root / layout.partition_prefix(partition_id)
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root).as_posix()
            info = layout.classify(rel)
            if info is None or info.partition != partition_id:
                continue
            if locale is not None and info.locale != locale:
                continue
            found.add(rel)
    return sorted(found)


def iter_layout_documents(repo_root: Path, layout, partition_ids,
                          locale: str | None = None) -> Iterable:
    """(rel_path, text) pairs for discover_layout_files(); the SourceProvider
    contract lte/io/source_provider.py hands to graph_builder."""
    repo_root = Path(repo_root)
    for rel in discover_layout_files(repo_root, layout, partition_ids, locale):
        yield rel, read_document_text(repo_root / rel)


def walk_corpus_files(corpus_dir: Path, layout) -> list:
    """[(rel_path, Path)] for every regular file under the layout's walk
    directories, with `corpus_dir` standing in for layout.root, sorted by
    rel_path, ignore rules applied. rel_path is always rooted at
    layout.root, whatever `corpus_dir` is called locally.

    Unlike discover_layout_files() this does NOT require partition
    membership or a conforming name: batch ingest reports non-conforming
    files as SKIPPED and rejects conforming files outside every partition,
    and the corpus linter inspects both. Both need to see them.
    """
    corpus_dir = Path(corpus_dir)
    found = {}
    for directory in layout.walk_directories():
        base = corpus_dir / layout.root_relative(directory)
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = layout.corpus_path(path.relative_to(corpus_dir).as_posix())
            if not layout.is_ignored(rel):
                found[rel] = path
    return sorted(found.items())
