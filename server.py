"""server.py — Insight Bug Fixer MCP server entry point.

Data-provider design (matches the Insait Platform MCP): the server never calls
an LLM. It gathers data and returns it to the MCP client (Claude), which does
the analysis; a second tool saves the client's analysis. No ANTHROPIC_API_KEY.

Tools:
  generate_bug_report  — gather agent/transcript/interactions + knowledge,
                         return the analysis context + a report_id.
  save_bug_report      — write the client-produced analysis to a report file.
  save_dev_feedback    — append a developer correction to golden_examples.md.

The Insait REST data flow (section 5, steps 2-6), report format (step 8), and
golden_examples format (step 9) are unchanged; the Opus 4.8 call (step 7) is
removed. A None node name renders as "(unresolved)".
"""

import asyncio
import json
import os
import re
from datetime import datetime

import httpx
from dotenv import load_dotenv

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
load_dotenv()

INSAIT_API_KEY = os.environ.get("INSAIT_API_KEY")
INSAIT_BASE_URL = os.environ.get("INSAIT_BASE_URL", "https://api-platform.insait.io")
BUGFIXER_OUTPUT_DIR = os.environ.get("BUGFIXER_OUTPUT_DIR")
BUGFIXER_KB_DIR = os.environ.get("BUGFIXER_KB_DIR")

# ---------------------------------------------------------------------------
# Section 3 — Session start instructions (named MCP prompt)
# Adapted from the spec for the data-provider design: the client (Claude)
# performs the analysis, so the flow is gather -> analyze -> save.
# ---------------------------------------------------------------------------
BUG_FIXER_INSTRUCTIONS = """You are the Insight Bug Fixer assistant. Your job is to help developers
analyze and fix bugs in the Insait platform's bot builder (UI/flow level only).

When a developer asks you to analyze a bug, you MUST collect all three of the
following before calling generate_bug_report. Do not call the tool if any are
missing — ask for them one at a time if needed:

  1. agent_id       — the agent's unique ID (found in the platform URL when
                      viewing the agent, looks like a UUID or short alphanumeric ID)
  2. bug_description — a detailed explanation of what went wrong, what the bot
                      did, and what was expected instead. The more detail, the
                      better the analysis. Do not accept a one-line description
                      — ask the developer to elaborate if it's too vague.
  3. conversation_id — the specific conversation ID where the bug occurred
                      (found in the platform UI conversation detail page URL)

Once you have all three, call generate_bug_report. It does not analyze the bug
itself — it returns a report_id and a context block (conversation trace, agent
configuration, and knowledge base). YOU perform the analysis: read that context
and produce the Root Cause, Solution A, and (only if genuinely different and
valuable) Solution B, following the instructions in the returned context. Then
call save_bug_report with that report_id and your analysis text to write the
report.

After the report is saved, stay in the session. If the developer tells you the
root cause or solution is wrong, ask them to explain the correct fix, then call
save_dev_feedback with their correction and the report_id from this session.

You only analyze UI-level and bot-builder-level issues. You do not debug
platform backend code, infrastructure, or anything outside the flow/node
configuration. If a bug is clearly a backend/infra issue, say so directly
and suggest escalating to the right team."""

# ---------------------------------------------------------------------------
# Section 4 — Tool description strings.
# save_dev_feedback is verbatim from the spec; generate_bug_report is adapted
# (it now gathers and returns context), and save_bug_report is new.
# ---------------------------------------------------------------------------
GENERATE_BUG_REPORT_DESCRIPTION = (
    "Gathers everything needed to analyze a bug in an Insait platform bot and returns it for YOU to analyze.\n"
    "\n"
    "This tool does NOT analyze the bug or write the report. It fetches the conversation transcript, execution trace, agent configuration, and knowledge base, and returns a report_id plus a context block. You then read the context, produce the Root Cause / Solution A / Solution B analysis, and call save_bug_report with the report_id and your analysis.\n"
    "\n"
    "BEFORE CALLING THIS TOOL — confirm all three inputs are present:\n"
    "  • agent_id: the unique ID of the agent (from the platform URL). Ask the developer if missing.\n"
    "  • bug_description: a detailed explanation of the bug — what happened, what was expected, which flow/node seems involved. If the developer gave a vague one-liner, ask them to elaborate before calling.\n"
    "  • conversation_id: the specific conversation ID where the bug occurred (from the platform UI). Ask the developer if missing.\n"
    "\n"
    "Do NOT call this tool with placeholder, empty, or guessed values. Do NOT call it until all three are explicitly provided by the developer.\n"
    "\n"
    "Keep the returned report_id in context — it is required for save_bug_report and for save_dev_feedback."
)


