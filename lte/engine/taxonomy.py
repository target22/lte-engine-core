# -*- coding: utf-8 -*-
"""lte/engine/taxonomy.py -- LEAF MODULE. Pure partition taxonomy.

Extracted from scripts/graph_lib.py lines 134-335 plus
parse_graph.partitions_for_scope(). Zero I/O: no open(), no read_text(),
no subprocess. Imports only stdlib.

WHY THIS IS A VALUE OBJECT AND NOT MODULE-LEVEL CONSTANTS -- the central
design decision of this extraction, and it is forced, not stylistic.

The legacy module exposed PARTITIONS, DOMAIN_TO_PARTITION, PARTITION_SCOPE
and friends as module-level names built at IMPORT time:

    _TAXONOMY = config_loader.load_taxonomy()      # <-- reads a file
    PARTITIONS = [... for row in _TAXONOMY]

That is disk I/O executed on `import graph_lib`. Reproducing it here would
violate the zero-side-effects constraint on the first line of the first
leaf module. The constant and its I/O cannot be separated while the
constant lives at module scope: a module-level constant derived from a
config file must read that file to exist.

So the taxonomy becomes a frozen VALUE an orchestrator constructs once,
from data lte/io/config_reader.py already read, and injects downstream --
exactly the collaborator style lte/cli/compile.py already uses
(`graph_builder.build_lang_graph(..., taxonomy=taxonomy)`).

PATHS ARE NOT A TAXONOMY CONCERN. A row's `scope` is an opaque identifier
declared in config/corpus_layout.yaml; which directory a partition lives in,
and which partitions a compile target reads, are CorpusLayout answers
(lte/engine/layout.py). The three methods below that touch paths or target
selection take the layout as a required keyword and delegate to it; none of
them encodes a directory shape or a scope name.

WHAT DID NOT CHANGE: `partitions` is still a sequence of
(partition_id, display_label, anchor_domain, codeowner, scope) 5-tuples in
declaration order. Both the tuple shape and the ordering are load-bearing
-- git_cas unpacks positionally (`for pid, *_ in allowed`) and graph.json's
"partitions" array, hence the left nav, renders in this order. A tuple
rather than a list so the frozen dataclass is genuinely immutable rather
than nominally so.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# A scope id declared in config/corpus_layout.yaml. Not a closed literal:
# the set of scopes is configuration, validated by compile_layout().
Scope = str

# --- Compiled-output contract version ----------------------------------
# A true constant: no config file, no I/O, so it stays at module scope.
#
# It lived in graph_lib specifically to dodge the import cycle
# compile_from_git -> git_cas -> parse_graph. That workaround is now
# unnecessary: this module is a layer-1 leaf importing nothing inside lte/,
# so the cycle it routed around cannot form. The constant is here on its
# merits, not to escape a layering defect.
#
# Distinct from config_version in config/*.yaml: that versions the INPUT
# contract this pipeline reads, this versions the OUTPUT contract its
# consumers read. Bump when graph.json's shape changes in a way a reader
# must react to -- additive keys do not qualify. tractate_path,
# prologue_html, dependent_status and computed_status were all added
# without a bump.
GRAPH_SCHEMA_VERSION = "1.0.0"


class TaxonomyError(ValueError):
    """The taxonomy rows or UI projection are malformed.

    A ValueError subclass so a caller that does not care about the
    distinction still catches it with `except ValueError`, and one that
    does can name it.
    """


def domain_of(anchor_id: str) -> str:
    """'sales-004' -> 'sales'. 'adrint-001' -> 'adrint'.

    Module-level and taxonomy-free on purpose: it needs no config at all.
    Every known domain is a single word with no internal hyphen (enforced
    in build_taxonomy below), so splitting on the FIRST hyphen is exact --
    this is not a heuristic.
    """
    return anchor_id.split("-", 1)[0]


@dataclass(frozen=True)
class Taxonomy:
    """An immutable partition taxonomy plus its tri-pane layer projection.

    Construct with build_taxonomy(); do not instantiate directly. The
    derived maps are computed once in the factory rather than exposed as
    properties, because every one of them is read in inner loops over the
    whole corpus.
    """

    partitions: tuple
    labels: Mapping[str, str]
    codeowners: Mapping[str, str]
    partition_scope: Mapping[str, str]
    domain_to_partition: Mapping[str, str]
    partition_to_domain: Mapping[str, str]
    known_domains: tuple
    # scope id -> partition ids in declaration order
    partitions_by_scope: Mapping[str, tuple]
    partition_flavor: Mapping[str, str]
    # AST role -> {"layer_by_flavor": {...}, ...}. Two tables, because the
    # two entity classes are not interchangeable: a contract role emits a
    # node and has no graph_key; a dependent role emits a callout and must
    # have one.
    node_projection: Mapping[str, Mapping]
    callout_projection: Mapping[str, Mapping]
    # Dependent roles in ui_projection declaration order. ORDER IS
    # LOAD-BEARING: graph_builder emits one array per role in this order, and
    # graph_hash depends on the resulting key order.
    dependent_roles: tuple

    # -- layer projection ------------------------------------------------

    def _flavor_of(self, partition: str) -> str:
        """The declared flavor of `partition`.

        RAISES for an unknown partition rather than defaulting. The code this
        ultimately replaces was
        `X if partition in _KERNEL_FLAVORED_PARTITIONS else Y`, whose implicit
        else silently returned the POLICY label for ANY unrecognized partition
        string, typos included. Every live call site passes a partition_id
        sourced from `partitions`, so this path is unreachable in the pipeline
        -- and "unreachable" is the reason to make it loud, not the reason to
        keep guessing.
        """
        try:
            flavor = self.partition_flavor[partition]
        except KeyError:
            raise KeyError(
                "{0!r} is not a declared partition (known: {1})".format(
                    partition, sorted(self.partition_flavor))
            ) from None
        return flavor

    def contract_layer_of(self, partition: str) -> str:
        """Layer 1 -- KERNEL_CONTRACT / POLICY_CONTRACT / META_CONSTITUTION.

        layer_by_flavor completeness across every declared flavor is verified
        once, at construction, by validate_ui_projection().
        """
        return self.node_projection["contract"]["layer_by_flavor"][self._flavor_of(partition)]

    def callout_layer_of(self, role: str, partition: str) -> str:
        """The Layer 2/3 label for a dependent block of `role` in `partition`.

        REPLACES debate_layer_of() and ops_layer_of(), which were a
        two-function enumeration of a table that now has three rows and may
        have more. A pair of named methods is the same shape as
        graph_builder's old `if kind == "ref" ... else` -- it works until a
        role is added, then quietly routes the new one to whichever branch
        the else happens to be.

        RAISES for an unknown role rather than defaulting, for the same
        reason _flavor_of() raises for an unknown partition: every live call
        site passes a role resolved through the grammar's alias table, so
        this path is unreachable -- which is the reason to make it loud.
        """
        try:
            spec = self.callout_projection[role]
        except KeyError:
            raise KeyError(
                "{0!r} is not a declared dependent role (known: {1})".format(
                    role, sorted(self.callout_projection))
            ) from None
        return spec["layer_by_flavor"][self._flavor_of(partition)]

    def callout_graph_key(self, role: str) -> str:
        """The graph.json array name a dependent role's entries occupy.

        `debates` and `ops` are historical names app.js already reads; they
        are declared in config rather than derived from the role id so that
        renaming a role never renames a published JSON key.
        """
        try:
            return self.callout_projection[role]["graph_key"]
        except KeyError:
            raise KeyError(
                "{0!r} is not a declared dependent role (known: {1})".format(
                    role, sorted(self.callout_projection))
            ) from None

    # -- anchor -> placement --------------------------------------------

    def partition_of(self, anchor_id: str) -> str | None:
        """'spec-001' -> 'core'. 'adrint-001' -> 'adr-internal'.

        Returns None for a domain outside known_domains, which the compiled
        anchor pattern already prevents from matching -- kept as a safe
        default rather than raising, since callers also pass arbitrary
        strings here defensively.
        """
        return self.domain_to_partition.get(domain_of(anchor_id))

    def scope_of(self, anchor_id: str) -> str | None:
        """The scope id of the partition the anchor's domain maps to. None if
        the anchor's domain is unknown (mirrors partition_of)."""
        partition = self.partition_of(anchor_id)
        return self.partition_scope.get(partition) if partition else None

    def partition_dir(self, repo_root: Path, partition_id: str, *, layout) -> Path:
        """The partition's directory under repo_root, as the layout declares it.

        PURE DESPITE THE Path RETURN TYPE: this is path arithmetic, not a
        filesystem query. It does not check existence, deliberately.

        Raises KeyError for a partition_id this taxonomy does not declare --
        callers always pass one sourced from `partitions`, so this is a
        programmer error, not a runtime data condition to degrade from.
        """
        if partition_id not in self.partition_scope:
            raise KeyError("{0!r} is not a declared partition".format(partition_id))
        directory = layout.partition(partition_id).path
        return Path(repo_root) / directory if directory else Path(repo_root)

    def scope_of_source_file(self, source_file: str, *, layout) -> str | None:
        """Scope of the partition directory a repo-relative path sits in.

        None for a path outside every partition directory. String
        arithmetic only, delegated to the layout; no filesystem access.
        """
        spec = layout.partition_of_path(source_file)
        return spec.scope if spec is not None else None

    # -- target filtering ------------------------------------------------

    def partitions_for_scope(self, target: str, *, layout) -> list:
        """The partition rows a compile of `target` may read, in declaration
        order.

        `target` is a compile target id or alias from
        config/corpus_layout.yaml. This is the BUILD-TIME half of the
        two-layer security boundary: a target never receives a row from a
        scope it does not declare, so a compile of that target never reads
        those directories. The request-time half is the web server's
        authentication on restricted artifacts. Neither substitutes for the
        other.
        """
        allowed = set(layout.partitions_for_target(target))
        return [p for p in self.partitions if p[0] in allowed]

    def is_known_domain(self, domain: str) -> bool:
        return domain in self.domain_to_partition


