# Implementation Guide

How to use `specs5/` and `specs-reference/` together when implementing AIC⚡DC.

## Context: Why Two Suites Exist

AIC⚡DC's specification is split across two peer directories:

- **`specs5/`** — behavioural contracts, invariants, module decomposition, data flow. Written at the level a capable reimplementer actually needs. Deliberately omits byte-level detail that a fresh implementation would legitimately handle differently.
- **`specs-reference/`** — implementation detail that specs5 deliberately leaves unspecified but an implementation must reproduce for interop. Byte-level formats, numeric constants, persistent storage schemas, RPC argument shapes, dependency quirks. Mirrors specs5's path structure; each twin supplements its specs5 counterpart.

The goal is equivalent user-visible behaviour and interop compatibility, not line-by-line reproduction. Internal structures (module boundaries, class hierarchies, internal APIs, framework patterns) are the implementer's choice. External structures (wire formats, file formats, numeric thresholds that affect observable behaviour) are contracts, because existing data, existing user configs, and other AIC⚡DC instances depend on them.

There is a third input, and it is not a spec: **the installed Claude Agent SDK**. See [Reading the SDK](#reading-the-sdk) below.

## The Two-Suite Relationship

**specs5 is the primary reference.** It describes behavioural contracts, invariants, module decomposition, and data flow at a level suitable for clean-room implementation. Design from specs5, structure modules per specs5, write tests against specs5's invariants.

**specs-reference is the detail reference.** Byte-exact formats, numeric thresholds, storage schemas, dependency quirks. When specs5 leaves a format or threshold unspecified, consult the mirrored file at the same path and name. Twins exist only where specs5 needs supplementing — a missing twin means the specs5 spec is self-sufficient.

The conversion added a third kind of twin content: **SDK-behaviour findings**. Where the SDK does something surprising — a protocol method probed by attribute presence, a mirror that is asynchronous despite an eager flag, a bare-flag `extra_args` convention — the finding lives in the twin's *Dependency quirks* section, because that is exactly what it is.

## When to Use Which

### Use specs5 for:

- Architecture and module decomposition
- What components exist and how they relate
- Behavioural contracts (what must happen)
- Invariants (properties that must hold)
- Event flows and lifecycle descriptions
- Test design — invariants become test properties
- Whether a capability is ours at all, or the engine's

### Use `specs-reference/` for:

- **Byte-level formats** — the symbol map compact format, doc outline annotation syntax (`←N`, `→target#Section`, `~Nln`, content-type markers), request-ID and block-identity formats, permission-rule syntax
- **Numeric constants** — debounce intervals, timeouts, retry and backoff schedules, truncation limits, port ranges
- **Persistent storage schemas** — engine-transcript line format and session-key path mapping, the events-log record shape, the derived index's layout, docuvert provenance headers, doc-cache sidecars
- **Config file schemas** — exact field names, nesting, whitelists, legacy-format fallbacks
- **Dependency quirks** — tree-sitter TypeScript function name, Vite `optimizeDeps` exclusions, PyInstaller hidden imports, Monaco worker configuration, `mcp` version floor, CLI discovery and version skew
- **RPC wire formats** — exact argument shapes, return shapes, event payload structures

## Reading the SDK

Layer 3 is written against the Claude Agent SDK, which is a dependency rather than a contract we own. Three rules follow:

1. **[`../plan/sdk-surface.md`](../plan/sdk-surface.md) records what was verified, and when.** It is a snapshot, not a guarantee. Read the installed package before implementing anything in layer 3.
2. **The installed wheel wins over every document in this repo, including this one.** If the SDK's behaviour contradicts a spec, the spec is stale — fix the spec rather than working around it silently.
3. **Never assume a class or option exists because a spec names it.** Several plausible-sounding names in the origin brief turned out not to exist; the same will happen again as the SDK moves. Attribute-probe rather than import-and-hope where the spec says the surface is optional.

Version skew between the SDK and the `claude` CLI is its own failure class, surfaced as an engine-health banner rather than an exception. See [`specs-reference/3-engine/session.md` § Dependency quirks](../../specs-reference/3-engine/session.md#dependency-quirks).

## Conflict Resolution

**When specs5 and a twin conflict, specs5 wins.** specs5 owns behavioural contracts. If a twin's detail describes behaviour specs5 contradicts, the twin is out of date and needs updating.

**When specs5 is silent on a byte-level or numeric detail, the twin is authoritative.** Do not invent alternative wire formats, file formats, or thresholds that affect observable behaviour. Changing them silently breaks compatibility with existing data or existing user configs.

**When both are silent, the implementer chooses.** Class hierarchies, method names, module organisation, framework patterns, and internal APIs are not contracts. A cleaner design is welcome.

**When a twin's detail looks like a workaround for something that no longer exists, raise it.** Some twin detail captures compromises made against the native engine or against library bugs. Flagging "the twin says X but it reads as a workaround for a subsystem we deleted" is more valuable than silent preservation.

**When you have a better approach than a twin prescribes, propose it before implementing.** Describe the twin's approach, the alternative, and the trade-offs. A short discussion beforehand is cheaper than a refactor afterwards.

## Freedom the Implementer Has

- Module organisation — restructure files, merge or split modules, pick better names
- Internal APIs — method signatures, class hierarchies, dependency-injection patterns
- Framework choices — different async primitives, different data structure types
- Error handling — a unified approach rather than case-by-case
- Code style — naming, type hinting, docstring conventions
- Test structure — organise around specs5's invariants
- Performance work — specs5's invariants do not prescribe caching strategies beyond what correctness requires

## Freedom the Implementer Does Not Have

Changes that cross a boundary visible to users, git, the engine, or other AIC⚡DC instances are fixed by interop:

- The persistent storage formats — engine transcript and session-key layout under `.aic-dc/`, the events-log schema, docuvert headers, `.bundled_version` marker, doc-cache sidecars. The derived index is *not* in this list: it is rebuildable, so its format may change freely
- The RPC surface the webapp expects, including server-push event names and payload shapes
- The config file schemas users edit, and the whitelist of what the Settings tab may write
- The `aic-dc` MCP tool names, argument shapes, and output formats — the agent's prompt cache and any project-level tool-permission rules are keyed to them
- Permission rule syntax written into project settings, which the CLI reads too

There is one entry that used to be on this list and is now absent: **LLM-facing prompt text**. AIC⚡DC no longer has any. The engine's system prompt is the engine's, and repo conventions belong in `CLAUDE.md`, which the user owns.

## Subtle Cases

Some specs5 descriptions can be satisfied in several valid ways, but a twin pins a sequencing or timing detail that downstream behaviour depends on. The load-bearing examples:

- **Index flush before a tool answer.** Re-indexing is debounced, but a pending flush must complete before any `aic-dc` tool returns. Get the ordering wrong and the agent silently reads a stale map — no error, just a wrong answer.
- **Drain before the next turn.** Cancellation must run the pump to `ResultMessage`. Breaking out of iteration routes the interrupted turn's tail into the next turn's UI and produces asyncio cleanup failures.
- **Hooks are not a decision channel.** A `PreToolUse` hook that returns a permission decision silently disables the dialog. Observe in hooks; decide in `can_use_tool`.
- **Credential resolution.** Nothing may export provider credentials into the process environment. The CLI resolves its own; polluting the environment changes which account a turn bills to.

When in doubt, read the twin for sequencing and ordering constraints, not just formats.

## Where specs5 Is Incomplete Without specs-reference

| Area | specs-reference location |
|---|---|
| Symbol map compact format | [`2-indexing/symbol-index.md`](../../specs-reference/2-indexing/symbol-index.md) (legend, abbreviations, ditto marks, path aliases, test collapsing) |
| Doc outline annotation syntax | [`2-indexing/document-index.md`](../../specs-reference/2-indexing/document-index.md) (keyword parentheses, content-type markers, section size, ref counts, outgoing refs) |
| Request ID, block identity, timestamp formats | [`3-engine/session.md` § Byte-level formats](../../specs-reference/3-engine/session.md#byte-level-formats) |
| SDK options assembly | [`3-engine/session.md` § Schemas](../../specs-reference/3-engine/session.md#schemas) (every `ClaudeAgentOptions` field we set, and why) |
| Streaming and lifecycle event payloads | [`3-engine/session.md` § Service: AcApp](../../specs-reference/3-engine/session.md#service-acapp--server--browser) — the authoritative server-push event set |
| Permission rule syntax and `PermissionUpdate` shape | [`3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md) (rule content syntax, callback signature, return types, tool classification map) |
| Which permission requests have a deadline, and the ID format | [`3-engine/permissions.md` § Numeric constants](../../specs-reference/3-engine/permissions.md#numeric-constants) |
| `.aic-dc/` layout, session keys, engine transcript lines | [`3-engine/history.md` § Byte-level formats](../../specs-reference/3-engine/history.md#byte-level-formats) |
| Mirrored-store JSONL schema | [`3-engine/history.md` § Schemas](../../specs-reference/3-engine/history.md#schemas) |
| `SessionStore` conformance harness | [`3-engine/history.md` § Dependency quirks](../../specs-reference/3-engine/history.md#dependency-quirks) |
| RPC method signatures | [`1-foundation/rpc-inventory.md`](../../specs-reference/1-foundation/rpc-inventory.md) (full inventory with argument and return shapes) |
| Config file schemas | [`1-foundation/configuration.md`](../../specs-reference/1-foundation/configuration.md) |
| Docuvert provenance header | [`4-features/doc-convert.md`](../../specs-reference/4-features/doc-convert.md) |
| Collaboration admission messages | [`4-features/collaboration.md`](../../specs-reference/4-features/collaboration.md) (message types, 120 s timeout, close code 1008, share-info payload) |
| Startup progress stages | [`6-deployment/startup.md`](../../specs-reference/6-deployment/startup.md) (stage name strings, reconnect backoff, port probe range) |
| Dependency quirks | [`2-indexing/symbol-index.md`](../../specs-reference/2-indexing/symbol-index.md) (tree-sitter TypeScript), [`5-webapp/diff-viewer.md`](../../specs-reference/5-webapp/diff-viewer.md) (Monaco workers), [`6-deployment/build.md`](../../specs-reference/6-deployment/build.md) (Vite `optimizeDeps`, PyInstaller imports), [`3-engine/session.md`](../../specs-reference/3-engine/session.md) (CLI discovery, `mcp` floor) |

Two rows that earlier suites carried are gone rather than moved: cache-tier thresholds and model-specific cache minimums (no tiering), and system prompt text (no prompt).

## Architectural Position Changes

This suite changes what AIC⚡DC *is*, not merely how it is built. The binding decisions, each with its rationale, are in [`../plan/decisions.md`](../plan/decisions.md); the file-by-file disposition is in [`../plan/inventory.md`](../plan/inventory.md). Read `decisions.md` before writing code in any layer — several specs read as under-specified until you know that the corresponding capability is deliberately the engine's.

The four that most change how a layer is built:

| Decision | Consequence for the implementer |
|---|---|
| [CC-1](../plan/decisions.md#cc-1--total-replacement-not-a-dual-engine-mode-user) Total replacement | No dual-engine abstraction layer. Do not write an interface that both a native engine and the SDK could implement; there is one engine. |
| [CC-6](../plan/decisions.md#cc-6--the-indexes-reach-claude-code-as-mcp-tools-not-as-prompt-text) Indexes as tools | The indexes have one consumer shape — request/response — instead of two. No assembly path, no per-turn seeding. |
| [CC-3](../plan/decisions.md#cc-3) + [CC-19](../plan/decisions.md#cc-19) Mirrored history | **One** transcript, two roles. Never read it back to build context — continuity is `resume`/`fork_session`. Never give the store an entry the CLI did not write. Anything else under `.aic-dc/` is derived and rebuildable, or holds only what the transcript never had. |
| [CC-15](../plan/decisions.md#cc-15--permission-prompts-are-localhost-only) Localhost-only permissions | The authority check belongs in the resolution path, not in the UI. A hidden button is not a security boundary. |

Contracts inherited from earlier suites that remain live and are not obvious from any single spec: the repository layer's **per-path write mutex**; the **one user-initiated turn at a time** guard, which counts user intent and therefore does not gate subagents; **streaming state keyed by request ID** rather than a singleton passive-stream flag; and the **single bespoke SVG editor** on both panes, with the left pane constructed read-only.

## Build Order Suggestion

Bottom-up, matching the layer numbering:

1. **Foundation** — RPC transport, configuration, repository (git operations, file I/O)
2. **Indexing** — symbol index, document index, reference graph, keyword enrichment
3. **Engine** — session and message pump, permissions, tool surface and hooks, `SessionStore` and history, MCP bridge, context visibility
4. **Features** — images, code review, collaboration, document convert
5. **Webapp** — shell, chat, viewers, file picker, search, settings, specialised components
6. **Deployment** — build, startup, packaging

Each layer depends only on layers below it. Within layer 3 the order matters more than usual, and it is not the order the specs are listed in: **session and pump first, then permissions, then hooks and the tool surface, then history, then the MCP bridge, then context visibility**. Permissions before the tool surface because a tool surface built under `bypassPermissions` bakes in the assumption that tools never block. The MCP bridge after history because the bridge is the first component with two masters — the agent and the browser — and it is easier to get right once the storage boundaries are settled.

For converting an existing installation rather than building fresh, the phase table in [`../plan/README.md`](../plan/README.md) supersedes this ordering: it keeps the tree shippable at every step, which a bottom-up rebuild does not.

## Testing Strategy

- Every spec ends with an invariants section; treat these as test property sources.
- **Unit tests** verify component-level invariants — "the pump routes an unknown block kind to the generic path", "a permission rule is always tool-plus-pattern, never a bare tool grant".
- **Integration tests** verify cross-layer invariants — "a `PostToolUse` write is reflected in the next `symbol_map` result", "a cancelled turn's transcript ends with a `ResultMessage`".
- **End-to-end tests** verify user-facing contracts — "a deny-read rule written from the picker survives a browser reload", "a restart resumes the previous session".
- **Contract tests against the SDK** are their own category and the only defence against version skew: the `SessionStore` conformance harness, the message-taxonomy coverage check, and an options-assembly test that fails when a field we set disappears from `ClaudeAgentOptions`. These are cheap, and they fail loudly on an SDK upgrade instead of quietly at runtime.

Anything that requires a real model call belongs behind a marker and out of the default run. The engine is a subprocess with credentials; a test suite that needs it is a test suite nobody runs.

## Verifying UI Work Against a Running Engine

Green suites have met a live CLI and lost four times (phases 3–6). What they cannot reach is a claim
made in prose, a layout, or anything whose mechanism lives in the browser rather than in our code. This
section is the standing recipe; it was migrated out of the background-subagent fix list when that list
closed, and the traps in it were each paid for once.

**Use a dedicated backend, never the live session.** A plain launch serves the same built bundle the
`--preview` path does, without rebuilding `webapp/dist` underneath a session that is serving it:

```
.venv/bin/aic-dc --repo-path /tmp/aicdc-uitest --server-port 18190 --webapp-port 19110 \
    --no-browser --verbose
```

**Read the real ports off the startup log** rather than trusting the numbers requested. To stop it,
iterate `pgrep -f aicdc-uitest` and check `/proc/$p/cmdline` — a `pkill -f` whose pattern appears in its
own command line kills the shell that ran it.

Match the *serving mode* to what is being tested. `--dev` runs Vite, `--preview` rebuilds and runs
`vite preview`, and a plain launch serves `webapp/dist` from Python. A fault seen on one is not
reproduced by testing another, and 6c cost a sitting to that distinction.

**Frontend DOM notes for driving it:** `aic-chat-panel` is at shadow-DOM depth 2 (walk the shadow roots
recursively), `panel._tabs` is a `Map`, and `panel.messages` is the active tab's view. RPC goes through
`document.querySelector('aic-app-shell').call['ClaudeCodeService.<method>'](...)`.

### Traps

- **Send through the input box, not `chat_streaming` directly.** The panel routes chunks by request id,
  so a turn it did not start lands in no tab: the transcript stays empty and no subagent row appears.
  Set `.input-textarea`'s value, fire `input`, then a `keydown` of Enter.
- **A native `confirm` handled through a CDP harness re-appears on the next navigation.** It looks
  exactly like a dialog replaying on page load. Stub `window.confirm` **in the page, before any app
  script** — `Page.addScriptToEvaluateOnNewDocument`, not an `evaluate` after load — so no native dialog
  is ever created and nothing downstream can re-surface one. Trust in-page instrumentation over what the
  harness reports.
- **Editing frontend source while the page is open triggers a Vite reload** under `--dev`, which drops
  live subagent tabs mid-inspection. Backend edits need a full restart — Python is not hot-reloaded.
- **`chrome-devtools-mcp` holds one Chrome profile at a time.** A second session's server cannot launch
  and fails with "the browser is already running". Do not kill the Chrome holding it; it belongs to
  another session. Launch an independent Chrome on a scratch `--user-data-dir` with its own
  `--remote-debugging-port` and drive it over plain CDP — `scripts/permission_mode_load_probe.py` is the
  worked example, and `websockets` plus `requests` in the venv are all it needs.

### A probe that reports an absence needs a positive control

`scripts/permission_mode_load_probe.py` is the pattern to copy. It answers "does the app raise this
dialog on page load?" and the answer is an absence, so it drives a scenario that *must* produce the
dialog and fails the run as **uninterpretable** if that scenario is also clean. Two things this caught
about the probe itself, both of which would have shipped as findings:

- Its `change` recorder wrapped only *function* listeners, and Lit's EventPart registers the part
  **object** (`handleEvent`). The arm was dead, and a dead arm reports "no phantom change events" for a
  mechanism it was never watching. The positive control is what exposed it — the confirmation fired with
  no `change` logged in front of it.
- Distinguish "the guard worked" from "the mechanism never existed". Park the bait, then **remove the
  guard and re-run**. For 6c that is what demoted `autocomplete="off"` from load-bearing to belt-and-
  braces: with the attribute stripped, Chrome 151 still restored nothing.

Anything a probe bounds — a top-N, a sampling, a skipped retry — gets logged as dropped. Silent
truncation reads as "covered everything".

### The launch cost is shared now — `scripts/_live_app_probe.py`

Two of these probes existed before the third made the pattern obvious, and everything above the first
assertion was the same in all of them: pick free ports, launch a backend, parse the real ones out of its
log, launch a Chrome nobody else owns, install a recorder, wait for the app, send a turn, wait for it to
end, write a screenshot, tally. [`scripts/_live_app_probe.py`](../../scripts/_live_app_probe.py) is that
support — `Backend`, `Browser`, `Page`, `Report`, `send_turn`, `wait_for_turn` — and it is what made
[`scripts/turn_cost_probe.py`](../../scripts/turn_cost_probe.py) and
[`scripts/subagent_stop_probe.py`](../../scripts/subagent_stop_probe.py) each a file of *scenarios*.
The same economics as a layout scene: the second live probe costs a fraction of the first, so the reason
to write one is now a single sentence in a spec that no test can reach.

**Record at the window, not on the instance.** The app shell re-dispatches every server push as a window
`CustomEvent`, so a document-start `window.addEventListener` sees `stream-complete`, `subagent-event`,
`permission-request` and the rest with no patching of anything the app owns — and it survives a reload,
because the record lives in `sessionStorage`. **Trim what you keep**: a full result payload carries the
whole transcript and will blow the storage quota mid-run, so the recorder stores the dozen fields the
assertions actually read.

**A probe's `window.confirm` stub answers `false` unless a scenario says otherwise.** The default decides
what happens when a probe clicks something it did not mean to, and every confirmation in this app guards
something irreversible — a stop, a grant, a destroy. Defaulting to "yes" makes a harness bug into an
action.

**Assert the basis and the rendering separately.** A basis that arrives and renders nothing is a finding
a probe should be able to state, and a single check over both cannot tell you which half broke.

**Say which verdict you mean.** `add` is pass/fail, `skip` is "not reachable and here is why in a
sentence a reader can check", and `void` marks the run **uninterpretable** — reserved for a precondition
whose absence would void *every* assertion. Getting that boundary right is a judgement per probe: a
missing green sibling in the stop probe makes "the LED distinguishes outcomes" untested, but it does not
touch the finding that the ⏹ produced a terminal status, because the refused-confirmation control already
carries that. Downgrading it to a loud skip was correct; downgrading a dead precondition would not be.

**Keep a wrong prediction in the docstring.** `turn_cost_probe.py` was written expecting a `reset` basis
from `/help` on the strength of a smoke run, and the correction — a zero read from a single-turn session
is not evidence about the turn — is worth more than the prediction would have been if it were right.

#### Traps, each paid for once

- **`pgrep -g` will not find the CLI child.** The SDK's `claude` process is *not* in the backend's
  process group, so a group query reports nothing while a turn is visibly streaming. Walk `ppid` over
  `/proc` instead.
- **Do not identify the CLI by excluding `aic-dc` from its command line.** The SDK launches it with an
  inline `--mcp-config` that names our own MCP server, so `"aic-dc" not in cmdline` filters out the one
  process being looked for. Match the basename of `argv[0]`/`argv[1]` against `claude` / `cli.js`.
- **A skip has to be diagnosable.** The version of that filter that found nothing skipped its scenario
  with a one-line reason and no evidence, twice. It now dumps the descendant list it rejected.
- **Require `rpcConnected` before sending.** `send()` returns silently when the socket is not up, so a
  probe that only waits for the panel to mount waits out its timeout on a turn it never sent.
- **Abort a turn wait on a permission request.** Nothing in a headless probe answers one, so a dialog
  turns a real finding into a timeout. Fail immediately, naming the tool that asked.
- **Inspect subagent tabs before the next send.** `send()` calls `clearSubagentTabs`, so a scenario that
  sends again has thrown away the state its assertions were about.
- **Both halves of an absence, when the absence is a stop.** Click ⏹ with the confirmation *refused* and
  assert nothing happened; then confirm, and assert a sibling subagent still went green. The first proves
  the confirmed click did the work, the second proves the resulting colour is a distinction.

## Measuring Layout in a Real Browser

jsdom does no layout. Every UI test in the suite therefore asserts that a CSS rule *arrives*, and none of
them can see what it lays out — which is how a permission dialog shipped with 520px of empty editor for a
38px diff in front of 60 passing dialog tests, and why the tool-card header's grid had been read by hand at
three widths, twice, with neither reading repeatable by anything in the suite.

[`scripts/layout_probe.py`](../../scripts/layout_probe.py) closes that gap, and the shape of it is the part
worth copying:

**It is a measurement harness that writes screenshots, not a screenshot harness.** Every check asserts on
numbers read out of a real layout engine; the PNGs beside them are evidence for whoever has to understand a
failure, never the assertion. A picture nobody looks at is not a regression test — it is a file. This also
avoids a golden-image maintenance burden, where every deliberate visual change is a diff to re-bless and the
suite teaches people to re-bless without looking.

**Screenshots go to files, never inline.** Raising the buffer ceiling made one inline screenshot survivable,
and a ceiling is not a budget. Output defaults to `.aic-dc/layout-probe/`, which is gitignored.

**The app declares the bounds; the probe reads them.** Where a check needs a number the CSS already knows —
a floor, a ceiling, a rail width — it reads the custom property off the computed style instead of carrying a
copy. A number duplicated into the harness that checks it is a number that will disagree with itself.

**Components are mounted directly, on synthetic payloads.** [`webapp/src/layout-harness.js`](../../webapp/src/layout-harness.js)
exposes `window.__layout.build(scene, opts)` and each scene returns measurements. These are layout
questions, so a live engine would add credentials, a turn of latency and a non-deterministic payload to
answer a question about pixels — the argument for driving the real app in the section above is about claims
in prose and mechanisms in our own code. What is load-bearing is that the CSS, the component and the browser
are the real ones.

**Served by Vite dev, off its own bare page.** A harness pointed at `webapp/dist` reports on whatever was
last built, and a regression harness that can pass against stale bytes is worse than none. The
serving-mode caveat above does not bite here because every style involved is either a Lit `css` literal or
emitted by Monaco at runtime: there is no build-time CSS transform for the two modes to disagree about.
`webapp/layout-harness.html` is not in the shipped bundle — Vite's build input is `index.html` alone.

**And it has a positive control**, per the section above: an implementation that made every editor 90px
tall would pass "the editor is content-driven", so a 200-line diff must be *at* the ceiling and must
scroll. Both checks run every time.

**Adding a scene is a function, not a project, and the third one shows what that buys.** The usage HUD
(2026-09-08, checks [5]–[7]) had two claims nothing could reach: that five sections and their heads fit
300px, and that closing one costs no height. `usage-hud.js` states both and jsdom could only confirm the
half that is presence. 34 checks now measure them, plus a 40-file positive control — see
[`5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md) § *Five Sections In 300px Is Measured Now* for
what they found, including one claim in a source comment that the measurement made *weaker* rather than
confirming. That is the shape to expect: a scene is cheap enough that the reason to write one is a
sentence in a stylesheet nobody has ever put a number to.

**Mutation-test a new claim before believing the PASS.** Delete the declaration the check exists to
protect, confirm the check fails, put it back. Two of the HUD's three claims were verified this way
(remove `flex: none` from the token count → the count becomes the clipped half; remove `max-height` →
the HUD grows unbounded and stops scrolling), and the third's failure has never been witnessed, which is
recorded in the check's own docstring rather than left to be assumed. A green check that has never been
red is a check that has not been shown to test anything — the same argument as the positive control, one
level down.

### Traps

- **Read the bound port out of Vite's log.** `strictPort: false` means the port requested is not
  necessarily the port served.
- **Keep the page's console and print it on failure.** The first real run of this probe hit a module that
  failed to parse and reported only "the harness page never became ready" — true, useless, and a sitting's
  worth of guessing away from the parse error Chrome had already printed. Enable both `Log.enable` and
  `Runtime.enable`: a parse failure arrives as a Log entry, a throw during evaluation as a Runtime
  exception. A harness that watches a page and discards what the page said is reporting an absence it
  created.
- **No backticks inside a Lit `css` tagged template literal**, including in comments. One backtick in a
  comment in `permission-dialog/styles.js` terminated the literal and made the module a syntax error.
  `node --check` on the file catches it in a second and the browser will not tell you.
- **Wait on a *decoration*, not on a rendered line.** Monaco computes the diff asynchronously and the
  alignment view zones it inserts are part of the content height, so a scene that measures as soon as a
  `.view-line` exists measures a pane that is about to change size.
- **A scene's wait must reject, not resolve false.** A scene that measures something which never appeared
  returns zeros, and zeros read as a finding.
- **Clear `localStorage` at the top of a scene that measures a persisted preference.** The HUD stores its
  collapsed-section set; a previous build in the same page decides what the next one measures, and the
  first symptom is a check that passes or fails depending on the order the scenes ran in.
- **Content box or border box — decide before asserting a declared width.** `.hud` declares `width: 300px`
  and draws a 1px border under the default `box-sizing: content-box`, so `getBoundingClientRect()` says
  302 and the check said "302px against a declared 300px". Assert `clientWidth` against the declaration
  and print the footprint beside it; asserting 300 on the border box asserts a `box-sizing` the component
  never declared.
- **A fixture whose worst row still fits proves nothing about an ellipsis rule.** Two model-id forms fit
  the HUD's 300px with room, so the "the name gives way, not the count" check passed while clipping
  nothing. Split it: *every* row's count is unclipped, *and* at least one row clipped its name. The second
  half is the positive control, and finding the input that trips it (a full Bedrock inference-profile ARN)
  is how you learn where the rule actually bites.
- **Drive a component's own gesture, not its private timers.** The HUD auto-hides after 8s; the way to
  hold it open for a measurement is a real `PointerEvent('pointerenter')`, which is the documented pause.
  A scene that reached into the timer would be measuring a state no user can produce.
- **Set the app-level context a label depends on.** File chips render `toRepoPath(...)`, so a harness that
  never called `setRepoRoot` measured absolute paths — wider than the app ever draws, and a width no user
  will see. Screenshots caught it; the numbers looked fine.
