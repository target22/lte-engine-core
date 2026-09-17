# MIGRATION_TESTING_CHECKLIST.md (v3, Batch 3)

Progressive verification of the `lte/` modular migration. **Fail-fast: do not
advance to phase N+1 until every gate in phase N exits 0.**

## What changed from v2, and why

The v2 run on 2026-09-16 failed for five root causes, not for five dozen
separate reasons. Each one is fixed below, where it applies:

| # | Symptom in the log | Root cause | Fix in v3 |
|---|---|---|---|
| R1 | 1.2 "expect 0" still failed; `frontmatter.py` still imports `subprocess` | root's shell aliases `cp='cp -i'` / `rm='rm -i'`; the blank answer to `overwrite?` means **no** | Phase 0.1 repair; every restore uses `\cp -f` / `\rm -f` |
| R2 | `SyntaxError: future feature annotations` on the host | host is Python 3.6.8; `lte.engine`, `lte.io`, `lte.validators.{corpus,draft}` and `lte.cli.*` need 3.9+ | every such step runs through `ctr` (below) |
| R3 | container heredoc printed nothing | `docker run` without `-i` never attaches stdin, so `python -` read EOF | `ctr` always passes `-i` |
| R4 | `scripts/validate_markdown.py` not found; `FATAL: /app/scripts/docs` | legacy scripts moved to `scripts/core_pipeline/` and still derive the repo root one level too shallow; `check_py_compat.py` is in `tools/` | paths corrected; the legacy entry points become shims over `lte.cli.*` (Phase 0.6) |
| R5 | `grep -rl archived_reason: docs/` matched nothing; 2.6 `--dry-run` unrecognized; `lte/runner/pre_commit.py` not found | v2 assumed a corpus file, a legacy flag and a module path that do not exist | 2.4 uses a synthetic fixture; 2.6 compares committed **trees**; 2.7 targets `lte/cli/hooks/pre_commit.py` |

Two findings from the log are not command errors and need a decision, both
in Phase 0: the 1.3 audit flagged `open()` in `lte/engine/test_engine.py`
(tests live inside a pure package), and `lte/cli/ingest.py` was an unported
copy (`import config_loader`).

## Conventions

Run everything from the repository root. `-m` is mandatory for package
modules.

```bash
cd "$(git rev-parse --show-toplevel)"
export SRKH_REPO_ROOT="$PWD"
export SRKH_CONFIG_DIR="$PWD/config"

# Container runner. -i is required for heredocs (R3). Host SRKH_* paths are
# NOT forwarded: they do not exist inside the container.
ctr() {
  docker run --rm -i -v "$SRKH_REPO_ROOT":/app -w "${CTR_WD:-/app}" \
    -e SRKH_REPO_ROOT=/app -e SRKH_CONFIG_DIR=/app/config lte-cas "$@"
}
```

| Runs on the 3.6.8 host | Runs in `lte-cas` (3.11) via `ctr` |
|---|---|
| `lte.validators.architecture`, `lte.cli.pipeline`, `lte.runner.*`, `tools/check_py_compat.py`, the **dispatcher** half of the pre-commit hook, stdlib-only heredocs | everything importing `lte.engine`, `lte.io`, `lte.validators.{corpus,draft,bilingual,compiler_parity}`, `lte.cli.{compile,lint,ingest,ask}`, every legacy script under `scripts/core_pipeline/` |

---

## Phase 0 — Repair and install (one-time)

### 0.1 — Undo the injected violation left by the v2 run (R1)

```bash
if [ -f /tmp/fm.bak ]; then \cp -f /tmp/fm.bak lte/engine/frontmatter.py && \rm -f /tmp/fm.bak; fi
grep -n "deliberate violation" lte/engine/frontmatter.py && echo "STILL DIRTY" || echo "frontmatter.py clean"
```

If `/tmp/fm.bak` is gone and the file is still dirty, delete the two lines
the `printf` appended (a blank line and the `import subprocess` line):
`sed -i '/# deliberate violation$/d' lte/engine/frontmatter.py`, then remove
the trailing blank line it leaves. Confirm with `git diff lte/engine/frontmatter.py`.

### 0.2 — Install the Batch 3 files

