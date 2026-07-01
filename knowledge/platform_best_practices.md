# Insait Agent Builder — Working Guide

The Agent Builder is the visual graph editor of the Insait platform. An agent's entire behavior is one JSON document — an **`AgentFlow`** — stored in the `agents.flow_definition` JSONB column. The same document drives both the editor canvas and the runtime engine (`FlowOrchestrator`): nodes are vertices, **exits** are directed edges with conditions, and **variables** are the shared mutable state. Debugging an agent = debugging this document plus the deterministic rules below for how the orchestrator walks it. Everything in this guide was read from the platform source (paths cited as provenance only; all rules are stated inline).

---

## The Flow Data Model

**Top-level `AgentFlow`** (`backend/app/schemas/flow/agent_flow.py`). Required fields are marked ✱.

| Field | Type / values | Notes |
|---|---|---|
| `id` | str (uuid) | auto-generated |
| `name` ✱ | str 1–255 | |
| `version` | int ≥1, default 1 | flow schema version, not the save counter |
| `channel` | `"voice"` \| `"chat"`, default `"chat"` | **`channel:"voice"` without `voice_settings` fails Pydantic parse** — the agent won't load |
| `global_settings` ✱ | GlobalSettings | see below |
| `voice_settings` | VoiceSettings \| null | TTS/STT/VAD/idle/DTMF etc. |
| `security_prompt`, `security_enabled`, `security_confidence_threshold` (0.5–1.0), `security_block_message`, `security_check_input_enabled`, `security_check_output_enabled`, `security_check_injection_enabled`, `security_fail_policy` (`open`/`closed`), `security_injection_patterns`, `security_passive_message`, `security_tail_message`, `security_evaluation_prompt`, `security_llm_provider`, `security_llm_model` | | active security evaluation config |
| `variables` | `VariableDefinition[]` | flow-level variable declarations |
| `tools` | ToolsConfig | global tools + built-in toggles |
| `knowledge_bases` | `KnowledgeBaseReference[]` | RAG wiring |
| `widget_settings`, `recording_settings`, `privacy_settings`, `extraction_config`, `sentiment_config`, `summary_config`, `analytics_variable_config`, `scoring_config`, `outcome_criteria_config`, `pdf_fill_config`, `post_conversation_actions_config` | | post-conversation / channel config, not flow routing |
| `real_time_assistant_settings` | RealTimeAssistantSettings \| null | when `enabled:true`, the flow is a listen-only coaching graph executed by `RealTimeAssistantGraphRunner`, **not** by FlowOrchestrator |
| `flow` ✱ | FlowGraph | the graph itself |

**`FlowGraph`**:

- `start_node_id` ✱ — must reference an existing key in `nodes` (Pydantic validator rejects otherwise).
- `nodes` ✱ — `Dict[node_id → Node]`; must be non-empty. Node IDs follow the convention `node-<8 hex chars>` (e.g. `node-a1b2c3d4`) but any string key works.
- `exits` — `Exit[]` at flow level (**new format**; each exit carries `source_node_id`). **Legacy format** stores exits on each `node.exits` (then `source_node_id` may be null). The orchestrator supports both; a flow can even mix them.

**Key `GlobalSettings` fields** (defaults in parentheses):

| Field | Default / values |
|---|---|
| `system_prompt` | `""` — base prompt prepended by nodes with `use_agent_prompt:true` |
| `llm_provider` | `"openai"` \| `"claude"` \| `"gemini"` (platform default provider) |
| `llm_model` | platform `DEFAULT_MODEL_ID` (`"gpt-5.5"` at time of writing) |
| `temperature` | 0.0 (range 0–2); `max_tokens` null (≤16000) |
| `greeting_message` | null — fallback greeting when start node has none |
| `first_speaker` | `"agent"` \| `"user"` |
| `fallback_models` | null — ordered LLM fallback chain |
| `enrich_history_with_system` | **true** — inject flow events (transitions, variable updates, API results) as system context messages |
| `enrich_api_results` | true; `enrich_api_results_max_chars` 500 (100–2000) |
| `enrich_history_with_rag` | false (requires `enrich_history_with_system:true` — validator rejects otherwise); `enrich_history_rag_depth` 2 |
| `limit_history` | false; `history_limit_depth` null (0–50 prior **user** messages kept; 0 = current turn only). `enrich_history_depth` is DEPRECATED and ignored |
| `skip_collect_completion_response` | false — when true, a collect node that completes all fields in a turn transitions immediately without streaming a completion reply |
| `agent_language`, `timezone`, `custom_language_instruction` | null |
| `transfers` | `TransferSchedule[]` — named working-hours schedules for transfer nodes (supersedes deprecated `transfer_availability_enabled`/`transfer_availability`/`off_hours_node_id`, which are auto-converted). `transfer_availability_enabled` is auto-reset to false if schedule or off-hours node is missing |
| `progress_bar`, `document_collection` | chat-widget UX config |

**Minimal working flow skeleton** (annotated):

```jsonc
{
  "id": "…uuid…", "name": "Demo", "version": 1, "channel": "chat",
  "global_settings": { "system_prompt": "You are …", "llm_provider": "openai",
                       "llm_model": "gpt-5.5", "temperature": 0.0,
                       "greeting_message": "Hi!" },
  "variables": [ { "name": "intent", "type": "string", "required": true } ],
  "tools": { "global_tools": [], "built_in_tools": { "end_call": true } },
  "knowledge_bases": [],
  "flow": {
    "start_node_id": "node-start01",
    "nodes": {
      "node-start01": { "id": "node-start01", "type": "start",
        "data": { "greeting_message": "Hi!", "skip_if_user_starts": true },
        "exits": [ { "id": "e1", "name": "go", "target_node_id": "node-main01",
                     "priority": 99, "condition": { "type": "always" } } ] },
      "node-main01": { "id": "node-main01", "type": "conversation",
        "data": { "prompt": "Help the customer.", "use_agent_prompt": true },
        "exits": [ { "id": "e2", "name": "loop", "target_node_id": "node-main01",
                     "priority": 99, "condition": { "type": "always" } } ] }
    },
    "exits": []   // new-format flows put ALL exits here with source_node_id set
  }
}
```

**Persistence & editing** (`backend/app/models/agent.py`, `backend/app/api/agents.py`):

- Column: `agents.flow_definition` (JSONB, nullable). Companion counter `agents.flow_version` (int, default 1) increments on every flow change — optimistic locking.
- Edit: **`PATCH /api/v1/agents/{agent_id}`** with body field `flow_definition` (schema `AgentUpdate`; all fields optional = partial update). Pass expected version via `X-Expected-Flow-Version` header (or query param); a mismatch returns **409** with `error: "concurrency_conflict"` plus current/expected versions and who modified.
- Query params on PATCH: `create_version=true` (+`version_name`) snapshots before saving; `switch_to_version=<n>` restores a snapshot.
- Publish: `POST /api/v1/agents/{agent_id}/versions/{version_id}/publish` sets `published_version_id` (live conversations use the published snapshot; drafts use `flow_definition`). Approval workflow may gate publish (403 if `require_approval_before_publish`).
- Export/import: `GET /api/v1/agents/{agent_id}/export` / `POST /api/v1/agents/import`. Export `flow_definition` is a validated `AgentFlow` when `metadata.validation_status == "success"`, else the raw dict.
- On save, invalid KB references are silently filtered out, and `tools.built_in_tools.transfer_to_human` is synced to the `allow_human_transfer` column.
- SDK: `backend/flow_builder_sdk/insait_flow_sdk.py` — `FlowBuilder(name, channel=…)` with `.start()/.conversation()/.collect()/.end()/.set_variables()/.code()/.api()/.transfer_to_human()` node methods returning `NodeRef`, `.variable()`, `.add_knowledge_base()`, `.tools_config()`, connection helpers `.connect_always(src, dst, priority=99)`, `.connect_on_expression(src, dst, expression, priority=0)`, `.connect_on_llm(src, dst, prompt, context_window=3)`, `.connect_on_result(src, dst, "success"|"error")`, then `.set_start_node(node)` and `.build()` (runs `.validate()` → list of error strings: name, start node, exits, variables, reachability).

