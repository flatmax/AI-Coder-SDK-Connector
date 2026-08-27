# Document Index

A document-oriented analog to the symbol index. Extracts structural outlines from documentation files and serves them to the agent as an MCP tool and to the browser's navigation surfaces. Supports markdown and SVG.

## Motivation

- Symbol index covers programming languages only; documentation files appear by path but produce no structural representation
- Many repositories are documentation-heavy (specs, READMEs, wikis, design docs)
- Structural awareness of document content lets an agent navigate a docs tree without reading every file, and lets the browser show an outline for a file it has not opened

## Core Mapping

| Code concept | Document equivalent |
|---|---|
| Class / module | Document |
| Method / function | Heading |
| Imports | Links to other docs |
| Call sites | Cross-references between documents |
| Cross-type references | Doc → code links |

## Data Model

- DocHeading — text, level (1–6), keywords, start line, children, outgoing refs, incoming ref count, content types, section lines
- DocLink — target path, target heading (fragment), source heading, image flag
- DocSectionRef — target path, target heading (nullable for doc-level link)
- DocOutline — path, doc type, headings tree, links

## Module Structure

- Orchestrator with cache, formatter, keyword enricher, reference index, extractor registry
- Extractors registered by file extension
- Separation from symbol index is deliberate — different data model, no tree-sitter dependency, documentation cross-references work differently

## Compact Output Format

- Text block structurally similar to symbol map output
- Returned by the `doc_outline` tool, whole-repo or scoped to a subtree
- Annotations added — document type, keywords per heading, section-level cross-references, incoming reference counts, content-type hints, section sizes

### Annotations

- Document type tag after path (spec, guide, reference, decision, readme, notes, unknown)
- Keywords in parentheses after heading text
- Content type markers — table, code, formula — detected by regex during extraction
- Section size in lines, omitted below threshold
- Incoming reference count with arrow notation
- Outgoing section refs rendered as indented children with arrow notation
- Outgoing doc refs when link has no fragment
- Outgoing code refs for doc → code links

## Document Type Detection

- Heuristic, path- and heading-based
- Path keywords — spec, rfc, design, guide, tutorial, reference, api, decision, adr, notes, meeting
- Filename — README for readme type
- Heading-based fallback for ADR format (Status, Context, Decision) and numbered specs
- `unknown` is the default when no heuristic matches

## Line Numbers

- Raw per-heading line numbers are not in the compact output (mirrors the code context format)
- Start line is stored internally for section text slicing and size computation
- Section sizes are emitted — they convey budget information without the token cost of per-heading positions

## Markdown Extraction

- Line-by-line regex scanning, no external dependencies
- Headings detected, link extraction (inline and image references)
- Content-type detection — table separator rows, fenced code blocks, display/inline math

## SVG Extraction

SVG extraction produces a heading outline by combining three signals, in decreasing order of confidence: explicit group labels, geometric box containment, and spatial clustering. The algorithm is deterministic and depends only on the SVG's XML structure and coordinate geometry — no visual rendering, no OCR, no font-size heuristics.

Stdlib `xml.etree.ElementTree` is the only dependency. Non-visual elements are skipped entirely: `<defs>`, `<style>`, `<script>`, `<metadata>`, `<filter>`, gradients, `<clipPath>`, `<mask>`, `<marker>`, `<pattern>`, `<symbol>`.

### Root-Level Elements

- `<title>` → top-level heading (level 1)
- `<desc>` → level-2 heading
- `<a xlink:href=...>` with non-fragment target → `DocLink` entry for cross-reference tracking
- Duplicate text labels across the document are deduplicated

### Containment Model — The Primary Structural Signal

SVGs rarely have explicit semantic grouping, but they usually have geometric boxes (rects, circles, polygons) that visually contain text. The extractor builds a **containment tree** from shape bounding boxes and attaches text elements to the box that contains them.

**Shape collection.** Every closed shape contributes a bounding box resolved to root-canvas coordinates:

- `<rect>` — bounding box from x, y, width, height
- `<circle>` / `<ellipse>` — bounding box around the oval
- `<polygon>` — axis-aligned bounding box of the point set
- `<path>` — axis-aligned bounding box of the `d` attribute

**Transform resolution.** Every coordinate is transformed to root-canvas coordinates before containment testing. The extractor walks root-to-leaf composing 2D affine matrices — `translate(x, y)`, `scale(s)`, `rotate(deg)`, `matrix(a,b,c,d,e,f)`. Without transform resolution, an Inkscape group translated via `transform="translate(100,50)"` would have its children's coordinates misinterpreted, breaking containment detection.

