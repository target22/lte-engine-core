"""
lte/cli/ask.py -- ORCHESTRATOR. Zero-Server embedded RAG consumer over the
compiled read-model.

REPLACES scripts/core_pipeline/ask_lte.py. Read-model only: loads one graph
artifact, retrieves, assembles a prompt, and either prints it or sends it
once to a completion endpoint, then exits. No index is persisted, no
process stays running.

  Mode 1 (default)  print the assembled prompt to stdout, for pasting
                    into any chat client.
  Mode 2 (--api)    POST it to an OpenAI-compatible /chat/completions
                    endpoint and print the answer.

  python -m lte.cli.ask "what bounds the price floor?"
  python -m lte.cli.ask --lang en --internal "spec-lte-01-001 obligations"
  python -m lte.cli.ask --target full "..."     # any target from corpus_layout.yaml

Targets, their artifact paths and the default language come from
config/corpus_layout.yaml.
  LTE_API_KEY=... python -m lte.cli.ask --api --model M --base-url URL "..."

Logic lives in lte.engine.retrieval (pure), lte.io.graph_reader and
lte.io.llm_client (side effects). Runs in the lte-cas container: its
imports pull lte.io, which targets Python 3.9+.

Exit codes: 0 answered/printed, 1 no matching units or API failure,
2 usage, graph or scope error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lte.engine import retrieval
from lte.engine.grammar import ConfigError
from lte.engine.layout import LayoutError
from lte.io import config_reader
from lte.io.graph_reader import GraphReadError, default_graph_path, read_graph
from lte.io.llm_client import DEFAULT_TIMEOUT_SECONDS, ChatCompletionClient, LlmClientError

EXIT_OK, EXIT_NO_ANSWER, EXIT_USAGE = 0, 1, 2
API_KEY_ENV_VARS = ("LTE_API_KEY", "OPENAI_API_KEY")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python -m lte.cli.ask",
        description="Retrieve from the compiled JSON graph and build a grounded prompt.")
    parser.add_argument("query", nargs="+", help="question text; anchor ids are matched exactly")
    parser.add_argument("--graph", type=Path, help="artifact path (default: the target's artifact)")
    parser.add_argument("--target", help="compile target whose artifact to read "
                                         "(default: first public target, or first restricted with --internal)")
    parser.add_argument("--internal", action="store_true",
                        help="allow restricted artifacts, and default to the first restricted target")
    parser.add_argument("--lang", default=None, help="corpus locale (default: the layout's first locale)")
    parser.add_argument("--kinds", default=",".join(retrieval.ALL_KINDS),
                        help="seed kinds, comma list of contract,debate,ops "
                             "(parents/children are still attached)")
    parser.add_argument("--limit", type=int, default=5, help="seed hits (default: 5)")
    parser.add_argument("--max-units", type=int, default=12,
                        help="context blocks after expansion (default: 12)")
    parser.add_argument("--retriever", default="inmemory", choices=sorted(retrieval.RETRIEVERS))
    parser.add_argument("--show-scores", action="store_true")
    parser.add_argument("--repo-root", type=Path, help="override SRKH_REPO_ROOT discovery")
    api = parser.add_argument_group("API mode")
    api.add_argument("--api", action="store_true", help="send the prompt instead of printing it")
    api.add_argument("--model", default=os.environ.get("LTE_API_MODEL"))
    api.add_argument("--base-url", default=os.environ.get("LTE_API_BASE", "https://api.openai.com/v1"))
    api.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    unknown = sorted(set(kinds) - set(retrieval.ALL_KINDS))
    if unknown or not kinds:
        parser.error("--kinds: unknown or empty kind(s): %s" % (", ".join(unknown) or "(none)"))
    if args.limit < 1 or args.max_units < 1:
        parser.error("--limit and --max-units must be >= 1")
    if args.api and not args.model:
        parser.error("--api requires --model (or LTE_API_MODEL)")
    args.kinds = kinds
    args.query = " ".join(args.query)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = config_reader.load(repo_root=args.repo_root)
        layout = config.layout
        lang = args.lang or layout.locales[0]
        target = args.target or layout.default_target(restricted=args.internal)
        path = args.graph or default_graph_path(config.repo_root, layout, target)
        graph = read_graph(path)
        scope = retrieval.check_graph_scope(
            graph, allow_restricted=args.internal,
            restricted_targets=layout.restricted_target_names(),
            restricted_prefixes=layout.restricted_source_prefixes())
        units = retrieval.flatten(graph, lang)  # index every kind; --kinds limits seeds
        retriever = retrieval.build_retriever(args.retriever, units, seed_kinds=args.kinds)
        seeds = retriever.retrieve(args.query, args.limit)
        hits = retrieval.expand(retriever, seeds, args.max_units)
    except (ConfigError, LayoutError, GraphReadError, retrieval.RetrievalError) as exc:
        print("[ask] %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except NotImplementedError as exc:
        print("[ask] %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    if not hits:
        print("[ask] no units in %s (%s, %d indexed) match the query" % (path, lang, len(units)),
              file=sys.stderr)
        return EXIT_NO_ANSWER

    envelope = retrieval.PromptEnvelope(
        system_instructions=retrieval.SYSTEM_INSTRUCTIONS, hits=tuple(hits),
        query=args.query, graph_meta=retrieval.graph_meta(graph, lang, scope))
    prompt = retrieval.assemble_prompt(envelope, show_scores=args.show_scores)

    if not args.api:
        sys.stdout.write(prompt)
        return EXIT_OK

    api_key = next((os.environ[v] for v in API_KEY_ENV_VARS if os.environ.get(v)), None)
    client = ChatCompletionClient(base_url=args.base_url, model=args.model,
                                  api_key=api_key, timeout=args.timeout)
    try:
        answer = client.complete(envelope.system_instructions, prompt)
    except LlmClientError as exc:
        print("[ask] API ERROR: %s" % exc, file=sys.stderr)
        return EXIT_NO_ANSWER
    sys.stdout.write(answer.rstrip("\n") + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