---

## Node Types

Discriminated union on `type` (`backend/app/schemas/flow/nodes.py`). Every node has: `id`, `type`, `name` (display, ≤100), `data` (type-specific), `exits: Exit[]`, `position {x,y}` (editor only). 15 types total; 9 conversational + 6 real-time-assistant (separate engine).

### `start`

| | |
|---|---|
| Purpose | First node; produces the greeting (or routes silently in router mode) |
| Config (`data`) | `prompt` (str, `""` → LLM-generated greeting when set) · `use_agent_prompt` (bool, true) · `greeting_message` (≤1000; static greeting used when `prompt` empty) · `skip_if_user_starts` (bool, true — skip greeting if the user sends the first message) · `router_mode` (bool, false — evaluate exits without greeting) · `no_match_message` (≤1000; shown when router mode matches nothing) · `suggested_questions` · `voice_config` (default `allow_interruptions:false`) |
| Produces | greeting text only; no variables |
| Exits | typically one `always`; router mode evaluates `expression` + `always` pre-node |
| Common bugs | Greeting resolution order is `node.greeting_message → global_settings.greeting_message → agent greeting → platform default` — editing the wrong layer "doesn't take". Router mode with no matching exit and no `no_match_message` falls back to a generic "Unable to route. Please try again." An `llm` exit on a start node is never evaluated (no LLM turn runs here) — dead edge. |

### `conversation`

| | |
|---|---|
| Purpose | The main LLM node: free conversation with tools, RAG, field extraction |
| Config (`data`) | `prompt` (`""`) · `use_agent_prompt` (true) · `tools` — **None = all agent tools; `[]` = no custom tools; non-empty list = only those** (items are tool-id strings or `NodeToolConfig` objects) · `kb_mode` (None=inherit \| `auto`\|`tool`\|`auto_tool`\|`disabled`) · `kb_trigger_message` (≤500) · `kb_fast_reply_mode` (`auto`\|`predefined`\|`off`) · `kb_ids` (None/empty = all flow KBs) · `dataset_ids` (None=inherit all, `[]`=disable, list=subset) · `save_fields_trigger_message` (≤500) · `llm_override` (`NodeLLMConfig`: provider/model/temperature 0–2/max_tokens ≤16000/fallback_models) · `extract_fields: FieldDefinition[]` · `include_all_variables` (true) / `prompt_variable_names` (explicit injection list when false) · `custom_system_prompt_template` (**single-brace `{placeholder}` syntax**, overrides default assembly) · `kb_tool_search_description` · `suggested_questions` · `voice_config` (default interruptible) |
| Produces | assistant reply; variables via the `save_fields` tool (when enabled) and `extract_fields` |
| Exits | `llm` exits fire mid-turn via the `check_exits` tool; `always`/`result` post-node; `expression` **pre-node only** — except post-node when `save_fields` wrote ≥1 variable at this node this turn (INS-2765), so a just-saved variable can route immediately |
| Common bugs | Tool loop is capped at **`MAX_TOOL_ITERATIONS = 10`** per turn. A conversation node with only `expression` exits on a variable it sets itself never leaves the node unless the variable is written via `save_fields`. Prompt assembly order: language instruction → voice instructions (voice) → agent prompt (if `use_agent_prompt`) → node prompt ("Current task:") → tool-ack preamble → security line. `include_all_variables:false` with empty `prompt_variable_names` injects **no** variables into the prompt. |

### `collect`

| | |
|---|---|
| Purpose | Deterministic structured data collection with validation |
| Config (`data`) | `field_names: str[]` — **pointer-based, references `flow.variables` by name (current approach)** · `fields: FieldDefinition[]` (legacy inline definitions, deprecated) · `field_validations: {field_name: ValidationRule[]}` · `prompt` · `use_agent_prompt` (true) · `validate_fields` (false — when true, LLM asks the user to confirm values before marking them confirmed) · `validation_instructions` · `llm_override` · `interactive` (`CollectInteractiveConfig` — forms/buttons/sections UI, see below) · `kb_mode`/`kb_trigger_message`/`kb_fast_reply_mode`/`kb_ids`/`dataset_ids` · `save_fields_trigger_message` (≤500; completion-turn message, only when `tool_ack_mode_enabled`) · `custom_system_prompt_template` (**double-brace `{{placeholder}}`** per docstring) · `custom_collect_tool_prompt` (dict of save_fields prompt overrides) · `suggested_questions` · `tools` (system-tool filter: None=all enabled, `[]`=disable toggleables) · `voice_config` (`CollectNodeVoiceConfig`: `collect_dtmf` false, `collect_dtmf_only` false, `dtmf_reprompt_interval_seconds` 6, `dtmf_max_reprompts` 3) |
| Produces | one variable per collected+confirmed field (exact field name, source `user_input`) |
| Exits | post-node `expression`/`always`/`result` — **evaluated only when all required fields are collected and the node isn't awaiting confirmation**; `llm` exits via `check_exits`; tool calls (`goto_node`, `end_call`, `transfer_to_human`) can leave the node any time |
| Common bugs | Highest bug density — see the dedicated section below. |

### `api`

| | |
|---|---|
| Purpose | Execute a pre-defined custom API tool (AgentTool) or a built-in system tool, without an LLM |
| Config (`data`) | `tool_id` (str, `""` when using a system tool) · `system_tool` (discriminated union, currently only `{"type":"send_sms", template_id, variable_bindings[], recipient_sources[], recipient_phone_variable, per_conversation_limit}`) · `parameter_mapping: {param → "var_name" or "{{var_name}}" or literal}` · `result_variable` (str \| null — stores the full response) · `timeout_seconds` (30; 1–300) |
| Produces | `result_variable` (if set) + any tool-configured extracted variables + status vars **`_last_api_success`** (bool) and **`_last_api_error`** (sanitized). The send_sms system tool sets `_last_sms_success`/`_last_sms_error` AND mirrors into `_last_api_success`/`_last_api_error` |
| Exits | typically `result: success` / `result: error`; `expression` allowed post-node; `llm` exits are **treated as `always`** here |
| Common bugs | Success test is literally "`'error' not in result`". Timeout resolution: per-request `errorConfig.timeout_seconds` → global tool `error_config` → legacy `response_timeout_secs` → **30s default**. In simulated/test conversations the tool's `test_url` replaces `url` and mocks are suppressed. System variables (`system__user_phone`, …) are auto-merged into parameters. |

### `code`

| | |
|---|---|
| Purpose | Sandboxed JS (isolated-vm) / Python (Pyodide/WASM) execution — no filesystem/network |
| Config (`data`) | `language` (`"javascript"`\|`"python"`, default `"python"`) · `code` (1–100 000 chars) · `input_variables: str[]` (exposed as the `input` object; missing → `None`/`null`; `system__*` resolvable) · `output_variable` (default `"result"`, `^[a-z0-9_]+$`) · `output_type` (`string`\|`number`\|`boolean`\|`object`\|`array`, default `string`) · `timeout_seconds` (30; 1–300) · `memory_limit_mb` (128; 16–512) |
| Produces | `output_variable` (coerced to the flow-variable type if declared) + status vars `_last_code_success`, `_last_code_error`, `_last_code_error_type`, `_last_code_result` |
| Exits | ⚠️ **`result`-type exits DO NOT read `_last_code_success`** — the result evaluator only reads `_last_function_success` / `_last_api_success` (verified: `_last_code_success` has zero readers outside `code.py`). On a code node, `result` exits see the *previous* api/function node's status (stale) or nothing (→ fall through to `always`). Use `expression` exits on `{{_last_code_success}} == true` / `== false` instead |
| Common bugs | Return-value access uses dot notation: `{{result.property}}`. Failures don't raise — they set the `_last_code_*` vars and continue. Blocked patterns: `require`, `import`, `process`, `global`, `eval`, `Function`, fs/network. |