**Containment tree construction:**

1. Compute root-canvas bounding box for every shape
2. Sort shapes by area descending
3. For each shape, find the smallest *other* shape that fully contains it — that's the parent box in the nesting hierarchy
4. Shapes with no containing shape are root-level boxes
5. Each text element is attached to the smallest box that contains its position

A box containing a sub-box produces a nested heading. A box with sibling boxes at the same nesting level emits them as sibling headings.

### Box Labeling — Three Confidence Levels

For each box in the containment tree, the extractor assigns a heading text using the highest-confidence rule that applies:

**Level 1 — Explicit label on the group.** If the `<g>` surrounding the box has `aria-label`, `inkscape:label`, or a non-auto-generated `id`, that string becomes the heading. Preference order: `aria-label` > `inkscape:label` > filtered `id`. High confidence — the author labeled it deliberately.

Auto-generated ids are filtered via a regex pattern: `^(g|group|path|rect|text|layer)(_?)\d+$` case-insensitive. Matches Inkscape's auto-generated `Group_42`, `g123`, etc. When the id matches this pattern, it's treated as if no label were present and the algorithm falls through to Level 2 or 3. Authors who name groups `Group_42` deliberately see the name verbatim; Inkscape's auto-ids don't pollute the outline.

**Level 2 — Single-text box.** If the box contains exactly one `<text>` element (possibly multi-line via `<tspan>` children or tightly-clustered `<text>` siblings joined), that text IS the box label. High confidence — whatever text is in the box, that's what labels it.

Multi-line joining: consecutive text elements with a vertical gap smaller than their own line height (measured as the y-delta between centroids relative to element bounding-box heights) are joined into one label, space-separated. This catches `<text>Backend</text><text>Services</text>` stacked vertically as the label "Backend Services".

**Level 3 — Multi-text box with no explicit label.** The algorithm does not attempt to detect a title within ambiguous multi-text boxes. Instead, the box heading is a neutral identifier (`(box)` or a short comma-joined excerpt of the first few texts) and all contained texts are emitted as sibling leaves under it. The LLM receives full label information via containment; it doesn't need a claimed title.

This is a deliberate tradeoff. Title detection within unlabeled multi-text boxes is fundamentally ambiguous — a box with "Frontend / Browser / Mobile" could be a labeled cluster (Frontend as title, two items under it) or a flat list of three parallel items. Rather than pick a rule that's right for some cases and wrong for others, we emit the unambiguous containment and let the LLM interpret.

### Long Text Elements — Prose Blocks for Enrichment

Text elements longer than 80 characters are treated as body prose, not labels. They are captured as **prose blocks** attached to the containing box (or at document root when no containing box exists) rather than emitted as heading leaves. Prose blocks participate in keyword enrichment via the same pipeline that enriches markdown sections, producing `(keyword1, keyword2, ...)` annotations in the outline.

Rationale: SVG authors occasionally embed paragraphs of body text via `<text>` with manual line breaks or flowing text in `<foreignObject>`. These aren't labels — they're content. Dropping them would lose real information the LLM could use for navigation. Rendering them as heading leaves would produce wildly-variable heading lengths and overwhelm short-label siblings in the compact output. The right treatment is the same as prose in a markdown section: run it through keyword enrichment and surface the distilled terms.

Threshold: 80 characters. This is roughly one line of readable prose in a diagram. Single-word labels, short phrase labels, and multi-line short labels all fall well under this. Intentional prose content exceeds it.

In the compact output, each prose block renders as an indented `[prose] (keyword1, keyword2, keyword3)` entry under its containing box. The 80-character threshold for prose-classification matches the `keywords_min_section_chars` threshold in [keyword-enrichment.md](keyword-enrichment.md); TF-IDF fallback for sections below `keywords_tfidf_fallback_chars` still applies uniformly (SVG prose blocks and markdown sections share the same enrichment corpus per document).

### Reading Order

At each nesting level, sibling boxes are emitted in reading order based on their root-canvas positions:

- **Primary sort key: y** (top to bottom)
- **Secondary sort key: x** (left to right) when y values are close (within one row of text height)

This produces natural reading order for top-to-bottom layouts, and left-to-right then top-to-bottom ordering for multi-column diagrams. No explicit column detection is needed — the y-then-x sort handles columns as a special case of general 2D reading order.

Text elements attached to a box are emitted in the same y-then-x reading order.

### Shape-Less Fallback

Some SVGs contain text without any containing shapes (text-only diagrams, labeled but ungrouped layouts). For these, the extractor falls back to **spatial proximity clustering**:

