# Antigravity — Verified Surface

Ground truth for the second engine, read directly from the installed wheel and the shipped binary
rather than from documentation, blog posts or recollection. Verified **2026-08-30**.

| What | Version | Where it was read |
|---|---|---|
| `google-antigravity` | **0.1.15** when this was written; **0.1.16** installed since 2026-09-03 — *Development Status :: 3 - Alpha*, Apache-2.0 | `.venv/lib/python3.14/site-packages/google/antigravity/`, `google_antigravity-0.1.15.dist-info/METADATA` |
| `agy` CLI | **1.1.22** (`agy --version`; released 2026-08-27), 208,429,312 bytes, stripped ELF. **Re-probed at 1.1.25 on 2026-09-03** for its hook surface — see [§ The `agy` hook surface](#the-agy-hook-surface--measured-2026-09-03). **1.1.26 installed since 2026-09-05**, which is what phase 8's live gate, write and isolation runs were driven against; nothing in this file was contradicted by that bump except as noted under the write-diversion amendment below | `~/.local/bin/agy` |
| `claude-agent-sdk` (for comparison) | 0.2.137 | `.venv/lib/python3.14/site-packages/claude_agent_sdk/` |

> **Do not re-derive this by guessing.** The SDK is at 0.1.x and alpha; it will move faster than
> `claude-agent-sdk` did. This file is a snapshot, not a contract. Re-read the installed package when
> implementing, and see [§ The probe](#the-probe) for the half of it that should be machine-checked.

**The trap, recorded because it will catch someone.** `import antigravity` succeeds on any CPython
install — it is the XKCD easter egg in the standard library and it is not evidence of anything. The
package that matters is `google.antigravity`. Check for that name specifically.

---

## There are two Antigravitys and they are not the same program

This is the finding everything else depends on, so it comes first.

**The Python SDK ships and spawns its own Go binary.** `google/antigravity/bin/localharness`,
119,721,512 bytes, bundled inside the wheel. `Agent` starts it as a subprocess and speaks protobuf
over a local WebSocket (`connections/local/local_connection.py:40`, `:303-342`;
`connections/local/local_connection.py:674-723` resolves the binary path). It does **not** drive
`agy`.

**`agy` is a separate product.** `strings ~/.local/bin/agy | grep -c localharness` returns **0**. The
two binaries differ by ~89 MB and share no such symbol. They have separate state directories —
`~/.gemini/antigravity/` is the SDK's `DEFAULT_APP_DATA_DIR`
(`connections/local/local_connection_config.py:35-37`), `~/.gemini/antigravity-cli/` is the CLI's.

**They authenticate differently, and this is a procurement fact before it is an engineering one.**

- The SDK accepts exactly two endpoint types. `GeminiAPIEndpoint.validate_endpoint()` raises unless
  `api_key` or `$GEMINI_API_KEY` is set (`models.py:109-124`); `VertexEndpoint` needs
  `(project, location)` or an Express-mode `api_key` (`models.py:127-167`). `build_models_proto`
  raises `ValueError(f"Unrecognized endpoint type: …")` for anything else
  (`connections/local/local_connection.py:200-201`), and `validate_endpoint()` is called on the
  connect path (`:1241`), so this fails at session start rather than lazily.
- A `grep -rn "oauth" --include="*.py"` over the whole package returns nothing outside tests.
- `agy models` succeeded with no `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT` or
  `GOOGLE_GENAI_USE_VERTEXAI` in the environment, so the CLI is OAuth-authenticated against the
  owner's account.

**The SDK therefore cannot borrow the CLI's login.** Using it requires a separately-provisioned,
separately-billed Gemini API key or Vertex project. See [`decisions.md` AG-2](decisions.md#ag-2) and
[`risks.md` AG-R-8](risks.md#ag-r-8).

### What `agy models` returns

Recorded because it is surprising and because it will be misread if it is discovered later without
context:

```
gemini-3.7-flash-{high,medium,low}    gemini-3.1-pro-{high,low}
gemini-3.6-flash-{high,medium,low}    claude-sonnet-4-6
gemini-3.5-flash-{high,medium,low}    claude-opus-4-6-thinking
                                      gpt-oss-120b-medium
```

`agy` routes to Claude models **through Google's account**. This is not Claude Code — no `claude`
CLI, none of Claude Code's tools, no Anthropic billing — and surfacing it naively in a UI that also
hosts a real Claude Code engine would make "which engine am I talking to" unanswerable.

**The output is two tab-separated columns, and both are load-bearing** — recorded because the model
picker parses it. Re-read at 1.1.25 on 2026-09-04, where the list has grown a `gemini-3.8-flash`
family and is fourteen entries:

```
gemini-3.8-flash-low<TAB>Gemini 3.8 Flash (Low)
claude-sonnet-4-6<TAB>Claude Sonnet 4.6 (Thinking)
```

The first column is the id `--model` takes; the second is a display label. The label was originally
discarded on the belief that the webapp's model list is a list of names — it is a list of
`{value, displayName, …}` objects, and discarding the label came from the same misreading that had
the ids sent as bare strings and rendered as nothing. Both columns are used now. The *"Fetching
available models…"* banner goes to **stderr**, so a parser reading stdout does not have to filter it.

Note also that reasoning effort is baked into the *model name* here, where the SDK models it as a
separate `ThinkingLevel` enum (`models.py:44-63`). The two Antigravity surfaces do not agree with
each other on this.

---

## The `agy` stream-json protocol — measured

Three live turns were run on 2026-08-30 against `gemini-3.7-flash-low` in a throwaway git repo under
`/tmp`, at a total cost of ~42k input tokens. What follows is transcribed from the captures.

The protocol is real and typed. The binary's own changelog calls it *"a strongly-typed NDJSON event
stream … with a stable, closed-vocabulary `step_type` discriminator."* Across every probe the
vocabulary was exactly:

```
events:      init | step_update | result
step_types:  user_input | agent_response | tool
```

**The `init` frame** is the direct analogue of what
[`../plan/sdk-surface.md` § The probe](../plan/sdk-surface.md#the-probe) reads from Claude's
initialize handshake via `diff_server_info`:

```json
{"event":"init","conversation_id":"57f59897-…","init":{
  "model":"gemini-3.7-flash-low",
  "cwd":"/tmp/agy-probe",
  "permission_mode":"request-review",
  "tools":[ 57 names ]}}
```

The 57 tools include `generate_image`, `invoke_subagent`, `call_mcp_tool`, `ask_question`,
`notebook_edit`, `run_command`, `write_to_file`, `multi_replace_file_content`, `list_permissions`,
`ask_permission`, `ask_custom_permission`, and a 20-tool browser-automation suite.

**Bidirectional mode works.** The input frame shape is *not* documented and took four attempts plus a
`strings` dig to find — the error `stream input "user" message is missing the "message" field` is
what gave it away:

```json
{"event":"user","message":{"role":"user","content":"…"}}
```

One turn per line, session held open across lines. `{"event":"user_message",…}` is rejected with
`warning: ignoring unsupported stream input message event "user_message"`, and a frame with no
`event` key fails the run with `stream input message is missing the "event" field`.

**Flag-order quirk, recorded so nobody loses twenty minutes to it:** `--print` takes a value, so
`agy -p --input-format stream-json` silently consumes `--input-format` as the prompt. The `=` form is
required: `--print="" --input-format stream-json`.

### The stream, measured in bidirectional mode (2026-09-03)

The vocabulary above was read from `-p` captures and is **incomplete in three ways** that matter to a
pump. Captured from one bidirectional turn, since that is the mode an adapter uses.

**Every frame is nested under its own event name**, exactly as `init` is:

```json
{"event":"step_update","step_update":{ … }}
{"event":"result","result":{ … }}
```

A parser written against a flat shape reads `None` for every field *without erroring* — the same
failure `diff_agy_init` was corrected for at 1.1.22, on a different frame.

**`step_type` has a fourth member.** The recorded vocabulary is
`user_input | agent_response | tool`; a plain read-a-file turn also produced **`system_message`**. On
a CLI releasing weekly a closed vocabulary that is not actually closed is how a step type arrives as
silence in the chat, so a pump must render an unknown `step_type` rather than drop it.

| `step_type` | Fields beyond `conversation_id`, `step_index`, `state` |
|---|---|
| `user_input` | — |
| `agent_response` | `text_delta`, `duration_seconds`, `usage` |
| `tool` | `tool_name`, `tool_info: {name, parameters}`, `duration_seconds` |
| `system_message` | `duration_seconds` |

`state` is `ACTIVE` or `DONE`.

**`text_delta` is a real delta, and the SDK's equivalent is not.** This is the one that would produce
a plausible, wrong pump. `streamChunk` on the SDK path carries the *whole accumulated block* — checked
in phase 3 and recorded above as "content is cumulative", with the browser replacing by `block_id`.
`agy`'s `text_delta` carries only the new fragment:

```
step_index 5 : "I am searching for `calc.py` to read its contents and summarize what it does.\n"
step_index 9 : "[calc.py](file:///…/calc.py) defines a "
```

A pump that replaced on this would render only the last fragment of every message; one that
accumulated on the SDK's would repeat the prefix of every message. The two transports need opposite
handling, and neither failure raises anything.

**Tool arguments are nested differently from the hook's.** The stream gives
`tool_info.parameters`, the hook gives `toolCall.args` — the same values under two paths, which is
[§ One call, two vocabularies](#one-call-two-vocabularies--measured-in-the-phase-4-live-run-2026-09-03)
appearing a third time. And `tool_info.output` was **absent** on a completed `find_by_name` here,
where the 1.1.22 `-p` correction found it present for `run_command`; so it is per-tool rather than
universal, and a pump must not require it.

**`result`** carries `status`, `response` (the turn's whole prose), `duration_seconds`, `num_turns`
and `usage` — `{input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}`.

### Why `agy` is nonetheless not the engine

> **Superseded 2026-09-03 — read this section as history, not as the current reasoning.** Both
> measurements below were re-checked against `agy` 1.1.25 and **both are obsolete**: each measured a
> channel that is not the lifecycle hook. A `PreToolUse` hook gates a call, carries the replacement
> text before the write, and can amend the arguments. The exclusion this section argued for was
> **reversed the same day** by [AG-14](decisions.md#ag-14), which adds `agy` as a second transport
> beside the SDK engine — and the measurements that replaced these are in
> [§ The `agy` hook surface](#the-agy-hook-surface--measured-2026-09-03). Kept in full because the
> decision stood on it for four days and because how it was superseded is worth reading: neither
> measurement was wrong, both were about the wrong channel.

Two measurements settle it. **Both were re-run against 1.1.22 on 2026-08-30** and one of them came
back materially different from the first pass — see the correction under point 2, which matters
because the original wording was broader than the evidence and would not survive a re-check.

**1. There is no permission channel.** Forcing `run_command` under the default `request-review` mode:

```
step_update | tool | ACTIVE | run_command | {"CommandLine":"echo NEEDS_PERM_12345"}
step_update | tool | DONE   | run_command | {"CommandLine":"echo NEEDS_PERM_12345"}   ← no output, no error
result      | status: "CANCELED"                                                       ← exit code 0
```

stderr, and only stderr, explains it:

```
jetski: no output produced — a tool required the "command" permission that headless mode
cannot prompt for, so it was auto-denied.
```

No request frame, no pause; the denial arrives already decided.

**This got worse between passes, not better.** The first probe saw the step go to `ERROR` carrying a
message. On 1.1.22 it goes to `DONE` with **no `error` key at all**, and the run reports
`status: "CANCELED"` with **exit 0** — the 1.1.16 change *"Fixed print mode … treating benign tool
execution errors and permission denials as fatal run failures with non-zero exit codes."* The
consequence for a host is that **a permission denial is no longer representable in the stream**: it
must be scraped from stderr, or guessed at from `CANCELED` plus a missing `output`. Two format
strings in the binary confirm the behaviour is structural rather than a print-mode accident:

```
"%s required the %s %s that headless mode cannot prompt for, so %s auto-denied.
 Add an allow-rule under permissions.allow in settings.json (e.g. %s)"

"the %s tool(s) required approval that headless mode cannot prompt for, so they were auto-denied.
 Settings allow-rules do not apply; re-run with --dangerously-skip-permissions to auto-approve
 all tools."
```

The three available postures are **auto-deny, static allowlist, or blanket bypass**. AIC⚡DC's
permission dialog — which [`../3-engine/permissions.md`](../3-engine/permissions.md) establishes as
the only ask path, localhost-gated because it authorises arbitrary shell — has nowhere to attach.

**2. Tool *content* is not on the stream. Tool *results* are — this file previously said otherwise
and was wrong.**

The earlier claim was "no result payload, no output, and no file content." The first two thirds of
that are false and the correction is recorded here rather than quietly edited away, because anyone
re-checking the old wording would find it untrue and might reopen a decision that is in fact sound.

`tool_info.output` exists, and has since the release that *"Enriched the structured stream with a
`tool_info` object for each tool call (canonical tool name, parameters, **and output**)."* The docs
agree: *"`tool_info` holds `name`, `parameters`, `output`, and — when the tool fails — an `error`
object."* Measured on 1.1.22, one turn, three tools:

| tool | what `tool_info` actually carried |
|---|---|
| `run_command` | `"output": "MARKER_OUT\r\n"` — **full stdout** |
| `view_file` | `"output": "2 lines, 18 bytes"` — **a summary, not the content** |
| `write_to_file` | **no `output` key**; `parameters` = `{"TargetFile": …}` only |

```json
{"event":"step_update","step_update":{
  "step_index":2,"state":"DONE","step_type":"tool","tool_name":"write_to_file",
  "duration_seconds":0.005473458,
  "tool_info":{"name":"write_to_file",
               "parameters":{"TargetFile":"…/scratch/probe.txt"}}}}
```

**The narrower finding is the one that disqualifies, and it survives intact.** The bytes being
written never appear on the wire — not in `parameters`, not in `output` — and file reads return a
byte-count summary rather than content. You cannot render a diff from this. Command output *would*
have filled a tool-result card; the diff viewer, which is the actual product, still has no data.

Two further corrections from the same re-run:

- **The frame shape is nested, not flat.** Every event is `{"event":"<name>","<name>":{…}}`. The flat
  frames quoted in the first pass were not what 1.1.22 emits — a reader who parsed against them would
  get `None` for every field and no error.
- The `init` frame's `permission_mode` reads `request-review` by default and `always-proceed` under
  `--dangerously-skip-permissions`.

The Python SDK's `Step` model carries `content`, `thinking`, `content_delta`, `thinking_delta`, full
`ToolCall` objects and per-step `usage_metadata` (`types.py:889-935`), and `PostToolCallHook` receives
a `ToolResult` (`hooks/hooks.py:186`). The data exists there. It is not exposed here.

### An unrelated hazard found by accident

The probe asked for `probe.txt` in `/tmp/agy-probe`; the file was written to
`~/.gemini/antigravity-cli/scratch/probe.txt` and the agent reported success with a `file://` link.
The cause is `~/.gemini/antigravity-cli/settings.json`:

```json
{"model":"Gemini 3.7 Flash (Low)",
 "trustedWorkspaces":["…/culvertHouse","…/remuneration.2026",
                      "…/TLSSpeaker.ai","…/Property"]}
```

An untrusted cwd does not error — it **silently diverts writes to a scratch directory** while the
agent believes it succeeded. For a product whose file tree and diff viewer are rooted at the repo,
that presents as "the agent says it edited the file and the diff is empty."
See [`risks.md` AG-R-3](risks.md#ag-r-3).

> **Amended 2026-09-05 — the diversion is real and `trustedWorkspaces` is not a sufficient
> explanation for it.** On 1.1.26, three phase-8 probe runs diverted a newly-created file from
> **inside** `/tmp/temp`, which *is* on the trust list above; git-initialising the workspace changed
> nothing. Every diverted file on record — `probe.txt` here, plus `hello.txt`,
> `test_hello_world.py` and `stranger.txt` — was **created**, whereas a same-day browser run
> *edited an existing* file under the same trusted root and the edit landed at its real path. The
> better-supported reading is that a bare filename handed to `write_to_file` does not resolve against
> the session's cwd. **This has not been isolated with a controlled probe**, so it is a sharper
> hypothesis, not a measurement — which is exactly the standard the rest of this file is held to, and
> the paragraph above did not meet it either.

**Unknown:** whether the Python SDK's `workspaces` field
(`connections/local/local_connection_config.py:147`) is subject to the same trust list. It appears to
be a separate mechanism — `workspace_only` policies are enforced at the platform layer
(`hooks/policy.py:442`, `:541-543`) — but this was not confirmed.

---

## The `agy` hook surface — measured 2026-09-03

§ *Why `agy` is nonetheless not the engine* rules `agy` out on the `--permission-mode` postures and
the `stream-json` output. **Neither is the hook**, and `agy` 1.1.25 has lifecycle hooks. This section
is what a direct probe found, kept whole because [AG-2](decisions.md#ag-2)'s amendment turns on it and
because most of it stays true whatever is decided.

Probed by installing a `PreToolUse` hook, capturing its stdin verbatim, and running headless turns
against a scratch workspace. All artefacts removed afterwards.

### Headless spends the paid subscription

`agy -p` authenticates from the OS keyring (`zalando/go-keyring`, service `antigravity`) and reaches
`daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent` — the same OAuth identity and the
same Google AI Pro quota as the interactive TUI. `--conversation <id>` resumes a conversation created
interactively, loading its existing turns.

This is the whole reason the question was reopened: the SDK path cannot reach that backend at any
price (AG-2 § *a backend split*), and the Gemini API key it must use instead refuses at 20 requests
per model per day.

### The `PreToolUse` payload carries the write

Captured stdin, unedited but for indentation:

```json
{
  "conversationId": "...", "modelName": "gemini-3.8-flash-low", "stepIdx": 4,
  "artifactDirectoryPath": "/home/.../brain/<id>",
  "transcriptPath": "/home/.../brain/<id>/.system_generated/logs/transcript_full.jsonl",
  "workspacePaths": [],
  "toolCall": {
    "name": "replace_file_content",
    "args": {
      "TargetFile": "/home/.../target.txt",
      "TargetContent": "FOOBAR_123",
      "ReplacementContent": "FOOBAR_456",
      "StartLine": "1", "EndLine": "1",
      "AllowMultiple": false, "Description": "...", "Instruction": "...",
      "toolAction": "Editing file", "toolSummary": "..."
    }
  }
}
```

`write_to_file` carries `CodeContent`. **These are the SDK's own argument names** — the ones
`permissions.ARG_ALIASES` already maps — so `normalise_args`, `build_diff_payload` and
`denormalise_args` would work on this payload essentially unchanged. Two products, one tool-argument
vocabulary, across two transports that share nothing else.

Other tools captured in the same run: `find_by_name` (`Pattern`, `SearchDirectory`), `view_file`
(`AbsolutePath`), `run_command` (`CommandLine`, `Cwd`, `WaitMsBeforeAsync`). Note `find_by_name` —
`agy` and the SDK do **not** agree on tool *names*, only on argument names.

### The response contract

`allow` / `deny` / `ask` / `force_ask`, plus `reason` (*"shown to the user/agent"*) and `overwrite`
— a shallow top-level merge into the tool call's arguments, where *"the modified tool call is what
actually executes and is recorded"*. `overwrite` is `modified_args` under another name, so the amend
path [AG-5](decisions.md#ag-5) chose the raw SDK hook to preserve exists here too.

`ask` is useless headlessly — it auto-denies, logged as `Print mode: soft-denying tool confirmation`
— but an adapter would never return it. The hook blocks, asks AIC⚡DC's own dialog, and returns the
human's answer.

### The response contract fails closed, except in one place

Measured across five failure modes on live edit turns. Only the last allows the tool:

| Hook behaviour | Tool | `agy`'s own words |
|---|---|---|
| exceeds `timeout` | **blocked** | killed at the deadline → non-zero exit |
| exits non-zero | **blocked** | `command failed: exit status 1` |
| prints malformed JSON | **blocked** | `failed to unmarshal result from hook … syntax error` |
| command missing | **blocked** | `exit status 127` |
| **exit 0, empty stdout** | **allowed** | parsed as `{}`; an empty decision defaults to allow |

`timeout` is passed straight to `context.WithTimeout` with **no ceiling** — verified at `86400`, where
a 40-second hook ran to completion and its `deny` was honoured. What *does* bound a dialog is
`--print-timeout`, which defaults to **5m** and caps the whole headless turn.

**This was initially measured backwards, and the way it went wrong is the finding.** A first probe
used `"matcher": "replace_file_content"`, saw a timed-out hook and a modified file, and concluded the
gate failed open. Re-run with `"matcher": "*"` the same timeout blocked the write. A blocked tool is
an error the model can see, and it reaches for a different tool — so anything outside the matcher is
ungated. [AG-R-12](risks.md#ag-r-12--an-agy-hook-gate-is-only-as-wide-as-its-matcher) carries it;
the seam is every tool, never a list.

### Bidirectional mode, and the isolation key — measured 2026-09-03

The two questions [AG-14](decisions.md#ag-14) turned on, both settled by one probe.

**Hooks fire in bidirectional `stream-json` mode, not only under `-p`.** One turn driven as

```
agy --print="" --input-format stream-json --output-format stream-json --dangerously-skip-permissions
{"event":"user","message":{"role":"user","content":"…"}}
```

produced **four** `PreToolUse` payloads — `run_command` ×2, `view_file`, `replace_file_content`. That
`run_command` is gated in this mode matters as much as the edit: it is AG-R-11's route-around tool.

**`conversationId` is a sound isolation key.** The hook payload's `conversationId` was *exactly* the
`init` frame's `conversation_id`, and `init` is the stream's first event, so a host learns the id it
owns before any tool call can arrive:

```
stream init conversation_id : cd4edb7f-6de3-468f-9815-e76b310a920a
hook  conversationId        : cd4edb7f-6de3-468f-9815-e76b310a920a   → match
hook  workspacePaths        : []                                     → unusable
```

This is what makes a **global** hook shippable. It will see the user's own unrelated `agy` sessions —
workspace-local `hooks.json` does not load headlessly — and it can allow-and-return immediately for
any conversation the host does not own. `workspacePaths` cannot do that job: it is empty in every
payload captured here, in both `-p` and bidirectional modes.

### The tool *names* differ, and only the tool names — measured 2026-09-03

The trap for anyone reusing `permissions.py` here. `agy` and the SDK agree on **argument** names and
disagree on **tool** names:

| Job | SDK (`BuiltinTools`) | `agy` |
|---|---|---|
| edit a file | `edit_file` | `replace_file_content` |
| create a file | `create_file` | `write_to_file` |
| find files | `find_file` | `find_by_name` |
| list a directory | `list_directory` | `list_dir` |
| read a file | `view_file` | `view_file` |
| run a command | `run_command` | `run_command` |

So `TOOL_CLASSES`, `MUTATING_TOOLS` and `ALWAYS_ASK` — all keyed on SDK names — match `agy` on two
entries and miss the rest. The failure is quiet rather than loud, and in the safe direction: an
unrecognised name classifies as `exec` and is gated. But `replace_file_content` would not be in
`ALWAYS_ASK`, the dialog would call a file edit a command, and `_diff_tool_for` would not recognise it,
so **no diff would render** — the gate holding while the product's central feature silently degrades.

A per-transport name map is therefore a requirement of phase 8, not a refinement. The argument names
needing no such map is the genuine convenience; the tool names are the thing that looks like it
transfers and does not.

### Two limits that remain

- **Discovery.** Hooks load from `~/.gemini/config/hooks.json`. In 1.1.25 a workspace-local
  `<workspace>/.agents/hooks.json` was **not** loaded in headless mode —
  `hooks_manager.go:53] loaded 0 named hooks from 0 hooks.json file(s)` — including with the exact
  workspace path in `trustedWorkspaces`. The embedded changelog's fix for this is wired to the TUI's
  workspace-change event, not the headless bootstrap. The global file *"fires unconditionally"*, so a
  gate installed there intercepts the user's own unrelated `agy` sessions and must pass them through.
  **The natural isolation key does not work:** `workspacePaths` was **empty** in every payload
  captured here. `conversationId` is the sound one — an adapter knows the conversation it started —
  and it is unverified.
- **Concurrency.** An `flock` on `presence/<id>.lock` serialises turns; an interactive session and a
  headless turn on the same conversation conflict. An adapter must own its conversation, which is no
  loss — driving the user's open TUI session was never necessary.

### Defence in depth is available

`permissions.allow` entries — `read_file(<path>)`, `write_file(<path>)`, `file(<glob>)`,
`command(<prefix>)` — stop the headless layer soft-denying without `--dangerously-skip-permissions`,
and a hook `deny` still overrides a settings `allow`: *"tool call denied by pre-tool hook"*, file
untouched. So the hook can be an additional veto rather than the only gate.

### `transcript_full.jsonl` is a phase-5 asset regardless

Every conversation writes `.system_generated/logs/transcript.jsonl` and
`transcript_full.jsonl`, in headless runs as well as interactive ones. The first truncates oversized
fields and names them in `truncated_fields`; **the second never truncates**. Records are typed
(`USER_INPUT`, `PLANNER_RESPONSE`, `CODE_ACTION`, `GENERIC`, `SEARCH_WEB`, `CHECKPOINT`), and a
completed edit carries a real unified diff:

```
[diff_block_start]
@@ -1,2 +1,2 @@
-FOOBAR_123
+FOOBAR_456
[diff_block_end]
```

That is *after the fact*, so it cannot serve the permission dialog — but it is exactly what the
history browser and the repo-local mirror need, and those are two of the surfaces `capabilities.py`
currently marks `unbuilt` for this engine.

### Not an entry point: `--remote-control`

The binary carries a `--remote-control` flag and a `remotecontrol` package, and a live session logs
`[RemoteControl] CLI launched without --remote-control, staying disconnected`. It is **not** a local
IPC: the symbols are WebRTC — `PeerSession`, ICE candidates, SCTP framing, `pendingPin`,
`GetRemoteControlInfoRequest` — i.e. a brokered peer channel for Google's own remote UI, with pin
pairing. A running `agy` listens only on two ephemeral localhost ports for its internal language
server, which answer `400`/`404` to anything else. **There is no way to attach to an already-running
session**, and that is architectural rather than a missing flag.

---

## The Python SDK surface

### Shape

`Agent` is an async context manager over a `Conversation` over a `Connection`
(`agent.py:34-218`). `AgentConfig` is a pydantic model with a `create_strategy` factory
(`connections/connection.py:41-233`); `LocalAgentConfig` is the concrete one that runs
`localharness`.

| Layer | Class | File |
|---|---|---|
| 1 — convenience | `Agent`, `Agent.chat()` | `agent.py` |
| 2 — session | `Conversation` — history, usage, cancel, resume | `conversation/conversation.py` |
| 3 — transport | `Connection` / `ConnectionStrategy` | `connections/connection.py` |

### What AIC⚡DC needs, and whether it is there

Verified line by line. This is the table that says the engine is viable.

| AIC⚡DC needs | Antigravity has | Verified at |
|---|---|---|
| Stream text / thinking / tool calls | `receive_chunks()` → `Thought` \| `Text` \| `ToolCall`; `receive_steps()` → `Step` | `conversation/conversation.py:122-177` |
| Cancel mid-turn | `conversation.cancel()` → `halt_request` | `conversation/conversation.py:334-336`; `local_connection.py:549` |
| Resume a session | `conversation_id` + `SessionContinuationMode.RESUME` + `save_dir`; history restored at handshake | `connection.py:65-68`, `:109-119`; `local_connection.py:356-358` |
| Permission gate | `policy.ask_user(tool, handler=…)` — async, receives the full `ToolCall` | `hooks/policy.py:296-358`, `:568-583` |
| Post-write re-index hook | `PostToolCallHook` | `hooks/hooks.py:186` |
| Compaction notice | `StepType.COMPACTION`, `compaction_indices`, `OnCompactionHook` | `types.py:777`; `conversation.py:235-243`; `hooks/hooks.py:228` |
| Subagent tabs | `SubagentConfig`; per-trajectory usage; `trajectory_id` / `parent_trajectory_id` / `depth` on every `Step` | `connection.py:71`; `conversation.py:300-308`; `types.py:889-935` |
| Serve the indexes to the agent | `tools: list[Callable]` — plain in-process Python callables | `connection.py:57`; `local_connection.py:206-271` |
| Budget cap | `BudgetConfig` + `StopReason.MAX_*_EXCEEDED` | `types.py:829-887` |
| Why the turn ended | `_last_turn_stop_reason` (private) today; `StopArgs.stop_reason` + `.error_message` from 0.1.16 — see § *The first drift it caught* | `conversation.py:326-328`; `hooks/hooks.py:239`, `types.py:1100-1119` |
| Per-turn usage | `Conversation.last_turn_usage` — a difference against turn-start | `conversation.py:310-318` |
| Image generation | `BuiltinTools.GENERATE_IMAGE`; image model wired by default | `types.py:308`; `local_connection_config.py:303-317` |

The tools row is a pleasant surprise worth calling out: Antigravity takes **plain Python callables**,
so the symbol-index bridge is *simpler* here than under Claude Code — no MCP server at all. Its MCP
support is stdio and streamable-HTTP only (`types.py:595`, `:613`, `:636`), so AIC⚡DC's in-process
`create_sdk_mcp_server` bridge (`src/aic_dc/claude_code/mcp_server.py:607-609`) would not port. It
does not need to. See [`decisions.md` AG-4](decisions.md#ag-4).

### `BuiltinTools` — the whole set

`types.py:278-330`. Thirteen values, with `read_only()` and `nondestructive()` classmethods that make
a default posture cheap to express:

```
list_directory  search_directory  find_file      view_file        create_file
edit_file       run_command       ask_question   start_subagent   generate_image
search_web      read_url_content  finish
```

`read_only()` = `{list_directory, search_directory, find_file, view_file, read_url_content, finish}`.

The default `AgentConfig.capabilities` is `read_only()` (`connection.py:52-56`), but
`BaseLocalAgentConfig` overrides that to all-tools-with-`confirm_run_command`
(`local_connection_config.py:134-139`). `Agent.__aenter__` **refuses to start** if write tools or MCP
servers are enabled with no policy and no decide-hook (`agent.py:93-103`) — a good failure, and one
that means the permission wiring cannot be forgotten.

### `CapabilitiesConfig`

`types.py:384-450`:

```
enable_subagents: bool = True          agent_behavior: AUTONOMOUS | INTERACTIVE
enabled_tools / disabled_tools         compaction_threshold: int | None
finish_tool_schema_json                max_subagent_depth / allowed_subagents
run_command_config: {enable_daemons, timeout_seconds}
```

`AgentBehavior.INTERACTIVE` is the one to set: it *"enables features like slash commands and planning
mode"* (`types.py:155-168`). `AUTONOMOUS` is the default and is the wrong posture for a UI with a
human in it.

### Permission fidelity — the gap, and the route around it

`AskUserHandler = Callable[[types.ToolCall], bool | Awaitable[bool]]` (`hooks/policy.py:92-94`). It
returns a **bool**.

Claude's `PermissionResultAllow` carries `updated_input` and `updated_permissions`
(`claude_agent_sdk/types.py:238-255`). So:

| Capability | Claude | Antigravity `policy.ask_user` | Antigravity raw hook |
|---|---|---|---|
| approve / deny | ✅ | ✅ | ✅ |
| deny with a reason the model reads | ✅ | canned string only | ✅ `HookResult.message` |
| **amend the tool input before running** | ✅ `updated_input` | ❌ | ✅ `HookResult.modified_args` |
| **persist a rule ("always allow")** | ✅ `updated_permissions` | ❌ | ❌ |

`HookResult` is `{allow, message, modified_args}` (`types.py:943-957`). So dropping below `policy` to
a raw `PreToolCallDecideHook` (`hooks/hooks.py:172`) recovers everything except rule persistence,
which AIC⚡DC would have to own itself. See [`decisions.md` AG-5](decisions.md#ag-5).

### The permission gate — measured, and it passes

This was the load-bearing unknown and the phase-2 go/no-go. It is now settled by measurement, not
inference. `scripts/probe_edit_args.py`, run 2026-08-30 against `gemini-3.6-flash` on a
free-tier key, seeded a file and asked for an edit while a `PreToolCallDecideHook` logged the
`ToolCall` and denied it.

**`ToolCall.args` carries the proposed content, and more of it than the dialog needs.**

`edit_file` hands over a complete diff hunk — old text, new text, and the line range — so the dialog
does not even have to read the file from disk to render one:

```json
{"TargetFile":         "/tmp/ag-probe-2vafph2n/target.py",
 "TargetContent":      "def add(a, b):\n    return a + b",
 "ReplacementContent": "def add(a, b):\n    return a + b + 1",
 "StartLine": 5, "EndLine": 7,
 "Instruction": "Change return a + b to return a + b + 1 in the add function",
 "AllowMultiple": false}
```

`create_file` hands over the whole new file:

```json
{"TargetFile":  "/tmp/ag-probe-2vafph2n/target.py",
 "CodeContent": "def greet(name):\n    return \"Hello, \" + name\n\n\ndef add(a, b):\n    return a + b + 1\n",
 "Overwrite":   true,
 "Description": "Update target.py to return a + b + 1 in add function"}
```

Both populate `ToolCall.canonical_path`. This is strictly more than `agy` puts on its wire, and it is
the fact that makes the second engine viable as master for writes.

**The gate holds.** `HookResult(allow=False)` left the seeded file byte-identical on disk, and the
denial reached the model as `"denied by pre-tool hook: <message>"` — a reason it reads and adapts to,
which is what [AG-5](decisions.md#ag-5) chose the raw hook over `policy.ask_user` to get.

**And it is not sufficient on its own — see [`risks.md` AG-R-11](risks.md#ag-r-11).** When the probe
denied `edit_file` and `create_file`, the agent went for the shell instead, twice, with the same
intent: `sed -i 's/return a + b/return a + b + 1/'` on the first run and an inline
`python3 -c "…content.replace(…)…"` on the second. Gating the file tools is not a containment
boundary. `run_command` has to be gated on the same seam, and only once it was did the file survive.

### The step stream — read in phase 3, and it is not shaped like Claude's

Building the pump turned up three things that reading the type stubs did not, all of them read
first-hand off `localharness_pb2.StepUpdate` and pinned by tests in `tests/test_antigravity_steps.py`.

**1. A builtin tool's arguments and its result are the same sub-message.** This is the one that
changes code. Claude sends a `tool_use` block and later a separate `tool_result` block carrying its
own id. Antigravity sends the *same* typed sub-message twice — once at `StepStatus.ACTIVE` with the
input fields populated, and again at `DONE` with the output fields filled in beside them — and
`LocalConnectionStep.from_dict` copies the whole thing into `ToolCall.args` both times
(`connections/local/event_processor.py:250-308`).

| Tool | Input fields | Output fields, on the `DONE` frame |
|---|---|---|
| `run_command` | `command_line`, `working_dir` | `exit_code`, `combined_output` |
| `list_directory` | `directory_path` | `results` |
| `find_file` | `directory_path`, `query` | `output` |
| `search_directory` | `directory_path`, `query` | `num_results` |
| `read_url_content` | `url` | `title`, `summary`, `content_path` |
| `search_web` | `query`, `domain` | `summary` |
| `generate_image` | `prompt`, `image_name`, `aspect_ratio` | `image_paths`, `output_path` |
| `view_file` | `file_path`, `start_line`, `end_line` | `content_offset` |
| `create_file` | `file_path`, `contents` | — |
| `edit_file` | `file_path`, `diff_block` | — |

A pump that forwarded `args` as the tool input renders a card whose "input" grows a command's entire
stdout when it completes, and emits no result at all. `steps.TOOL_RESULT_FIELDS` is the split, and
`test_no_tool_sub_message_has_an_unclassified_field` fails on a release that adds a field to either
side rather than letting it leak onto a card.

**2. `view_file` does not carry the file, and the step stream is not the hook.** Its sub-message is
`{file_path, start_line, end_line, content_offset}` — no content, which is the same gap that
disqualified `agy` (AG-2), surviving into the SDK's own stream. It does **not** re-open that
decision, and the reason is a distinction nothing before phase 3 had noticed: **the two paths have
different shapes for the same call.**

| | Step stream | Permission hook |
|---|---|---|
| Source | `StepUpdate.edit_file`, a typed proto sub-message | `PreToolArgs.arguments_json`, free-form JSON from Go |
| `edit_file` carries | `file_path`, `diff_block` | `TargetFile`, `TargetContent`, `ReplacementContent`, `StartLine`, `EndLine`, `Instruction` |
| Spelling | snake_case | CamelCase |

So the diff the dialog renders comes from the hook, which phase 2 measured carrying old text, new
text and a line range. Phase 4 must read it from there; nothing should be built as though the stream
could serve it.

**3. `BuiltinTools.nondestructive()` is not a write boundary.** It returns everything except
`run_command` — it classifies `create_file`, `edit_file` and `generate_image` as nondestructive.
That is defensible for *"will this hurt the machine"* and exactly backwards for *"will this change
the working tree"*, which is the question the permission dialog exists to ask. An adapter that
adopted it as its write seam would enable the two tools AG-5 was written for.
`options.MUTATING_TOOLS` is therefore ours — `create_file`, `edit_file`, `run_command`,
`generate_image`, `start_subagent` — and `test_the_sdk_calls_our_write_tools_nondestructive` pins the
disagreement so a release that fixes the SDK's classifier is a red test rather than a silent
ungating. `read_only()` *is* adopted, because the asymmetry is the safe one: a new read-only tool
should arrive with an SDK bump, and a new write tool must not.

`start_subagent` is in that set for a reason worth stating: a subagent inherits the tool set, so a
gate that stopped at the top-level trajectory is bypassed by asking a child to do the write. It is
the same shape of hole as AG-R-11's `run_command`, one level down.

#### One call, two vocabularies — measured in the phase-4 live run (2026-09-03)

**The permission hook and the step stream do not name a call's arguments the same way**, and the
difference is not a naming style — it is two independent spellings of the same values, with different
path formats. Read off one live turn's raw frames:

| Call | Hook `preToolArgs.argumentsJson` | Step stream |
|---|---|---|
| `find_file` | `Pattern`, `SearchDirectory` | `findFile.query`, `findFile.directoryPath` |
| `view_file` | `AbsolutePath` | `viewFile.filePath`, `startLine`, `endLine` |
| `edit_file` | `TargetFile`, `TargetContent`, `ReplacementContent`, `Instruction`, `StartLine`, `EndLine` | `editFile.filePath`, `editFile.diffBlock[].lines[].action` |

The hook sends **bare paths**; the step stream sends **`file://` URIs**. So a module that learns a
path key from one side and meets the other gets either a miss or a URI where it wanted a path, and
neither announces itself.

This is what `permissions.ARG_ALIASES` exists to absorb, and in the phase-4 run it had absorbed only
half: the mutating entries matched (which is why the dialog's diff rendered), and `view_file` was
aliased against `TargetFile` — a name the hook does not send — while `find_file` had no entry at all.
The dialog showed `PATH (none named)` above an input block containing the path.

**The probe cannot cover this.** § *The probe* states that reflection sees shape, and an argument name
inside a JSON string is not shape — the same blind spot that let `agy`'s contentless frames and
`policy.ask_user`'s bare bool through. Argument names are settled by reading a live frame, and this
table is that reading. Anything relying on it is relying on a measurement, not a contract.

### Usage and cost

`UsageMetadata` (`types.py:700-771`) is tokens only:

```
prompt_token_count   cached_content_token_count   candidates_token_count
thoughts_token_count total_token_count            service_tier
```

with `__add__` and `__sub__` defined, which is what makes `last_turn_usage` a clean difference.

**There is no USD figure anywhere in the SDK, and none on `agy`'s wire either.** Claude's
`ResultMessage.total_cost_usd` is what feeds `CostLedger` and the turn footer
(`src/aic_dc/claude_code/cost.py`). `BudgetConfig` caps calls and tokens, never dollars.
See [`decisions.md` AG-6](decisions.md#ag-6).

**There is no context-window read-back.** `compaction_threshold` is a number you *set*
(`types.py:436`), not a window you can query. Claude's `get_context_usage` — a pass-through of what
`/context` prints, and the thing three webapp readers share via `webapp/src/context-usage.js` — has
no counterpart. The Context tab has nothing to draw for this engine.

**Measured floor, from the `agy` probe:** 13,873 input tokens to answer *"reply with exactly the word:
ok"*. A large fixed system prompt rides on every turn, which makes `cached_content_token_count` the
field worth surfacing.

---

## What does not translate

### AIC⚡DC features with no Antigravity counterpart

| Feature | Today | Status |
|---|---|---|
| Account rate-limit windows | `account_usage.py` — Anthropic `GET /api/oauth/usage` | **None.** Anthropic-specific REST endpoint. Hide per-engine. |
| USD cost — turn footer, session cost, `max_budget_usd` | `cost.py` from `total_cost_usd` | **None.** Tokens only. |
| Live context-window usage / compaction thresholds | `get_context_usage`, Context tab | **None.** `compaction_threshold` is write-only. |
| Slash-command palette | `list_commands`, `SLASH_ROUTES` (`service.py:97-130`) | **Near-total loss.** `BuiltinSlashCommandName` has exactly one member: `PLAN` (`types.py:1455-1463`). |
| "Always allow" / persisted rules | `updated_permissions` | **None.** AIC⚡DC would own persistence. |
| Amend input before approving | `updated_input` | **Recoverable** via `HookResult.modified_args`, not via `policy.ask_user`. |
| In-process MCP bridge | `create_sdk_mcp_server` | **Does not port**, and is **obsolete** — pass callables instead. |
| Repo-local verbatim session mirror | `session_store.py` over the SDK's `SessionStore` protocol | **No protocol counterpart.** Antigravity owns an opaque `save_dir`. Rebuild as a step observer. |
| Transcript history rendering | `history.py` over the CLI's transcript shape | **Needs a full sibling.** `Step` is flat with `trajectory_id`/`depth`, not nested content blocks. |
| `RateLimitEvent`, `ConversationResetMessage`, `Task*Message` | `messages.py` dispatch | Claude-specific. Nearest equivalents: `StopReason.QUOTA_EXHAUSTED`, per-trajectory steps. |

### Antigravity capabilities with no home in the current UI

| Capability | Where | Note |
|---|---|---|
| **Image generation** | `types.py:308`; `local_connection_config.py:303-317` | The headline ask. On by default; writes to an `output_path` that `local_connection_config.py:63-65` already normalises. Lands partly in the existing file tree, but there is no "generated image" surface. |
| **`ask_question` / structured interaction** | `types.py:285`, `:1010`; `hooks/hooks.py:216` | Agent-initiated multiple-choice with option IDs and multi-select. **No UI at all.** The permission dialog is the nearest thing and is a different shape. |
| `response_schema` / structured output | `connection.py:69`; `Step.structured_output` | AIC⚡DC declined the Claude equivalent as "for programmatic callers". |
| Audio and video input | `types.py:1443-1452`, `from_file()` | AIC⚡DC handles images only. |
| Daemon commands | `RunCommandConfig.enable_daemons` (`types.py:170-186`) | "Start a dev server without blocking the turn." No counterpart concept. |
| `triggers` | `connection.py:60`; `triggers/` | Out-of-band async messages into a live conversation. No AIC⚡DC concept. |
| Multi-model routing in one session | `models: list[ModelTarget]` — text and image simultaneously | `get_model`/`set_model` assume one model per session. |

---

## The probe

`../plan/sdk-surface.md` § *The probe* argues that a hand-written inventory drifts silently and that
the fix is to diff the *installed* package against this repo's own syntax trees, bucketing every name
as `handled` / `declined` / `pending` and failing the gate only on a name in **none** of the three.
That argument applies here with more force, not less: this SDK is at **0.1.16 and alpha**, where
`claude-agent-sdk` was at 0.2.137 and stable.

`src/aic_dc/antigravity/surface.py` should therefore be built in phase 1, alongside the consultant
and before any engine work — see [`decisions.md` AG-8](decisions.md#ag-8). The reflection targets
differ from the Claude probe's because the SDK's shape differs:

| Section | Read from |
|---|---|
| config fields | `pydantic` model fields on `LocalAgentConfig` / `AgentConfig` — **not** `dataclasses.fields` |
| builtin tools | `types.BuiltinTools` enum members |
| hook classes | subclasses of `InspectHook` / `DecideHook` / `TransformHook` in `hooks/hooks.py` |
| step types | `types.StepType`, `StepSource`, `StepTarget`, `StepStatus`, `StopReason` |
| policy builders | public callables in `hooks/policy.py` |
| capabilities | `types.CapabilitiesConfig` fields |

And the CLI half, which static reflection structurally cannot reach: **`agy`'s `init` frame is a
free, machine-readable capability inventory** — model, cwd, permission mode, and the full tool list.
One `agy models` call and one no-op `-p` turn produce it. That is the `diff_server_info` analogue,
and it is worth wiring even though `agy` is not the engine.

### The first drift it caught — `StopHook`, 0.1.16 (2026-09-03)

The gate earned itself on the first bump after it was written. `google-antigravity` 0.1.16 adds
**`StopHook`** (`hooks/hooks.py:239`), a `TransformHook[StopArgs, StopHookResult]` invoked when the root
trajectory reaches fully idle, and `tests/test_antigravity_surface.py` went red with that name in the
failure message. Nothing else in the release moved: no config field, no builtin tool, no enum member, no
stale entry. That is the mechanism working exactly as § *The probe* argues it should — a name arriving as
a red test rather than as a capability nobody notices for months.

**It is deferred, and on its observability half rather than its control one.** The two halves pull in
opposite directions and only one is worth having:

| Half | What it offers | Verdict |
|---|---|---|
| `StopArgs.stop_reason`, `.error_message` | A **public, typed** read of why the turn ended, plus a fatal-error string with no step-stream equivalent. | The reason to want it. |
| `StopDecision.CONTINUE` | Blocks termination and injects `reason` as a system prompt, resuming the agent loop. | Not a host's call to make silently. Resuming a turn the user did not ask to resume is the agent deciding it is not finished. |

The first half matters more than it looks. `session.stop_reason_of` currently reaches for
`_last_turn_stop_reason` on the conversation or its `_connection` — a private attribute whose
public-looking sibling **does not exist**, and every turn reported a blank reason until that was found on
2026-09-02 (see [`delivery.md` § *Phase 3*](delivery.md#phase-3--the-live-run-and-the-three-bugs-it-found-2026-09-02)).
`StopHook` is the documented route to the same value.

It waits anyway, because **the `StopReason` rows in `STEP_MEMBERS` are pending for a reason that applies
here first**: the pump forwards a stop reason verbatim onto `streamComplete` and nothing renders the
difference between a budget cap and an ordinary stop. Hardening the *source* of a value that has no
*sink* is the wrong order, and it would also mean registering a transform hook on the turn-termination
path of a daily-releasing alpha SDK to improve a read that currently works. Both move together when
something renders a stop reason.

**Two things the probe will not be able to do**, stated here because a green gate invites the wrong
inference. It reads *shape*, never semantics — every correction in this file that mattered
(`agy` carrying no tool results; `ask_user` returning a bare bool) was a type-satisfied,
behaviour-wrong case that no reflection would have caught. And nothing runs it on a schedule, so a
`pip install --upgrade` with no commits after it leaves a window where the report is stale and does
not say so.

---

## Verified, inferred, unknown

Stated explicitly, because a stated unknown is more useful than a confident guess.

**Verified by reading source or by measurement:** every file:line citation above; the credential
wall; `agy`'s flag surface, model list, NDJSON schema in both directions, and its disqualifying
omission of file content; the absence of USD cost, context-window read-back and OAuth in the SDK; the
`trustedWorkspaces` diversion; `Agent.__aenter__`'s refusal to start without a policy; **that a
`PreToolCallDecideHook` receives full edit content and that `allow=False` blocks the write**; **that
denying only the file tools is routed around via `run_command`**; **that `agy` and the SDK address
different backends**; **that `localharness` resolves ADC itself**; **that a bare `LocalAgentConfig()`
is not "auth-less"**; **that `Consultant.second_opinion` completes end-to-end on a free-tier key**.

**Inferred:** that `localharness` and `agy` are separate programs — from the zero-match symbol probe
and the size difference, which is strong but not proof.

**Corrected on re-measurement (2026-08-30, `agy` 1.1.22):** `agy`'s stream *does* carry tool results
in `tool_info.output`; the earlier "no result payload, no output" was wrong, and only the missing
file content is disqualifying. The frame shape is nested, not flat. A headless permission denial is
no longer visible in the stream at all — `DONE`, no `error`, `CANCELED`, exit 0.

**Closed in phase 1: whether `localharness` can be pointed at OAuth credentials by a path not
exposed through the Python SDK.** Phase 0 flagged this as worth one look before buying a key, because
if it were possible AG-R-8 would change shape entirely. It is not, and the answer is sharper than a
bare no. The harness's own wire protocol offers exactly four auth shapes
(`proto/localharness_pb2.ModelConfig`): `GeminiAPIEndpoint{base_url, http_headers, api_key, options}`,
`VertexEndpoint{base_url, http_headers, project, location, options, api_key}`,
`GemmaEndpoint{base_url}`, and `CustomEndpoint{backend_type, config_json}`. **There is no OAuth field
anywhere**, and `agy`'s token lives in `~/.gemini/antigravity-cli/` in a format nothing in the SDK
reads.

But `base_url` and `http_headers` are on both real endpoints, and `validate_endpoint()` returns early
when `base_url` is set — *"External API, validation is done by the external API"* (`models.py:115-118`).
So on that path **no `GEMINI_API_KEY` is required at all**, and the harness can be pointed at any
proxy carrying any credential in a header. AG-R-8 therefore stands, but its shape is now precise: the
wall is that AIC⚡DC has no supported way to *mint or refresh* an Antigravity OAuth token, not that
there is no way to present one. A user who already has a token-bearing proxy has a route this
directory does not otherwise offer.

**Sharpened 2026-08-31 — the wall is a backend boundary, and there is nothing behind it to present a
token *to*.** The paragraph above is right that no OAuth field exists; what it does not say is that
even a minted token would have no service to call. `agy` requests the scope
`https://www.googleapis.com/auth/aicode` and calls **`cloudcode-pa.googleapis.com`** — the Code
Assist backend, and the surface authorised to spend a consumer AI Pro/Ultra subscription's coding
quota. Of the four wire endpoints, none addresses it: the Python SDK constructs `GeminiAPIEndpoint`
and `VertexEndpoint` only (`models.py` defines no other `ModelEndpoint` subclass), `GemmaEndpoint`
takes a bare `base_url`, and `CustomEndpoint{backend_type, config_json}` is unreachable from Python
while the bundled Go client rejects unknown backends outright (`Unsupported backend: %v`). So the
subscription is not merely unreadable — it is on the far side of a service boundary the SDK cannot
cross. See [AG-2](decisions.md#ag-2).

**Closed 2026-08-31: `localharness` resolves Application Default Credentials itself.** AG-11 flagged
this as unverified and worth a probe before phase 3. `LocalAgentConfig(vertex=True, project=…,
location=…)` with a nonexistent project clears Python validation, spawns the binary, and fails from
Go with `failed to configure default GCP credentials for Vertex AI: failed to find default
credentials` — that is `FindDefaultCredentials`, and `golang/oauth2/google`,
`application_default_credentials.json` and `GOOGLE_APPLICATION_CREDENTIALS` are all in the binary's
strings. Vertex standard mode is therefore a real OAuth path, and the only one on which no secret
passes through AIC⚡DC's Python at all. It authenticates identity and not entitlement: the bill lands
on the named project regardless of which account performed the login.

**Measured while closing the above: a bare `LocalAgentConfig()` is not "auth-less".** It builds a
`GeminiAPIEndpoint`, whose validator raises `AntigravityValidationError: A Gemini API key is
required` from `local_connection.py:1241` — *before* the 119 MB binary is spawned, and regardless of
any `agy` login on the machine. Recorded because the opposite is a plausible and widely repeated
reading of "the SDK uses your local credentials", and it is falsifiable in under a minute.

**Correction to phase 0's reading of the policy default.** `LocalAgentConfig` does not default
`policies` to empty. It defaults to `policy.confirm_run_command()` — deny `run_command`, **approve
everything else** — so a config that simply omits `policies` is running blanket approval for every
other tool. This is recorded here rather than only in the code because it inverts the natural reading
of `Agent.__aenter__`'s safety check: that check fires on *write tools with no policy*, and the
default guarantees there is always a policy. See [AG-5](decisions.md#ag-5).

**Could not determine:** whether `agy` has any supported programmatic contract or is best-effort —
the docs make **no stability or compatibility commitment** for `stream-json`, on a release cadence of
roughly one per day; the stability of `Step` across 0.1.x; whether the SDK's `workspaces` is
subject to the CLI's `trustedWorkspaces` list; **whether a host driving `agy` as a subprocess breaches
Antigravity's terms** — the clause *"Using third party software, tools, or services to access the
Service … is a breach of this Agreement"* is broad, and asking `agy` itself produced an explicit
"I don't know". Moot under [AG-2](decisions.md#ag-2), which does not drive `agy`.