SAVE_BUG_REPORT_DESCRIPTION = (
    "Writes YOUR bug analysis to a local Markdown report file.\n"
    "\n"
    "Call this after generate_bug_report, once you have produced the analysis (Root Cause, Solution A, and Solution B if warranted) from the context that generate_bug_report returned.\n"
    "\n"
    "Requires the report_id returned by generate_bug_report in this session and your analysis text. On success, returns the report_id and the path where the report was saved. Keep the report_id — it is needed if the developer wants to submit a correction via save_dev_feedback."
)

SAVE_DEV_FEEDBACK_DESCRIPTION = (
    "Saves a developer correction to the local golden_examples.md file and writes a feedback record to the local output folder.\n"
    "\n"
    "Call this ONLY when:\n"
    "  • A bug report has already been generated and saved in this session AND\n"
    "  • The developer has explicitly told you the root cause or solution in the report was wrong AND\n"
    "  • The developer has explained what the correct fix actually is\n"
    "\n"
    "Do NOT call this speculatively or before the developer has given you the actual correct answer.\n"
    "\n"
    "Requires the report_id returned by the generate_bug_report call in this session."
)

# ---------------------------------------------------------------------------
# REST calls — section 5, steps 2-4
# ---------------------------------------------------------------------------
# Spec does not specify a request timeout; use a sane default. Full timeout
# handling / messaging is added in PHASE 8.
REQUEST_TIMEOUT = 30.0  # seconds

# Safety cap on the interactions pagination loop. The spec has no reliable
# has_more/total field (Known Open Item #1), so this guards against an
# unbounded loop if the endpoint ignores `offset`.
MAX_INTERACTION_PAGES = 1000


def _headers() -> dict:
    return {"X-API-Key": INSAIT_API_KEY or ""}


def _error(message: str):
    """Return a tool result carrying a clear error message to the chat."""
    return [types.TextContent(type="text", text=message)]


def _first_missing(fields: dict):
    """Return the name of the first missing/blank required field, or None."""
    for name, value in fields.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            return name
    return None


def _build_node_name_map(flow_definition) -> dict:
    """Build node_id -> human-readable node name map from flow_definition.

    flow_definition's exact schema is not documented in the spec, so this is
    defensive: it accepts a `nodes` list (or dict of nodes) and tries common
    id/name keys.
    """
    node_map: dict = {}
    if not isinstance(flow_definition, dict):
        return node_map
    nodes = flow_definition.get("nodes")
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    if not isinstance(nodes, list):
        return node_map
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id") or node.get("node_id") or node.get("nodeId")
        name = node.get("name") or node.get("label") or node.get("title")
        if node_id is not None and name is not None:
            node_map[str(node_id)] = name
    return node_map


def _extract_agent_name(agent_data, agent_id: str) -> str:
    """Extract the agent/project name for folder naming; fall back to agent_id."""
    if isinstance(agent_data, dict):
        for key in ("name", "agent_name", "project_name", "display_name"):
            value = agent_data.get(key)
            if value:
                return value
    return agent_id