### `python` (legacy) and `function` (legacy)

- `python`: predecessor of `code` (Python only, restricted builtins). Fields: `code` ✱, `input_variables`, `output_variable`, `timeout_seconds` 30. New flows should use `code`.
- `function`: predecessor of `api`. Fields: `tool_id` ✱, `parameter_mapping`, `result_variable`, `timeout_seconds` 30 (wrapped in `asyncio.wait_for`; timeout → `{"success": false, "error": "Tool execution timed out after N seconds"}`). Sets **`_last_function_success`** / `_last_function_error` — these ARE read by `result` exits.

### `set_variables`

| | |
|---|---|
| Purpose | Pure variable manipulation — no LLM call, no user-visible output, passthrough |
| Config (`data`) | `assignments: [{variable_name (must match `^[a-z0-9_]+$` and exist in `flow.variables`), value: str ≤5000 \| null}]` — executed in order; `value:null` **resets** the variable; values support `{{var}}` templates (e.g. `"{{first_name}} {{last_name}}"`) |
| Produces | assigned variables (source `tool_assignment`); each is type-coerced via the flow variable's declared type — a value that fails coercion is **kept as the raw string** (no error raised) |
| Exits | `expression`/`always`/`result` post-node; pre-node exits are skipped (side-effect node) — it always runs first, then exits are evaluated |
| Common bugs | Resetting a variable that a collect node owns plants a `None` tombstone; the collect node detects this (timestamp comparison) and wipes its per-field state for re-collection (INS-2014). Assignment to a variable not declared in `flow.variables` is skipped. |

### `end`

| | |
|---|---|
| Purpose | Terminates the conversation |
| Config (`data`) | `prompt` (`""` → LLM-generated goodbye when set) · `use_agent_prompt` (true) · `end_message` (static, used when `prompt` empty) · `end_reason` (analytics) · `save_transcript` (true) · `voice_config` (default non-interruptible) |
| Produces | final message; sets state `status="completed"`, `ended_at`; updates the conversations row via raw SQL; publishes a `conversation ended` Kafka event; saves transcript if enabled |
| Exits | ignored ("Not used for end nodes") |
| Common bugs | On LLM failure the fallback goodbye is a static "Thank you for the conversation. Goodbye!". Anything wired *after* an end node never runs. |

### `transfer_to_human`

| | |
|---|---|
| Purpose | Hand off to a human: chat → `HumanHandoff` record + dashboard notify; voice → SIP/PSTN transfer; API channel → signal-only event |
| Config (`data`) | `transfer_message` (≤1000, `{{var}}` ok) · `targets: [{name, sip_uri \| phone_number (E.164), condition (expression, empty = default/fallback), priority (lower first)}]` · `sip_headers: [{name, default_value, conditions:[{expression, value}] (first match wins), hex_encode}]` · `transfer_method` (`dial_sip` default \| `refer` \| `invite` \| `bridge` — bridge keeps the call alive on failure and reports the outcome back; PSTN numbers always dial as PSTN) · `chat_transfer_reason` (≤500) · `transfer_timeout_seconds` (30; 5–120) · `fallback_message` (≤1000; empty → localized default) |
| Produces | marks conversation `transferred_to_human=true`, `status='completed'`, `end_reason='transfer_to_human'` (non-bridge); bridge retries capped by platform `VOICE_TRANSFER_MAX_ATTEMPTS` |
| Exits | normally none (node ends the conversation) |
| Common bugs | Target selection = sort by `priority`, first target whose `condition` evaluates true; else first **unconditional** target. Working hours: the node looks up the `TransferSchedule` in `global_settings.transfers` whose `source_node_ids` contains this node; outside hours it **redirects to the schedule's `target_node_id`** instead of transferring (exit id recorded as `off_hours_redirect`). A deleted redirect node silently disables the redirect. Timezone falls back to UTC when invalid. |

### Real-time-assistant nodes (separate engine)

`real_time_assistant_listen`, `moment_detector`, `objection_handler`, `coach_tip`, `speaker_identifier`, `qualification_tracker` — executed by `RealTimeAssistantGraphRunner` (`services/real_time_assistant/`), **never** by FlowOrchestrator. The graph is listen-only (never speaks into the call); each pass analyses a sliding transcript window and emits coaching events.

| Type | Key config (defaults) | Notes |
|---|---|---|
| `real_time_assistant_listen` | `window_turns` 12 (2–50) · `trigger_on` `customer_turn`\|`any_turn` · `debounce_ms` 1000 · `min_chars` 8 | entry node; connect its `always` exit to a detector |
| `moment_detector` | `moment_types` (8 defaults: `pricing_objection`, `competitor_mention`, `buying_signal`, `churn_risk`, `feature_question`, `timing_objection`, `trust_objection`, `next_step_opportunity`) · `min_confidence` 0.6 · `emit_moment_event` true · `llm_override` | sets `detected_moment` (key or `'none'`), `moment_confidence`, `moment_evidence`; route with `{{detected_moment}} == 'pricing_objection'` expression exits |
| `objection_handler` | `kb_ids` (None = flow KBs) · `retrieval_k` 4 (1–10) · `max_words` 45 · `include_citations` true | RAG-backed rebuttal suggestion |
| `coach_tip` | `tip_kind` `suggestion`\|`warning`\|`info`\|`battlecard` · `max_words` 35 | short rep-console tip |
| `speaker_identifier` | `enabled` true · `min_segments` 4 · `auto_relabel` true · `hints` | runs at transcript-ingest time; passthrough in the graph |
| `qualification_tracker` | `fields` (5 defaults: pain, budget, decision_maker, timeline, next_step) · `check_every_n_turns` 2 · `nudge_on_buying_signal` true · `nudge_after_minutes` 10 | executed **concurrently, outside the exit walk** — any exit drawn to it is visual documentation only |

---

## Exits & Conditions

**`Exit` object** (`backend/app/schemas/flow/exits.py`):

| Field | Constraint |
|---|---|
| `id` | str, auto uuid |
| `name` ✱ | 1–100 chars |
| `source_node_id` | required when exits live at flow level |
| `target_node_id` ✱ | must be an existing node — **exits pointing at deleted nodes are silently filtered out at evaluation time** (logged as `exit-target-missing`) |
| `priority` | int 0–1 000 000, default 0 — **lower = evaluated first; only meaningful for `expression` exits** (see below) |
| `condition` ✱ | one of the 4 types below |
| `ack_message` | ≤500, `{{var}}` interpolated, streamed to the user when the exit fires |
| `context_message` | ≤500, appended to the system context message when the exit fires |

**Condition types**:

