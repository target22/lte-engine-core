"""
lte/engine/graph_builder.py -- LEAF MODULE. Pure JSON Graph assembly.

THE SINGLE ASSEMBLY IMPLEMENTATION. Replaces the two near-copies that
scripts/core_pipeline/parse_graph.py (build_lang_graph) and
scripts/core_pipeline/git_cas.py (build_lang_graph_from_git /
build_full_graph) each carried. It receives already-read (rel_path, text)
pairs and returns plain dicts. No I/O, no pathlib, no imports from lte.io or
lte.cli, and no imports from sibling engine modules either: every grammar-,
taxonomy-, layout- or rendering-dependent operation arrives as an injected
collaborator, so this module can be exercised against fakes with no config.

LAYOUT-AGNOSTIC. This module never splits a path or builds a filename
suffix. Which files belong to a partition, which locale a file has, which
role it plays and which document it joins are all answers from the injected
`layout` (lte.engine.layout.CorpusLayout or a duck-typed fake).

REFERENCE BEHAVIOUR IS THE CAS COMPILER. compile_from_git.py is what the
production pipeline runs (build_config.yaml steps compile-public and
compile-internal), so where the two legacy copies disagree, this module
reproduces the CAS copy:

  * THE TARGET IS APPLIED FIRST. Documents outside the partitions the
    compile target reads are dropped before anything is parsed. The legacy
    disk copy parsed every partition and filtered only contract nodes, so a
    callout authored in a restricted scope that targeted a public anchor was
    attached to the PUBLIC graph. That leak is closed here, as in CAS.
  * FILE ORDER IS PLAIN STRING ORDER, which equals `git ls-tree -r` order.
    The disk copy sorted pathlib objects, which orders `a/x.md` before
    `a-b.md`; Git orders them the other way. The order decides the sequence
    of debates/ops under a node, so it is part of the payload.
  * EXCLUSIONS ARE ALWAYS ON: files with an unbalanced ``` fence, anchors
    defined more than once, and anchors whose domain does not match their
    partition are excluded, deterministically, instead of last-write-wins.
  * METADATA KEY ORDER is graph_hash, generated_at, total_nodes,
    status_values, invalidated_state, schema_version -- the order
    compile_from_git.py produced by appending schema_version last -- then
    `locales`, added with the corpus layout so a client can discover the
    artifact's language keys instead of hardcoding them.

TIER-3 GROUPING follows layout.classify(...).group_key: files sharing a
(group partition, tractate, document id) form one document. A partition
declared with `pairs_with` contributes its files to the paired partition's
documents and is not emitted as a partition of its own.

`exclude_cross_boundary=True` adds the one exclusion only the disk copy's
--soft-fail mode had: a file that cites a private anchor from a public file
is dropped entirely.

DIAGNOSTICS INSTEAD OF PRINTING. Both legacy copies printed [WARN] lines
from inside the assembly loop. Here every such event is appended to the
optional `diagnostics` list as {"level", "code", "message"}; the orchestrator
decides whether to print them and whether any of them fails the build.
Level "error" marks content that was excluded from the payload; level
"warn" marks content that was kept or silently dropped as before.
"""

from __future__ import annotations

import datetime
import hashlib
import json

FENCE_MARKER = "```"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
AST_NON_COMPLIANT = "ast_non_compliant"
DEPRECATED = "deprecated"


# ------------------------------------------------------------------ helpers

def _unwrap(item):
    """Integrity finders return nodes, callouts, or tuples led by one."""
    return item[0] if isinstance(item, tuple) else item


def _misplaced_parts(item):
    if isinstance(item, tuple) and len(item) >= 3:
        return item[0], item[1], item[2]
    return _unwrap(item), None, None


def _emit(diagnostics, level, code, message):
    if diagnostics is not None:
        diagnostics.append({"level": level, "code": code, "message": message})


def unbalanced_fence_count(text):
    """Number of ``` marker lines, when that number is odd; else 0."""
    count = sum(1 for line in text.splitlines() if line.strip().startswith(FENCE_MARKER))
    return count if count % 2 else 0