| File | Layer | State before |
|---|---|---|
| `lte/io/artifact_writer.py` | 1 | 0 B |
| `lte/io/git_cas_client.py` | 1 | 0 B (but `lte/cli/compile.py` already imports `GitClient` from it) |
| `lte/io/graph_reader.py` | 1 | new |
| `lte/io/llm_client.py` | 1 | new |
| `lte/engine/retrieval.py` | 0, pure | new (logic moved out of `ask.py`) |
| `lte/engine/payload_lock.py` | 0, pure | new (logic moved out of the hook) |
| `lte/validators/draft.py` | 2 | new (logic moved out of `ingest.py`) |
| `lte/cli/lint.py` | 3 | 0 B |
| `lte/cli/ingest.py` | 3 | unported legacy copy |
| `lte/cli/ask.py` | 3 | legacy copy with BM25/generators inline |
| `lte/cli/hooks/pre_commit.py` | 3 | legacy copy |

The four new lower-layer modules exist because the Batch 3 brief forbids
reusable logic in Layer 3. Leave `lte/engine/__init__.py` and
`lte/io/__init__.py` unchanged; the new modules are imported by path, so the
`__all__` counts in 1.6 stay 30 / 7 / 14.

### 0.3 — Register the two new pure modules with the DAG gate

`PURE_MODULES` in `lte/validators/architecture.py` is an explicit set; a
module missing from it is never purity-checked. Add
`"lte.engine.retrieval"` and `"lte.engine.payload_lock"` to it.

```bash
grep -n "PURE_MODULES" lte/validators/architecture.py
```

### 0.4 — Move the test suites out of the package

`lte/engine/test_engine.py` performs file I/O inside a package declared pure
(the three `open()` hits in the v2 1.3 output), and all three suites are
currently counted as package modules by 1.1.

```bash
mkdir -p tests
git mv lte/engine/test_engine.py lte/test_batch2.py lte/test_pipeline.py tests/
```

### 0.5 — Put the `graph_lib` shim where legacy scripts import it from

Legacy scripts in `scripts/core_pipeline/` run with their own directory on
`sys.path`, so `from graph_lib import ...` resolves
`scripts/core_pipeline/graph_lib.py`, **not** `lte/graph_lib.py`. Legacy
`ingest.py --help` exited 0 in the v2 run, which means some `graph_lib`
was found there. Check which one before moving anything:

```bash
ls -l scripts/core_pipeline/graph_lib.py && head -3 scripts/core_pipeline/graph_lib.py
```

If that header is not `TRANSITIONAL COMPATIBILITY SHIM`, Phase 2 has been
exercising the old monolith, not the shim. Replace it:

```bash
git mv -f lte/graph_lib.py scripts/core_pipeline/graph_lib.py
grep -n "_REPO_ROOT = " scripts/core_pipeline/graph_lib.py
```

`_REPO_ROOT` must resolve three levels up from the file
(`scripts/core_pipeline/graph_lib.py` → repo root), e.g.
`Path(__file__).resolve().parents[2]`. Rollback is `git revert`.

### 0.6 — Convert the legacy entry points into shims (R4)

Replace the bodies of `scripts/core_pipeline/validate_markdown.py` and
`scripts/core_pipeline/ingest.py` with this pattern (swap the import):

```python
#!/usr/bin/env python3
"""scripts/core_pipeline/validate_markdown.py -- SHIM over lte.cli.lint. Deleted at migration step 7."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lte.cli.lint import main  # noqa: E402  (ingest.py: from lte.cli.ingest import main)

sys.exit(main())
```

Three `dirname` calls, not two: the scripts are now two directories below
the root. **Run 2.6 before converting `ingest.py`**, because 2.6 needs the
legacy implementation as its baseline. `batch_sync.py` is replaced by
`python -m lte.cli.ingest --batch`; its legacy flags were not available to
this port, so switch the `batch-ingest` step at migration step 6 rather
than shimming it.

### 0.7 — Enable the payload lock in config

`config/linter_rules.json` still has `"payload_lock": {"enforced": false}`.
The new hook honors that flag: with `false`, lock violations are printed as
warnings and the commit proceeds (the atomic-deprecation guardrail blocks
regardless, per `dependent_lifecycle.severity`). 2.8 needs it on:

```bash
sed -i 's/"enforced": false/"enforced": true/' config/linter_rules.json
grep -n '"enforced"' config/linter_rules.json
```

