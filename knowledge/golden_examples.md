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