def _extract_interaction_list(payload):
    """Pull the list of interactions out of a page payload.

    The response shape is not documented, so accept either a bare list or a
    dict wrapping the list under a common key.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("interactions", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


async def _fetch_agent(agent_id: str):
    """Step 2: GET /api/v1/agents/{agent_id}."""
    url = f"{INSAIT_BASE_URL}/api/v1/agents/{agent_id}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=_headers())
        response.raise_for_status()
        return response.json()


async def _fetch_transcript(conversation_id: str):
    """Step 3: GET /api/v1/conversations/{conversation_id}/transcript.

    Served as a file download (Content-Disposition: attachment); the body is
    read and parsed as JSON. No pagination — a single call is always complete.
    """
    url = f"{INSAIT_BASE_URL}/api/v1/conversations/{conversation_id}/transcript"
    params = {"include_tools": "true", "format": "json"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=_headers(), params=params)
        response.raise_for_status()
        return response.json()


async def _fetch_interactions(conversation_id: str, organization_id, node_map: dict):
    """Step 4: GET .../interactions, paginated by incrementing `offset`.

    Loops until a short or empty page is returned (accepted heuristic — no
    reliable has_more/total field). Resolves each interaction's node_id to a
    node name using the map from Step 2.

    On a fetch error (e.g. timeout) the loop stops with whatever was collected
    so far and a partial-data warning is returned instead of failing (step 4:
    do not fail silently). Returns (interactions, partial_warning_or_None).
    """
    url = f"{INSAIT_BASE_URL}/api/v1/chat/conversations/{conversation_id}/interactions"
    interactions: list = []
    offset = 0
    page_size = None
    pages_fetched = 0
    partial_warning = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        while True:
            params = {
                "organization_id": organization_id,
                "offset": offset,
                "include_debug": "true",
            }
            try:
                response = await client.get(url, headers=_headers(), params=params)
                response.raise_for_status()
                page = _extract_interaction_list(response.json())
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                partial_warning = (
                    f"Interactions fetch did not complete ({type(exc).__name__}); "
                    "the report is based on partial execution-trace data."
                )
                break

            pages_fetched += 1

            if not page:
                break  # empty page — done

            for interaction in page:
                if isinstance(interaction, dict):
                    node_id = interaction.get("node_id") or interaction.get("nodeId")
                    interaction["node_name"] = (
                        node_map.get(str(node_id)) if node_id is not None else None
                    )
                interactions.append(interaction)

            if page_size is None:
                page_size = len(page)
            if len(page) < page_size:
                break  # short page — last page

            offset += len(page)
            if pages_fetched >= MAX_INTERACTION_PAGES:
                break  # safety cap

    return interactions, partial_warning


# ---------------------------------------------------------------------------
# Merge + knowledge loading — section 5, steps 5-6
# ---------------------------------------------------------------------------
# Per-turn debug-metadata fields (step 5). Each value is the list of candidate
# keys tried, in order — the interaction/turn_events schema is undocumented
# (Known Open Item #4), so extraction is defensive.
DEBUG_METADATA_FIELDS = {
    "exits": ["exits"],
    "variables": ["variables"],
    "rag_chunks": ["rag_chunks", "rag", "chunks"],
    "tools": ["tools"],
    "code": ["code"],
    "security": ["security"],
    "errors": ["errors"],
}

KNOWLEDGE_FILES = {
    "claude_sessions_knowledge": "claude_sessions_knowledge.md",
    "platform_best_practices": "platform_best_practices.md",
    "golden_examples": "golden_examples.md",
}


def _extract_transcript_messages(transcript):
    """Pull the ordered list of messages out of the transcript payload."""
    if isinstance(transcript, list):
        return transcript
    if isinstance(transcript, dict):
        for key in ("messages", "transcript", "data", "items"):
            value = transcript.get(key)
            if isinstance(value, list):
                return value
    return []


def _extract_message_text(message):
    """Extract human-readable text from a transcript message (defensive)."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        for key in ("text", "content", "message", "body"):
            value = message.get(key)
            if isinstance(value, str):
                return value
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            if parts:
                return "\n".join(parts)
    return ""


