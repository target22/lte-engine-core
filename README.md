# lte-engine-core

A decoupled, config-driven bilingual Markdown-to-graph knowledge compiler
and static JSON generator.

`lte-engine-core` reads a corpus of Markdown documents, extracts relational
nodes from explicit AST tokens declared in a repository's own configuration
(`taxonomy.yaml`, `linter_rules.json`, `ui_projection.yaml`,
`corpus_layout.yaml`), and compiles them into deterministic JSON graph
artifacts. It has no knowledge of any downstream domain: no business
vocabulary, no SOPs, no fixed document taxonomy. Everything domain-specific
lives in the config the calling repository supplies.

## What this repository is

- A Python package (`lte/`) exposing `compile`, `ingest`, `lint`, `ask`,
  `pipeline`, and `pre-commit` entry points under `lte.cli.*`.
- `lte-engine.manifest`: a declarative contract (command names, placement,
  environment mapping, container image recipe) consumed by the host-wide
  `lte` wrapper. This is the only file a future non-Python rewrite of the
  engine needs to update; see the manifest's own comments.
- `bin/lte`: a reference implementation of that wrapper, installed once per
  host at `/usr/local/bin/lte`.

## What this repository is not

- Not a corpus. It ships no `docs/`, no example content, and no downstream
  business rules.
- Not a deployment target on its own. A calling repository pins a released
  tag of this engine as a Git submodule (conventionally at `lib/lte`) and
  supplies its own `config/` directory alongside it.

## Usage

A calling repository adds this engine as a submodule, pins a tagged
release, and drives it through the `lte` wrapper:

```bash
git submodule add git@github.com:target22/lte-engine-core.git lib/lte
cd lib/lte && git checkout v1.0.0-LTS && cd -
lte compile --source disk --target public
```

See this engine's own `lte-engine.manifest` and the calling repository's
deployment playbook for the full wrapper contract, container image
requirements, and Phase 2 (native, no-Docker) installation path via
`pip install -e lib/lte`.

## Versioning

Releases are tagged `vX.Y.Z-LTS` on `main`. `pyproject.toml`'s `version`
field tracks the same `X.Y.Z` (PEP 440 forbids the `-LTS` suffix in package
metadata, so the release channel lives in the Git tag only). Downstream
repositories pin an exact tag via their submodule commit; nothing here ever
auto-upgrades a caller's pin.

## License

MIT. See `LICENSE`.
