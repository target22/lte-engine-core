# -*- coding: utf-8 -*-
"""Verification suite for lte/engine/ against the REAL config and corpus."""
import json, sys, yaml
from lte.engine import taxonomy, grammar, ast_blocks, frontmatter, state_machine, integrity, rendering

P=[0]; F=[0]
def check(label, cond, extra=""):
    if cond: P[0]+=1; print("  PASS  {0}".format(label))
    else: F[0]+=1; print("  FAIL  {0} {1}".format(label, extra))

# --- the lte/io/config_reader role: the ONLY disk access in this test ---
raw_tax = yaml.safe_load(open('config/taxonomy.yaml', encoding='utf-8'))
raw_ui  = yaml.safe_load(open('config/ui_projection.yaml', encoding='utf-8'))
raw_lr  = json.load(open('config/linter_rules.json', encoding='utf-8'))
rows = raw_tax['partitions'] if isinstance(raw_tax, dict) else raw_tax

print("taxonomy")
tax = taxonomy.build_taxonomy(rows, raw_ui)
check("8 partitions in declaration order", [p[0] for p in tax.partitions] ==
      ['core','adr','ops','exec','adr-internal','ops-internal','axioms','core-internal'])
check("5-tuple shape preserved (git_cas unpacks positionally)",
      all(len(p)==5 for p in tax.partitions))
check("partitions_for_scope('public') excludes every private row",
      all(tax.partition_scope[p[0]]=='public' for p in tax.partitions_for_scope('public')))
check("partitions_for_scope('internal') returns ALL rows",
      len(tax.partitions_for_scope('internal')) == len(tax.partitions))
check("domain->partition: spec->core, coreint->core-internal, axiom->axioms",
      tax.partition_of('spec-001')=='core' and tax.partition_of('coreint-x')=='core-internal'
      and tax.partition_of('axiom-x')=='axioms')
check("scope_of routes private correctly", tax.scope_of('axiom-x')=='private'
      and tax.scope_of('spec-001')=='public')
check("unknown domain -> None, not a raise", tax.partition_of('zzz-1') is None)
check("partition_dir is pure path arithmetic",
      str(tax.partition_dir(__import__('pathlib').Path('/r'),'core')) == '/r/docs/public/core')
try:
    tax.layer_for('note','typo-partition'); check("layer_for raises on unknown partition", False)
except KeyError: check("layer_for raises on unknown partition (no silent POLICY default)", True)
check("meta flavor projects META_* labels",
      tax.contract_layer_of('axioms').startswith('META'))
check("kernel vs policy flavors differ",
      tax.contract_layer_of('core') != tax.contract_layer_of('ops'))

print("\ngrammar")
g = grammar.compile_grammar(raw_lr, list(tax.known_domains))
grammar.assert_dependent_roles_producible(g)
check("all 13 declared patterns compiled",
      all(getattr(g,n) is not None for n in
          ('spec_anchor','heading','ref_callout_start','blockquote_line','admonition_open',
           'admonition_indent','admonition_blank','admonition_trailing_anchor',
           'admonition_ref_line','admonition_status','document_h1','split_document',
           'frontmatter_fence')))
check("longest-first alternation: adrint parses, not truncated to adr",
      bool(g.spec_anchor.match('^adrint-001')) and taxonomy.domain_of('adrint-001')=='adrint')
check("alternation is byte-stable (sorted, not set-ordered)",
      g.domain_alternation == grammar.compile_grammar(raw_lr, list(tax.known_domains)).domain_alternation)
check("file_naming accepts the evi role", bool(g.file_naming.match('01-x.evi.en.md')))
check("file_naming accepts a legacy no-role file", bool(g.file_naming.match('01-x.en.md')))
check("split_document does NOT treat evi as mergeable",
      g.split_document.match('01-x.evi.en.md') is None)
try:
    grammar.build_domain_alternation([], 'length_desc'); check("empty domain list raises", False)
except grammar.ConfigError: check("empty domain list raises", True)
bad = json.loads(json.dumps(raw_lr)); bad['dependent_lifecycle']['invalidated_state']='archived'
try:
    grammar.compile_grammar(bad, list(tax.known_domains)); check("invalidated_state collision raises", False)
except grammar.ConfigError as e: check("invalidated_state collision raises", 'must not be authorable' in str(e))
bad2 = json.loads(json.dumps(raw_lr)); bad2['dependent_lifecycle']['strictness_order'].pop()
try:
    grammar.compile_grammar(bad2, list(tax.known_domains)); check("non-permutation strictness raises", False)
except grammar.ConfigError as e: check("non-permutation strictness raises", 'permutation' in str(e))