def _extract_debug_metadata(turn_events, interaction):
    """Extract per-turn debug metadata (exits, variables, rag chunks, tools,
    code, security, errors) from turn_events / the interaction."""
    metadata = {}
    sources = [s for s in (turn_events, interaction) if isinstance(s, dict)]
    for canonical, aliases in DEBUG_METADATA_FIELDS.items():
        value = None
        for src in sources:
            for alias in aliases:
                if src.get(alias) is not None:
                    value = src.get(alias)
                    break
            if value is not None:
                break
        metadata[canonical] = value
    # If turn_events is not a dict (e.g. a list of events), preserve it raw.
    if turn_events is not None and not isinstance(turn_events, dict):
        metadata["turn_events"] = turn_events
    return metadata


def _build_turn(seq, message, interaction):
    """Build one unified per-turn record (message text + node name + debug)."""
    turn = {
        "seq": seq,
        "message_text": _extract_message_text(message),
        "node_name": interaction.get("node_name") if isinstance(interaction, dict) else None,
    }
    turn_events = interaction.get("turn_events") if isinstance(interaction, dict) else None
    if turn_events is None:
        # Noted as "no execution-side activity" — not missing data (step 5).
        turn["execution"] = None
    else:
        turn["execution"] = _extract_debug_metadata(turn_events, interaction)
    return turn


def _merge_transcript_interactions(transcript, interactions):
    """Step 5: join transcript messages with interactions by turn sequence.

    interactions carry a `seq` field aligned to message order in the transcript
    (numeric-string seq values are coerced to int for the index join). This
    join is not yet verified against real data (Known Open Item #5), so any
    interaction whose seq does not match a message index is still surfaced as
    its own turn rather than dropped.
    """
    messages = _extract_transcript_messages(transcript)

    by_seq = {}
    for interaction in interactions:
        if not isinstance(interaction, dict):
            continue
        seq = interaction.get("seq")
        if seq is None:
            continue
        key = seq
        try:
            key = int(seq)
        except (TypeError, ValueError):
            pass
        by_seq[key] = interaction

    turns = []
    consumed = set()
    for index, message in enumerate(messages):
        interaction = by_seq.get(index)
        if interaction is not None:
            consumed.add(index)
        turns.append(_build_turn(index, message, interaction))

    for key, interaction in by_seq.items():
        if key not in consumed:
            turns.append(_build_turn(key, None, interaction))

    turns.sort(key=lambda t: t["seq"] if isinstance(t["seq"], int) else len(messages))
    return turns


def _read_kb_file(path, missing_ok=False):
    if missing_ok and not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _load_knowledge():
    """Step 6: read the three local knowledge files (full content).

    golden_examples.md may not exist yet — inject an empty string silently.
    """
    kb_dir = BUGFIXER_KB_DIR or ""
    return {
        "claude_sessions_knowledge": _read_kb_file(
            os.path.join(kb_dir, KNOWLEDGE_FILES["claude_sessions_knowledge"])
        ),
        "platform_best_practices": _read_kb_file(
            os.path.join(kb_dir, KNOWLEDGE_FILES["platform_best_practices"])
        ),
        "golden_examples": _read_kb_file(
            os.path.join(kb_dir, KNOWLEDGE_FILES["golden_examples"]), missing_ok=True
        ),
    }


# ---------------------------------------------------------------------------
# Analysis context — section 5, step 7 (composed here, analyzed by the client)
# ---------------------------------------------------------------------------
# The server no longer calls a model. This template (verbatim from step 7) is
# returned to the MCP client (Claude), which performs the analysis.
ANALYSIS_PROMPT_TEMPLATE = """You are an expert Insait platform bot debugger. Analyze the following bug report
and provide:
1. Root Cause — what specifically caused this bug at the flow/node/configuration level.
2. Solution A — a fix tailored to this specific flow's context.
3. Solution B — only if there is a genuinely different and valuable general best-practice
   fix that differs meaningfully from Solution A. If Solution B would just restate
   Solution A, omit it entirely. Do not invent a second solution to fill a template.

Scope: UI and bot-builder level only (flow configuration, nodes, prompts, code blocks,
tool configurations). Do not suggest backend/infrastructure fixes.

--- BUG DESCRIPTION ---
{bug_description}

--- CONVERSATION TRANSCRIPT WITH EXECUTION TRACE ---
{merged_per_turn_data}

--- AGENT SYSTEM PROMPT ---
{system_prompt}

--- AGENT SECURITY PROMPT ---
{security_prompt}

--- FLOW DEFINITION (node structure) ---
{flow_definition_summary}

--- PLATFORM BEST PRACTICES ---
{platform_best_practices}

--- PAST SESSION KNOWLEDGE ---
{claude_sessions_knowledge}

--- APPROVED GOLDEN EXAMPLES ---
{golden_examples}"""