def build_taxonomy(rows: Sequence[Mapping[str, str]],
                   ui_projection: Mapping) -> Taxonomy:
    """Constructs a Taxonomy from already-parsed config data.

    `rows` is config/taxonomy.yaml's partition list; `ui_projection` is
    config/ui_projection.yaml. Scope ids are NOT checked against a fixed
    set here: lte/engine/layout.compile_layout() rejects any scope id the
    corpus layout does not declare. NEITHER FILE IS READ HERE -- lte/io/config_reader
    reads and YAML-parses them, this validates and compiles. That split is
    what keeps this module side-effect free while the data still originates
    in a file.

    Fail-fast on every branch. A malformed taxonomy must not be compiled
    around: a half-loaded taxonomy silently narrows what counts as a valid
    anchor and then reports the resulting empty corpus as a legitimate one.
    That asymmetry against this pipeline's treatment of malformed AUTHORED
    CONTENT (front matter and prologue degrade to empty rather than failing
    the build) is deliberate -- a bad .md file is an authoring mistake that
    should not take down the corpus; a bad config file is an operator
    mistake that must not be worked around.
    """
    if not rows:
        raise TaxonomyError("taxonomy declares no partitions.")

    required = ("id", "label", "domain", "codeowner", "scope", "flavor")
    partitions = []
    flavors: dict = {}
    seen_ids: set = set()
    seen_domains: dict = {}

    for index, row in enumerate(rows):
        missing = [k for k in required if not row.get(k)]
        if missing:
            raise TaxonomyError(
                "taxonomy row {0} is missing required key(s) {1}.".format(index, missing))
        pid = row["id"]
        if pid in seen_ids:
            raise TaxonomyError("taxonomy declares partition id {0!r} twice.".format(pid))
        seen_ids.add(pid)

        if not isinstance(row["scope"], str):
            raise TaxonomyError(
                "taxonomy row {0!r}: scope={1!r} must be a scope id string. Scope ids "
                "are declared in config/corpus_layout.yaml.".format(pid, row["scope"]))

        domain = row["domain"]
        if "-" in domain:
            # domain_of() splits an anchor id on its first hyphen. A
            # hyphenated domain would make that split wrong for every anchor
            # in the partition, silently misrouting all of them.
            raise TaxonomyError(
                "taxonomy row {0!r}: domain={1!r} contains a hyphen. domain_of() "
                "splits an anchor id on its FIRST hyphen, so a hyphenated domain "
                "cannot round-trip.".format(pid, domain))
        if domain in seen_domains:
            raise TaxonomyError(
                "taxonomy: domain {0!r} is claimed by both {1!r} and {2!r}. An anchor "
                "domain must resolve to exactly one partition.".format(
                    domain, seen_domains[domain], pid))
        seen_domains[domain] = pid

        partitions.append((pid, row["label"], domain, row["codeowner"], row["scope"]))
        flavors[pid] = row["flavor"]

    node_projection, callout_projection = validate_ui_projection(
        ui_projection, set(flavors.values()))

    by_scope: dict = {}
    for p in partitions:
        by_scope.setdefault(p[4], []).append(p[0])

    return Taxonomy(
        partitions=tuple(partitions),
        labels={p[0]: p[1] for p in partitions},
        codeowners={p[0]: p[3] for p in partitions},
        partition_scope={p[0]: p[4] for p in partitions},
        domain_to_partition={p[2]: p[0] for p in partitions},
        partition_to_domain={p[0]: p[2] for p in partitions},
        known_domains=tuple(p[2] for p in partitions),
        partitions_by_scope={scope: tuple(ids) for scope, ids in by_scope.items()},
        partition_flavor=flavors,
        node_projection=node_projection,
        callout_projection=callout_projection,
        dependent_roles=tuple(callout_projection),
    )


