"""server.py — Insight Bug Fixer MCP server entry point.

Data-provider design (matches the Insait Platform MCP): the server never calls
an LLM. It gathers data and returns it to the MCP client (Claude), which does
the analysis; a second tool saves the client's analysis. No ANTHROPIC_API_KEY.

Tools:
  generate_bug_report  — gather agent/transcript/interactions + knowledge,
                         return the analysis context + a report_id.
  save_bug_report      — write the client-produced analysis to a report file.
  save_dev_feedback    — append a developer correction to golden_examples.md.
  get_knowledge        — fetch knowledge sections on demand (by title or tag).

Knowledge injection: the knowledge .md files are split into sections at any
heading followed by a `Tags:` line. Sections tagged `always` are inlined into
every analysis context; the rest appear as an index (title + tags) and are
auto-attached only when their tags lexically match the bug description —
anything else is fetchable via get_knowledge.

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
# Load .env sitting next to this file, regardless of the launcher's cwd (the
# MCP client may start the server from a different directory). Real env vars
# already set by the launcher are not overridden.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

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
configuration, and knowledge). YOU perform the analysis: read that context
and produce the Root Cause, Solution A, and (only if genuinely different and
valuable) Solution B, following the instructions in the returned context. The
context inlines the core knowledge plus the sections that matched this bug,
and a KNOWLEDGE INDEX of everything else — call get_knowledge for any indexed
section whose tags match the failure shape you are investigating. Then call
save_bug_report with that report_id and your analysis text to write the
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

GET_KNOWLEDGE_DESCRIPTION = (
    "Fetches local knowledge sections (platform guide sections, verified bug patterns) by section title or by tag.\n"
    "\n"
    "The context returned by generate_bug_report contains a KNOWLEDGE INDEX listing every available section with its tags. Sections whose tags matched the bug are already attached to that context — call this tool for any OTHER section the index shows is relevant to the bug you are analyzing (e.g. its tags match the failure shape you are now suspecting).\n"
    "\n"
    "Provide `sections` (exact or partial section titles) and/or `tags` (exact tag strings from the index). Returns the full text of every matching section. Can be called at any time, including before generate_bug_report."
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
    """Build node_id -> human-readable node name map.

    Verified against the live Insait API: nodes live at
    flow_definition["flow"]["nodes"] (a dict keyed by node_id, each value a node
    with a `name`). Falls back to a top-level "nodes" for robustness.
    """
    node_map: dict = {}
    if not isinstance(flow_definition, dict):
        return node_map
    flow = flow_definition.get("flow")
    nodes = flow.get("nodes") if isinstance(flow, dict) else None
    if nodes is None:
        nodes = flow_definition.get("nodes")  # fallback
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


# debug_info fields kept in the execution trace (spec step-5 signals). Other
# fields are dropped. Verified live: these are the meaningful execution signals;
# the bulky `rag` chunk text is slimmed separately (see _trim_debug_info).
DEBUG_INFO_KEEP = (
    "exits",
    "transition_exit_id",
    "variables",
    "field_extractions",
    "security",
    "errors",
    "tools",
    "code_executions",
    "rag",
)


# Full-text fields dropped from each kept RAG chunk (they carry the document
# body; metadata like document_name/score is kept).
RAG_CHUNK_DROP = ("content", "enriched_text")

# Lightweight fields kept for NON-taken exit candidates (the taken exit is kept
# in full). Drops the bulky `condition` expression from candidates.
EXIT_SLIM_KEEP = ("id", "name", "priority", "target_node_id")


def _slim_exits(exits, taken_id):
    """Keep the taken exit in full; reduce other candidates to light metadata."""
    if not isinstance(exits, list):
        return exits
    slim = []
    for e in exits:
        if not isinstance(e, dict):
            slim.append(e)
            continue
        if e.get("id") == taken_id:
            slim.append(e)  # taken exit — keep full
        else:
            slim.append({k: e[k] for k in EXIT_SLIM_KEEP if k in e})
    return slim


def _trim_debug_info(debug_info):
    """Keep the key execution-signal fields; strip bulky RAG and exit payloads.

    Verified live: `rag` dominates debug_info (~150KB) via `all_chunks` + full
    document text; `exits` (~22KB) is mostly non-taken branch conditions. We
    drop `all_chunks`, strip chunk full-text, and collapse non-taken exits to
    metadata — keeping "what was retrieved" and "which branch fired" without the
    bulk.
    """
    if not isinstance(debug_info, dict):
        return debug_info
    trimmed = {k: debug_info[k] for k in DEBUG_INFO_KEEP if k in debug_info}
    rag = trimmed.get("rag")
    if isinstance(rag, dict):
        slim_rag = {k: v for k, v in rag.items() if k != "all_chunks"}
        if isinstance(slim_rag.get("chunks"), list):
            slim_rag["chunks"] = [
                {k: v for k, v in chunk.items() if k not in RAG_CHUNK_DROP}
                if isinstance(chunk, dict) else chunk
                for chunk in slim_rag["chunks"]
            ]
        trimmed["rag"] = slim_rag
    if "exits" in trimmed:
        trimmed["exits"] = _slim_exits(trimmed["exits"], debug_info.get("transition_exit_id"))
    return trimmed


# Transcript = conversation only. Execution data (rag_chunks, tool_metadata)
# is dropped here because it is duplicated — trimmed — in execution_trace's
# debug_info. Keeps the transcript to the fields a reader needs.
TRANSCRIPT_KEEP = ("role", "content", "timestamp")


def _trim_transcript_message(message):
    """Reduce a transcript message to conversational fields only."""
    if not isinstance(message, dict):
        return message
    return {k: message[k] for k in TRANSCRIPT_KEEP if k in message}


def _merge_transcript_interactions(transcript, interactions):
    """Step 5: produce a complete conversation record for the client.

    The transcript (all messages) and the interactions (the execution trace)
    are different granularities and do not map 1:1 — verified against the live
    API (e.g. 13 interactions vs 25 messages; interactions use a non-unique
    `sequence_number`). So instead of a lossy join, return BOTH:
      {"transcript": [...conversation only...],
       "execution_trace": [...all interactions, node_name-enriched...]}
    Division of labor (no duplication): the transcript carries the conversation
    (role/content/timestamp); execution_trace carries the execution (node,
    debug_info). Each interaction's debug_info is trimmed to the key signals.
    """
    messages = _extract_transcript_messages(transcript)
    trimmed_transcript = [_trim_transcript_message(m) for m in messages]
    trace = []
    for i in interactions:
        if isinstance(i, dict):
            entry = dict(i)
            if "debug_info" in entry:
                entry["debug_info"] = _trim_debug_info(entry["debug_info"])
            trace.append(entry)
        else:
            trace.append(i)
    return {
        "transcript": trimmed_transcript,
        "execution_trace": trace,
    }


def _read_kb_file(path, missing_ok=False):
    if missing_ok and not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Knowledge sectioning, index, and auto-match
# ---------------------------------------------------------------------------
# A knowledge section starts at a markdown heading (#/##/###) that is followed,
# within the next 3 non-empty lines, by a `Tags: a, b, c` line (backticks/bold
# around it are tolerated). Untagged headings and their content belong to the
# enclosing tagged section — so section granularity is controlled from the .md
# files themselves, not from code. The special tag `always` marks a section as
# inlined into every analysis context; all other sections are index+on-demand.
KNOWLEDGE_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
KNOWLEDGE_TAGS_RE = re.compile(r"^[*_`\s]*Tags:\s*(.+?)[*_`\s]*$", re.IGNORECASE)

# Auto-match scoring: a full tag phrase found in the bug description scores 3,
# each distinct tag token found scores 1. Sections at or above MIN_SCORE are
# attached (top MAX_SECTIONS by score). Tokens shorter than MIN_TOKEN_LEN are
# ignored so generic words ("node", "bug") don't match everything.
KNOWLEDGE_MATCH_MIN_SCORE = 2
KNOWLEDGE_MATCH_MAX_SECTIONS = 5
KNOWLEDGE_MATCH_MIN_TOKEN_LEN = 4


def _norm_token(word: str) -> str:
    """Fold trivial plurals so 'links' matches 'link', 'breaks' matches
    'break'. Both sides of the comparison are normalized identically."""
    return word[:-1] if word.endswith("s") and len(word) > KNOWLEDGE_MATCH_MIN_TOKEN_LEN else word


def _parse_knowledge_sections(file_label: str, text: str) -> list:
    """Split a knowledge markdown file into tagged, addressable sections.

    Returns a list of {"file", "title", "tags", "content"} dicts, in file
    order. Content before the first tagged heading is dropped (tag the top
    heading `always` to keep a file preamble).
    """
    lines = text.split("\n")
    sections: list = []
    current = None
    in_code_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
        heading = KNOWLEDGE_HEADING_RE.match(line)
        if heading and not in_code_fence:
            tags = None
            non_empty_seen = 0
            for lookahead in lines[i + 1 : i + 8]:
                stripped = lookahead.strip()
                if not stripped:
                    continue
                # a new heading ends the lookahead — tags belong to the
                # nearest heading above them, never through another heading
                if KNOWLEDGE_HEADING_RE.match(stripped):
                    break
                tag_match = KNOWLEDGE_TAGS_RE.match(stripped)
                if tag_match:
                    tags = [
                        t.strip().lower()
                        for t in tag_match.group(1).split(",")
                        if t.strip()
                    ]
                    break
                non_empty_seen += 1
                if non_empty_seen >= 3:
                    break
            if tags is not None:
                if current is not None:
                    sections.append(current)
                current = {
                    "file": file_label,
                    "title": heading.group(2),
                    "tags": tags,
                    "content_lines": [line],
                }
                continue
        if current is not None:
            current["content_lines"].append(line)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["content"] = "\n".join(section.pop("content_lines")).strip()
    return sections


def _load_knowledge_sections():
    """Step 6: read the knowledge files as sections + golden examples text.

    Files are re-read on every call (they are small) so knowledge edits take
    effect without a server restart. golden_examples.md stays fully inlined:
    it holds approved corrections and is kept short by periodic distillation
    into the pattern files.
    """
    kb_dir = BUGFIXER_KB_DIR or ""
    sections: list = []
    for label in ("claude_sessions_knowledge", "platform_best_practices"):
        text = _read_kb_file(
            os.path.join(kb_dir, KNOWLEDGE_FILES[label]), missing_ok=True
        )
        sections.extend(_parse_knowledge_sections(label, text))
    golden = _read_kb_file(
        os.path.join(kb_dir, KNOWLEDGE_FILES["golden_examples"]), missing_ok=True
    )
    return sections, golden


def _match_knowledge_sections(sections: list, query_text: str) -> list:
    """Deterministic lexical auto-match of sections against the bug text.

    No embeddings, no LLM — the match is inspectable: score = 3 per full tag
    phrase present in the query + 1 per distinct tag token present.
    `always` sections are excluded (they are inlined separately).
    """
    query = re.sub(r"[^a-z0-9א-ת ]", " ", (query_text or "").lower())
    query_words = {_norm_token(w) for w in query.split()}
    scored = []
    for section in sections:
        if "always" in section["tags"]:
            continue
        score = 0
        seen_tokens: set = set()
        for tag in section["tags"]:
            phrase = tag.replace("-", " ")
            if len(phrase) >= KNOWLEDGE_MATCH_MIN_TOKEN_LEN and phrase in query:
                score += 3
            for token in phrase.split():
                token = _norm_token(token)
                if (
                    len(token) >= KNOWLEDGE_MATCH_MIN_TOKEN_LEN
                    and token not in seen_tokens
                    and token in query_words
                ):
                    seen_tokens.add(token)
                    score += 1
        if score >= KNOWLEDGE_MATCH_MIN_SCORE:
            scored.append((score, section))
    scored.sort(key=lambda item: -item[0])
    return [section for _, section in scored[:KNOWLEDGE_MATCH_MAX_SECTIONS]]


def _render_knowledge_sections(sections: list) -> str:
    return "\n\n".join(f"[{s['file']}]\n{s['content']}" for s in sections)


def _build_knowledge_index(sections: list, attached_titles: set) -> str:
    """One line per on-demand section: title, tags, and whether it is already
    attached to this context."""
    lines = []
    for section in sections:
        if "always" in section["tags"]:
            continue
        mark = "  [attached below]" if section["title"] in attached_titles else ""
        lines.append(f"- {section['title']}  (tags: {', '.join(section['tags'])}){mark}")
    return "\n".join(lines)


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

--- CORE KNOWLEDGE (always applicable) ---
{knowledge_always}

--- KNOWLEDGE INDEX ---
These additional knowledge sections exist locally. The ones whose tags matched
this bug are auto-attached below (marked). Before finalizing your analysis,
scan this index: if any OTHER section's tags match this bug's failure shape,
call get_knowledge (by section title or tags) and read it first.
{knowledge_index}

--- KNOWLEDGE MATCHED TO THIS BUG (auto-attached) ---
{knowledge_matched}

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
    merged,
    system_prompt,
    security_prompt,
    flow_for_prompt,
    knowledge,
) -> str:
    """Step 7: compose the analysis context string returned to the client.

    `merged` is {"transcript": [...], "execution_trace": [...]}.
    No model call — the MCP client (Claude) performs the analysis.
    """
    trace = merged.get("execution_trace", []) if isinstance(merged, dict) else []
    # Render unresolved node names as "(unresolved)" rather than a bare None.
    rendered_trace = [
        {**t, "node_name": _render_node_name(t.get("node_name"))}
        if isinstance(t, dict)
        else t
        for t in trace
    ]
    conversation = {
        "transcript": merged.get("transcript", []) if isinstance(merged, dict) else [],
        "execution_trace": rendered_trace,
    }
    return ANALYSIS_PROMPT_TEMPLATE.format(
        bug_description=bug_description or "",
        merged_per_turn_data=_serialize(conversation),
        system_prompt=system_prompt or "",
        security_prompt=security_prompt or "",
        flow_definition_summary=_serialize(flow_for_prompt),
        knowledge_always=knowledge.get("always", ""),
        knowledge_index=knowledge.get("index", ""),
        knowledge_matched=knowledge.get("matched", "(none matched — check the index)"),
        golden_examples=knowledge.get("golden_examples", ""),
    )


# ---------------------------------------------------------------------------
# Flow trimming — keep only the nodes the conversation touched (neighbors opt-in)
# ---------------------------------------------------------------------------
def _trim_flow(flow_definition, touched_ids, include_neighbors=False):
    """Return (trimmed_flow_definition, missing_node_ids).

    Keeps only the nodes the conversation touched (and, if include_neighbors,
    their direct neighbors one edge away via flow["exits"]); prunes the rest.
    flow["exits"] is still kept for any edge touching an included node, so the
    routing decisions from/into executed nodes are visible even when the
    neighbor node body is not included. All other flow data (variables, tools,
    settings, start_node_id, subflow_groups) is kept intact. Verified against
    the live API: nodes at flow["flow"]["nodes"], edges at flow["flow"]["exits"]
    with source_node_id / target_node_id.
    """
    if not isinstance(flow_definition, dict):
        return flow_definition, []
    flow = flow_definition.get("flow")
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), dict):
        return flow_definition, []

    nodes = flow["nodes"]
    exits = flow.get("exits") if isinstance(flow.get("exits"), list) else []

    touched = {t for t in touched_ids if t in nodes}
    missing = [t for t in dict.fromkeys(touched_ids) if t not in nodes]

    included = set(touched)
    if include_neighbors:
        for e in exits:
            if not isinstance(e, dict):
                continue
            s, t = e.get("source_node_id"), e.get("target_node_id")
            if s in touched or t in touched:
                if s is not None:
                    included.add(s)
                if t is not None:
                    included.add(t)

    trimmed_flow = dict(flow)
    trimmed_flow["nodes"] = {nid: nodes[nid] for nid in included if nid in nodes}
    trimmed_flow["exits"] = [
        e for e in exits
        if isinstance(e, dict)
        and (e.get("source_node_id") in included or e.get("target_node_id") in included)
    ]
    jumps = flow.get("jump_nodes")
    if isinstance(jumps, list):
        trimmed_flow["jump_nodes"] = [
            j for j in jumps
            if isinstance(j, dict) and j.get("data", {}).get("targetNodeId") in included
        ]

    trimmed_fd = dict(flow_definition)
    trimmed_fd["flow"] = trimmed_flow
    return trimmed_fd, missing


# ---------------------------------------------------------------------------
# Report writing — section 5, step 8
# ---------------------------------------------------------------------------
# In-memory registry of reports generated this session, keyed by report_id.
# save_dev_feedback (PHASE 7) reads the original root cause from here — the
# tool only receives a report_id, and the report's file path is not derivable
# from report_id alone (the folder is the agent name, not the agent_id).
REPORTS: dict = {}


def _safe_folder_name(name) -> str:
    """Make an agent/project name safe to use as a folder name."""
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", str(name)).strip()
    return safe or "unknown"


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


def _estimate_tokens(text: str) -> int:
    """Rough token estimate without a tokenizer dependency: ~4 chars per token
    for ASCII (English/JSON), ~1 char per token for non-ASCII (e.g. Hebrew)."""
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4) + non_ascii_chars)


def _write_gather_context(agent_name, timestamp, text) -> str:
    """Always persist the full gather context (exactly what is handed to the MCP
    client) to a local file, so it is inspectable on every run. Local write
    only — never sent anywhere. May contain conversation PII."""
    folder = os.path.join(BUGFIXER_OUTPUT_DIR or "", _safe_folder_name(agent_name))
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"gather_context_{timestamp}.txt")
    header = f"[Estimated tokens in this file: {_estimate_tokens(text)}]\n\n"
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(header + text)
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

    # Step 5: build a complete conversation record (transcript + execution trace).
    merged = _merge_transcript_interactions(transcript, interactions)

    # Step 6: load knowledge as sections; inline the `always` layer, auto-match
    # the rest against the bug description, and index everything else for
    # on-demand retrieval via get_knowledge.
    sections, golden = _load_knowledge_sections()
    always_sections = [s for s in sections if "always" in s["tags"]]
    matched_sections = _match_knowledge_sections(sections, bug_description)
    knowledge = {
        "always": _render_knowledge_sections(always_sections),
        "index": _build_knowledge_index(
            sections, {s["title"] for s in matched_sections}
        ),
        "matched": _render_knowledge_sections(matched_sections),
        "golden_examples": golden,
    }

    # Trim the flow to the nodes this conversation touched (+ their neighbors).
    touched_ids = [
        i.get("node_id") for i in interactions
        if isinstance(i, dict) and i.get("node_id")
    ]
    flow_for_prompt, missing_nodes = _trim_flow(flow_definition, touched_ids)

    # Step 7: compose the analysis context (no model call — the client analyzes).
    analysis_context = _build_analysis_context(
        bug_description,
        merged,
        system_prompt,
        security_prompt,
        flow_for_prompt,
        knowledge,
    )

    # Mint report_id and record what save_bug_report / save_dev_feedback will
    # need. The report file is written later by save_bug_report.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_id = f"{agent_id}_{timestamp}"
    warnings = []
    if not interactions:
        warnings.append(
            "No execution/interaction data was returned for this conversation."
        )
    if missing_nodes:
        warnings.append(
            f"{len(missing_nodes)} node(s) used in the conversation are not in the "
            "current flow version (the flow changed since; their definitions are "
            f"unavailable): {missing_nodes}"
        )
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

    return_text = (
        f"report_id: {report_id}\n\n"
        "Analyze the bug using the context below, then call "
        "save_bug_report with this report_id and your analysis "
        "(Root Cause, Solution A, and Solution B only if warranted).\n\n"
        f"{analysis_context}"
    )

    # Always persist the full gather context locally so it is inspectable on
    # every run (best-effort — a write failure must not block the tool).
    try:
        gather_path = _write_gather_context(agent_name, timestamp, return_text)
        REPORTS[report_id]["gather_context_path"] = gather_path
    except OSError as exc:
        gather_path = None
        REPORTS[report_id]["gather_context_path"] = None
        warnings.append(f"Could not save the gather-context file locally ({exc}).")

    header = f"report_id: {report_id}\n"
    if gather_path:
        header += f"(Full gather context saved to: {gather_path})\n"
    header += "\n"

    return [
        types.TextContent(
            type="text",
            text=(
                header
                + "Analyze the bug using the context below, then call "
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
# get_knowledge — on-demand knowledge section retrieval
# ---------------------------------------------------------------------------
async def get_knowledge(section_queries, tag_queries):
    """Return the full text of knowledge sections matching the given titles
    (exact or substring, case-insensitive) and/or exact tags."""
    wanted_titles = [
        q.strip().lower()
        for q in (section_queries or [])
        if isinstance(q, str) and q.strip()
    ]
    wanted_tags = [
        q.strip().lower()
        for q in (tag_queries or [])
        if isinstance(q, str) and q.strip()
    ]
    if not wanted_titles and not wanted_tags:
        return _error(
            "Provide at least one of: sections (section titles) or tags. "
            "See the KNOWLEDGE INDEX in the generate_bug_report context for "
            "available sections and their tags."
        )

    sections, _ = _load_knowledge_sections()
    picked = []
    for section in sections:
        title_lower = section["title"].lower()
        title_hit = any(w in title_lower for w in wanted_titles)
        tag_hit = any(t in section["tags"] for t in wanted_tags)
        if title_hit or tag_hit:
            picked.append(section)

    if not picked:
        available = "\n".join(
            f"- {s['title']}  (tags: {', '.join(s['tags'])})" for s in sections
        )
        return _error(
            "No knowledge section matched those titles/tags. Available sections:\n"
            + available
        )

    return [
        types.TextContent(type="text", text=_render_knowledge_sections(picked))
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
            name="get_knowledge",
            description=GET_KNOWLEDGE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Section titles to fetch (exact or partial, case-insensitive), as listed in the KNOWLEDGE INDEX",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact tag strings to fetch sections by, as listed in the KNOWLEDGE INDEX",
                    },
                },
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
    if name == "get_knowledge":
        return await get_knowledge(
            arguments.get("sections"),
            arguments.get("tags"),
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