| `condition.type` | Fields | Meaning | When evaluated | Gotchas |
|---|---|---|---|---|
| `"expression"` | `expression` (1–1000 chars) | deterministic expression over variables | pre-node guard (most nodes) and post-node for collect/set_variables/api/function/code; on conversation nodes post-node **only if `save_fields` wrote a variable this turn** | see grammar below; a referenced-but-undefined variable makes the exit **skipped** (not false) unless it's a bare truthy/falsy check |
| `"llm"` | `prompt` (1–1000), `context_window` 3 (1–10) | natural-language yes/no decision | **never in the exit evaluator** — the conversation/collect LLM sees exit descriptions and fires them via the internal `check_exits` tool mid-turn. On api/function/code nodes, `llm` exits are **converted to `always`** | an `llm` exit on a start/end/set_variables node is dead; on an api node it acts as an unconditional jump |
| `"result"` | `result`: `"success"` \| `"error"` | branch on last tool execution | post-node | reads `_last_function_success` **or** `_last_api_success` only — NOT `_last_code_success`; before any api/function node has run, neither exists → both success and error exits fail → falls to `always` |
| `"always"` | — | unconditional fallback | last, after all other types | the **first `always` exit in list order** is taken — `priority` is ignored for `always` exits |

### Expression grammar — exactly what the evaluator accepts

(`backend/app/services/orchestrator/exit_evaluators/__init__.py`; schema-time validation in `exits.py`.)

**Schema-time (save) validation** rejects expressions that: contain no `{{…}}` reference (unless the whole expression is `true`/`false`); have unbalanced `{{`/`}}`; contain an empty `{{}}`; reference names not matching `[a-zA-Z_][a-zA-Z0-9_]*(\.…)*`; contain no comparison operator and aren't a bare truthy/falsy check; start/end with an operator; have dangling or consecutive `AND`/`OR`.

**Runtime resolution** — each `{{var}}` / `{{var.nested.path}}` (regex `\{\{([\w.]+)\}\}`) is substituted:

| Variable value | Substituted as |
|---|---|
| string | `'value'` (single-quoted; double quotes in the expression are normalized to single) |
| bool | `true` / `false` (lowercase) |
| None / missing / broken nested path | bare token `None` |
| dict | `'<object>'` sentinel — whole-object comparisons are meaningless; use dot-notation (`{{obj.status}}`) |
| list | JSON (`["a","b"]`) |
| number | as-is |

`system__*` names resolve from built-in system variables (see Variables). AND/OR splitting happens on the **raw** expression (quote-aware) *before* substitution, so values containing apostrophes or the words "AND"/"OR" can't corrupt operator boundaries (INS-3123).

**Supported comparisons** (anchored patterns; anything that matches none of them evaluates to **False with only a log warning**):

| Form | Semantics |
|---|---|
| `{{var}} == 'x'` / `!=` | exact string equality (values with apostrophes/newlines OK) |
| `{{n}} > 10`, `<`, `>=`, `<=`, `==`, `!=` | numeric (floats; negatives OK) |
| `'5' == 5` and `5 == '5'` mixes | numeric coercion both directions (all 6 operators) |
| `{{b}} == true` / bool-vs-`'true'` mixes | boolean equality/inequality, case-insensitive |
| `{{var}} == None` / `!= None` (either side) | content-agnostic set/unset check |
| `'x' in {{list_var}}` | list membership (list rendered as JSON) |
| `'x' in {{string_var}}` | substring containment |
| `{{var}}` | truthy: non-empty string, non-zero number; dict → always truthy (`'<object>'`); None → false |
| `!{{var}}` | negation of the above |
| `true` / `false` literal | constant |
| `A AND B`, `A OR B` | case-insensitive, space-bounded; precedence **AND > OR**; short-circuiting |

**Operators NOT supported**: `===`, `!==`, `&&`, `||`, arithmetic, parentheses for grouping, regex. The frontend validator flags these before save.

### Evaluation & priority algorithm (the exact runtime rules)

`evaluate_exits()` groups a node's exits **by condition type** and processes groups in a fixed order — type beats priority:

1. **Filter**: drop exits whose `target_node_id` no longer exists.
2. **Expression exits** — sorted by `priority` ascending; **all** are evaluated; the first (lowest-priority-number) match wins. Multiple matches → warning logged, first taken. An exit whose comparison references an undefined variable is **skipped** with `skip_reason: "Variables not defined: …"` (bare `{{var}}` / `!{{var}}` checks are always evaluated — undefined = falsy).
3. **LLM exits** — skipped here (fired via `check_exits` during the LLM turn), except `treat_llm_as_always` on api/function/code nodes prepends them to the always group.
4. **Result exits** — in **list order** (not priority-sorted); first match wins.
5. **Always exits** — the **first in list order** is taken unconditionally.
6. Nothing matched → returns None → no transition; the conversation stays on the current node awaiting the next user message.

Every evaluation appends a `ConditionEvaluationRecord` to `turn.processing.condition_evaluations` with `exit_id`, `exit_name`, `source_node_id`, `condition_type`, `expression`, `resolved_expression`, `result`, `target_node_id`, `latency_ms`, `skipped`, `skip_reason` — **this is the primary debugging artifact for routing questions**.

**Pre-node vs post-node timing** (from `flow_orchestrator.py`):

| Phase | Which exits | Which nodes |
|---|---|---|
| **Pre-node** (entry guard — if one matches, the node body is skipped and the flow transitions immediately) | `expression` only (router-mode start nodes also get `always`) | all nodes EXCEPT side-effect nodes (`code`, `api`, `function`, `set_variables` — they must run first to produce the variables their exits read) and nodes entered via `goto_node` (`force_execute`) |
| **Post-node** (after the body runs) | `always`, `result` — all node types; + `expression` on `collect`/`set_variables`; + `expression` + `llm`(as always) on `api`/`function`/`code`; + `expression` on `conversation` **only when `save_fields` persisted ≥1 variable at that node this turn** | collect skips post-node evaluation entirely while awaiting confirmation or while required fields are incomplete |

**Classic routing bugs**: an unreachable exit because a higher-priority expression always matches first; an `expression` exit on a conversation node that never fires because the variable is set the same turn without `save_fields`; a missing `always` fallback leaving the node stuck when no expression matches; relying on `priority` to order `always`/`result` exits (only list order counts); a self-loop `always` exit on a hub node accidentally shadowing nothing (self-transitions are skipped unless tool-triggered — a self-`always` is the idiomatic "stay here").

---

## Variables

**Types** (`VariableType`): `string`, `number`, `boolean`, `date`, `enum`, `list`, `object`, `document`, `array` (legacy alias of list).

**`VariableDefinition` fields**: `name` ✱ (regex `^[a-z][a-z0-9_]*$`), `type` ✱, `default`, `description` (≤500), `required` (true), `persist` (true), `source` (`user`\|`collect`\|`tool`\|`system`\|`session`), `source_node_id` (which collect node owns it), `collection_mode` (`explicit` = must ask the user directly \| `deducible` = may extract from context; default `explicit`), `validation_rules`, `options` (enum), `allowed_file_types`/`max_file_size_mb` (document; ≤30 MB; default extensions pdf, doc, docx, txt, png, jpg, jpeg, webp[, csv, xlsx]), `sensitive` (encrypt at rest; masked as `<SENSITIVE>` in logs).

**Reserved names** (rejected on save): `user`, `system`, `assistant`, `tool`, `node`, `flow`, `turn`, `message`, `messages`, `conversation_id`, `external_id`, `stream`, `session_data`. Names starting with `system__` are also reserved.

**Runtime `VariableValue`** tracks `value`, `type`, `source` ∈ {`default`, `user_input`, `tool_assignment`, `llm_extraction`, `system`, `session`, `session_auto`, `interaction_extraction`, `document_extract`}, `set_at_turn`, `set_at_node`, `updated_at`. Internal status variables use a leading underscore (`_last_api_success`, `_last_code_success`, …) and are referencable from expressions.

