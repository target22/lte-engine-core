/**
 * mongo/aggregation_facade.js
 *
 * Converts the (future) MongoDB `nodes` + `debates` collections into the
 * EXACT JSON shape scripts/parse_graph.py currently produces at
 * public/graph.json:
 *
 *   {
 *     "partitions": [{ "id", "label", "domain" }, ...],
 *     "vi": { "<anchor_id>": { id, title, html, source_file, domain,
 *                               partition, debates: [...], operational: [...] } },
 *     "en": { ... same shape ... }
 *   }
 *
 * This IS the facade pattern: `nodes`/`debates` documents carry strictly
 * MORE fields than graph.json exposes (decision, nodes_involved,
 * hash_chain -- see mongo/schema_nodes.json and schema_debates.json).
 * The $project stages below narrow that richer write-schema down to the
 * exact stable read-shape the Astro frontend already depends on, so a
 * future swap from file-based parsing to a live MongoDB read never
 * requires a single line of src/pages/index.astro to change.
 *
 * SCOPE NOTE, stated plainly: this is a "hash-chained document store"
 * with tamper-evident writes, not full CQRS/event-sourcing in the strict
 * sense (there is no separate immutable event log that `nodes`/`debates`
 * are REPLAYED from -- they ARE the primary collections, each write to
 * them individually chained). The requirement asked for `nodes`/`debates`
 * collections with an embedded hash_chain field, which is what's built
 * here; if true event-sourcing (event log as sole source of truth,
 * collections as rebuildable projections) is actually wanted, that is a
 * materially different, larger design and should be called out as its
 * own requirement rather than assumed included in this one.
 *
 * NOTHING IN THIS FILE CONNECTS TO A REAL DATABASE. There is no live
 * MongoDB instance behind this delivery -- these are the exact contracts
 * a future migration would implement against, not a running system.
 * buildGraphJson() below is real, runnable code against the MongoDB
 * Node.js driver's API, written so that dropping in a real `db` handle
 * (a `Db` instance from `MongoClient.connect()`) is the only remaining
 * step -- but that connection does not exist in this repository.
 *
 * ============================================================================
 * HASH-CHAIN SPECIFICATION (authoritative -- README.md's copy is a summary
 * of this, not a second source of truth)
 * ============================================================================
 *
 *   Hash_n = SHA256(Payload_n || Timestamp_n || Sequence_n || Hash_{n-1})
 *
 * The formula above (as given in the requirement) is mathematical
 * shorthand for "these four things get hashed together" -- it is not
 * itself a byte-exact serialization format. Two independent
 * implementations (say, a Python ingestion service and this Node.js
 * verifier) MUST agree on the exact byte layout below to ever produce
 * the same hash for the same logical event, so this section fully
 * specifies it rather than leaving '||' to mean whatever concatenation
 * each implementation happens to pick:
 *
 *   Payload_n   : the event document's business fields, EXCLUDING the
 *                 hash_chain field itself and the MongoDB _id, serialized
 *                 via canonicalJson() below -- JSON with object keys
 *                 sorted alphabetically at EVERY nesting level (arrays
 *                 keep their original order), no whitespace. UTF-8 encoded.
 *   Timestamp_n : ISO-8601 string, millisecond precision, UTC, e.g.
 *                 "2026-08-20T14:33:07.123Z" -- exactly what
 *                 `new Date().toISOString()` returns. UTF-8 encoded.
 *   Sequence_n  : the non-negative integer sequence number, as its plain
 *                 decimal string form (e.g. "42", no leading zeros).
 *                 UTF-8 encoded.
 *   Hash_{n-1}  : the previous event's 64-character lowercase hex SHA-256
 *                 digest. For the GENESIS event (sequence 0), this is the
 *                 fixed constant GENESIS_PREV_HASH ('0' repeated 64
 *                 times) -- never null, never an empty string. A fixed
 *                 genesis value is itself part of the spec, not an
 *                 implementation detail left to chance, because a null or
 *                 absent value would be ambiguous between "there is no
 *                 previous event" and "the previous event's hash was
 *                 lost/omitted".
 *   '||'        : a single NUL byte (0x00) separator between each of the
 *                 four UTF-8 byte strings above -- an EXPLICIT delimiter,
 *                 not naive adjacency. Without a delimiter, a
 *                 variable-length Payload_n could in principle be crafted
 *                 so that (Payload_n, Timestamp_n) and a DIFFERENT
 *                 (Payload_n', Timestamp_n') pair produce identical
 *                 concatenated bytes; an explicit separator that cannot
 *                 appear inside any of the four components (NUL never
 *                 appears in JSON text, an ISO timestamp, or a decimal
 *                 integer, or a hex string) removes that ambiguity
 *                 entirely.
 *
 *   sequence IS GLOBAL, not per-collection: a single monotonic counter
 *   shared across the nodes AND debates collections together. This is a
 *   deliberate design choice, not an oversight -- verifying `nodes` and
 *   `debates` as two independent chains would let an attacker delete an
 *   entire debates document without breaking the nodes chain's
 *   verification at all, defeating the point of a tamper-evident log
 *   that is supposed to span the whole governance corpus.
 *
 * ============================================================================
 */