Update the section's `NOTHING ENFORCES THESE YET` comment in the same commit.

### 0.8 — Confirm `compile.py` and `source_provider.py` call methods `GitClient` has

`git_cas_client.py` was empty, so its API was written without seeing the
call sites in `CasSourceProvider`. Every name printed here must be one of:
`run`, `output`, `rev_parse`, `tree_of`, `list_paths`, `list_index_paths`,
`read_blobs`, `read_texts`, `show`, `staged_changes`, `config_value`,
`commit_paths`, `hash_blobs`, `init_bare`, `is_repository`.

```bash
grep -n "GitClient(\|git_client\.[a-z_]*\|self\.git[a-z_]*\.[a-z_]*" lte/io/source_provider.py lte/cli/compile.py
```

A name outside that list is an `AttributeError` at 3.2. Adapt the call
site, or send me the lines and I'll align the client.

---

## Phase 1 — Static Architecture Validation

### 1.1 — DAG gate (host)

```bash
python3 -m lte.validators.architecture --package lte; echo "exit=$? (expect 0)"
```

**Expect exit 0** and `14 pure module(s)`. The module count is
**42** after Phase 0: 41 before, +5 new modules, −3 moved tests, −1 moved
shim. The edge count is informational.

### 1.2 — Negative control (host)

```bash
\cp -f lte/engine/frontmatter.py /tmp/fm.bak
printf '\nimport subprocess  # deliberate violation\n' >> lte/engine/frontmatter.py
python3 -m lte.validators.architecture --package lte; echo "exit=$?  (expect 1)"
\cp -f /tmp/fm.bak lte/engine/frontmatter.py && \rm -f /tmp/fm.bak
python3 -m lte.validators.architecture --package lte; echo "exit=$?  (expect 0)"
grep -c "deliberate violation" lte/engine/frontmatter.py   # expect 0
```

### 1.3 — Independent side-effect audit of `lte/engine/` (host; AST only)

Same script as v2, with one change: skip test modules, in case any are
added back under the package later.

```bash
python3 - <<'PY'
import ast, os, sys
BAD_IMPORTS = {"subprocess", "socket", "requests", "urllib", "shutil", "tempfile", "os"}
BAD_CALLS = {"read_text","write_text","read_bytes","write_bytes","open","mkdir","rglob","glob","unlink"}
bad = []
for root, _, files in os.walk("lte/engine"):
    for f in sorted(files):
        if not f.endswith(".py") or f.startswith("test_"): continue
        p = os.path.join(root, f)
        for n in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
            if isinstance(n, ast.Import):
                bad += ["%s: import %s" % (p, a.name) for a in n.names if a.name.split(".")[0] in BAD_IMPORTS]
            elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in BAD_IMPORTS:
                bad.append("%s: from %s" % (p, n.module))
            elif isinstance(n, ast.Call):
                fn = n.func
                if isinstance(fn, ast.Attribute) and fn.attr in BAD_CALLS:
                    bad.append("%s:%d: .%s()" % (p, n.lineno, fn.attr))
                elif isinstance(fn, ast.Name) and fn.id == "open":
                    bad.append("%s:%d: open()" % (p, n.lineno))
print("engine side-effect violations:", bad or "NONE")
sys.exit(1 if bad else 0)
PY
```

**Expect exit 0**, `NONE`.

### 1.4 — Host interpreter floor (host)

```bash
python3 tools/check_py_compat.py --target 3.5 \
  lte/__init__.py lte/cli/__init__.py lte/cli/hooks/__init__.py \
  lte/cli/hooks/pre_commit.py \
  lte/validators/__init__.py lte/validators/architecture.py \
  lte/runner/*.py
```

**Expect exit 0**, 13 `ok` lines. `pre_commit.py` joins the list because
Git executes it on the host.

### 1.5 — Runtime import isolation (host)

Unchanged from v2; it passed. Also import the hook module, which must pull
in nothing from the container layers:

```bash
python3 -c "
import sys, lte.runner, lte.cli.hooks.pre_commit
leak = sorted(m for m in sys.modules if m.startswith(('lte.engine','lte.io','lte.validators')))
assert not leak, 'host-side import leaked container modules: %s' % leak
print('host-side import isolation: OK')
"
```

### 1.6 — Package export surfaces resolve (container, R2)