def _serialize(data) -> str:
    """JSON-serialize structured data for inclusion in the analysis prompt."""
    try:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)


def _render_node_name(node_name) -> str:
    """Render a node name for output; None/empty -> '(unresolved)'."""
    return node_name if node_name else "(unresolved)"


def _build_analysis_context(
    bug_description,
    merged_turns,
    system_prompt,
    security_prompt,
    flow_definition,
    knowledge,
) -> str:
    """Step 7: compose the analysis context string returned to the client.

    No model call — the MCP client (Claude) performs the analysis.
    """
    # Render unresolved node names as "(unresolved)" rather than a bare None.
    turns_for_prompt = [
        {**turn, "node_name": _render_node_name(turn.get("node_name"))}
        if isinstance(turn, dict)
        else turn
        for turn in merged_turns
    ]
    return ANALYSIS_PROMPT_TEMPLATE.format(
        bug_description=bug_description or "",
        merged_per_turn_data=_serialize(turns_for_prompt),
        system_prompt=system_prompt or "",
        security_prompt=security_prompt or "",
        flow_definition_summary=_serialize(flow_definition),
        platform_best_practices=knowledge.get("platform_best_practices", ""),
        claude_sessions_knowledge=knowledge.get("claude_sessions_knowledge", ""),
        golden_examples=knowledge.get("golden_examples", ""),
    )


# ---------------------------------------------------------------------------
# Report writing — section 5, step 8
# ---------------------------------------------------------------------------
# Relative count difference above which a pagination/merge mismatch warning is
# added to the report header (step 4 cross-check). Threshold not specified by
# the spec.
COUNT_MISMATCH_RATIO = 0.2

# In-memory registry of reports generated this session, keyed by report_id.
# save_dev_feedback (PHASE 7) reads the original root cause from here — the
# tool only receives a report_id, and the report's file path is not derivable
# from report_id alone (the folder is the agent name, not the agent_id).
REPORTS: dict = {}


def _safe_folder_name(name) -> str:
    """Make an agent/project name safe to use as a folder name."""
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", str(name)).strip()
    return safe or "unknown"


def _pagination_warnings(interaction_count: int, transcript_message_count: int) -> list:
    """Step 4 cross-check: warn if the interaction count differs significantly
    from the transcript message count."""
    warnings = []
    larger = max(interaction_count, transcript_message_count)
    if larger > 0 and abs(interaction_count - transcript_message_count) / larger > COUNT_MISMATCH_RATIO:
        warnings.append(
            f"Interaction count ({interaction_count}) differs significantly from "
            f"transcript message count ({transcript_message_count}); data may be "
            "incomplete (pagination heuristic — Known Open Item #1)."
        )
    return warnings


def _extract_root_cause(analysis: str) -> str:
    """Best-effort extraction of the Root Cause section from the analysis text.

    The analysis is free-form model output, so this is heuristic: capture from
    the first line mentioning 'root cause' until a line mentioning 'solution'.
    """
    if not analysis:
        return ""
    captured = []
    capturing = False
    for line in analysis.splitlines():
        lower = line.lower()
        if not capturing:
            if "root cause" in lower:
                capturing = True
                captured.append(line)
            continue
        if "solution" in lower:
            break
        captured.append(line)
    text = "\n".join(captured).strip()
    return text or analysis.strip()


