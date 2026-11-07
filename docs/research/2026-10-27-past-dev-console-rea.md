# past.dev's engine, as its own console describes it (REA pass, 27 October 2026)

Method: the founder signed up for past.dev's free tier in a separate Chrome profile; REA's `analyze_web_bundle` attached over CDP to `app.past.dev` (read only, no clicks, no storage keys), and the public console bundle (`/assets/index-DVwW_qKZ.js`, 3.4 MB, Vite + React, Apollo GraphQL at `/graphql`, AG Grid, React Flow + ELK for graphs) was read statically for its GraphQL documents and its help text. No source maps are published. Nothing was executed and no API call was made. Everything below is what the console asks the server for and how it explains the engine to its users; server internals (models, prompts, storage) stay unknown.

## 1. The data model

**Two node kinds in one graph: sources and memories.** "Blue is a source, the thing you gave us. Orange is a memory." Every memory has `inputs` (the sources and memories it was built from, each with `artifactId`, `dataPointId`, `roles`, `occurredAt`, `excerpt`) and the timeline returns `edges {inputId, derivedId}`. Memories are derived from sources and from other memories: "What a memory was built from and what was built on it, on the graph and on a clock."

**A memory** (`fragment MemoryData on Memory`): `id, kind, content, createdAt, validFrom, validTo, source {dataPointId, occurredAt, excerpt}, audience {scopeId, kind, name}, phase`, plus `render {kind, label, value, values}` for display. The trace node adds `headline, memoryKind, dataPointRevision, readyAt, current, replacedArtifactId, inputCount, outputCount, failure, ingestionId, requestId`.

**Memory kinds**, from the help text (labels, not the enum names):
- a **statement**: "A statement the source makes about the world, with who said it. It can be true, false or contested."
- an **observation**: "Something the source records as seen or measured, without a claim about why."
- a **topic**: "A topic the source talks about, kept so recall can find the source by subject."
- a dated **event** ("A date or a period the source ties a fact to"; "step from event to event")
- **entities and relationships** ("Browse entities, memories and the graph"; "Who belongs to what: a person in a team, an account in a segment")

**Bi-temporal.** Two clocks per memory: when it is true (`validFrom`/`validTo`; "The bar covers the time the memory is true. A dashed end means it is still true") and when past.dev learned it (`createdAt`, `readyAt`; "A black dot marks the time past.dev learned a fact. A gap before the bar means the fact is backdated"). The timeline view switches between "Validity" and "Arrival".

**Supersession, not overwrite.** `supersessions {artifactId, targetId, at}` and `replacedArtifactId`; "A grey arrow at the end of a bar shows where the statement that superseded it starts"; "Black is the current memory: the one recall returns for this now"; the root's strip lists "one chain of statements, earliest first". `current` is a boolean on each node.

**Corrections are new data points.** `mutation CorrectMemory(projectId, memoryId, content) → {dataPointId, ingestionId}`. "You do not edit a memory. You send a new data point. past.dev reads it and records what changed. This memory does not change." Revisions of the same data point are tracked (`dataPointRevision`; the root's sections are "Revisions, Built from, Produced, and Elsewhere in the lineage").

**Deletion cascades through derivation.** "Removes the memory and anything derived from it. This is the erasure path, and it is the one to use for a deletion request."

## 2. The ingest pipeline (phases, from the console)

1. **Accepted**: "past.dev stored the data point and processes it in the background."
2. **Preparing**: "The source text is being saved and indexed before memories are extracted."
3. **Prepared**: "The source text is searchable and this push is waiting for its turn to extract memories." (a queue: `pipelineQueue`)
4. **Captured**: "past.dev has read the content and is still understanding it: the source is searchable already, and recall keeps improving as memories are drawn from it. This takes seconds to minutes."
5. **Indexed**: "Every memory drawn from this data point is in memory and reachable by recall."
6. **Failed**: kept, retryable "at no cost".

So the raw source is searchable within seconds (a text index over sources), and extraction into memories runs after, in a queue. Recall works over both: sources while memories are pending, memories once drawn.

## 3. Recall

The insight view per request (`request.recall`): `searchSeeds, hydratedArtifacts, selected, usedEvidenceTokens` and `timings {expansionMs, searchMs, materializationMs, rerankMs, compositionMs, computeMs}`. That names the stages:

1. **expansion** (the query is expanded),
2. **search** (seeds found: `searchSeeds`),
3. **materialization** (seeds hydrated into artifacts: `hydratedArtifacts`, i.e. graph neighbours of the hits pulled in),
4. **rerank** ("Candidates ranked"; "The scored settings: {limit} candidates and {level} evidence, a {tokens}-token page"),
5. **composition** (the evidence page fitted to the token budget: `usedEvidenceTokens`; "Recall reads only the evidence it selects").

The timeline records each recall against the memory (`recalls {receivedAt, returned, askedAsOf, hitIds}`, `recalledCount`, `lastRecalledAt`), so they know which memories are actually used. A recall is always "as" an identity: "On recall, send the identity alone: past.dev resolves its audiences and never returns evidence from outside them." There is also "A grounded answer with citations, over the same evidence" (an answer endpoint beside recall).

## 4. Access, identity and doors

- **Audiences** are `fixed` lists or `rule`s over identity traits; memories stay when an audience goes ("Its memories stay and reach nobody until an audience reaches them again"; a new audience with the same slug "starts empty").
- **Identities** are created on first sight, carry traits, can be imported by CSV and synced from an **identity provider** (`ConnectDirectory`, `SyncMcpDirectory`, `idp_groups`): "That person's audiences, decided by your identity provider."
- **Three doors**: "Memory across every agent session" (plugins for Claude Code, Codex, Cursor, Junie, Hermes: "When a session is over it sends what was said, and on every prompt it puts what past.dev recalls into the context"); "Memory in Claude, ChatGPT and Cursor" (one MCP address per organization; users sign in at your IdP and "every tool call answers as the person who signed in"; one link per identity for agents); "Memory inside your own product" (the API).
- Console operations also cover webhooks as an ingest source, spend caps, contract credits, organization tags, access groups and rules, and MCP write toggles (139 GraphQL operations in all).

## 5. What this means for our build

It confirms the research's inference and makes it concrete. past.dev is:

1. **A typed, bi-temporal derivation graph**: sources → memories (statements with speaker and truth status, observations, topics, events, entities and relationships), every memory carrying validity and learned-at times, supersession chains with one `current` per chain, derivation edges for lineage and cascading deletion. This is Graphiti's model (valid/invalid edges, episodes as evidence) extended with typed claims and a second derivation layer (memories built from memories).
2. **Two-speed ingestion**: index the raw source first (searchable in seconds), extract memories asynchronously in a queue.
3. **A five-stage recall**: expand → seed search → graph materialization → rerank → compose to a token budget, under identity-resolved audiences, with recalls logged per memory.
4. **Corrections and deletions as first-class data**: a correction is a data point; deletion follows derivation.

What we copy for Lightbulb's engine (shape, not code): the two node kinds and the derivation edges; the memory kinds plus our lessons (workflow, correction, gotcha, convention); validity and learned-at on every memory; supersession with one current per chain; corrections as data points; cascading erasure; the five recall stages with per-stage timings exposed to the customer; per-memory recall logging (which also gives us the "which memories helped" signal Beacon lacks); audiences resolved from rooms and from an IdP.

What we still do not know: the extraction prompts and models, the search index (BM25, vectors or both), the expansion method, the reranker, and how a statement is judged to supersede another. The API probes (crafted contradictions, updates and timelines, read back through `explain` and the lineage) answer the last one empirically.