```bash
ctr python - <<'PY'
import lte.engine as e, lte.io as io, lte.runner as r
for mod in (e, io, r):
    missing = [n for n in mod.__all__ if not hasattr(mod, n)]
    assert not missing, '%s.__all__ names nothing: %s' % (mod.__name__, missing)
    print('%-12s %2d exports, all resolve' % (mod.__name__, len(mod.__all__)))
assert not hasattr(e, 'parse_text'), 'ambiguous parse_text was flattened into lte.engine'
print('parse_text correctly NOT flattened (ast_blocks vs frontmatter)')
PY
```

**Expect** 30 / 7 / 14, as in the v2 run.

---

## Phase 2 — Shim Compatibility

All legacy scripts live in `scripts/core_pipeline/` and need 3.9+, so every
check here runs in the container.

### 2.1 — Shim imports and binds config

```bash
ctr python - <<'PY'
import sys; sys.path.insert(0, 'scripts/core_pipeline')
import graph_lib as gl
assert 'COMPATIBILITY SHIM' in (gl.__doc__ or ''), 'imported the legacy monolith, not the shim (see 0.5)'
print('partitions      :', len(gl.PARTITIONS))
print('status values   :', list(gl.STATUS_VALUES))
print('schema version  :', gl.GRAPH_SCHEMA_VERSION)
print('cache version   :', gl.CACHE_VERSION)
print('invalidated     :', gl.INVALIDATED_STATE)
assert len(gl.PARTITIONS) == 8 and gl.CACHE_VERSION == 2
PY
```

The docstring assertion is new: the v2 run passed this check against
`lte/graph_lib.py` via `sys.path.insert(0, 'lte')`, which is not the path any
legacy script uses.

### 2.2 — Symbol coverage (recursive glob; the v2 glob matched nothing)

```bash
ctr python - <<'PY'
import ast, glob, subprocess, sys
names, files = set(), 0
for f in glob.glob("scripts/**/*.py", recursive=True):
    if f.endswith("graph_lib.py"): continue
    try: tree = ast.parse(open(f, encoding="utf-8").read())
    except SyntaxError: continue
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "graph_lib":
            names |= {a.name for a in n.names}; files += 1
print("legacy files importing graph_lib:", files, "| symbols:", len(names))
assert names, "zero symbols found -- the glob is wrong, not the shim"
code = ("import sys; sys.path.insert(0,'scripts/core_pipeline'); import graph_lib as g; "
        "m=[n for n in %r if not hasattr(g,n)]; print('MISSING:', m); sys.exit(1 if m else 0)" % sorted(names))
sys.exit(subprocess.call([sys.executable, "-c", code]))
PY
```

**Expect exit 0**, `MISSING: []`, and a non-zero symbol count.

### 2.3 — Legacy lint entry point runs, from two working directories

After 0.6, `validate_markdown.py` is a shim over `lte.cli.lint`, which
discovers the root through `SRKH_REPO_ROOT` or an upward walk (R4).

```bash
ctr python scripts/core_pipeline/validate_markdown.py; echo "exit=$? (expect 0)"
CTR_WD=/tmp ctr python /app/scripts/core_pipeline/validate_markdown.py; echo "exit=$? (expect 0)"
docker run --rm -v "$SRKH_REPO_ROOT":/app -w /tmp lte-cas \
  python /app/scripts/core_pipeline/validate_markdown.py; echo "exit=$? (expect 0, no SRKH_REPO_ROOT: upward walk)"
```

### 2.4 — Lint still rejects an injected violation (synthetic fixture, R5)

No corpus file carries `archived_reason`, so create one and always remove it.

```bash
F=docs/public/core/_lint_fixture
trap '\rm -rf "$F"' EXIT
mkdir -p "$F"
printf -- '---\nstatus: active\n---\n# F\n\n!!! note "KERNEL CONTRACT"\n    Fixture rule.\n    ^spec-lintfx-01-001\n' > "$F/01-f.contract.en.md"
printf -- '---\nstatus: archived\narchived_reason: free text not in the enum\n---\n# F debate\n\n???+ warning "TECHNICAL DEBATE"\n    > [!ref-spec-lintfx-01-001]\n    Fixture debate.\n' > "$F/01-f.debate.en.md"
ctr python -m lte.cli.lint; echo "exit=$? (expect 1)"
ctr python -m lte.cli.lint --soft >/dev/null; echo "exit=$? (expect 0)"
\rm -rf "$F"; trap - EXIT
ctr python -m lte.cli.lint; echo "exit=$? (expect 0)"
```