def _build_report_markdown(agent_id, conversation_id, timestamp, report_id, analysis, warnings) -> str:
    lines = [
        "# Bug Report",
        "",
        f"- **report_id:** {report_id}",
        f"- **agent_id:** {agent_id}",
        f"- **conversation_id:** {conversation_id}",
        f"- **timestamp:** {timestamp}",
    ]
    if warnings:
        lines.append("- **warnings:**")
        lines.extend(f"  - {warning}" for warning in warnings)
    lines.extend(["", "## Analysis", "", analysis or "", ""])
    return "\n".join(lines)


def _write_bug_report(agent_id, agent_name, conversation_id, timestamp, report_id, analysis, warnings) -> str:
    """Create the output folder and write bug_report_{timestamp}.md."""
    folder = os.path.join(BUGFIXER_OUTPUT_DIR or "", _safe_folder_name(agent_name))
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"bug_report_{timestamp}.md")
    content = _build_report_markdown(
        agent_id, conversation_id, timestamp, report_id, analysis, warnings
    )
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return file_path


async def generate_bug_report(agent_id, bug_description, conversation_id):
    """Gather the data for a bug report and return it for the client to analyze
    (section 5, steps 1-6), with error handling per section 8.

    Does NOT call a model and does NOT write the report. It mints a report_id,
    composes the analysis context (step 7 template), stores what save_bug_report
    and save_dev_feedback will need, and returns the context to the client.
    """
    # Step 1: validate inputs — do not call any REST endpoint if one is missing.
    missing = _first_missing(
        {
            "agent_id": agent_id,
            "bug_description": bug_description,
            "conversation_id": conversation_id,
        }
    )
    if missing:
        return _error(
            f"Missing required input: {missing}. Please provide it before I can "
            "analyze this bug."
        )

    # Step 2: fetch agent data + derive node map and agent name.
    try:
        agent_data = await _fetch_agent(agent_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error("Agent ID not found. Please check the agent_id and try again.")
        return _error(
            "Failed to fetch agent data from the Insait platform "
            f"(HTTP {exc.response.status_code})."
        )
    except httpx.TimeoutException:
        return _error(
            "Timed out fetching agent data from the Insait platform. Please try again."
        )
    except httpx.HTTPError as exc:
        return _error(f"Failed to fetch agent data from the Insait platform: {exc}")

    system_prompt = agent_data.get("system_prompt") if isinstance(agent_data, dict) else None
    security_prompt = agent_data.get("security_prompt") if isinstance(agent_data, dict) else None
    flow_definition = agent_data.get("flow_definition") if isinstance(agent_data, dict) else None
    organization_id = agent_data.get("organization_id") if isinstance(agent_data, dict) else None
    node_map = _build_node_name_map(flow_definition)
    agent_name = _extract_agent_name(agent_data, agent_id)

    # Step 3: fetch transcript (single call, no pagination).
    try:
        transcript = await _fetch_transcript(conversation_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(
                "Conversation ID not found. Please check the conversation_id and "
                "try again."
            )
        return _error(
            "Failed to fetch the conversation transcript "
            f"(HTTP {exc.response.status_code})."
        )
    except httpx.TimeoutException:
        return _error("Timed out fetching the conversation transcript. Please try again.")
    except httpx.HTTPError as exc:
        return _error(f"Failed to fetch the conversation transcript: {exc}")

    # Step 4: fetch interactions (paginated). On a fetch error, proceed with
    # partial data and record a warning (do not fail silently).
    interactions, partial_warning = await _fetch_interactions(
        conversation_id, organization_id, node_map
    )

    # Step 5: merge transcript + interactions into a per-turn structure.
    merged_turns = _merge_transcript_interactions(transcript, interactions)

    # Step 6: load local knowledge files.
    knowledge = _load_knowledge()

    # Step 7: compose the analysis context (no model call — the client analyzes).
    analysis_context = _build_analysis_context(
        bug_description,
        merged_turns,
        system_prompt,
        security_prompt,
        flow_definition,
        knowledge,
    )

    # Mint report_id and record what save_bug_report / save_dev_feedback will
    # need. The report file is written later by save_bug_report.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_id = f"{agent_id}_{timestamp}"
    transcript_message_count = len(_extract_transcript_messages(transcript))
    warnings = _pagination_warnings(len(interactions), transcript_message_count)
    if partial_warning:
        warnings.append(partial_warning)

    REPORTS[report_id] = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "conversation_id": conversation_id,
        "timestamp": timestamp,
        "bug_description": bug_description,
        "warnings": warnings,
        "analysis": None,
        "root_cause": None,
        "file_path": None,
    }

    return [
        types.TextContent(
            type="text",
            text=(
                f"report_id: {report_id}\n\n"
                "Analyze the bug using the context below, then call "
                "save_bug_report with this report_id and your analysis "
                "(Root Cause, Solution A, and Solution B only if warranted).\n\n"
                f"{analysis_context}"
            ),
        )
    ]


async def save_bug_report(report_id, analysis):
    """Write the client-produced analysis to a report file (section 5, step 8).

    Looks up the gathered context stored by generate_bug_report. Error handling
    per section 8.
    """
    missing = _first_missing({"report_id": report_id, "analysis": analysis})
    if missing:
        return _error(
            f"Missing required input: {missing}. Please provide it before I can "
            "save this report."
        )

    report = REPORTS.get(report_id)
    if report is None:
        return _error(
            f"Unknown report_id '{report_id}'. Run generate_bug_report in this "
            "session first, then use the report_id it returned."
        )

    agent_id = report.get("agent_id", "")
    agent_name = report.get("agent_name", "")
    conversation_id = report.get("conversation_id", "")
    timestamp = report.get("timestamp", datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    warnings = report.get("warnings", [])
    root_cause = _extract_root_cause(analysis)

    # Attempt the local write; on failure, fall back to returning the report
    # content directly (section 8).
    save_error = None
    try:
        file_path = _write_bug_report(
            agent_id, agent_name, conversation_id, timestamp, report_id, analysis, warnings
        )
    except OSError as exc:
        save_error = exc
        file_path = None

    report["analysis"] = analysis
    report["root_cause"] = root_cause
    report["file_path"] = file_path

    if save_error is not None:
        content = _build_report_markdown(
            agent_id, conversation_id, timestamp, report_id, analysis, warnings
        )
        return _error(
            f"WARNING: could not save the report locally ({save_error}). "
            f"report_id: {report_id}\n\nReport content follows:\n\n{content}"
        )

    summary = root_cause.strip()
    if len(summary) > 300:
        summary = summary[:300].rstrip() + "…"

    return [
        types.TextContent(
            type="text",
            text=(
                f"Report saved.\n"
                f"- report_id: {report_id}\n"
                f"- path: {file_path}\n\n"
                f"Root cause summary:\n{summary}"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Feedback — section 5, step 9
# ---------------------------------------------------------------------------
# Header used when golden_examples.md is auto-created on first feedback
# (section 7.3).
GOLDEN_EXAMPLES_HEADER = "# Golden Examples — Approved Corrections\n\n---\n"


def _build_correction_block(
    report_id, timestamp, agent_id, bug_description, original_root_cause, user_correction
) -> str:
    """Exact golden_examples append format (section 5, step 9)."""
    return (
        f"## [{report_id}] — [{timestamp}]\n"
        f"**Agent:** {agent_id}\n"
        f"**Bug description:** {bug_description}\n"
        f"**Original root cause (wrong):** {original_root_cause}\n"
        f"**Correct fix:** {user_correction}\n"
        f"---\n"
    )


def _append_golden_example(block: str) -> None:
    """Append a correction block to golden_examples.md, creating the file with
    its header (section 7.3) if it does not exist yet."""
    path = os.path.join(BUGFIXER_KB_DIR or "", "golden_examples.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(GOLDEN_EXAMPLES_HEADER)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n" + block)


def _write_feedback_file(agent_name, timestamp, block: str) -> str:
    """Write feedback_{timestamp}.md (same content) to the agent's output folder."""
    folder = os.path.join(BUGFIXER_OUTPUT_DIR or "", _safe_folder_name(agent_name))
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"feedback_{timestamp}.md")
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(block)
    return file_path


async def save_dev_feedback(report_id, user_correction):
    """Step 9: append the developer's correction to golden_examples.md and
    write a feedback record, with error handling per section 8.

    Reads the original report context from the in-memory REPORTS registry
    (populated by generate_bug_report).
    """
    # Validate inputs.
    missing = _first_missing({"report_id": report_id, "user_correction": user_correction})
    if missing:
        return _error(
            f"Missing required input: {missing}. Please provide it before I can "
            "save this correction."
        )

    # The report must have been generated in this session.
    report = REPORTS.get(report_id)
    if report is None:
        return _error(
            f"Unknown report_id '{report_id}'. Run generate_bug_report in this "
            "session first, then use the report_id it returned."
        )

    agent_id = report.get("agent_id", "")
    agent_name = report.get("agent_name", "")
    bug_description = report.get("bug_description", "")
    original_root_cause = report.get("root_cause", "")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    block = _build_correction_block(
        report_id, timestamp, agent_id, bug_description, original_root_cause, user_correction
    )

    # Attempt the local writes; on failure, return the correction content
    # directly as a fallback (section 8).
    save_error = None
    try:
        _append_golden_example(block)
        _write_feedback_file(agent_name, timestamp, block)
    except OSError as exc:
        save_error = exc

    if save_error is not None:
        return _error(
            f"WARNING: could not save the feedback locally ({save_error}). "
            f"Correction content follows:\n\n{block}"
        )

    return [
        types.TextContent(
            type="text",
            text="Correction saved. It will be included as a reference in future bug analyses.",
        )
    ]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
app = Server("insight-bug-fixer")


@app.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="bug_fixer_instructions",
            description="Session start instructions for the Insight Bug Fixer assistant.",
            arguments=[],
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
    if name != "bug_fixer_instructions":
        raise ValueError(f"Unknown prompt: {name}")
    return types.GetPromptResult(
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=BUG_FIXER_INSTRUCTIONS),
            )
        ]
    )


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="generate_bug_report",
            description=GENERATE_BUG_REPORT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The unique identifier of the agent in the Insait platform",
                    },
                    "bug_description": {
                        "type": "string",
                        "description": "Detailed explanation of the bug — what happened, what was expected, which node/flow seems involved",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "The unique identifier of the conversation where the bug occurred",
                    },
                },
                "required": ["agent_id", "bug_description", "conversation_id"],
            },
        ),
        types.Tool(
            name="save_bug_report",
            description=SAVE_BUG_REPORT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The report_id returned by generate_bug_report in this session",
                    },
                    "analysis": {
                        "type": "string",
                        "description": "Your bug analysis: Root Cause, Solution A, and Solution B if warranted",
                    },
                },
                "required": ["report_id", "analysis"],
            },
        ),
        types.Tool(
            name="save_dev_feedback",
            description=SAVE_DEV_FEEDBACK_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The report_id returned by generate_bug_report in this session",
                    },
                    "user_correction": {
                        "type": "string",
                        "description": "The correct root cause and/or solution as explained by the developer",
                    },
                },
                "required": ["report_id", "user_correction"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "generate_bug_report":
        return await generate_bug_report(
            arguments.get("agent_id"),
            arguments.get("bug_description"),
            arguments.get("conversation_id"),
        )
    if name == "save_bug_report":
        return await save_bug_report(
            arguments.get("report_id"),
            arguments.get("analysis"),
        )
    if name == "save_dev_feedback":
        return await save_dev_feedback(
            arguments.get("report_id"),
            arguments.get("user_correction"),
        )
    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