def validate_ui_projection(ui_projection: Mapping, flavors: set) -> tuple:
    """Validates config/ui_projection.yaml against the declared flavor set.

    Returns (node_projection, callout_projection).

    THE COMPLETENESS CHECK IS THE POINT. Every flavor declared in the
    taxonomy must appear in every layer_by_flavor map. Without it, adding a
    partition with a new flavor produces a KeyError deep inside a corpus
    walk -- or worse, a silent fallthrough to a default label -- rather than
    a clear error at construction time.

    WHAT THIS NO LONGER READS. The `admonitions:` block is gone with Grammar
    2. Worth stating rather than quietly dropping: that block was the ONLY
    one the previous revision read. The old `callout_kinds:` block was never
    consulted by anything, and Grammar-1 callouts resolved their layer
    through the admonition table (debate_layer_of -> layer_for("warning")).
    So this is not a legacy-path deletion; it is a rewrite of the only live
    path, and ui_projection.yaml must be re-keyed in the same commit.

    THE ROLE SET IS SELF-DECLARING HERE. config_reader builds the taxonomy
    before it compiles the grammar, so this function cannot cross-check
    against dependent_lifecycle.dependent_roles. That check needs both
    finished objects and lives in assert_callout_roles_agree(), called from
    config_reader.load() -- the same post-hoc shape as
    assert_dependent_roles_producible().
    """
    def _check_layers(table: str, role: str, spec: Mapping) -> None:
        if not isinstance(spec, dict):
            raise TaxonomyError(
                "ui_projection.{0}.{1} must be a mapping.".format(table, role))
        by_flavor = spec.get("layer_by_flavor")
        if not isinstance(by_flavor, dict):
            raise TaxonomyError(
                "ui_projection.{0}.{1}.layer_by_flavor must be a mapping.".format(
                    table, role))
        missing = sorted(flavors - set(by_flavor))
        if missing:
            raise TaxonomyError(
                "ui_projection.{0}.{1}.layer_by_flavor has no entry for flavor(s) "
                "{2}. Every flavor declared in the taxonomy needs a label, or a "
                "partition using it falls through to nothing.".format(
                    table, role, missing))
        extra = sorted(set(by_flavor) - flavors)
        if extra:
            raise TaxonomyError(
                "ui_projection.{0}.{1}.layer_by_flavor declares flavor(s) {2} that "
                "no partition uses -- dead config that looks live.".format(
                    table, role, extra))

    node_projection = ui_projection.get("node_projection")
    if not isinstance(node_projection, dict) or "contract" not in node_projection:
        raise TaxonomyError(
            "ui_projection: 'node_projection' must be a mapping declaring at least "
            "'contract'. A corpus with no contract projection has no middle pane.")
    for role, spec in node_projection.items():
        _check_layers("node_projection", role, spec)
        if spec.get("graph_key"):
            raise TaxonomyError(
                "ui_projection.node_projection.{0} declares graph_key={1!r}, but a "
                "node is emitted as a contract node, never into a dependent array. "
                "The value would never be read.".format(role, spec["graph_key"]))

    callout_projection = ui_projection.get("callout_kinds")
    if not isinstance(callout_projection, dict) or not callout_projection:
        raise TaxonomyError(
            "ui_projection: 'callout_kinds' must be a non-empty mapping keyed on "
            "INTERNAL dependent roles, not on authored tokens. Tokens are "
            "renameable in linter_rules.json#callout_kinds.aliases; a layer table "
            "keyed on them would move every label on a rename.")

    if "admonitions" in ui_projection:
        raise TaxonomyError(
            "ui_projection still declares an 'admonitions' block. Grammar 2 is "
            "removed and nothing reads it; leaving it in place would be a second, "
            "stale copy of the layer labels. Delete the block.")

    # graph_key must be present, unique, and must not collide with a field
    # graph_builder already writes onto a contract node -- entry[graph_key]
    # = [] would silently overwrite it.
    RESERVED = {"anchor_id", "title", "layer", "body", "status", "parent_anchor"}
    seen_keys: dict = {}
    for role, spec in callout_projection.items():
        _check_layers("callout_kinds", role, spec)
        graph_key = spec.get("graph_key")
        if not isinstance(graph_key, str) or not graph_key:
            raise TaxonomyError(
                "ui_projection.callout_kinds.{0} has no 'graph_key'. Every "
                "dependent role needs the graph.json array name its entries "
                "occupy.".format(role))
        if graph_key in RESERVED:
            raise TaxonomyError(
                "ui_projection.callout_kinds.{0}.graph_key={1!r} collides with a "
                "contract node field. The array would overwrite it.".format(
                    role, graph_key))
        if graph_key in seen_keys:
            raise TaxonomyError(
                "ui_projection.callout_kinds.{0}.graph_key={1!r} is already used by "
                "role {2!r}. Two roles sharing an array is indistinguishable "
                "downstream from one role.".format(role, graph_key, seen_keys[graph_key]))
        seen_keys[graph_key] = role

    return node_projection, callout_projection


def assert_callout_roles_agree(taxonomy: "Taxonomy", grammar) -> None:
    """Cross-check: ui_projection's dependent roles and linter_rules'
    dependent_lifecycle.dependent_roles must be the SAME SET.

    Separate from build_taxonomy() because it needs the finished Grammar,
    which config_reader compiles second. Same shape and same reason as
    grammar.assert_dependent_roles_producible().

    Both directions are errors, not just one. A role in linter_rules with no
    ui_projection row has no layer or array and would KeyError mid-walk. A
    role in ui_projection with no linter_rules row can never be produced by
    any alias, so its array is emitted empty on every node forever -- dead
    weight in every artifact, which is the harder one to notice.
    """
    declared = set(taxonomy.dependent_roles)
    lifecycle = set(grammar.dependent_lifecycle["dependent_roles"])
    if declared != lifecycle:
        raise TaxonomyError(
            "dependent role sets disagree: ui_projection.callout_kinds declares {0}, "
            "linter_rules.json dependent_lifecycle.dependent_roles declares {1}. "
            "Only in ui_projection: {2}. Only in linter_rules: {3}.".format(
                sorted(declared), sorted(lifecycle),
                sorted(declared - lifecycle), sorted(lifecycle - declared)))