print("\nast_blocks (both grammars, real corpus text)")
DOC = '''---
status: deprecated
---
# State Resolution Kernel

Prologue prose that precedes every block.

!!! note "KERNEL CONTRACT: Dependent State Machine"

    Dependent nodes carry an independent lifecycle.
    {status: active}
    Strictest constraint wins. ^spec-lte-01-001

???+ warning "TECHNICAL DEBATE: Optimistic vs Pessimistic"

    > [!ref-spec-lte-01-001] Optimistic vs Pessimistic

    Optimistic hides cascading failure.

!!! tip "EXECUTION CHECKLIST: Rollout"

    > [!ref-spec-lte-01-001] Rollout steps

    - [ ] Verify graph compiles

## Legacy heading grammar

Grammar-1 body text.
^spec-lte-01-002

> [!ops-spec-lte-01-002] Legacy ops callout
> Body line.
'''
nodes, callouts = ast_blocks.parse_text(g, 'docs/public/core/x.en.md', DOC)
check("both grammars parsed in one pass: 2 nodes", len(nodes)==2, [n.spec_id for n in nodes])
check("3 callouts (warning, tip, legacy ops)", len(callouts)==3, [(c.kind,c.target_id) for c in callouts])
check("warning -> 'ref', tip -> 'ops' by admonition TYPE not token text",
      callouts[0].kind=='ref' and callouts[1].kind=='ops')
check("node-level {status:} override captured raw", nodes[0].status_override=='active')
check("the {status:} line is stripped from rendered content",
      '{status:' not in nodes[0].content)
check("trailing anchor stripped from content", '^spec-lte-01-001' not in nodes[0].content)
check("source_scope derived from path", callouts[0].source_scope=='public')
check("prologue stops at the first block token",
      ast_blocks.prologue_from_text(g, DOC) == 'Prologue prose that precedes every block.')
check("title_from_text reads the true H1 only",
      ast_blocks.title_from_text(g, DOC, 'fallback')=='State Resolution Kernel')
check("title_from_text falls back when no H1",
      ast_blocks.title_from_text(g, 'no heading here', 'the-doc-id')=='the-doc-id')
check("unanchored note block is silently unindexed, not an error",
      ast_blocks.parse_text(g, 'x.md',
        '!!! note "T"\n\n    body with no anchor\n')==([],[]))
check("unlinked warning block silently skipped -- no positional guessing",
      ast_blocks.parse_text(g, 'x.md',
        '???+ warning "T"\n\n    no ref line\n')==([],[]))
check("split-file siblings share one document_id",
      ast_blocks.document_id_from_name(g,'01-x.contract.en.md')
      == ast_blocks.document_id_from_name(g,'01-x.debate.en.md')
      == ast_blocks.document_id_from_name(g,'01-x.ops.en.md') == '01-x')

print("\nfrontmatter")
check("parses the fenced block", frontmatter.parse_text(DOC)['status']=='deprecated')
check("unterminated fence degrades to {} (does not raise)",
      frontmatter.parse_text('---\nstatus: x\nno close')=={})
check("invalid YAML degrades to {}", frontmatter.parse_text('---\na: [unclosed\n---\n')=={})
check("non-mapping frontmatter degrades to {}", frontmatter.parse_text('---\n- a\n- b\n---\n')=={})
check("normalize_status lowercases + trims", frontmatter.normalize_status('  ACTIVE ')=='active')
check("normalize_status refuses a non-string (no guessing)",
      frontmatter.normalize_status(42) is None)
md_ = frontmatter.metadata_from_text('---\nstatus: active\ntags: solo\narchived_reason: obsolete_logic\n---\n')
check("scalar tag coerced to a list", md_['tags']==['solo'])
check("metadata_from_text DROPS archived_reason (documented trap)",
      'archived_reason' not in md_)
check("...but parse_text keeps it",
      frontmatter.parse_text('---\narchived_reason: obsolete_logic\n---\n')['archived_reason']=='obsolete_logic')

print("\nstate_machine -- Pessimistic State Resolution Matrix")
M = [(('active','archived'),'archived'), (('deprecated','active'),'invalidated'),
     (('deprecated','deprecated'),'deprecated'), (('deprecated','archived'),'archived'),
     (('draft','active'),'draft'), (('active',None),'active'), (('deprecated',None),'deprecated'),
     ((None,'archived'),'archived'), (('archived','deprecated'),'invalidated'),
     (('pending','draft'),'draft')]
ok=True
for (c,d),want in M:
    got = state_machine.resolve_dependent_status(g,c,d)
    ok &= got==want
    if got!=want: print("      contract={0} dependent={1} -> {2} want {3}".format(c,d,got,want))
check("all 10 matrix cases match spec", ok)
check("None inherits (purely additive over the existing corpus)",
      state_machine.resolve_dependent_status(g,'deprecated',None)=='deprecated')