1. Sort texts by y (top to bottom)
2. Compute median line height (median of text element heights)
3. Walk texts in order; a vertical gap larger than 2× median line height starts a new cluster
4. Each cluster becomes a sibling heading at the root level; texts within a cluster are leaves under it

This is weaker than containment-based extraction but better than emitting every text as a root-level leaf. AI-generated SVGs without groups and without boxes fall into this path.

### Example — Labeled Box Nesting

```xml
<svg viewBox="0 0 800 600">
  <g id="frontend">
    <rect x="20" y="80" width="300" height="250" fill="#eef"/>
    <text x="40" y="110">Frontend</text>
    <text x="40" y="150">Browser UI</text>
    <text x="40" y="180">Mobile App</text>
    <g>
      <rect x="30" y="210" width="280" height="100" fill="#ddf"/>
      <text x="40" y="240">Authentication</text>
      <text x="50" y="270">OAuth2 flow</text>
      <text x="50" y="290">Session tokens</text>
    </g>
  </g>
  <g>
    <rect x="380" y="80" width="300" height="250" fill="#efe"/>
    <text x="400" y="110">Backend</text>
    <text x="410" y="150">API Server</text>
    <text x="410" y="180">Database</text>
  </g>
</svg>
```

Produces:

```
## architecture.svg [spec]
### frontend
- Frontend
- Browser UI
- Mobile App
#### Authentication (or "(box)" if no label)
- OAuth2 flow
- Session tokens
### (box with Backend, API Server, Database — or use first text as identifier)
- Backend
- API Server
- Database
```

The outer frontend group has an explicit `id="frontend"` (Level 1) → used as heading. The inner auth group has no label (Level 3) → emits its texts as siblings with a neutral identifier. The backend group (no label, multi-text) → same Level 3 treatment.

Under Level 2, the inner auth box would receive `Authentication` as its label if it contained only that one text. With three texts, we fall to Level 3.

### Example — Shape-Less SVG (Spatial Clustering)

```xml
<svg viewBox="0 0 600 400">
  <text x="50" y="30" font-size="20">System Overview</text>
  <text x="50" y="80">Request arrives</text>
  <text x="50" y="110">Router dispatches</text>
  <text x="300" y="80">Error Path</text>
  <text x="300" y="110">400 returned</text>
</svg>
```

No shapes at all — falls to spatial clustering. Reading order (y then x): "System Overview", then "Request arrives" and "Error Path" at y=80 (left then right), then "Router dispatches" and "400 returned" at y=110. Gap detection merges adjacent rows into clusters. Output is a flat or minimally-grouped list; with no containment or labels, the extractor cannot infer hierarchy reliably.

### SVG Indexing Policy

- SVG structural extraction is unconditional. The old policy made it mode-dependent (implementation artifacts in code mode, documentation in doc mode); with modes gone there is nothing left to switch on, and an SVG outline is cheap enough that withholding it buys nothing
- A `doc_outline` call scoped to a subtree therefore returns SVG outlines alongside markdown ones. The caller's scope, not a global mode, decides whether diagrams are in view
- Short text labels (≤ 80 chars) skip enrichment — they are already concise identifiers and running KeyBERT on individual words produces no useful signal
- Long text elements (> 80 chars) are treated as prose blocks and participate in keyword enrichment via the same pipeline used for markdown sections — see "Long Text Elements" above
- SVG files with no prose blocks skip enrichment entirely (the common case — architecture diagrams and flowcharts are almost entirely labels)

### What This Design Deliberately Does Not Do

- **No font-size heuristics.** Font sizes in SVG have no defined semantic meaning — a 24pt text may be a heading or a particularly large body label. Using font-size to infer hierarchy produces false positives and false negatives that aren't recoverable.
- **No title detection within ambiguous multi-text boxes.** See Level 3 above. Emitting unambiguous containment beats emitting wrong hierarchy.
- **No visual layout rendering.** No playwright, no resvg, no Chromium. Coordinate math is sufficient for reading order and containment.
- **No OCR.** SVG `<text>` elements contain the text directly — there's nothing to recognize.
- **No keyword enrichment of short labels.** Labels ≤ 80 chars bypass KeyBERT. Running embedding-based keyword extraction on individual words or short phrases produces no useful signal; the label *is* the keyword.

## Image References

- Extracted as DocLink entries with image flag set
- Enables doc → SVG and doc → image cross-references in the reference graph
- Path-extension scan per line — matches paths ending in image extensions regardless of embedding syntax
- Matched paths validated against the repository file tree (unless validation disabled for single-file extraction)
- External URLs excluded by repo validation