**Expect** the first run to name the closed set
(`orphaned_dependency, obsolete_logic, …`).

### 2.5 — Legacy `ingest.py` shim resolves from two working directories

```bash
ctr python scripts/core_pipeline/ingest.py --help >/dev/null; echo "exit=$? (expect 0)"
CTR_WD=/tmp ctr python /app/scripts/core_pipeline/ingest.py --help >/dev/null; echo "exit=$? (expect 0)"
```

### 2.6 — Ingest parity: same draft, same committed tree

Byte-diffing stdout cannot work: the legacy tool has no `--dry-run` and the
new one words its diagnostics differently. What must not change is **what
lands in the CAS**. Commit ids differ (timestamps), so compare tree ids.
Both runs share one container, because `/tmp` does not survive between two
`docker run --rm` invocations. Run this **before** 0.6 converts `ingest.py`.

```bash
ctr sh -ec '
mkdir -p /tmp/d/drafts/public/core/_fx
printf "# FX\n\n!!! note \"KERNEL CONTRACT\"\n    Parity rule.\n    ^spec-parity-01-001\n" > /tmp/d/drafts/public/core/_fx/01-p.contract.en.md
for t in legacy new; do git init -q --bare /tmp/$t.git; git --git-dir=/tmp/$t.git symbolic-ref HEAD refs/heads/main; done
A="--author-name ParityBot --author-email p@x.io"
python scripts/core_pipeline/ingest.py /tmp/d/drafts/public/core/_fx/01-p.contract.en.md --repo /tmp/legacy.git $A
python -m lte.cli.ingest              /tmp/d/drafts/public/core/_fx/01-p.contract.en.md --repo /tmp/new.git    $A
python -m lte.cli.ingest /tmp/d/drafts/public/core/_fx/01-p.contract.en.md --repo /tmp/new.git --dry-run
a=$(git --git-dir=/tmp/legacy.git rev-parse "main^{tree}"); b=$(git --git-dir=/tmp/new.git rev-parse "main^{tree}")
echo "legacy tree $a"; echo "new    tree $b"; test "$a" = "$b"'
echo "exit=$? (expect 0: identical trees)"
```

A differing tree almost always means different target-path inference or
newline handling. Inspect with `git --git-dir=... ls-tree -r main` inside the
same `ctr sh -c`.

### 2.7 — Hook host half: 3.5 floor, no module-level container imports

```bash
python3 tools/check_py_compat.py --target 3.5 lte/cli/hooks/pre_commit.py; echo "exit=$? (expect 0)"
python3 - <<'PY'
import ast, sys
tree = ast.parse(open("lte/cli/hooks/pre_commit.py", encoding="utf-8").read())
top = [a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names]
top += [n.module for n in tree.body if isinstance(n, ast.ImportFrom)]
leaks = [m for m in top if m and m.startswith("lte")]
print("module-level imports:", top, "| lte imports:", leaks or "NONE")
sys.exit(1 if leaks else 0)
PY
```

**Expect** `['os', 'subprocess', 'sys']` and `NONE`. The container half
imports `lte.engine`/`lte.io` inside `_run_checks()` only; `cli → engine` is
a legal downward edge, so 1.1 stays green.

### 2.8 — Live commit rejection (host Git, checks in the container)

Prerequisites: 0.7 applied, `lte-cas` built. The hook mounts the fixture
repo at `/work` and your checkout (resolved through the symlink) at
`/lte-src`, so the fixture needs no `lte/` of its own.

