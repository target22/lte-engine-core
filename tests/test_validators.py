#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_validators.py -- the pre-ingest and corpus-wide gates.

Covers lte/validators/draft.py and lte/engine/integrity.py: that the draft
gate reads its diagnostic patterns off the Grammar rather than compiling a
second copy, that banned constructs are hard errors, that fenced legacy
syntax is NOT flagged, and that parent/orphan resolution never invents an
edge.

Run: python3 tests/test_validators.py
"""
import inspect
import sys

from _harness import Reporter, build, engine_config, source_of

from lte.engine import ast_blocks, integrity
from lte.validators import draft

r = Reporter()
config = engine_config()
g = config.grammar
REL = "docs/public/core/01-price.contract.en.md"


# ----------------------------------------------------------------------
print("--- the draft gate compiles nothing of its own ---")
# ----------------------------------------------------------------------
for gone in ("DiagnosticPatterns", "compile_diagnostic_patterns", "find_linter_rules",
             "check_truncated_admonitions"):
    r.check("draft.%s is gone" % gone, not hasattr(draft, gone))

src = source_of("lte", "validators", "draft.py")
r.check("draft.py compiles no regex at all", "re.compile" not in src)

for fn, expected in [
    ("check_likely_typos", ["grammar", "text"]),
    ("check_banned_constructs", ["grammar", "text"]),
    ("validate_batch", ["config", "candidates", "corpus_documents"]),
    ("validate_draft", ["config", "draft_text", "target", "corpus_documents"]),
]:
    got = list(inspect.signature(getattr(draft, fn)).parameters)
    r.check("draft.%s%s" % (fn, tuple(expected)), got == expected, str(got))

# Identity, not merely equivalence: the objects the gate matches with must be
# the Grammar's own, or the two could drift.
probe = '!!! note "x"\n'
r.check("the gate uses the Grammar's compiled patterns",
        bool(g.loose_admonition_open.match(probe))
        and len(draft.check_banned_constructs(g, probe)) == 1)


# ----------------------------------------------------------------------
print("\n--- banned constructs are errors, and fence-aware ---")
# ----------------------------------------------------------------------
BAD = '!!! note "T"\n    body\n\n| a | b |\n|---|---|\n'
found = draft.check_banned_constructs(g, BAD)
r.check("un-fenced admonition and table rows flagged", len(found) == 3, str(len(found)))
for line_no, message in found:
    print("     line %d: %s" % (line_no, message[:64]))

GUIDE = '''# Migration guide

Before:

```markdown
!!! note "Price bounds"

    Bids stay inside the band.

    ^spec-lte-01-002

| old | new |
|-----|-----|
```

After:

## Price bounds
Bids stay inside the band.

^spec-lte-01-002
'''
r.check("a migration guide's FENCED legacy syntax is not flagged",
        draft.check_banned_constructs(g, GUIDE) == [],
        str(draft.check_banned_constructs(g, GUIDE)))

typos = draft.check_likely_typos(
    g, "^legal-contracts-15a1-1501a2\n> [!refs-spec-lte-01-002] x\n")
r.check("an unknown anchor domain is warned", any("anchor" in m for _, m in typos))
r.check("an unmatched token names the configured kinds",
        any(k in m for _, m in typos for k in g.callout_kind_aliases), str(typos))


# ----------------------------------------------------------------------
print("\n--- _file_checks: errors block, warnings do not ---")
# ----------------------------------------------------------------------
DRAFT_OK = "## Price bounds\nBand holds.\n\n^spec-lte-01-002\n"
DRAFT_BAD = '!!! note "Price bounds"\n\n    Band holds.\n\n    ^spec-lte-01-002\n'

errors, warnings, nodes, callouts = draft._file_checks(config, REL, DRAFT_BAD)
banned = [e for e in errors if "BANNED CONSTRUCT" in e]
r.check("an un-fenced admonition is an ERROR", len(banned) == 1, str(errors))
r.check("it is not filed as a warning", not any("BANNED" in w for w in warnings))

errors, warnings, nodes, callouts = draft._file_checks(config, REL, DRAFT_OK)
r.check("a conforming draft raises no banned-construct error",
        not any("BANNED CONSTRUCT" in e for e in errors), str(errors))
r.check("and still parses its node", len(nodes) == 1)

report = draft.validate_draft(config, DRAFT_OK, REL, [])
r.check("validate_draft accepts a conforming draft", report.ok, str(report.errors))
r.check("and counts its nodes", report.node_count == 1)
report = draft.validate_draft(config, DRAFT_BAD, REL, [])
r.check("validate_draft rejects a banned construct", not report.ok)


# ----------------------------------------------------------------------
print("\n--- integrity: no invented edges ---")
# ----------------------------------------------------------------------
known = {"spec-lte-01-002": 1}
r.check("a declared suffix over a real prefix resolves",
        integrity.parent_anchor_of(g, "spec-lte-01-002-c01", known) == "spec-lte-01-002")
r.check("a composite key splits on the separator",
        integrity.parent_anchor_of(g, "spec-lte-01-002#ops-p01", known) == "spec-lte-01-002")
r.check("an undeclared suffix letter is not a suffix",
        integrity.parent_anchor_of(g, "spec-lte-01-002-x99", known) is None)
r.check("a suffix whose prefix names nothing stays top-level",
        integrity.parent_anchor_of(g, "spec-other-99-001-c01", known) is None)

nodes, callouts = ast_blocks.parse_text(
    g, REL,
    "## X\nt\n\n^spec-lte-01-002\n\n"
    "> [!ref-spec-lte-01-002] a\n> b\n\n"
    "> [!evi-spec-lte-01-002#debate-d01] cites the debate\n> b\n")
r.check("a composite child key is an addressable target",
        integrity.find_orphan_refs(nodes, callouts) == [])

_, missing = ast_blocks.parse_text(g, REL, "> [!ref-spec-nope-01-001] x\n> y\n")
r.check("a genuinely missing anchor is still an orphan",
        len(integrity.find_orphan_refs([], missing)) == 1)

r.check("duplicate anchors are detected",
        list(integrity.find_duplicate_spec_ids(nodes + nodes)) == ["spec-lte-01-002"])

sys.exit(r.finish())