## Indexing Lifecycle

### Triggers

- Server startup (background structural extraction, then enrichment)
- `PostToolUse` for a tool that wrote a document file — targeted invalidation, re-extraction, and an enrichment queue entry (see [`../3-engine/tool-surface.md`](../3-engine/tool-surface.md#reacting-to-file-changes))
- Before a `doc_outline` tool call answers — any pending re-index is flushed first, so the agent never reads an outline that predates its own edit
- Periodically, and on repo-changing events (branch switch, commit, review enter/exit) — a full `index_repo(file_list)` pass that re-extracts changed files via the mtime cache AND prunes stale entries for files no longer on disk (user deleted, `git rm`, branch switch)
- User edits in viewer (lazy detection via mtime on next structure pass)

### Mtime-Based Cache

- Files with unchanged mtime are not re-parsed
- Re-indexing after saves is effectively free for unchanged files

### Explicit Invalidation

- The agent's file-writing tool calls trigger invalidation of both symbol and doc caches for all modified files
- Modified files get fresh unenriched outlines instantly
- Enrichment is queued for background processing

### Two-Phase Principle

- Structural extraction is synchronous and instant (< 5ms per file); exposed as `doc_index_ready` on the engine service's state snapshot
- Keyword enrichment is asynchronous and never blocks user-facing operations; completion exposed as `doc_index_enriched`
- Preset switches are instant — unenriched outlines are available immediately
- A turn never waits for enrichment; a `doc_outline` call waits only for structural extraction
- Features gated on structural readiness only (the `doc_outline` tool, outline display) check `doc_index_ready`; features that cosmetically benefit from enriched outlines (keyword display in compact format) check `doc_index_enriched` but degrade gracefully when False

## Disk Persistence

- Per-file JSON sidecar in the per-repo working directory
- Sidecar filename derived from path (slashes replaced)
- Sidecar content — path, mtime, content hash, keyword model name, serialized outline
- Sidecars survive server restart, avoiding expensive re-enrichment on every restart
- Corrupt sidecars silently removed on load
- Model change invalidates entries — stored model name is checked on cache lookup, mismatched entries treated as stale

## Structure-Only Cache Lookup

- A separate code path accepts any cached outline regardless of keyword model
- Used by preset switching and tool calls to avoid blocking on enrichment
- Files whose mtime has changed are re-parsed; enriched or unenriched outlines are reused as-is

## Progress Feedback

- Non-blocking header progress bar in the dialog header
- Shows current file and completion percentage during background enrichment
- Auto-dismisses when all pending files are enriched
- Driven by the per-file `doc_enrichment_*` progress events on the `compactionEvent` channel
- No toast — toasts would obstruct the chat input area during multi-minute enrichment

## Graceful Degradation

- If the keyword library is unavailable, structural outlines still work fully
- Heading tree, cross-references, reference counting, and the `doc_outline` tool all function without keywords
- A one-time warning toast informs the user
- Header progress bar is suppressed when no enrichment is possible

## Consumers

- **The agent, via the MCP bridge** — the `doc_outline` tool. This is the index's highest-value
  consumer: there is no built-in equivalent, and SVG in particular is otherwise opaque to an agent
  (`Read` on an SVG returns coordinate soup; the outline returns a labelled containment hierarchy).
  See [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md).
- **Browser document navigation** — markdown outline navigation, SVG structure display, and
  cross-reference following.
- **The document converter** — provenance and staleness classification.

Keyword enrichment continues to run, because keywords are what make an outline searchable by intent
rather than by heading text. See [keyword-enrichment.md](keyword-enrichment.md).

## Snapshot Discipline

Re-indexing happens at **tool-call boundaries**. Within the execution window of a single query the
document index is a **read-only snapshot** — outline queries, per-file block lookups, and reference
graph queries all return consistent data.

A file-mutating tool call triggers incremental re-extraction of the touched document files (debounced),
and a pending re-index is flushed synchronously before any index-reading tool returns. Structure is
re-extracted immediately; keyword enrichment remains deferred, since it is CPU-bound and an unenriched
outline is still a correct outline.

Background keyword enrichment runs outside any query window. A query that starts while enrichment is in
progress sees whatever outlines are currently cached (enriched or unenriched); enrichment completions
never mutate a snapshot mid-query.

## Invariants

- A file's unchanged outline is never re-extracted
- SVG files never undergo keyword enrichment
- Keyword enrichment never blocks a turn, a `doc_outline` call, or any other user-facing operation
- The outline's content hash is stable for unchanged content
- Extractor dispatch is by file extension alone — there is no global mode to consult