**Type coercion** (`validate_and_coerce_value`, applied on collect/set_variables writes): number → int first, then float; boolean accepts `true/yes/1/on` and `false/no/0/off` (case-insensitive); date must be ISO 8601 (stored as the original string); enum matched **fuzzily** — lowercase, spaces/hyphens folded to underscores, and the canonical option key is stored; list/object must be valid JSON of the right container type; empty string → `""` for string type, `None` for every other type.

**Templating**:

| Context | Syntax | Missing-variable behavior |
|---|---|---|
| Node prompts, ack/trigger/greeting messages, interactive labels, SMS bindings, API params | `{{var}}` / `{{var.nested.path}}` | left **verbatim** (`{{missing}}` appears literally in the prompt/output) |
| Exit expressions | same `{{…}}` | resolves to `None`; comparison exits referencing an undeclared variable are **skipped** entirely; truthy checks treat it as falsy |
| `custom_system_prompt_template` | conversation node: single-brace `{placeholder}`; collect node: double-brace `{{placeholder}}` | — |

**System variables** (built at evaluation/prompt time, `build_system_variables` in `node_executors/base.py`): `system__conversation_id`, `system__source_channel`, `system__user_phone`, `system__date` (YYYY-MM-DD, agent timezone), `system__time` (HH:MM:SS local), `system__time_utc` (ISO), `system__timezone`, `system__agent_turns`, `system__chat_history`, `system__caller_host`, `system__caller_user` (SIP user part, leading `+` stripped), `system__caller_id` (E.164 with `+`; default SMS recipient), plus `system__agent_id` / `system__agent_name` when an agent is in scope. The builder UI also suggests voice-only `system__called_number`, `system__call_duration_secs`, `system__call_sid` — ⚠️ unverified whether the orchestrator-side resolver populates these (they are not in `build_system_variables`).

**Unset vs empty matters**: unset/None → `{{v}} == None` true, `{{v}}` falsy. Empty string `''` → `!= None` true, but `{{v}}` truthy check **false** (zero length). A dict-valued variable is always truthy and only comparable via dot-notation.

**Session data**: keys in `session_data` (widget/API) fill variables declared with `source:"session"`; **all remaining keys are auto-mapped** into variables (source `session_auto`) except those starting with `_`.

**Scope/lifetime**: all variables are conversation-scoped, stored in `conversations.current_state.variables`, persisted across turns; `flow.variables[].default` seeds them at conversation start.

---

## Collect Nodes & Validation

**Extraction model.** One unified LLM call per collection turn: the LLM receives all field definitions + per-field status + history, and persists values by calling the internal **`save_fields`** tool. The tool loop within a single turn is capped at **`MAX_COLLECT_TOOL_ITERATIONS = 5`**. Per-field state lives in `state.graph_state.collect_state[node_id][field_name]` as `{collected, confirmed, value, set_at_turn}` and persists across turns (partial collection). A field's value is copied into `state.variables` **only when `confirmed == true` and value is not None**.

- `validate_fields:false` (default): fields are auto-confirmed on extraction.
- `validate_fields:true`: the LLM asks the user to confirm before setting `confirmed`; while confirming, `graph_state.awaiting_confirmation = node_id` and **post-node exits are not evaluated**.
- `collection_mode`: `explicit` fields may only be extracted from the **last user message**; `deducible` fields from any history.
- Re-entry sync (INS-2014): if a variable's `set_at_turn` is newer than the collect state's (e.g. a Set Variables node reset it to None), the field's collect state is wiped and it is re-collected.
- Digression: if the user goes off-topic, the LLM answers without calling `save_fields`; the flow stays on the collect node.
- `global_settings.skip_collect_completion_response:true` suppresses the completion reply and transitions immediately once all fields land in one turn.

**Validation rules** — `ValidationRule = BuiltInValidationRule | LLMValidationRule`:

- `{"type":"builtin", "validation_type": <enum>, "params": {…}, "error_message": "…"}`
- `{"type":"llm", "prompt": "…", "error_message": "…", "llm_provider": null→openai, "llm_model": null→gpt-4o-mini}` — LLM validators run in parallel and **fail open** (value saved) on LLM errors.

**All 23 `BuiltInValidationType` values** (exact strings):

| Value | Validates | Params |
|---|---|---|
| `phone` | international phone (phonenumbers lib) | `country` (ISO code; UI offers IL, US, GB, DE, FR, ES, IT, NL, BE, AT, CH, CA, AU) |
| `national_id` | national ID (python-stdnum) | `country` |
| `postal_code` | postal/ZIP (stdnum) | `country` |
| `iban` | IBAN, auto-detects country | — |
| `vat_number` | EU VAT | `country` (or `auto`) |
| `phone_il` | Israeli phone (legacy) | — |
| `israeli_id` | Israeli ID checksum (legacy) | — |
| `email` | simplified RFC-5322 regex | — |
| `url` | URL format | `require_https` (false) |
| `date_format` | strptime format match | `format` (default `%d/%m/%Y`; UI offers `%d/%m/%Y`, `%m/%d/%Y`, `%Y-%m-%d`, `%d-%m-%Y`, `%d.%m.%Y`) |
| `time_format` | time format | `format` (default `%H:%M`) |
| `credit_card` | Luhn, 13–19 digits | — |
| `date_not_future` | date ≤ today (flexible parsing) | `timezone` (default `Asia/Jerusalem`) |
| `date_not_past` | date ≥ today | `timezone` |
| `date_range` | within min/max | `format`, `min_date`, `max_date` |
| `number_range` | numeric bounds | `min`, `max` |
| `number_positive` | > 0 | — |
| `number_integer` | whole number | — |
| `not_empty` | non-blank string / non-empty container | — |
| `min_length` | string length ≥ | `min` (default 1) |
| `max_length` | string length ≤ | `max` (default 100) |
| `regex` | custom pattern (1 s match timeout, 10 000-char input cap) | `pattern` ✱, `ignore_case` (false), `message` |
| `one_of` | value in allowed list | `options` (list or comma-separated), `ignore_case` (true) |

**Failure behavior**: an invalid value is returned to the LLM inside the `save_fields` tool result as `{failed: {field: error}, remaining_fields: […]}`; the LLM re-asks the user. There is **no per-field retry counter and no "max attempts" exit** — retries are bounded only by the 5-iteration cap per turn, then the next user turn starts fresh. If you need an attempts-based escape hatch, count attempts yourself (set_variables + expression exit).

**FieldDefinition constraints** (schema-enforced): `type:"enum"` requires non-empty `options`; `type:"object"` requires `object_properties`; `is_list:true` requires `list_item_type`; names must match `^[a-z0-9_]+$`.

**Special field types**: `document` fields use the sentinel value `"not_uploaded"` before upload and store `{s3_key, filename, content_type, file_size_bytes, uploaded_at}` after; boolean extraction maps "true"/"yes"/"1" → true, anything else false; enum values are fuzzy-matched to the canonical option.

**Interactive collect** (`data.interactive`, `CollectInteractiveConfig`): `enabled` (false), `show_form`, `message` (+`skip_llm_message`), `image`, `buttons` (each sets `field_name` to `field_value` on click), `field_display` per-field label/placeholder, `submit_label` ("Submit"), and the newer ordered `sections[]` (discriminated union: `message`, `image`, `form_field`, `pin_code`, `choice_cards`, `multi_select_grid`, `personal_info_form`, `document_upload`, `hero_stats`, `terms_accept`, `final_review`, `buttons`) which **overrides** the legacy fields when present. Chat channel only.