```bash
rm -rf /tmp/hook_test && mkdir -p /tmp/hook_test && cd /tmp/hook_test
git init -q && git config user.email t@t.com && git config user.name "Alan N."
ln -sf "$SRKH_REPO_ROOT/lte/cli/hooks/pre_commit.py" .git/hooks/pre-commit
chmod +x "$SRKH_REPO_ROOT/lte/cli/hooks/pre_commit.py"
C=docs/public/core/_fixture/01-x.contract.en.md
D=docs/public/core/_fixture2/01-y.debate.en.md
mkdir -p "$(dirname $C)" "$(dirname $D)"

printf -- '---\nstatus: active\n---\n# X\n\n!!! note "KERNEL CONTRACT"\n    Immutable rule body.\n    ^spec-fixture-01-01\n' > $C
git add . && git commit -qm seed; echo "seed          exit=$? (expect 0)"
printf '???+ warning "TECHNICAL DEBATE"\n    > [!ref-spec-fixture-01-01]\n    Some debate text.\n' > $D
git add . && git commit -qm lock; echo "add ref       exit=$? (expect 0)"

sed -i 's/Immutable rule body\./Mutated locked body./' $C && git add .
git commit -qm illegal; echo "2.8a mutate   exit=$? (expect 1)"
git checkout -q HEAD -- $C

sed -i 's/status: active/status: deprecated/' $C && git add .
git commit -qm dep; echo "2.8b deprecate exit=$? (expect 1: dependent not reconciled)"
{ printf -- '---\nstatus: archived\narchived_reason: superseded_contract\n---\n'; git show HEAD:$D; } > $D
git add . && git commit -qm dep-atomic; echo "2.8c atomic   exit=$? (expect 0)"
cd "$SRKH_REPO_ROOT"
```

**Expect** 2.8a to print
`body of locked node ^spec-fixture-01-01 changed; a referenced contract body is immutable`,
and 2.8b to list `01-y.debate.en.md` as `not staged`.

> **`--lock-on-staged` (corrected note).** v2 said the flag inverts which
> commit fails; it does not. With the default HEAD reading, a commit that
> adds the first reference **and** edits the body in the same commit passes
> (nothing was referenced at HEAD). With `--lock-on-staged` the reference is
> read from the index, so that same commit is blocked. Every commit in the
> sequence above behaves the same under both readings. Git passes no
> arguments to hooks; to use the flag, install a two-line wrapper script as
> `.git/hooks/pre-commit` that execs the hook with `--lock-on-staged`.

> **Fail-closed.** If Docker or the image is unavailable, the hook blocks
> the commit and says so. `git commit --no-verify` is the deliberate bypass.

---

## Phase 3 — CLI Orchestrator Integration

### 3.1 — Entry points respond

```bash
for m in lte.cli.pipeline lte.validators.architecture; do
  python3 -m $m --help >/dev/null 2>&1 && echo "OK   host $m" || echo "FAIL host $m"
done
for m in lte.cli.compile lte.cli.lint lte.cli.ingest lte.cli.ask \
         lte.validators.corpus lte.validators.bilingual lte.validators.compiler_parity; do
  ctr python -m $m --help >/dev/null 2>&1 && echo "OK   ctr  $m" || echo "FAIL ctr  $m"
done
```

**Expect nine `OK` lines.** v2 ran `lte.validators.corpus` and friends on
the host, where they cannot import (R2).

### 3.2 — Container compile, both sources

Unchanged from v2. `--source cas` is the first real exercise of
`GitClient`; if it raises `AttributeError`, return to 0.8. Once
`compile.py` routes its write through
`lte.io.artifact_writer.write_graph(graph, path, scope)`, its
`# noqa: F401` on that import can go.

### 3.3 — Host pipeline dry run

Unchanged from v2.

### 3.4 — Lint and bilingual validators

```bash
ctr python -m lte.cli.lint; echo "exit=$? (expect 0)"
ctr python -m lte.validators.bilingual --check-graph public/data/graph.json
```

### 3.5 — RAG consumer, both modes (new)

```bash
ctr python -m lte.cli.ask --lang en --show-scores "price bounds" | head -20; echo "exit=${PIPESTATUS[0]} (expect 0, or 1 if nothing matches)"
ctr python -m lte.cli.ask --lang en --graph public/internal-data/graph-internal.json "x"; echo "exit=$? (expect 2: internal refused without --internal)"
ctr python -m lte.cli.ask --api "x" 2>&1 | tail -1                                      # expect: --api requires --model
```

API mode reads the key from `LTE_API_KEY` (or `OPENAI_API_KEY`); pass it with
`docker run -e LTE_API_KEY`, never on the command line. `--kinds` now
limits **seed** kinds only; parent contracts are still attached to debate
hits, so a debate never reaches the prompt ungrounded.

