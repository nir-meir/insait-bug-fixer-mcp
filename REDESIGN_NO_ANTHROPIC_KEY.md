# Redesign — No Anthropic API Key (match the Insait Platform MCP pattern)

## Why

Grounded in the real code at `/Users/nirmeir/insait/backend/app/mcp/`:
**the Insait Platform MCP never calls Anthropic.** No `anthropic` import, no
`messages.create()`. It only exposes tools that return data; **Claude (the
client) does all the reasoning.** That's why it needs no Anthropic key.

So we do the same: **remove the server-side Opus call.** The server gathers
data and hands it to Claude Code; Claude writes the analysis; the server saves
it. No `ANTHROPIC_API_KEY` anywhere.

(The Insait MCP authenticates to *its own platform* via Keycloak OAuth JWT —
that's a separate axis from calling a model. See "Not doing" below.)

---

## Core change

The single `generate_bug_report` (gather → **analyze with Opus** → write) splits,
because the analysis moves out of the server into Claude:

```
Before:  generate_bug_report  → fetch → merge → OPUS CALL → write report_id
After:   generate_bug_report  → fetch → merge → return context + report_id  →
         [Claude produces Root Cause / Solution A / B]                        →
         save_bug_report(report_id, analysis)  → write file
```

Three tools now: **generate_bug_report** (gather), **save_bug_report** (new,
write), **save_dev_feedback** (unchanged).

---

## Changes vs. the original plan (only what must change)

| Spec ref | Change |
|---|---|
| **§5 Step 7** (Opus 4.8 call) | **Removed.** No model call in the server. The exact step-7 prompt text is now *returned to Claude* as the analysis instruction, not sent to an API. |
| **§4 `generate_bug_report`** | Now does steps 1–6, creates `report_id`, stores gathered context, and **returns** the composed analysis context + `report_id` + "analyze, then call `save_bug_report`". Description updated: it returns context, not a finished report. |
| **§4 new `save_bug_report`** | New tool. Inputs: `report_id`, `analysis`. Writes `bug_report_{timestamp}.md` (§5 step 8 format, unchanged), returns path + root-cause summary. |
| **§4 `save_dev_feedback`** | Wording only: "after a report has been saved" (flow now goes through `save_bug_report`). Logic unchanged. |
| **§3 prompt** | Add the analyze-then-`save_bug_report` step to the flow. Everything else stays. |
| **§5 Step 8** (report_id + write) | `report_id` created in `generate_bug_report`; file write moves to `save_bug_report`. Same id scheme, folder logic, and file format. |
| **§8 error table** | Drop the "Anthropic API error" row (no model call). Add `save_bug_report`: missing `analysis`, unknown `report_id`. |
| **§10 requirements.txt** | Remove `anthropic>=0.25.0` (server no longer imports it). Keeps `mcp`, `httpx`, `python-dotenv`. |
| **`.env` / env loading** | Remove `ANTHROPIC_API_KEY` (unused). |

### server.py specifics
- Delete: `import anthropic`, `ANALYSIS_MODEL`, `ANALYSIS_MAX_TOKENS`, the
  Opus call inside `_run_analysis`.
- Keep `_run_analysis`'s prompt composition, but rename it to build/return the
  context string (no API call).
- `REPORTS[report_id]` is populated at gather time (context + warnings); its
  `analysis`/`root_cause` are filled when `save_bug_report` runs.

---

## Unchanged (reused as-is)

- Steps 1–6: validate inputs, fetch agent / transcript / interactions
  (paginated), merge by `seq`, load the 3 knowledge files.
- The **§5 step 7 prompt template** text (now returned to Claude verbatim).
- **§5 step 8** report file format, `report_id = {agent_id}_{timestamp}`,
  folder = agent name, `%Y%m%d_%H%M%S_%f`, `"(unresolved)"` node names,
  pagination-mismatch warning.
- **§5 step 9** golden_examples append (literal `[brackets]`) + feedback file.
- Insait data auth via `X-API-Key` (your `.env`) — kept for the prototype.
- §6 file structure, §7 knowledge files.

---

## Not doing (separate — flag only)

These would fully match the platform MCP but are **not needed** to remove the
Anthropic-key requirement, so they're out of scope unless you ask:

1. Switch Insait data auth from `X-API-Key` to OAuth Device Flow (Keycloak) JWT
   passthrough with an `X-Internal-API-Key` fallback — that's how the real MCP
   authenticates to the platform.
2. Host as a claude.ai connector (remote FastAPI + RFC 9728 `.well-known`
   metadata + Keycloak) instead of a local stdio server. Major infra; the local
   prototype stays stdio.

---

## Net effect

- **`ANTHROPIC_API_KEY` no longer needed** — the blocker is removed, not worked
  around.
- Reuses all fetch/merge/knowledge/report/feedback code already written.
- One deviation from the spec you had me implement: the server no longer calls
  Opus (§5 step 7) and the tool surface gains `save_bug_report`.

---

## Architecture (new)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Developer's IDE  (Cursor / Claude Code)                                        │
│                                                                                │
│   Developer types the bug details in chat.                                     │
│   Claude (the client LLM)  ── DOES ALL REASONING / THE ANALYSIS ──             │
│        │  tool call        ▲  result        │  tool call       ▲  result       │
└────────┼───────────────────┼────────────────┼──────────────────┼──────────────┘
         │                   │                 │                  │
     MCP (stdio JSON-RPC)                   MCP (stdio JSON-RPC)
         │                   │                 │                  │
┌────────▼───────────────────┴─────────────────▼──────────────────┴─────────────┐
│ Local Python MCP server   (server.py, stdio)      ──  NO Anthropic API key ──  │
│                                                                                │
│  §3 named prompt  bug_fixer_instructions  ──read on connect──▶ Claude          │
│                                                                                │
│  ┌───────────────────────── PHASE 1 · GATHER ──────────────────────────────┐  │
│  │ TOOL  generate_bug_report(agent_id, bug_description, conversation_id)    │  │
│  │   1  validate inputs (else clear error, no REST call)                    │  │
│  │   2  GET /api/v1/agents/{agent_id}              ─┐                        │  │
│  │   3  GET .../conversations/{id}/transcript       │  X-API-Key            │  │
│  │   4  GET .../chat/.../interactions  (paged loop) ─┘  ───▶ Insait REST    │  │
│  │        · build node_id→name map · resolve node names · partial-warn      │  │
│  │   5  merge transcript + interactions by `seq`  → per-turn structure      │  │
│  │   6  read 3 knowledge files                    ◀── knowledge/*.md        │  │
│  │      report_id = {agent_id}_{timestamp}                                  │  │
│  │      pagination-mismatch warning (count cross-check)                     │  │
│  │      store context in REPORTS[report_id]  (no analysis yet)              │  │
│  │      RETURN to Claude:  report_id                                        │  │
│  │                       + §5-step-7 prompt/context (bug desc, merged       │  │
│  │                         turns, system/security prompt, flow_def, KB)     │  │
│  │                       + "analyze; then call save_bug_report(report_id,   │  │
│  │                          analysis)"                                      │  │
│  └──────────────────────────────────────────────────────────────────────────┘│
│                                   │ context                                    │
│                                   ▼                                            │
│        ┌─────────────────────────────────────────────────────────┐            │
│        │  CLAUDE (client) produces:                               │            │
│        │    Root Cause · Solution A · Solution B (if warranted)   │            │
│        └─────────────────────────────────────────────────────────┘            │
│                                   │ analysis text                              │
│                                   ▼                                            │
│  ┌───────────────────────── PHASE 2 · SAVE ────────────────────────────────┐  │
│  │ TOOL  save_bug_report(report_id, analysis)                              │  │
│  │      look up REPORTS[report_id] (agent_name, conv_id, timestamp, warns) │  │
│  │      unknown id / missing analysis → clear error                        │  │
│  │   8  write  reports/{agent_name}/bug_report_{timestamp}.md              │  │
│  │        header(report_id, agent_id, conv_id, ts, warnings) + analysis    │  │
│  │        (node_name None → "(unresolved)")                                │  │
│  │      update REPORTS[report_id].analysis / root_cause                    │  │
│  │      on write failure → return content to chat + warning                │  │
│  │      RETURN:  report_id · file path · root-cause summary                │  │
│  └──────────────────────────────────────────────────────────────────────────┘│
│                                                                                │
│  ┌──────────────────────── FEEDBACK · §9 (optional) ───────────────────────┐  │
│  │ TOOL  save_dev_feedback(report_id, user_correction)                     │  │
│  │      validate inputs · unknown report_id → error                        │  │
│  │      look up REPORTS[report_id] (agent_id, bug desc, orig root cause)    │  │
│  │   9  append correction block ─▶ knowledge/golden_examples.md            │  │
│  │        (auto-create §7.3 header if missing; heading keeps [brackets])   │  │
│  │      write reports/{agent_name}/feedback_{timestamp}.md (same block)    │  │
│  │      on write failure → return content to chat + warning                │  │
│  │      RETURN:  "Correction saved. It will be included as a reference..." │  │
│  └──────────────────────────────────────────────────────────────────────────┘│
│                                                                                │
│  In-memory REPORTS registry:  report_id → {agent_id, agent_name, conv_id,      │
│     timestamp, bug_description, warnings, analysis, root_cause, file_path}      │
│     (populated in PHASE 1, completed in PHASE 2, read by FEEDBACK)              │
└────────────────────────────────────────────────────────────────────────────────┘
        │                          │                            │
        ▼                          ▼                            ▼
  Insait Platform REST       knowledge/  (read all 3;     reports/{agent_name}/
  API   (X-API-Key)          golden_examples.md            bug_report_{ts}.md
  · agents · transcript      appended on feedback)         feedback_{ts}.md
  · interactions (paged)
```

### End-to-end sequence

```
1   Connect         → server serves §3 prompt; Claude reads the collect-3-inputs rules
2   Developer       → gives agent_id + bug_description + conversation_id
3   Claude          → calls generate_bug_report(...)
4   Server PHASE 1  → steps 1–6, mints report_id, stores context, returns context to Claude
5   Claude          → writes Root Cause / Solution A / Solution B from the returned context
6   Claude          → calls save_bug_report(report_id, analysis)
7   Server PHASE 2  → writes bug_report_{ts}.md, returns report_id + path + summary
8   (if wrong)      → developer gives the correct fix
9   Claude          → calls save_dev_feedback(report_id, user_correction)
10  Server FEEDBACK → appends to golden_examples.md + writes feedback_{ts}.md
```

### Where the model call used to be

```
OLD (spec §5 step 7):  server ── prompt ──▶ Anthropic API (Opus 4.8)   [needs ANTHROPIC_API_KEY]
NEW:                   server ── context ──▶ Claude Code (already reasoning)   [no key]
```

### Auth & config

| Edge | Auth |
|---|---|
| IDE / Claude ↔ MCP server | local stdio (no network auth) |
| MCP server → Insait REST API | `X-API-Key: {INSAIT_API_KEY}` (unchanged) |
| MCP server → Anthropic model | **none — the server never calls a model** |

Env vars after redesign: `INSAIT_API_KEY`, `INSAIT_BASE_URL`,
`BUGFIXER_OUTPUT_DIR`, `BUGFIXER_KB_DIR`.  **Removed:** `ANTHROPIC_API_KEY`.

### Tools at a glance

| Tool | Phase | In | Out | Side effects |
|---|---|---|---|---|
| `generate_bug_report` | gather | agent_id, bug_description, conversation_id | report_id + analysis context | 3 REST reads; KB reads; `REPORTS[id]` created |
| `save_bug_report` (new) | save | report_id, analysis | report_id, path, summary | writes `bug_report_{ts}.md`; `REPORTS[id]` updated |
| `save_dev_feedback` | feedback | report_id, user_correction | confirmation | appends `golden_examples.md`; writes `feedback_{ts}.md` |