**Voice/DTMF**: `voice_config.collect_dtmf` (false — keypad input for number fields; a one-time migration set it true on pre-existing collect nodes, but restored old versions/imports read false), `collect_dtmf_only` (mute STT, keypad only; gated on `collect_dtmf`), reprompt every `dtmf_reprompt_interval_seconds` (6) up to `dtmf_max_reprompts` (3), then voice re-enables. DTMF arrives as a message shaped `__dtmf__:{digits}`.

---

## Tools & Knowledge Bases

**Built-in tools** (`tools.built_in_tools`, defaults): `end_call` **true**; `transfer_to_human`, `schedule_appointment`, `goto_node`, `show_confirmation`, `extract_from_document`, `save_fields`, `suggestion_buttons` all **false**. Related config on `ToolsConfig`: `global_tools` (custom tool IDs available across nodes), `end_call_trigger_message`, `transfer_trigger_message`, `goto_node_jumps` (`[{id, name, target_node_id, description, fast_reply_text}]`), `goto_node_fast_reply_mode` (`auto`), `tool_ack_mode_enabled` (false — INS-2930; when ON, per-tool `*_fast_reply_mode` fields are ignored in favor of a global tool-ack preamble + deterministic predefined messages), `save_fields_trigger_message`, `tool_ack_preamble`, `send_sms` config.

**Per-node tool config** (`NodeToolConfig` inside `data.tools`): `tool_id` ✱, `trigger_message` (≤500, fast reply on invocation), `fast_reply_mode` (`auto`\|`predefined`\|`off`, None = inherit), `goto_node_jump_ids` (restrict which jumps this node offers), `config` (tool-specific: `show_confirmation.field_names`, `extract_from_document.{extraction_prompt, vision_model, field_names, document_field, max_pages}`, `save_fields.field_names`).

**Filter semantics everywhere**: `null` = inherit everything; `[]` = disable; non-empty list = whitelist. Applies to `data.tools`, `data.kb_ids`, `data.dataset_ids`.

**How tools fire**: in `conversation`/`collect` nodes the LLM calls them (function calling, ≤10 / ≤5 iterations per turn); in `api`/`function` nodes the tool runs directly with `parameter_mapping`. `goto_node` performs a tool-driven transition that **bypasses exit evaluation** and forces the target node to execute (pre-node guard skipped). `end_call` / `transfer_to_human` end the turn/conversation.

**Knowledge bases** (`knowledge_bases[]`, `KnowledgeBaseReference`): `kb_id` ✱, `mode` (`auto` default = retrieve every turn and inject into the system prompt; `tool` = LLM calls `knowledge_search` on demand; `auto_tool` = LLM-optimized query search every message; `disabled`), `retrieval_k` 7 (1–20), `embeddings_model` (`text-embedding-3-large`), `include_conversation_context` true, `trigger_message` (fast reply while searching, tool mode), `fast_reply_mode`, `kb_tool_search_description` (LLM-facing tool description; per-node override on ConversationNodeData wins over per-KB, which wins over the agent-level `AgentFlow.kb_tool_search_description` fallback), `hybrid_search` `{enabled:true, alpha:50}` (0=keyword…100=semantic), `reranking` `{enabled:false, provider: bedrock|llm, mode: scoring|up_to_top_k|exact_top_k, top_k:20, final_k:5}`, `multi_query_count` 1 (1–5), `query_dictionary`.

