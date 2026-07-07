# Golden Examples — Approved Corrections

---

## [7d2ae183-b846-44eb-b59d-10a1bdfc008c_20260702_103835_851473] — [20260702_122146_900078]
**Agent:** 7d2ae183-b846-44eb-b59d-10a1bdfc008c
**Bug description:** WhatsApp channel: when the bot sends the eSIM activation link, the follow-up question "במה עוד אוכל לעזור?" (spoken by a second node in the same turn) is glued directly onto the URL by the WhatsApp delivery layer — no separator between same-turn `node_content_segments` — so WhatsApp's linkifier swallows the first Hebrew word into the link and corrupts it.

**What was recommended and proved WRONG (field-verified 2026-07-02):** changing the speaking node's outgoing edge from `always` to an expression that is already true at entry (`{{resend_esim_link}} == True`), expecting it to fire "pre-node on the NEXT user turn". In the developer's real test the agent **skipped the "ESIM Success" node entirely — the eSIM link was never sent**.

**Verified lesson:** `expression` exits are a PRE-NODE ENTRY GUARD evaluated EVERY time a node is entered — including same-turn chain entry from the previous node — not only at the start of the next user turn. If the expression is already true when the flow transitions into the node, the guard matches and the node body is skipped immediately ("Match → transition, continue loop WITHOUT executing the node body"). Never propose an already-true expression exit on a node that must speak.

**Corollary (logical, not yet field-tested):** to split a turn on a conversation node, the exit expression must be FALSE at same-turn entry and TRUE on the next user turn — a static flag can never do this; it requires a value that changes between turns (e.g. a turn counter). Any concrete recipe for this (e.g. `system__agent_turns` marker) is UNVERIFIED until tested — do not present it as a confirmed fix.

**Status of the underlying WhatsApp-glue bug: STILL OPEN — NO FIX as of 2026-07-02.** Customer constraints (question stays in Collect User Intent; no text after the link; no added visible content, so no ack_message) rule out every agent-side option; buffer-line and ack_message approaches were explicitly rejected. The required fix is platform-side (Insait R&D): the WhatsApp adapter must send each `node_content_segment` as its own message. One untested agent-side fallback (turn-marker guard, changes UX) is documented in report `bug_report_20260702_103835_851473.md` — do not treat anything there as golden until the developer confirms a real-device test.
<!-- UPDATE WHEN RESOLVED: replace this status with the verified fix, and graduate the pre-node entry-guard lesson into the numbered knowledge patterns (new §2.16). -->
---

## [0bc30e29-3380-4d9b-aaeb-e2e3d8680e88] — empty bubble before every message (semi-prod) — 2026-07-06
**Agent:** 0bc30e29-3380-4d9b-aaeb-e2e3d8680e88 (PassportCard Support Agent - WhatsApp, workspace Passportcard Production)
**Conversation:** 9440af07-8e00-4d55-b943-16b8cb7b4e92

**Bug description:** Semi-prod environment only: every bot message arrives on WhatsApp as an **empty text bubble first, then the real message** — two separate bubbles, each with its own timestamp. The empty message does not appear in the platform transcript.

**Field-verified facts (2026-07-06 investigation):**
- Full structural diff of the UAT vs PROD agent exports (2026-07-06): **functionally identical** — only env URLs (`services-dev…/uat/` vs `services…`), tool/KB/topic UUIDs, `is_live`, UAT-only testing prompts, and PROD KB `include_conversation_context: true`. `filler_sentences` is `[]` in both. **Agent config ruled out as the cause.**
- Platform transcript stores each bot turn as exactly **one non-empty message**; raw content has no leading whitespace/RLM/newline. → The empty bubble is created by the **semi-prod WhatsApp outbound connector** (two WhatsApp API sends per turn, first with an empty body).

**Workaround applied (dev team, 2026-07-06):** appended to the relevant prompt, verbatim:
```
IMPORTANT: ALWAYS put two new line symbols ("\n\n") in the end of your response message!
```
- This is a **prompt-tier, best-effort** fix (see knowledge pattern 2.14 — LLM-generated whitespace is a suggestion, and models often trim trailing whitespace).
- UNVERIFIED: the mechanism by which a trailing `\n\n` suppresses the empty first bubble (connector internals) — not established from config or logs.
- UNVERIFIED: whether the workaround actually eliminates the empty bubble on a real device — no Nir-tested confirmation recorded yet.

**Status: WORKAROUND IN PLACE, root cause STILL OPEN.** The underlying fix is platform/infra-side: the semi-prod WhatsApp connector must stop issuing an empty first send per turn (confirm via WhatsApp Business API outbound logs — expect two `messages` POSTs per turn, first empty). Full investigation: `empty-whatsapp-bubble-investigation-2026-07-06.md` (repo root); change spec: `~/Desktop/PassportCard/WhatsappBot/FIX_empty_bubble_trailing_newlines.md`.
<!-- UPDATE WHEN RESOLVED: after a real-device test confirms the workaround, mark it verified; when the connector is fixed, record the platform fix and remove the prompt workaround (it adds trailing blank lines to every message). -->
---
