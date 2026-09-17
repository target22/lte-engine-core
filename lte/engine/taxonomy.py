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
    # admonition type -> {"layer_by_flavor": {...}, "callout_kind": "..."}
    admonition_projection: Mapping[str, Mapping]
    # admonition type -> "ref" | "ops". 'note' is absent by design: a note
    # block returns a SpecNode and never reaches this lookup.
    admonition_callout_kind: Mapping[str, str]

    # -- layer projection ------------------------------------------------

    def layer_for(self, admonition_type: str, partition: str) -> str:
        """Resolves the Layer 1/2/3 label for `partition` under the AST role
        `admonition_type` denotes.

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
        # layer_by_flavor completeness across every declared flavor is
        # verified once, at construction, by validate_ui_projection().
        return self.admonition_projection[admonition_type]["layer_by_flavor"][flavor]

    def contract_layer_of(self, partition: str) -> str:
        """Layer 1 -- KERNEL_CONTRACT / POLICY_CONTRACT / META_CONSTITUTION."""
        return self.layer_for("note", partition)

    def debate_layer_of(self, partition: str) -> str:
        """Layer 2 -- TECHNICAL_DEBATE / POLICY_DEBATE / META_DEBATE.

        Serves both Grammar-2 `???+ warning` blocks and Grammar-1
        `> [!ref-...]` callouts; validate_ui_projection asserts those two
        declarations agree.
        """
        return self.layer_for("warning", partition)

    def ops_layer_of(self, partition: str) -> str:
        """Layer 3 -- OPERATIONAL_EXTENSIONS / EXECUTION_CHECKLIST /
        META_CHECKLIST."""
        return self.layer_for("tip", partition)

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

    projection = validate_ui_projection(ui_projection, set(flavors.values()))

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
        admonition_projection=projection,
        admonition_callout_kind={
            a_type: spec["callout_kind"]
            for a_type, spec in projection.items()
            if spec.get("callout_kind")
        },
    )


def validate_ui_projection(ui_projection: Mapping, flavors: set) -> Mapping:
    """Validates config/ui_projection.yaml against the declared flavor set.

    THE COMPLETENESS CHECK IS THE POINT. Every flavor declared in the
    taxonomy must appear in every layer_by_flavor map. Without it, adding a
    partition with a new flavor produces a KeyError deep inside a corpus
    walk -- or worse, a silent fallthrough to a default label -- rather than
    a clear error at construction time.
    """
    admonitions = ui_projection.get("admonitions")
    if not isinstance(admonitions, dict) or not admonitions:
        raise TaxonomyError("ui_projection: 'admonitions' must be a non-empty mapping.")

    for a_type in ("note", "warning", "tip"):
        if a_type not in admonitions:
            raise TaxonomyError(
                "ui_projection.admonitions is missing {0!r}. All three admonition "
                "types are reachable from the grammar.".format(a_type))

    for a_type, spec in admonitions.items():
        if not isinstance(spec, dict):
            raise TaxonomyError(
                "ui_projection.admonitions.{0} must be a mapping.".format(a_type))
        by_flavor = spec.get("layer_by_flavor")
        if not isinstance(by_flavor, dict):
            raise TaxonomyError(
                "ui_projection.admonitions.{0}.layer_by_flavor must be a mapping.".format(
                    a_type))
        missing = sorted(flavors - set(by_flavor))
        if missing:
            raise TaxonomyError(
                "ui_projection.admonitions.{0}.layer_by_flavor has no entry for "
                "flavor(s) {1}. Every flavor declared in the taxonomy needs a label, "
                "or a partition using it falls through to nothing.".format(a_type, missing))
        extra = sorted(set(by_flavor) - flavors)
        if extra:
            raise TaxonomyError(
                "ui_projection.admonitions.{0}.layer_by_flavor declares flavor(s) {1} "
                "that no partition uses -- dead config that looks live.".format(
                    a_type, extra))

    # `note` must NOT declare a callout_kind: a note block compiles to a
    # SpecNode, never to a callout, so the value would never be read.
    if admonitions["note"].get("callout_kind"):
        raise TaxonomyError(
            "ui_projection.admonitions.note declares callout_kind={0!r}, but a note "
            "block compiles to a SpecNode and never to a callout. The value would "
            "never be read.".format(admonitions["note"]["callout_kind"]))
    for a_type in ("warning", "tip"):
        kind = admonitions[a_type].get("callout_kind")
        if kind not in ("ref", "ops"):
            raise TaxonomyError(
                "ui_projection.admonitions.{0}.callout_kind={1!r} must be 'ref' or "
                "'ops'.".format(a_type, kind))

    return admonitions
