"""
lte/io/source_provider.py -- SIDE-EFFECT BOUNDARY. Corpus source access.

THE INJECTION SEAM THAT COLLAPSES TWO COMPILERS INTO ONE. Both providers
yield (rel_path, text) pairs for one compile target and one locale, sorted,
and nothing else; lte.engine.graph_builder cannot tell them apart.

    DiskSourceProvider   a checked-out working tree   (was parse_graph.py)
    CasSourceProvider    a bare repository at a commit (was git_cas.py)

LAYOUT-DRIVEN. Which directories are listed, which suffixes pre-filter the
listing and which files count are all answers from the CorpusLayout. Only
the partition directories the target reads are listed, so a public compile
never opens, and never fetches, a file from a restricted scope.
"""

from __future__ import annotations

from pathlib import Path

from lte.io import corpus_reader


class DiskSourceProvider:
    """Reads a checked-out working tree."""

    def __init__(self, repo_root: Path, layout):
        self.repo_root = Path(repo_root).resolve()
        self.layout = layout

    def iter_documents(self, target: str, locale: str):
        partition_ids = self.layout.partitions_for_target(target)
        return corpus_reader.iter_layout_documents(self.repo_root, self.layout, partition_ids, locale)

    def describe(self) -> str:
        return "disk:%s" % self.repo_root


class CasSourceProvider:
    """Reads a bare repository at one commit. All git calls go through git_client."""

    def __init__(self, repo_dir: Path, commit: str, git_client, layout):
        self.repo_dir = Path(repo_dir)
        self.commit = commit
        self.git_client = git_client
        self.layout = layout

    def iter_documents(self, target: str, locale: str):
        suffixes = self.layout.listing_suffixes(locale)
        wanted = set(self.layout.partitions_for_target(target))
        paths = set()
        for partition_id in sorted(wanted):
            prefix = self.layout.partition_prefix(partition_id)
            for rel in self.git_client.list_paths(self.commit, prefix, suffixes):
                info = self.layout.classify(rel)
                if info is not None and info.locale == locale and info.partition in wanted:
                    paths.add(rel)
        texts = self.git_client.read_texts(self.commit, sorted(paths))
        return iter(sorted(texts.items()))

    def describe(self) -> str:
        return "cas:%s@%s" % (self.repo_dir, self.commit[:12])