**Node-level KB overrides**: `data.kb_mode` (None = inherit the KB reference's mode), `data.kb_ids` (subset), `data.kb_trigger_message`, `data.kb_fast_reply_mode`. Without reranking, injected chunk count defaults to 5 (`_DEFAULT_FINAL_K`).

**What breaks**: a KB id referenced in `kb_ids` but not in `knowledge_bases` (or deleted) is ignored — the node silently has no KB; `mode:"tool"` with the tool never being called usually means the `kb_tool_search_description` doesn't describe the content; `auto` mode retrieves off the raw last user message (short replies like "yes" retrieve garbage — `auto_tool` reformulates); custom tool referenced by a stale `tool_id` on an api node → node errors out (`_last_api_success=false`).

---

## Runtime Execution Model

(`backend/app/services/orchestrator/flow_orchestrator.py`.)

- Entry: `FlowOrchestrator.process_message(message, …)` — an async generator streaming events (`RESPONSE`, `FAST_REPLY`, `NODE_TRANSITION`, `INTERACTION_PROMPT`, `FUNCTION_CALL`, `CONVERSATION_END`, `ERROR`, …).
- **First turn**: the literal message `"__start__"` (`START_TRIGGER_MESSAGE`, `backend/app/constants.py`) triggers greeting; it is not stored as user input. Voice repeat-caller routing may override the start node and speak `repeat_caller_pre_routing_message` first.
- **State**: `ConversationState` (persisted to `conversations.current_state` after every turn) holds `variables`, `messages`, `turns`, `graph_state` (`current_node_id`, `node_history`, `collect_state`, `awaiting_confirmation`), `status`. New conversations seed variables from `flow.variables[].default` and session data.
- **Node-chaining loop** — per user turn, up to **`max_chain_depth = 20`** node executions:

```
user message (or "__start__")
  └─ while chain_depth < 20:
       1. PRE-NODE exits: expression-only guard (skipped for code/api/function/
          set_variables and goto_node entries). Match → transition, continue loop
          WITHOUT executing the node body.
       2. EXECUTE node via its executor (LLM call / tool run / passthrough).
          Streams RESPONSE / FAST_REPLY; may fire check_exits / goto_node /
          end_call / transfer_to_human mid-turn (tool transitions bypass exits).
       3. POST-NODE exits: allowed types per node type (see Exits section).
          Collect: only when all required fields collected & not awaiting confirm.
       4. Match → record transition (node_history, current_node_id), stream the
          exit's ack_message if set, continue loop at target node.
          Self-transition → skipped (stay, wait for next user message) unless
          tool-triggered. No match → break: wait for next user message.
       End node executed → conversation completed, transcript saved (if enabled),
       Kafka "conversation ended" event, CONVERSATION_END streamed.
  └─ save turn (messages, condition_evaluations, llm_calls, tool_executions,
     variable_changes, timings) + save state → DONE event
```

- **Variable mutation timing**: set_variables/api/code/function write variables during step 2, so their own post-node expression exits see fresh values; conversation-node `save_fields` writes mid-turn and unlocks the post-node expression re-check (INS-2765); collect writes on confirmation.
- **Errors**: an executor exception → the partial turn is saved with `turn.processing.error` and a **sanitized, localized user-facing error message** (raw errors never reach the user). API/code timeouts don't raise — they set `_last_*_success=false` and let `result:error` / expression exits route. LLM failures fall back through `global_settings.fallback_models` if configured.
- **Loop protection**: exceeding 20 chained nodes aborts the chain (logged with `flow.max_chain_depth`) — a cycle of always-exits between non-interactive nodes (set_variables → code → set_variables…) hits this.
- **Turn forensics**: everything is recorded on the Turn: `processing.condition_evaluations` (every exit checked, with `resolved_expression`), `processing.llm_calls`, `processing.tool_executions`, `processing.variable_changes`, `output.node_content_segments` (which node produced which text).

---

## Flow Validation Rules

**Backend (hard — Pydantic parse fails, flow can't be saved/loaded)** (`schemas/flow/*`):
- `flow.nodes` empty; `start_node_id` not in `nodes`.
- `channel:"voice"` with `voice_settings:null`.
- Expression syntax violations (see grammar section) at exit save time.
- Variable name not `^[a-z][a-z0-9_]*$`, reserved, or `system__`-prefixed; field/property names not `^[a-z0-9_]+$`.
- enum field without `options`; object field without `object_properties`; `is_list` without `list_item_type`; multi_selector extraction field with <2 options; numeric extraction `min_value > max_value`.
- Length/range bounds on every field as listed in the node tables.
- Note self-healing on load (NOT failures): out-of-range `slow_tempo` clamped; unfilled speech-rate pattern rows dropped; `background_noise_enabled` auto-disabled when no type; legacy shapes migrated (`tts_prompt`→`voice_instructions`, `progress_bar_enabled`→`progress_bar`, old SMS recipient shape).

**Frontend (`frontend/src/lib/agent-builder/flow-validation.ts`)** — errors (E) block, warnings (W) advise:

- Expressions: single `=` (E); `===`/`!==` (E); unbalanced quotes/brackets/braces (E); empty `{{}}` (E); invalid variable name (E); `&&`/`||` instead of AND/OR (W); no comparison operator (W); numeric comparison against a string (W); `in` without list/string target (W); whole-object comparison to a non-null value (W); typos `contians`/`lenght` (E).
- Nodes: api node with no tool selected / no system-tool template / no URL (E); code node with empty code (E); collect node with no fields (W), document field not covered by a form section (W), interactive button with empty label or value (E); start node with neither greeting nor prompt and not router mode (W); end node with no message/prompt (W); conversation node with no prompt (W); transfer schedule pointing at a deleted node (E).
- Edges: result condition missing/invalid value (E); empty expression (E); llm condition without a prompt (W).
- Variables: defined-but-unused (W); referenced-but-undefined (W, with location).

**SDK `validate()`**: flow name, start node set, exits reference existing nodes, variables declared, node reachability.

**What nothing validates** (real gaps a debugger must check manually): a node with **no exits at all** (dead end — conversation just stays there, which is only sometimes intended); no `always` fallback among expression exits; two expression exits with identical priority (first-in-sort-order wins, effectively list order); `llm` exits on node types that never evaluate them.

---

## Best Practices

- **Every routing node gets exactly one `always` fallback exit**, listed last. Expression exits get distinct, explicit priorities (0,1,2,…); reserve 99 for the fallback (SDK convention).
- **Route on variables, not vibes**: prefer `expression` exits on collected/saved variables over `llm` exits; `llm` exits cost an LLM decision, and on api/function/code nodes they silently become unconditional.
- **On conversation nodes, any variable you want to route on the same turn must be written via `save_fields`** (enable the built-in tool and reference the field) — otherwise the expression exit can only fire on the *next* turn's pre-node check.
- **Code nodes: never use `result` exits.** Use `{{_last_code_success}} == true` / `== false` expression exits.
- **Declare every variable in `flow.variables`** before referencing it: set_variables skips assignments to undeclared names, and comparison exits on undeclared variables are skipped, not false. For "is it set yet?" checks use the bare truthy form `{{var}}` / `!{{var}}`, which is always evaluated.
- **Match types**: store numbers as `number` (the coercer keeps `'42'`-vs-`42` working, but `'abc' > 5` just evaluates False); compare enums against the canonical option key (fuzzy matching stores the canonical form).
- **Collect hygiene**: use `field_names` pointing at flow variables (not legacy inline `fields`); mark truly optional fields `required:false`; use `deducible` for things users volunteer; add validation rules (there's no automatic retry cap — design an escape route for repeated failures); use `validate_fields:true` for high-stakes data (adds an explicit confirmation turn).
- **OTP/security flows**: mark secret fields `sensitive:true` (log masking + encryption at rest); keep security checks enabled (`security_enabled`, input/output/injection toggles); don't put secrets in `ack_message`/prompts — they interpolate `{{var}}` into user-visible text.
- **Transfers**: always configure `fallback_message` and one unconditional target; wire the off-hours schedule (`global_settings.transfers`) and keep its `target_node_id` alive when refactoring.
- **Editing**: send `X-Expected-Flow-Version` on PATCH to avoid clobbering concurrent edits; create a version snapshot (`create_version=true`) before risky changes; remember drafts vs published — production traffic runs the **published** version.
- **Avoid loops of non-interactive nodes** (set_variables/code/api chains that cycle) — the 20-node chain cap will cut the turn.

---

## Known Quirks & Common Bug Patterns

| Symptom | Likely root cause | Where to look | Fix |
|---|---|---|---|
| Bot stays on a node forever / repeats itself | No exit matched and there's no `always` fallback; or the only exits are expression exits whose variables are undefined (exits **skipped**, not false) | `turn.processing.condition_evaluations` — look for `skipped: true`, `skip_reason: "Variables not defined"` | add an `always` exit; declare the variable or use `{{var}}`/`!{{var}}` truthy form |
| Exit "should match" but a different one fires | Expression exits are priority-sorted; equal priorities resolve by list order; multiple simultaneous matches log a warning and take the first | condition_evaluations order + `priority` values | give unique priorities |
| `always` exit with priority 0 "ignored" | `priority` only orders **expression** exits; type order is expression → result → always regardless of priority; among `always` exits the first in list order wins | exits list order | reorder the list, don't rely on priority |
| Conversation node never takes an expression exit on a variable set during that same turn | Expression exits on conversation nodes are pre-node only, unless `save_fields` wrote a variable at that node this turn (INS-2765) | was `save_fields` enabled & called? `turn.processing.tool_executions` | enable `save_fields` for the field, or route via an `llm` exit / next-turn pre-node |
| Code node always takes the `success` exit (or falls to `always`) even when the code fails | `result` exits read `_last_function_success`/`_last_api_success` only; the code node writes `_last_code_success`, which the result evaluator never reads → stale/absent status | node exits config | replace with expression exits on `{{_last_code_success}}` |
| `result: error` exit never fires before any API ran | Neither `_last_api_success` nor `_last_function_success` exists → both result exits evaluate false | condition_evaluations | ensure the api/function node actually executes before the branch |
| Expression involving an object variable always False / always truthy | Whole dicts resolve to the `'<object>'` sentinel — any `== 'x'` comparison fails; bare `{{obj}}` is always truthy | `resolved_expression` in condition_evaluations shows `'<object>'` | use dot-notation `{{obj.field}}` |
| `'x' in {{var}}` "matches" unexpectedly / doesn't match | For string vars this is substring containment; for lists, membership; any unrecognized resolved pattern silently returns False | `resolved_expression` | check the variable's actual runtime type |
| Exit with `AND`/`OR` behaves oddly | Precedence is AND > OR; `&&`/`||`/parentheses unsupported; unparseable leaves → False | frontend validator + resolved expression | rewrite with AND/OR, restructure to avoid needing parentheses |
| Literal `{{variable}}` shows up in bot messages | Prompt/message interpolation leaves unknown variables verbatim (only exit expressions map them to None) | which name is misspelled / never set | fix the name or guarantee it's set before this node |
| Flow "jumps randomly" from an api/function/code node | `llm` exits on those nodes are converted to `always` (unconditional) | exits of that node | use `result`/`expression` conditions there |
| Edge to a node that was deleted; conversation continues but never routes there | Exits with missing targets are silently filtered at evaluation (warning log `exit-target-missing`) | flow JSON vs node keys; backend warning logs | re-point or delete the stale exit |
| Collect node re-asks for a field the user already gave | Field wasn't confirmed (`validate_fields:true` awaiting confirmation), value failed validation, or a set_variables reset tombstoned it (re-entry wipe, INS-2014) | `graph_state.collect_state[node_id]`, `awaiting_confirmation`, variable_changes | check validation errors in the save_fields tool result; avoid resetting collect-owned variables mid-flow |
| Collect node never exits | A required field is uncollected (post-node exits gated on completeness), or node is `awaiting_confirmation` | collect_state: which `required` field has `collected:false` | mark optional fields `required:false`; finish the confirmation |
| Collected value present in collect_state but the variable is empty | Variables are written only when `confirmed:true` and value non-None | collect_state confirmed flags | `validate_fields` confirmation not completed |
| Endless validation loop on a field | No retry cap exists; the LLM re-asks forever within its 5-iteration/turn budget | field's `validation_rules` | add an attempts counter (set_variables) + escape exit; soften the rule |
| Turn ends abruptly mid-chain, later nodes skipped | 20-node chain-depth cap hit (`max_chain_depth`) | backend log with `flow.max_chain_depth` | break cycles of non-interactive nodes |
| Agent greets even though the user spoke first (or vice versa) | `skip_if_user_starts` on the start node; greeting layer precedence (node → global → agent) | start node data + global_settings | set the intended layer |
| Router start node answers "Unable to route. Please try again." | `router_mode:true`, no exit matched, no `no_match_message` configured | start node exits + condition_evaluations | add a fallback exit / set `no_match_message` |
| Transfer goes to the wrong target / does nothing off-hours | Targets sorted by priority, first true condition wins, else first **unconditional**; off-hours redirects to the schedule's `target_node_id` (silently disabled if that node was deleted) | `data.targets`, `global_settings.transfers` | order targets, keep one unconditional, keep redirect target alive |
| Voice agent fails to load entirely | `channel:"voice"` without `voice_settings` fails Pydantic parse | flow_definition JSON | add `voice_settings` |
| Saving the flow keeps failing with 409 | `flow_version` optimistic-lock conflict — someone else (or another tab) saved | PATCH response body (current vs expected, modifier) | reload, re-apply, resend with fresh `X-Expected-Flow-Version` |
| KB answers vanish on one node only | Node-level `kb_mode:"disabled"` / stale `kb_ids` whitelist; or `dataset_ids: []` disabling datasets | node data vs `knowledge_bases[]` | null the override to inherit |
| Fast replies / trigger messages stopped following per-tool modes | `tools.tool_ack_mode_enabled: true` ignores the per-tool `*_fast_reply_mode` fields (INS-2930) | ToolsConfig | pick one regime deliberately |
| Enum variable never equals the configured option | LLM returned a variant; fuzzy coercion stores the **canonical option key** — compare against that exact key (case/spacing of the option list) | variable value vs `options` | compare with the canonical key |
| Imported/restored agent: collect DTMF silently off | `collect_dtmf` read-default is false; the backfill migration didn't touch old exports/version snapshots | collect node voice_config | re-enable per node |

---

## Debugging Checklist

When a flow misbehaves, work through this order (each step names the artifact to inspect):

1. **Load the flow JSON** (`agents.flow_definition` or the published version snapshot). Confirm which one the conversation actually ran — draft vs published (`published_version_id`), and `flow_version` at the time.
2. **Locate the failing turn** in the conversation's state/transcript: `current_state.turns[i]` — note `node_id`, `input.content`, `output.node_content_segments` (which node said what).
3. **Verify the node the flow was on** matches expectations: `graph_state.current_node_id` and `node_history` for the transition trail.
4. **Trace exit evaluation for that turn**: `turn.processing.condition_evaluations` — for each exit: `condition_type`, `expression`, `resolved_expression` (variables already substituted — this is the ground truth), `result`, `skipped` + `skip_reason`, and which `target_node_id` won. Remember the type order (expression → result → always) and that `llm` exits won't appear here (look in `tool_executions` for `check_exits`/`goto_node`).
5. **Check variable values at that moment**: `current_state.variables` (+ `turn.processing.variable_changes` for what changed this turn, with old/new values and source). Watch for: unset vs `''` vs `None`-tombstone, string-typed numbers, dicts needing dot-notation, misspelled names (compare with `flow.variables`).
6. **Collect node?** Inspect `graph_state.collect_state[node_id]` per field (`collected`/`confirmed`/`value`/`set_at_turn`) and `graph_state.awaiting_confirmation`; check `field_validations` rules and the save_fields tool result in `tool_executions` for `{failed: …}` validation errors.
7. **Tool/API step?** `turn.processing.tool_executions` — request URL/method/body, response status, error; then `_last_api_success`/`_last_api_error` (or `_last_code_*`, `_last_function_*`) in variables; confirm `result_variable` landed and nested paths used downstream exist in the actual response shape.
8. **LLM behavior odd?** `turn.processing.llm_calls` — the exact system prompt assembled (checks `use_agent_prompt`, node prompt, injected variables per `include_all_variables`, RAG context) and any `llm_override` model differences.
9. **Routing dead ends**: for the current node list all exits (node.exits + flow.exits filtered by `source_node_id`), confirm every `target_node_id` exists in `flow.nodes`, an `always` fallback exists, and priorities are unique.
10. **Chain issues**: if later nodes silently didn't run, check backend logs for the `max_chain_depth` (20) abort or for `exit-target-missing` warnings.
11. **Still stuck?** Re-validate the flow (frontend validation or `AgentFlow.model_validate` / SDK `validate_flow.py`) — self-healing loaders may be masking a config that silently degraded (auto-disabled toggles, filtered KB refs, dropped rows).

---

## Source Map

Provenance only — the guide stands alone without these files.

| Concept | Platform file(s) |
|---|---|
| Top-level flow schema, GlobalSettings, VoiceSettings, ToolsConfig, KnowledgeBaseReference, FlowGraph | `backend/app/schemas/flow/agent_flow.py` |
| All node types, FieldDefinition, ValidationRule, `BuiltInValidationType`, NodeToolConfig, interactive sections | `backend/app/schemas/flow/nodes.py` |
| Exit + 4 condition types, expression save-time validation | `backend/app/schemas/flow/exits.py` |
| VariableDefinition/VariableValue, reserved names, type coercion | `backend/app/schemas/flow/variables.py` |
| Working-hours / transfer schedules; SMS config | `backend/app/schemas/flow/working_hours.py`, `sms.py` |
| Main loop, pre/post-node exit timing, chain depth, turn saving | `backend/app/services/orchestrator/flow_orchestrator.py` |
| Exit evaluation order, expression evaluator grammar | `backend/app/services/orchestrator/exit_evaluators/__init__.py` |
| Executor base contract, `{{var}}` template resolution, `system__*` variables | `backend/app/services/orchestrator/node_executors/base.py` |
| Per-node executors | `backend/app/services/orchestrator/node_executors/{start,conversation,collect,api,code,function,set_variables,end,transfer_to_human}.py` |
| Conversation state / turn records (condition_evaluations, llm_calls, …) | `backend/app/schemas/state/` (`conversation_state.py`, `turn.py`) |
| Start trigger constant | `backend/app/constants.py` (`START_TRIGGER_MESSAGE = "__start__"`) |
| Default LLM model/provider | `backend/app/models_config.py` (`DEFAULT_MODEL_ID`) |
| Persistence model & API routes (PATCH/versions/publish/export/import) | `backend/app/models/agent.py`, `backend/app/api/agents.py`, `backend/app/schemas/agent.py` |
| Programmatic builder SDK & offline validator | `backend/flow_builder_sdk/insait_flow_sdk.py`, `validate_flow.py` |
| Editor validation rules | `frontend/src/lib/agent-builder/flow-validation.ts` |
| Node type registry (canvas) | `frontend/src/lib/flow-builder/node-types.ts` |
| Builder TS types, variable suggestions, validator param UI | `frontend/src/lib/agent-builder/types.ts`, `variables.ts`, `validators.ts`, `normalize-variable.ts` |
| Node config panels (per-type UI fields) | `frontend/src/components/agent-builder/panels/NodeConfigPanel.tsx`, `panels/node-config/Node*Config.tsx` |
| Real-time-assistant graph engine | `backend/app/services/real_time_assistant/` |