try:
    state_machine.resolve_dependent_status(g,'active','bogus'); check("unknown status raises", False)
except KeyError: check("unknown status raises (loud, not ranked as unknown)", True)
check("dependent_role_of: ops/debate/evi yes, contract/legacy no",
      state_machine.dependent_role_of(g,'a/x.ops.en.md')=='ops'
      and state_machine.dependent_role_of(g,'a/x.evi.vi.md')=='evi'
      and state_machine.dependent_role_of(g,'a/x.contract.en.md') is None
      and state_machine.dependent_role_of(g,'a/x.en.md') is None)
cdl = lambda fm, p='a/x.ops.en.md': state_machine.check_dependent_lifecycle(g,p,fm)
check("no status -> inherits, no diagnostic", cdl({})==[])
check("archived without archived_reason -> diagnostic", len(cdl({'status':'archived'}))==1)
check("archived with free text reason -> diagnostic",
      any('free text' in d['message'] for d in cdl({'status':'archived','archived_reason':'we felt like it'})))
check("archived with a declared tag -> clean",
      cdl({'status':'archived','archived_reason':'orphaned_dependency'})==[])
check("deprecated needs BOTH reason and superseded_by",
      len(cdl({'status':'deprecated','archived_reason':'obsolete_logic'}))==1)
check("deprecated with a non-anchor superseded_by -> diagnostic",
      any('not a valid anchor' in d['message'] for d in
          cdl({'status':'deprecated','archived_reason':'obsolete_logic','superseded_by':'02-some-doc'})))
check("deprecated with a caret-prefixed anchor -> clean",
      cdl({'status':'deprecated','archived_reason':'obsolete_logic','superseded_by':'^spec-lte-01-002'})==[])
check("non-dependent file is never checked", cdl({'status':'archived'},'a/x.contract.en.md')==[])

print("\nintegrity")
check("orphan ref detected",
      len(integrity.find_orphan_refs(nodes,[ast_blocks.RefCallout('ref','spec-nope-9-9','t','','f.md',1)]))==1)
check("no false orphans on the real parse", integrity.find_orphan_refs(nodes,callouts)==[])
dup = nodes + [ast_blocks.SpecNode('spec-lte-01-001','dup','','other.md',9)]
check("duplicate anchor detected", list(integrity.find_duplicate_spec_ids(dup))==['spec-lte-01-001'])
priv = [ast_blocks.SpecNode('coreint-lte-01-001','p','','docs/private/core-internal/a.en.md',1)]
pub_c= [ast_blocks.RefCallout('ref','coreint-lte-01-001','t','','docs/public/core/b.en.md',1)]
check("public->private leak flagged", len(integrity.find_cross_boundary_refs(tax,priv,pub_c))==1)
priv_c=[ast_blocks.RefCallout('ref','spec-lte-01-001','t','','docs/private/core-internal/b.en.md',1)]
check("private->public NOT flagged", integrity.find_cross_boundary_refs(tax,nodes,priv_c)==[])
check("misplaced anchor detected (wrong partition)",
      len(integrity.find_misplaced_anchors(tax,[ast_blocks.SpecNode('spec-x-1-1','t','','docs/public/ops/a.en.md',1)]))==1)
check("correctly placed anchor not flagged", integrity.find_misplaced_anchors(tax,nodes)==[])
check("check_file_naming clean on a conforming name", integrity.check_file_naming(g,'a/01-x.ops.en.md')==[])
check("check_file_naming flags a bad locale",
      any('locale segment' in d['message'] for d in integrity.check_file_naming(g,'a/01-x.ops.de.md')))

print("\nrendering")
r1 = rendering.build_renderer(); r2 = rendering.build_renderer()
check("Markdown -> HTML", '<strong>' in r1.render('**bold**'))
check("Renderer is callable as a plain render collaborator", r1('*i*')==r1.render('*i*'))
check("no shared state: two renderers are independent instances", r1._md is not r2._md)
a = r1.render('[x]: http://e.com\nSee [x].'); b = r1.render('See [x].')
check("reset() prevents reference leakage between convert() calls", 'http://e.com' not in b, b)
check("plain_text_summary strips tags", rendering.plain_text_summary('<p>Hello <em>world</em></p>')=='Hello world')
check("entities unescaped AFTER stripping (authored &lt;script&gt; survives)",
      '<script>' in rendering.plain_text_summary('<p>&lt;script&gt;</p>'))
check("truncates on a word boundary with an ellipsis",
      rendering.plain_text_summary('word '*100, 40).endswith('\u2026'))

print("\n  {0} passed, {1} failed".format(P[0], F[0]))
sys.exit(1 if F[0] else 0)