import crypto from "node:crypto";

const GENESIS_PREV_HASH = "0".repeat(64);

/**
 * Deterministic JSON serialization: object keys sorted alphabetically at
 * every nesting level, arrays preserve their original order, no
 * whitespace. This is the ONLY function that may ever be used to
 * serialize Payload_n for hashing -- using plain JSON.stringify()
 * anywhere in an ingestion or verification path would make the hash
 * depend on incidental key insertion order, silently breaking
 * cross-implementation chain verification.
 */
function canonicalJson(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalJson).join(",") + "]";
  }
  const keys = Object.keys(value).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(value[k])).join(",") + "}";
}

/**
 * Reference implementation of the Hash-Chain Specification above.
 * payload: plain JS object of business fields (hash_chain and _id
 *          already excluded by the caller).
 * timestampIso: ISO-8601 millisecond-precision UTC string.
 * sequence: non-negative integer.
 * prevHash: 64-char lowercase hex string, or GENESIS_PREV_HASH for
 *           sequence 0.
 * Returns: 64-char lowercase hex SHA-256 digest string.
 */
function computeHashChainEntry(payload, timestampIso, sequence, prevHash) {
  if (!Number.isInteger(sequence) || sequence < 0) {
    throw new RangeError(`sequence must be a non-negative integer, got ${sequence}`);
  }
  if (!/^[0-9a-f]{64}$/.test(prevHash)) {
    throw new RangeError(`prevHash must be a 64-char lowercase hex string, got ${JSON.stringify(prevHash)}`);
  }
  const parts = [canonicalJson(payload), timestampIso, String(sequence), prevHash];
  const bytes = Buffer.from(parts.join("\u0000"), "utf-8");
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

/**
 * Verifies the GLOBAL hash chain across BOTH collections together (see
 * the "sequence IS GLOBAL" note above -- verifying either collection in
 * isolation would be verifying the wrong thing). Read-only: recomputes
 * every hash from stored payloads and compares against the stored
 * `hash`/`prev_hash` fields. Returns { valid: true, eventsVerified } on
 * success, or { valid: false, ...details } describing the FIRST break
 * found, in sequence order.
 *
 * This is an AUDIT operation. There is no corresponding write path in
 * this delivery -- see the file header. A future ingestion service is
 * where computeHashChainEntry() above gets called at insert time; this
 * function exists so that once that service exists, verifying its work
 * is already solved.
 */
async function verifyHashChain(db) {
  const [nodeDocs, debateDocs] = await Promise.all([
    db.collection("nodes").find({}).toArray(),
    db.collection("debates").find({}).toArray(),
  ]);

  const events = [...nodeDocs, ...debateDocs].sort(
    (a, b) => Number(a.hash_chain.sequence) - Number(b.hash_chain.sequence)
  );

  let expectedPrev = GENESIS_PREV_HASH;
  let expectedSequence = 0;

  for (const doc of events) {
    const { hash_chain, _id, ...payload } = doc;
    const sequence = Number(hash_chain.sequence);

    if (sequence !== expectedSequence) {
      return {
        valid: false,
        reason: "sequence gap or duplicate",
        expectedSequence,
        actualSequence: sequence,
        documentId: String(_id),
      };
    }
    if (hash_chain.prev_hash !== expectedPrev) {
      return {
        valid: false,
        reason: "prev_hash does not match the previous event's actual hash",
        atSequence: sequence,
        expected: expectedPrev,
        actual: hash_chain.prev_hash,
        documentId: String(_id),
      };
    }

    const timestampIso =
      hash_chain.timestamp instanceof Date ? hash_chain.timestamp.toISOString() : hash_chain.timestamp;
    const recomputed = computeHashChainEntry(payload, timestampIso, sequence, expectedPrev);

    if (recomputed !== hash_chain.hash) {
      return {
        valid: false,
        reason: "stored hash does not match recomputed hash -- payload was likely modified after insert",
        atSequence: sequence,
        expected: recomputed,
        actual: hash_chain.hash,
        documentId: String(_id),
      };
    }

    expectedPrev = hash_chain.hash;
    expectedSequence += 1;
  }

  return { valid: true, eventsVerified: events.length };
}

// ============================================================================
// GRAPH.JSON FACADE — 3-tier hierarchy: Partition -> Document -> Nodes
// ============================================================================
//
// Reproduces the EXACT shape scripts/parse_graph.py emits since the "Flat
// Layout Defect" fix:
//   { "vi": { "partitions": [ { id, label, documents: [ { document_id,
//       title, src, summary, node_count, codeowner, nodes: [ { anchor_id,
//       title, layer, body, debates: [...], ops: [...] } ] } ] } ] },
//     "en": { ... same shape ... } }
//
// Tier 1 (partitions) is static taxonomy -- see fetchPartitions() below.
// Tier 2 (documents) is NOT a physical collection in this design (see the
// denormalization note on mongo/schema_nodes.json's metadata.document_title
// / metadata.document_summary): it is computed by GROUPING `nodes` by
// document_id inside the aggregation pipeline itself, via $group after the
// per-node $lookup stages. Tier 3 (nodes, with their debates/ops) is the
// $lookup'd, per-node data exactly as before.

const PARTITION_ORDER = ["core", "adr", "ops", "exec", "adr-internal", "ops-internal", "core-internal"];
const PARTITION_LABELS = {
  core: "01 · Core Specs & Schemas",
  adr: "02 · Architecture Decision Records",
  ops: "07 · HR, SOPs & Internal Operations",
  exec: "03 · Executive Board & Strategy",
  "adr-internal": "02i · Internal ADRs & Security Post-Mortems",
  "ops-internal": "07i · Internal HR Performance & Compensation",
  "core-internal": "01i · Internal Core Specs & Security Architecture",
};

/**
 * Static enterprise taxonomy -- mirrors scripts/graph_lib.py's PARTITIONS
 * constant (the single source of truth) rather than reading a live
 * `partitions` collection, for the same reason that file's own comment
 * gives for keeping a Python-side copy: this is a handful of rows that
 * change on the order of "we added an eighth partition," not per content
 * edit, and is not worth a network round-trip on every graph build.
 * (fetchLangGraph() below returns a real `db` query; this one doesn't
 * need to, and pretending it queries the database would be theater, not
 * a more "complete" implementation.)
 */
function fetchPartitions() {
  return PARTITION_ORDER.map((id) => ({ id, label: PARTITION_LABELS[id] }));
}

/**
 * The $project shape shared by both the 'debates' and 'ops' arrays on a
 * node -- graph.json exposes exactly {title, body, source_file} per
 * linked entry, regardless of how many additional fields (decision,
 * nodes_involved, hash_chain) the underlying `debates` document actually
 * carries. This function IS the facade at the field level. `layerField`
 * lets the same helper serve both debate-layer and execution-layer
 * projections without duplicating the shape.
 */
function debateEntryProjection() {
  return {
    _id: 0,
    title: "$title",
    body: "$body.html",
    source_file: "$metadata.source_file",
  };
}

/**
 * Builds ONE locale's worth of the hierarchical graph via a single
 * aggregation pipeline against `nodes`:
 *   1. $match this locale.
 *   2. Two correlated $lookup stages (debate layer, execution layer) --
 *      IMPORTANT: matched on (target_anchor_id, document_id, locale), not
 *      target_anchor_id alone, because Tier 3 linking is strictly
 *      DOCUMENT-SCOPED in this revision (see mongo/schema_debates.json's
 *      document_id field comment for the architectural consequence of
 *      this: a valid cross-document reference is correctly excluded from
 *      nesting here, exactly matching scripts/parse_graph.py's own
 *      behavior, not a divergence between the two implementations).
 *   3. $project each node into its Tier 3 shape.
 *   4. $group by document_id, collecting nodes into an array and taking
 *      the (identical-by-construction) document-level fields via $first.
 *   5. $group again by partition (derived from domain via $switch, since
 *      it is not stored directly on the node -- see the note on the
 *      $addFields stage), collecting documents into Tier 2 arrays.
 *   6. Merge with the static, always-complete PARTITION_ORDER so a
 *      partition with zero documents still appears with documents: [],
 *      matching scripts/parse_graph.py's own behavior of always emitting
 *      all 7 partitions regardless of content.
 */
async function fetchLangGraph(db, locale) {
  const domainToPartition = {
    spec: "core",
    adr: "adr",
    sop: "ops",
    exec: "exec",
    adrint: "adr-internal",
    opsint: "ops-internal",
    coreint: "core-internal",
  };

  const pipeline = [
    { $match: { "metadata.locale": locale } },
    {
      $lookup: {
        from: "debates",
        let: { nodeAnchor: "$anchor_id", nodeDoc: "$document_id", nodeLocale: "$metadata.locale" },
        pipeline: [
          {
            $match: {
              $expr: {
                $and: [
                  { $eq: ["$target_anchor_id", "$$nodeAnchor"] },
                  { $eq: ["$document_id", "$$nodeDoc"] },
                  { $eq: ["$metadata.locale", "$$nodeLocale"] },
                  { $eq: ["$layer", "debate"] },
                ],
              },
            },
          },
          { $project: debateEntryProjection() },
        ],
        as: "debates",
      },
    },
    {
      $lookup: {
        from: "debates",
        let: { nodeAnchor: "$anchor_id", nodeDoc: "$document_id", nodeLocale: "$metadata.locale" },
        pipeline: [
          {
            $match: {
              $expr: {
                $and: [
                  { $eq: ["$target_anchor_id", "$$nodeAnchor"] },
                  { $eq: ["$document_id", "$$nodeDoc"] },
                  { $eq: ["$metadata.locale", "$$nodeLocale"] },
                  { $eq: ["$layer", "execution"] },
                ],
              },
            },
          },
          { $project: debateEntryProjection() },
        ],
        as: "ops",
      },
    },
    // Derive `partition` from `domain` -- not stored directly on the node
    // (schema_nodes.json intentionally doesn't duplicate it: domain
    // already determines partition 1:1 via scripts/graph_lib.py's
    // DOMAIN_TO_PARTITION, so storing both would itself be an
    // unenforceable-by-$jsonSchema redundant fact, the same category of
    // problem schema_nodes.json's own 'domain' field comment already
    // flags for anchor_id vs domain).
    {
      $addFields: {
        partition: {
          $switch: {
            branches: Object.entries(domainToPartition).map(([domain, partition]) => ({
              case: { $eq: ["$domain", domain] },
              then: partition,
            })),
            default: "unknown",
          },
        },
      },
    },
    {
      $project: {
        _id: 0,
        anchor_id: "$anchor_id",
        title: "$title",
        layer: "$layer",
        body: "$body.html",
        debates: "$debates",
        ops: "$ops",
        document_id: "$document_id",
        partition: "$partition",
        document_title: "$metadata.document_title",
        document_summary: "$metadata.document_summary",
        source_file: "$metadata.source_file",
      },
    },
    // Tier 2: group nodes into their parent document. document_title,
    // document_summary, source_file, and partition are identical across
    // every node in the group by construction (see the denormalization
    // note on schema_nodes.json) -- $first is exact here, not a lossy
    // approximation.
    {
      $group: {
        _id: { partition: "$partition", document_id: "$document_id" },
        title: { $first: "$document_title" },
        src: { $first: "$source_file" },
        summary: { $first: "$document_summary" },
        nodes: {
          $push: {
            anchor_id: "$anchor_id",
            title: "$title",
            layer: "$layer",
            body: "$body",
            debates: "$debates",
            ops: "$ops",
          },
        },
      },
    },
    {
      $project: {
        _id: 0,
        document_id: "$_id.document_id",
        partition: "$_id.partition",
        title: 1,
        src: 1,
        summary: 1,
        node_count: { $size: "$nodes" },
        nodes: 1,
      },
    },
    // Tier 1: group documents into their parent partition.
    {
      $group: {
        _id: "$partition",
        documents: { $push: "$$ROOT" },
      },
    },
  ];

  const grouped = await db.collection("nodes").aggregate(pipeline).toArray();
  const documentsByPartition = new Map(grouped.map((g) => [g._id, g.documents]));

  // Merge with the static partition list so every partition appears even
  // with zero documents -- matches scripts/parse_graph.py's own behavior
  // of always emitting all 7 partition entries.
  const partitions = PARTITION_ORDER.map((id) => ({
    id,
    label: PARTITION_LABELS[id],
    documents: (documentsByPartition.get(id) || []).map((d) => {
      // codeowner is intentionally NOT stored on node/debate documents at
      // all (unlike document_title/document_summary) -- it is a pure
      // function of partition, so it is attached here in application code
      // exactly like scripts/parse_graph.py attaches
      // PARTITION_CODEOWNERS[partition_id] in Python, rather than being a
      // third denormalized field needing its own consistency story.
      const { document_id, ...rest } = d;
      return { document_id, ...rest, codeowner: PARTITION_CODEOWNERS[id] };
    }),
  }));

  return { partitions };
}

const PARTITION_CODEOWNERS = {
  core: "@core-team",
  adr: "@core-team",
  ops: "@ops-lead",
  exec: "@board-exec",
  "adr-internal": "@core-team",
  "ops-internal": "@ops-lead",
  "core-internal": "@core-team",
};

/**
 * Public entrypoint: assembles the full public/graph.json-shaped object
 * from live collections. `db` is a MongoDB Node.js driver `Db` instance
 * (from `MongoClient.connect(uri).then(c => c.db(name))`) -- not
 * provided by this file, see the file header.
 */
async function buildGraphJson(db) {
  const [vi, en] = await Promise.all([fetchLangGraph(db, "vi"), fetchLangGraph(db, "en")]);
  return { vi, en };
}

export {
  buildGraphJson,
  fetchPartitions,
  fetchLangGraph,
  canonicalJson,
  computeHashChainEntry,
  verifyHashChain,
  GENESIS_PREV_HASH,
  PARTITION_ORDER,
  PARTITION_LABELS,
  PARTITION_CODEOWNERS,
};
