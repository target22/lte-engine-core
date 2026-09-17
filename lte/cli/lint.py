"""
lte/cli/lint.py -- ORCHESTRATOR. Corpus lint entry point.

REPLACES scripts/core_pipeline/validate_markdown.py. All checks live in
lte.validators.corpus; this module resolves the repo root and config,
calls it, and maps the outcome to a process exit code.

  python -m lte.cli.lint                  strict: CI gate
  python -m lte.cli.lint --soft           dev loop: report everything, exit 0
  python -m lte.cli.lint <path>           lint one file or subtree of the corpus

Exit codes: 0 clean (or --soft), 1 validation failure or unreadable corpus
file, 2 usage or configuration error.

Repo-root discovery goes through config_reader.resolve_repo_root()
(SRKH_REPO_ROOT, then an upward walk), which is what fixes the legacy
"FATAL: /app/scripts/docs does not exist" failure: the legacy script derived
the root from its own depth, and that broke when it moved into
scripts/core_pipeline/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lte.engine.grammar import ConfigError
from lte.io import config_reader
from lte.io.corpus_reader import CorpusReadError
from lte.validators import corpus as corpus_validator

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m lte.cli.lint",
        description="Referential-integrity and lifecycle lint over docs/.")
    parser.add_argument("target", nargs="?", type=Path,
                        help="file or directory under docs/ (default: whole corpus)")
    parser.add_argument("--soft", action="store_true",
                        help="report every issue and exit 0 (local dev loop)")
    parser.add_argument("--repo-root", type=Path, help="override SRKH_REPO_ROOT discovery")
    parser.add_argument("--config-dir", type=Path, help="override SRKH_CONFIG_DIR")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = config_reader.load(config_dir=args.config_dir, repo_root=args.repo_root)
    except (ConfigError, OSError) as exc:
        print("[lint] CONFIG ERROR: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    target = None
    if args.target is not None:
        target = args.target if args.target.is_absolute() else Path.cwd() / args.target
        target = target.resolve()
        if not target.exists():
            print("[lint] target not found: %s" % target, file=sys.stderr)
            return EXIT_USAGE

    try:
        rc = corpus_validator.run(config.repo_root, config=config, target=target, soft=args.soft)
    except CorpusReadError as exc:
        print("[lint] READ ERROR: %s" % exc, file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK if rc == 0 else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
