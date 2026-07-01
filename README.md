# Insait Bug Fixer MCP

An MCP server that helps developers analyze and fix **UI / bot-builder (flow-level) bugs** on the [Insait platform](https://platform.insait.io). It gathers all the context around a bug and lets Claude (the MCP client) do the analysis — the server itself never calls an LLM.

## What it does

Given an **agent ID**, a **bug description**, and the **conversation ID** where the bug happened, the server:

1. Fetches the agent config, the conversation transcript, and the execution trace (interactions) from the Insait REST API — **read-only GETs, it never writes to the platform**.
2. Loads local knowledge files (best practices + past corrections).
3. Returns a `report_id` and a full context block for Claude to analyze.

Claude reads that context, produces the **Root Cause + Solution(s)**, and saves it as a Markdown report. If a developer says the fix was wrong, their correction is saved back into the knowledge base so future analyses improve.

> Scope: UI / flow / node configuration only. Backend and infrastructure bugs are out of scope — the tool will tell you to escalate those.

## The three tools

| Tool | What it does |
|------|--------------|
| `generate_bug_report` | Validates the 3 inputs, fetches agent/transcript/interactions + knowledge, returns a `report_id` + context. **Does not analyze.** |
| `save_bug_report` | Writes Claude's analysis (Root Cause / Solution A / Solution B) to a Markdown report file. |
| `save_dev_feedback` | Appends a developer's correction to `golden_examples.md` so future reports learn from it. |

Typical flow: **gather → analyze (Claude) → save → (optional) correct.**

## Setup

1. Install dependencies:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file next to `server.py`:
   ```
   INSAIT_API_KEY=<your Insait API key>
   INSAIT_BASE_URL=https://api-platform.insait.io
   BUGFIXER_OUTPUT_DIR=<folder where reports are written>
   BUGFIXER_KB_DIR=<folder holding the knowledge/ files>
   ```

3. Register the server with your MCP client (already set up in `.mcp.json`):
   ```json
   {
     "mcpServers": {
       "insight-bug-fixer": {
         "command": "/path/to/.venv/bin/python",
         "args": ["/path/to/server.py"]
       }
     }
   }
   ```

## How to use it

In an MCP client (e.g. Claude Code / Claude Desktop), just ask to analyze a bug and provide the three inputs:

> "Analyze this bug on agent `<agent_id>`, conversation `<conversation_id>`: the bot asked for the policy number twice instead of moving to the claims node."

Claude will call the tools automatically:
1. `generate_bug_report` gathers the context.
2. Claude analyzes it and calls `save_bug_report` — a report lands in `BUGFIXER_OUTPUT_DIR/<agent name>/`.
3. If the analysis is wrong, tell Claude the correct fix and it calls `save_dev_feedback` to record it.

## Repo layout

```
server.py           MCP server + the 3 tools (stdio transport)
knowledge/          Best-practices + session knowledge fed into every analysis
reports/            Generated bug reports (per agent)
.mcp.json           MCP client registration
.env                Local config (never committed)
requirements.txt    mcp, httpx, python-dotenv
```