> `lte.cli.ask` is container-only now, like `compile`. The legacy
> `ask_lte.py` ran on the host because it imported nothing from `lte`. If
> host execution is a hard requirement, the retrieval modules need a
> 3.5-safe package with an import-free `__init__`, like `lte.runner`.

### 3.6 — Artifact write keeps the live viewer current (new)

`docker-compose.yml` bind-mounts each graph as a **single file**, which pins
the inode; a rename-based write would leave nginx serving stale data.
`write_graph()` rewrites in place by default for this reason.

```bash
before=$(stat -c %i public/data/graph.json)
ctr python -m lte.cli.compile --source disk --scope public --output /app/public/data/graph.json
after=$(stat -c %i public/data/graph.json)
test "$before" = "$after" && echo "inode preserved" || echo "INODE CHANGED -- restart srkh-viewer or mount the directory"
curl -s -o /dev/null -w "viewer HTTP %{http_code}\n" http://localhost:4321/data/graph.json
```

This holds only once `compile.py` writes through `artifact_writer`. Mounting
`./public/data` as a directory instead removes the in-place torn-read window
and allows `preserve_inode=False`.

---

## Phase 4 — Artifact Parity Verification

Unchanged from v2 except for these paths:

- 4.2 baseline: `ctr python scripts/core_pipeline/parse_graph.py --scope public --output /app/.cache/graph-legacy.json`.
  The comparison heredocs are stdlib-only and run on the host.
- 4.5 suites after 0.4:

```bash
PYTHONPATH=. python3 tests/test_pipeline.py         # host: 25 assertions
ctr env PYTHONPATH=/app python tests/test_engine.py  # 70 assertions
ctr env PYTHONPATH=/app python tests/test_batch2.py  # 44 assertions
```

If a suite resolves fixture paths relative to its own file, fix that path
in the same commit as the move.

---

## Sign-off gate

Advance to migration **step 6** only when every box is checked.

- [ ] **0.1** `frontmatter.py` clean; `git diff` empty for it
- [ ] **0.2** Batch 3 files installed (11)
- [ ] **0.3** `PURE_MODULES` includes `retrieval` and `payload_lock`
- [ ] **0.4** Test suites moved to `tests/`
- [ ] **0.5** `scripts/core_pipeline/graph_lib.py` is the shim; `_REPO_ROOT` is three levels up
- [ ] **0.6** `validate_markdown.py` shimmed; `ingest.py` shimmed **after** 2.6
- [ ] **0.7** `payload_lock.enforced` is `true`
- [ ] **0.8** Every `GitClient` method called by `source_provider.py`/`compile.py` exists
- [ ] **1.1** DAG gate: exit 0, 42 modules, 14 pure
- [ ] **1.2** Gate fails on an injected violation, then recovers (with `\cp -f`)
- [ ] **1.3** Engine side-effect audit: `NONE`
- [ ] **1.4** 13 host-side files hold the 3.5 floor
- [ ] **1.5** Host import of `lte.runner` and the hook leaks nothing
- [ ] **1.6** 30 / 7 / 14 exports resolve (container)
- [ ] **2.1** Shim (not the monolith) binds config; 8 partitions, `CACHE_VERSION 2`
- [ ] **2.2** Non-zero legacy symbol count, `MISSING: []`
- [ ] **2.3** Legacy lint entry point exits 0 from three working directories
- [ ] **2.4** Fixture violation rejected; clean after removal
- [ ] **2.5** Legacy ingest shim resolves from two working directories
- [ ] **2.6** Legacy and new ingest produce identical CAS trees
- [ ] **2.7** Hook: 3.5 floor, module-level imports `os, subprocess, sys` only
- [ ] **2.8** Locked edit blocked; unreconciled deprecation blocked; atomic deprecation accepted
- [ ] **3.1** Nine entry points respond (2 host, 7 container)
- [ ] **3.2** Container compile succeeds from `disk` and `cas`
- [ ] **3.3** Pipeline `--list` / `--dry-run` execute nothing
- [ ] **3.4** Lint exits 0; bilingual report reviewed
- [ ] **3.5** `ask` prints a grounded prompt; internal graph refused without `--internal`
- [ ] **3.6** Graph inode preserved across a compile; viewer serves HTTP 200
- [ ] **4.1–4.5** As v2

**Rollback** is still reverting the single `build_config.yaml` commit.
`scripts/` stays until migration step 7.
