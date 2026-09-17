# -*- coding: utf-8 -*-
"""lte.io -- LAYER 1. Side-effect boundaries.

The exclusive owners of disk reads, disk writes, file hashing, cache state
and Git subprocess calls. May import lte.engine (downward); must never
import lte.validators or lte.cli.

`graph_builder` is NOT re-exported here even though io feeds it -- it lives
in lte.engine and is reached through that package. A convenience alias would
make a pure transform look like an I/O facility.

Submodules are exported as MODULES rather than flattened. Every name below
is an I/O operation, and `from lte.io import read_document_text` at a call
site hides that fact; `corpus_reader.read_document_text(...)` does not. In a
package whose entire purpose is to make side effects visible, the qualifier
is the feature.
"""
from lte.io import build_cache, config_reader, corpus_reader, source_provider
from lte.io.config_reader import EngineConfig, load as load_engine_config
from lte.io.corpus_reader import CorpusReadError

__all__ = [
    "build_cache",
    "config_reader",
    "corpus_reader",
    "source_provider",
    "EngineConfig",
    "load_engine_config",
    "CorpusReadError",
]
