# specs-reference

Companion tree to `specs5/`. Holds implementation detail that `specs5/` deliberately leaves unspecified: byte-level formats, numeric constants, persistent storage schemas, RPC argument shapes, SDK call signatures, dependency quirks, and similar concrete detail that's load-bearing for interop but too specific for a behavioral contract.

## The Mechanical Twin Rule

When implementing from `specs5/{path}/{name}.md`, also load `specs-reference/{path}/{name}.md` if it exists.

One rule, one path transformation. The two trees mirror each other:

```
specs5/
  3-engine/
    permissions.md         — behavioral contract
specs-reference/
  3-engine/
    permissions.md         — callback signature, return types, rule syntax, timeouts
```

Path structure is identical. File names are identical. Only the top-level tree name differs.

## Sparse Twin Policy

Twins exist **only when there is content to put in them**. A missing twin is not a bug — it means the parent specs5 spec is self-sufficient and a reimplementer needs no supplementary detail to implement it correctly.

AI tooling loading specs content should check for twin existence on each load. The cost is trivial; the alternative (creating empty twins preemptively) clutters the tree and makes "is there real content here?" a harder question for human readers.

## The Non-Replication Rule

**A twin file must never restate content from its parent specs5 spec.** It supplements, it doesn't duplicate.

Concrete test for each proposed twin section:

- Can a reimplementer make correct behavioral decisions from the specs5 spec alone? If yes, nothing about behavior goes in the twin.
- Is this a byte-level format, numeric constant, schema, SDK signature, or dependency quirk? If yes, it belongs in the twin.
- Is this a design decision or rationale? If yes, it belongs in the specs5 spec, not the twin.

When in doubt, leave it out. Sparse twins are fine; duplicating specs5 content is not — duplication creates drift risk on every spec update.

## Section Conventions

Standardise the layout within each twin so readers (human and AI) know where to look:

```markdown
# Reference: {Spec Name}

**Supplements:** `specs5/{path}/{name}.md`

## Byte-level formats
...exact marker bytes, exact delimiter specs...

## Numeric constants
...thresholds, timeouts, retry counts...

## Schemas
...JSONL fields, config keys, RPC argument and return shapes...

## Dependency quirks
...implementation-specific gotchas for this area...

## Cross-references
- Related detail: `specs-reference/{other path}`
```

Sections empty or omitted when nothing applies. The standardised headings let a reader find the relevant detail without reading the whole file.

## Cross-Cutting Content

When detail genuinely spans multiple specs (a format produced by one spec and consumed by another), pick a **canonical owner** and have other twins link to it rather than duplicate.

Rule of thumb: the twin for the spec that *produces* the format owns it; twins for specs that *consume* it link.

Named examples, all pointing at files that exist:

- **Turn-scoped event payloads** (`ChunkPayload`, `ToolCard`, `StreamCompleteResult`, `compactionEvent` stages) — canonical owner: `specs-reference/3-engine/session.md`. `specs-reference/1-foundation/rpc-inventory.md` and `specs-reference/5-webapp/chat.md` link to it rather than restate the shapes.
- **Permission request, decision, and rule syntax** — canonical owner: `specs-reference/3-engine/permissions.md`. The RPC inventory links for `resolve_permission`; the file-picker twin links for the `Read(path)` deny rule it writes.
- **Symbol map compact syntax** — canonical owner: `specs-reference/2-indexing/symbol-index.md`. Consumers (the MCP bridge's tool results, the LSP methods) link when they need the format.
- **Mirrored-store record schema** — canonical owner: `specs-reference/3-engine/history.md`. Anything that calls a record a `MessageDict` links there.
- **Non-engine RPC argument and return shapes** — canonical owner: `specs-reference/1-foundation/rpc-inventory.md`. Consumers link to specific methods rather than re-documenting them.

Canonical ownership is the one place where topical thinking survives in the mirrored layout. Duplication creates drift risk; linking creates a single source of truth.

## What Stays Outside

- Behavioral contracts, invariants, module decomposition, data flow, design rationale — all stay in `specs5/`
- Historical delivery records — stay in `specs5/impl-history/`. They reference specs3 and the native engine intentionally and are not migrated
- Conversion planning, decisions, and risk register — stay in `specs5/plan/`. `specs5/plan/sdk-surface.md` looks like a twin but is not one: it is a dated snapshot of an external dependency, and the installed wheel outranks it

## No Synced Mirror

An earlier revision of this tree carried `specs-reference/3-llm/prompts/` — byte-exact copies of the
prompt files under `src/aic_dc/config/`, kept in step by `scripts/sync_prompts.py`. It existed because
prompt text was LLM-interop: a careless edit could break compaction JSON parsing, edit-block
reliability, or commit-message conventions.

It is gone, along with the prompts, the sync script, and the reason for either. AIC⚡DC assembles no
LLM-facing prompt text, so no file in this repository is interop-critical in that way, and nothing here
mirrors source. The one surviving file with prompt-shaped content is `commit.md`, and it is the text of
an ordinary user turn — if it is wrong, the user reads a poor commit message and edits it, which needs
no byte-exact twin.