def compute_graph_hash(lang_graphs):
    """
    SHA-256 over {locale: lang_graph} only; metadata never perturbs it.
    sort_keys makes the digest independent of locale order, so the default
    vi/en layout hashes exactly as the legacy {"vi", "en"} digest did.
    """
    canonical = json.dumps(dict(lang_graphs), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def total_nodes(lang_graph):
    return sum(d["node_count"] for p in lang_graph["partitions"] for d in p["documents"])


def build_anchor_index(partitions_out):
    index = {}
    for partition in partitions_out:
        for doc in partition["documents"]:
            for node in doc["nodes"]:
                index[node["anchor_id"]] = {
                    "src": doc["src"],
                    "document_id": doc["document_id"],
                    "partition": partition["id"],
                    "title": node["title"],
                }
    return index


# ------------------------------------------------------------- assembly

def build_lang_graph(documents, lang, scope, *,
                     layout, parse_text, render, metadata_from_text, resolve_dependent_status,
                     taxonomy, summarize, normalize_status, title_from_text,
                     prologue_from_text,
                     find_duplicate_spec_ids, find_misplaced_anchors, find_orphan_refs,
                     find_cross_boundary_refs, status_values, document_roles,
                     exclude_cross_boundary=False, diagnostics=None):
    """
    One language's graph: {"partitions": [...], "anchor_index": {...}}.

    documents        iterable of (rel_path, text); rel_path is repo-relative
                     POSIX. Paths the layout does not classify, paths in
                     another locale and paths outside the target are
                     ignored, so a provider may over-supply.
    lang             a locale id from the layout.
    scope            a compile target id or alias from the layout.

    Injected collaborators (all pure, all pre-bound to grammar/taxonomy by
    the caller):
      layout: classify(rel_path) -> FileInfo | None,
              partitions_for_target(scope), emitted_partitions_for_target(scope)
      parse_text(rel_path, text) -> (spec_nodes, ref_callouts)
      render(markdown) -> html
      summarize(html) -> str
      metadata_from_text(text) -> {status, author, date, version, tags,
                                   supersedes, superseded_by}
      normalize_status(raw) -> str | None
      resolve_dependent_status(contract_status, dependent_status) -> str | None
      title_from_text(rel_path, text) -> str   (falls back to the document id)
      prologue_from_text(text) -> str
      find_duplicate_spec_ids(nodes) -> {anchor_id: [nodes]}
      find_misplaced_anchors(nodes) -> [(node, expected, actual) | node]
      find_orphan_refs(nodes, callouts) -> [callouts]
      find_cross_boundary_refs(nodes, callouts) -> [callouts]
      taxonomy: labels, codeowners, partition_scope, contract_layer_of(pid),
                debate_layer_of(pid), ops_layer_of(pid)
      status_values, document_roles: ordered vocabularies
    """
    allowed = frozenset(layout.partitions_for_target(scope))
    emitted = list(layout.emitted_partitions_for_target(scope))
    status_set = frozenset(status_values)

    # --- 1. in-target corpus, production order ---------------------------
    content_by_path = {}
    info_by_path = {}
    for rel_path, text in documents:
        info = layout.classify(rel_path)
        if info is None or info.locale != lang or info.partition not in allowed:
            continue
        content_by_path[rel_path] = text
        info_by_path[rel_path] = info
    ordered_paths = sorted(content_by_path)
    partition_by_path = {rel: info.partition for rel, info in info_by_path.items()}

    # --- 2. file-level exclusions ----------------------------------------
    excluded_files = {}
    for rel in ordered_paths:
        fences = unbalanced_fence_count(content_by_path[rel])
        if fences:
            excluded_files[rel] = "%d unclosed code fence marker(s)" % fences
            _emit(diagnostics, "error", "unclosed_fence",
                  "Skipping file: %s (%s)" % (rel, excluded_files[rel]))

    # --- 3. parse once --------------------------------------------------
    nodes_by_path = {}
    callouts_by_path = {}
    meta_by_path = {}
    all_nodes = []
    all_callouts = []
    for rel in ordered_paths:
        text = content_by_path[rel]
        nodes, callouts = parse_text(rel, text)
        nodes_by_path[rel] = list(nodes)
        callouts_by_path[rel] = list(callouts)
        meta_by_path[rel] = metadata_from_text(text)
        all_nodes.extend(nodes)
        all_callouts.extend(callouts)

    # --- 4. corpus-level exclusions -------------------------------------
    for orphan in find_orphan_refs(all_nodes, all_callouts):
        orphan = _unwrap(orphan)
        _emit(diagnostics, "warn", "orphan_ref",
              "Dropping [!%s-%s] in %s:%s: no matching anchor in the '%s' corpus"
              % (orphan.kind, orphan.target_id, orphan.source_file, orphan.line_no, lang))

    if exclude_cross_boundary:
        for violation in find_cross_boundary_refs(all_nodes, all_callouts):
            violation = _unwrap(violation)
            reason = ("cites anchor '%s' in a higher-visibility scope (security boundary violation)"
                      % violation.target_id)
            excluded_files[violation.source_file] = reason
            _emit(diagnostics, "error", "cross_boundary",
                  "Skipping file: %s (%s)" % (violation.source_file, reason))

    excluded_anchor_ids = set()
    duplicates = find_duplicate_spec_ids(all_nodes)
    for spec_id in sorted(duplicates):
        dupes = duplicates[spec_id]
        excluded_anchor_ids.add(spec_id)
        locations = ", ".join("%s:%s" % (n.source_file, n.line_no) for n in dupes)
        _emit(diagnostics, "error", "duplicate_anchor",
              "Excluding anchor ^%s: defined %d times (%s)" % (spec_id, len(dupes), locations))
    for item in find_misplaced_anchors(all_nodes):
        node, expected, actual = _misplaced_parts(item)
        excluded_anchor_ids.add(node.spec_id)
        _emit(diagnostics, "error", "misplaced_anchor",
              "Excluding anchor ^%s (%s): belongs under '%s/', committed under '%s/'"
              % (node.spec_id, node.source_file, expected or "?", actual or "?"))

    # --- 5. document status, validated once per file --------------------
    file_status_by_path = {}
    for rel in ordered_paths:
        raw_status = meta_by_path[rel].get("status")
        if raw_status is not None and raw_status not in status_set:
            _emit(diagnostics, "warn", "unknown_status",
                  "Unrecognized status %r in %s (rendering as unset)" % (raw_status, rel))
            raw_status = None
        file_status_by_path[rel] = raw_status

    def effective_node_status(node, home_rel):
        """An inline {status: x} override wins; else the document status."""
        if node.status_override is not None:
            normalized = normalize_status(node.status_override)
            if normalized is not None and normalized not in status_set:
                _emit(diagnostics, "warn", "unknown_status",
                      "Unrecognized status override %r on ^%s (%s:%s)"
                      % (node.status_override, node.spec_id, node.source_file, node.line_no))
            elif normalized is not None:
                return normalized
        return file_status_by_path.get(home_rel)

    # --- 6. Tier-4 contract nodes ---------------------------------------
    nodes_by_id = {}
    node_home = {}
    for rel in ordered_paths:
        if rel in excluded_files:
            continue
        pid = partition_by_path[rel]
        for node in nodes_by_path[rel]:
            if node.spec_id in excluded_anchor_ids:
                continue
            nodes_by_id[node.spec_id] = {
                "anchor_id": node.spec_id,
                "title": node.title,
                "layer": taxonomy.contract_layer_of(pid),
                "body": render(node.content),
                "status": effective_node_status(node, rel),
                "debates": [],
                "ops": [],
            }
            node_home[node.spec_id] = rel

    # --- 7. Tier-4 callouts, cascading deprecation ----------------------
    for rel in ordered_paths:
        if rel in excluded_files:
            continue
        for callout in callouts_by_path[rel]:
            target = nodes_by_id.get(callout.target_id)
            if target is None:
                continue
            contract_status = target["status"]
            # `rel` is the file the CALLOUT was authored in; in a split
            # document that is the .debate/.ops sibling, whose own front
            # matter declares the dependent's independent lifecycle.
            dependent_status = file_status_by_path.get(rel)
            entry = {
                "title": callout.title,
                "body": render(callout.content),
                "source_file": callout.source_file,
                "contract_status": contract_status,
                "dependent_status": dependent_status,
                "computed_status": resolve_dependent_status(contract_status, dependent_status),
                # "is my parent contract dead?" -- deliberately NOT
                # computed_status == invalidated; two questions, two fields.
                "invalidated": contract_status == DEPRECATED,
            }
            target_pid = partition_by_path[node_home[callout.target_id]]
            if callout.kind == "ref":
                entry["layer"] = taxonomy.debate_layer_of(target_pid)
                target["debates"].append(entry)
            else:
                entry["layer"] = taxonomy.ops_layer_of(target_pid)
                target["ops"].append(entry)

    grouped_nodes_by_path = {}
    for spec_id, node_dict in nodes_by_id.items():
        grouped_nodes_by_path.setdefault(node_home[spec_id], []).append(node_dict)
    for rel, node_dicts in grouped_nodes_by_path.items():
        line_no_of = {}
        for n in nodes_by_path[rel]:
            line_no_of[n.spec_id] = n.line_no
        node_dicts.sort(key=lambda nd: line_no_of[nd["anchor_id"]])

    # --- 8. Tier-3 split-file grouping ----------------------------------
    role_rank = {}
    for index, role in enumerate(document_roles):
        role_rank[role] = index

    def role_sort_key(rel):
        role = info_by_path[rel].role
        return (-1 if role is None else role_rank[role], rel.rsplit("/", 1)[-1])

    groups_by_partition = {}
    for rel in ordered_paths:
        group_partition, tractate, document_id = info_by_path[rel].group_key
        groups_by_partition.setdefault(group_partition, {}).setdefault(
            (tuple(tractate), document_id), []).append(rel)

    partitions_out = []
    for pid in emitted:
        groups = groups_by_partition.get(pid, {})

        documents_out = []
        for (tractate, doc_id) in sorted(groups):
            group_rels = sorted(groups[(tractate, doc_id)], key=role_sort_key)
            candidate_rels = [r for r in group_rels if r not in excluded_files]
            if not candidate_rels:
                continue

            # The highest-precedence SURVIVING file is metadata-authoritative.
            primary_rel = candidate_rels[0]
            primary_text = content_by_path[primary_rel]
            nodes = []
            for rel in candidate_rels:
                nodes.extend(grouped_nodes_by_path.get(rel, []))

            prologue_md = prologue_from_text(primary_text)
            prologue_html = render(prologue_md) if prologue_md else ""
            if nodes:
                summary = summarize(nodes[0]["body"])
            elif prologue_html:
                summary = summarize(prologue_html)
            else:
                summary = ""
            meta = meta_by_path[primary_rel]
            status = file_status_by_path[primary_rel]
            ast_valid = len(nodes) > 0
            if not ast_valid:
                status = AST_NON_COMPLIANT
                _emit(diagnostics, "warn", "ast_non_compliant",
                      "%s: 0 parsed AST contract node(s) across %d file(s) in this document group"
                      % (primary_rel, len(candidate_rels)))

            document = {
                "document_id": doc_id,
                "title": title_from_text(primary_rel, primary_text),
                "src": primary_rel,
                "summary": summary,
                "node_count": len(nodes),
                "codeowner": taxonomy.codeowners[pid],
                "tractate_path": list(tractate),
                "prologue_html": prologue_html,
                "status": status,
                "ast_valid": ast_valid,
                "author": meta.get("author"),
                "date": meta.get("date"),
                "version": meta.get("version"),
                "tags": meta.get("tags"),
                # Document-level lifecycle: document ids, not anchor ids.
                "supersedes": meta.get("supersedes"),
                "superseded_by": meta.get("superseded_by"),
                "nodes": nodes,
            }
            if len(candidate_rels) > 1:
                document["source_files"] = list(candidate_rels)
            documents_out.append(document)

        partitions_out.append({
            "id": pid,
            "label": taxonomy.labels[pid],
            "scope": taxonomy.partition_scope[pid],
            "documents": documents_out,
        })

    return {"partitions": partitions_out, "anchor_index": build_anchor_index(partitions_out)}


def build_full_graph(per_lang_graphs, *, scope, locales, schema_version, status_values,
                     invalidated_state, clock):
    """
    {"scope", <locale>..., "metadata"} -- the artifact a compile target
    writes. Locale keys appear in `locales` order (the layout's order).

    `clock()` returns a datetime; injected so generated_at is deterministic
    under test. A naive datetime is taken to be UTC already.
    """
    locales = tuple(locales)
    missing = [lang for lang in locales if lang not in per_lang_graphs]
    extra = sorted(set(per_lang_graphs) - set(locales))
    if missing or extra:
        raise ValueError("per_lang_graphs does not match the layout locales (missing: %s, extra: %s)"
                         % (", ".join(missing) or "-", ", ".join(extra) or "-"))
    ordered = [(lang, per_lang_graphs[lang]) for lang in locales]

    moment = clock()
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc)

    graph = {"scope": scope}
    graph.update(ordered)
    graph["metadata"] = {
        "graph_hash": compute_graph_hash(ordered),
        "generated_at": moment.strftime(TIMESTAMP_FORMAT),
        "total_nodes": sum(total_nodes(g) for _, g in ordered),
        "status_values": list(status_values),
        "invalidated_state": invalidated_state,
        "schema_version": schema_version,
        "locales": list(locales),
    }
    return graph
