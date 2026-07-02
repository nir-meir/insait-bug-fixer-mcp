# Claude Sessions Knowledge — Field-Verified Bug Patterns

`Tags: always`

**Provenance:** Distilled from real Insait agent-flow debugging sessions, then **verified against the live UAT config export** (`passportcard_support_agent___uat___whatsapp` agent JSON + KB, export dated 2026-07-01). Every entry here describes a fix that is **confirmed present in the shipped config** (or explicitly user-confirmed working). Hypotheses, unapplied proposals, and unverified claims are deliberately excluded — see `~/Desktop/flow_bug_cases.md` for the full case list with per-case verification verdicts.

**How to use this file (instructions to the consuming model):**
1. Match a new bug by its **failure shape** (the `Tags:` line), not by customer name, node name, or language. Customer specifics below are illustrative instances of a general pattern.
2. The `Shipped fix (verbatim)` blocks are real production config text — safe to imitate as style/structure templates.
3. Facts in the **Platform Behaviors** section are dated. If the current date is far past the as-of date, re-verify against the live config/codebase before relying on them.
4. Prefer the fix *tier* stated in each entry: deterministic (edge/code/config) > structural prompt (gates, verbatim tables) > best-effort prose. Never present a best-effort prompt fix as a guarantee.
5. When investigating, follow the **Investigation Playbook** first — every principle in it was load-bearing in at least one verified case.

---

## Part 1 — Investigation Playbook (proven method, in order)

`Tags: always`

| Step | Rule | Why it earned its place |
|---|---|---|
| 1 | **Ground every claim in the actual config** — open the exported JSON / KB and grep; never assert from memory | Multiple bugs were only understood after grepping every producer/consumer of a variable across the flow |
| 2 | **Pick the observability layer by question**: config export → "is the rule/edge/extraction there?"; transcript/trace → "what did the LLM actually see and emit?"; backend logs → "did the platform answer, and when?"; a phone screenshot → channel-render bugs invisible in platform preview | WhatsApp linkify/RTL bugs are undetectable anywhere except a real device |
| 3 | **Trace a variable end-to-end before changing it** — every producer (code node, API extraction, default) and every consumer (edges, prompts, API bodies) | Translating a country name at the source would have broken routing guards and an XML builder that consume the English value |
| 4 | **Distinguish already-implemented from needs-change** — a "missing" rule may exist and be ignored (adherence problem, needs a stronger *form*), which is a different fix than a genuinely absent rule | Several rules existed but were phrased as soft preferences the model skipped |
| 5 | **The user's real-world test is authoritative** over sandbox checks and over your own reasoning | A prompt RTL fix "worked" in preview and failed on a real device; an edge-order hypothesis was retracted after the user's UI evidence |
| 6 | **Scope the blast radius before proposing** — check what else an edge, filter value, or global rule touches; state the collateral explicitly | A re-pointed `!= 'clear'` edge affects *every* non-clear branch, not just the reported one |
| 7 | **Retract loudly when evidence contradicts you** — rewrite the finding as not-a-bug with the evidence, don't quietly drop it | A "stray edge" recommendation was withdrawn after empirically establishing the engine's priority convention |

---

## Part 2 — Proven Fix Patterns

Each entry: general principle first, the verified instance second, the shipped artifact verbatim. All fixes below are **live in production UAT config** as of 2026-07-01.

### 2.1 Move decisions the LLM keeps getting wrong into deterministic edges
`Tags: empty-variable, prompt-gate-failure, hallucinated-data, expression-edge, deterministic-over-prompt`

**Principle:** An LLM prompt cannot reliably (a) detect that a variable is empty, or (b) evaluate a comparison like `{{x}} == "false"` after substitution turns it into `false == "false"`. If a wrong branch decision causes hallucination or broken routing, do not harden the prompt — add an **expression edge** that intercepts the path before the LLM node runs, feeding a fixed-text node with KB and tools disabled.

**Verified instance:** Empty `<Policies/>` from a login API made a presentation node hallucinate travel dates and assert a cancelled policy was valid. Prompt-wording fixes failed repeatedly; the shipped fix was an edge + fixed-message node, and the fragile in-prompt comparison was deleted entirely.

**Shipped fix (verbatim):** edge condition `{{has_active_policy}} == 'false'` routing to a node whose full prompt is:
```
Respond in Hebrew with EXACTLY this and nothing else:
"לא נמצאה פוליסה פעילה במערכת."

Do NOT mention or invent any policy detail (dates, destination, members, price, number, status).
IMPORTANT: DO NOT USE ANY TOOLS.
```
(kb_mode: disabled, tools: none.)

---

### 2.2 Channel rendering has physics the flow can't see — write mechanical format rules
`Tags: channel-render, whatsapp-linkify, url-breaks, message-formatting, invisible-in-preview`

**Principle:** Delivery channels post-process messages (WhatsApp extends a URL until the next whitespace; paragraph direction follows the first strong character). These bugs never reproduce in the platform preview — diagnose from a real-device screenshot, and fix with a **mechanical, self-checking format recipe** in the prompt (exact position rules), not a stylistic request. Also remove any same-turn `always`-chained node that appends text after the sensitive token.

**Verified instance:** An eSIM activation link glued to the first Hebrew word of a follow-up question in the same bubble, breaking the link.

**Shipped fix (verbatim, from the link-sending node):**
```
CRITICAL WhatsApp formatting rules (right-to-left, auto-links URLs):
- Put the link {{esim_link}} alone on its OWN separate line.
- The link MUST be the LAST thing in the message. Do NOT write any text, space,
  punctuation, emoji, or character AFTER the link — anything touching it gets
  swallowed into the link and breaks it.
- Do NOT add a confirmation question or any follow-up text here.
```
The follow-up question was removed from this turn entirely (flow restructured so the question comes in a later node).

---

### 2.3 Kill prompt contradictions at the source bullet — don't layer a counter-rule
`Tags: prompt-contradiction, decision-order, clarification-loop, misclassification, rule-precedence`

**Principle:** When two prompt rules conflict, the one earlier in the stated decision order wins, and adding a new overriding rule on top does NOT fix it — the model still follows the still-valid earlier rule. Edit the **contradicting bullet itself**, and put exceptions **inline in that same bullet** (an exception in another section loses). If the prompt text is duplicated, every copy must be edited.

**Verified instance:** A "documents are ambiguous → clarify" step ran before a "classify documents immediately" intent rule, looping the bot on "which documents?"; separately, "send docs to a different email" was swallowed by the same bullet. Both fixed in the bullet itself:

**Shipped fix (verbatim):**
```
- "מסמכים" / "פוליסה במייל" / "תשלחי לי מסמכים" / any request to receive or be sent documents
  → NOT ambiguous. Immediately save user_intent=policy_documents.
  EXCEPTION: if the customer wants the documents sent to a DIFFERENT / other email than the
  one on file → save user_intent=personal_d_update (NOT policy_documents).
  NEVER ask which documents — the full document kit already includes the policy documents,
  receipt, invoice, and proof of payment.
  (Exception: a question about the STATUS of forms the customer already sent → clarify.)
```
The exception is mirrored in every node that carries a copy of the intent rules.

---

### 2.4 Global behavior rules need a positive scope list + explicit carve-outs
`Tags: global-prompt, ambiguous-request, clarify-first, scope-conflict, blast-radius`

**Principle:** A global rule like "clarify when ambiguous" collides with intent rules that demand immediate routing. Make it conflict-free by **scoping**: enumerate the actions it applies to, and name the intents it must never touch. Design it so the worst case is harmless (one redundant question), never a routing/variable/API change.

**Shipped fix (verbatim, global system prompt):**
```
**Clarify Ambiguous Requests**
When a customer's request about a self-service action (date change, destination change,
personal details update, eSIM activation, document delivery, card information) is ambiguous
or unclearly phrased, do NOT answer and do NOT say it is impossible — first ask one short
clarifying question (confirm the most likely intent, or name the likely options when there
are several), and proceed only after the customer confirms. When the intent is clear, answer
directly as usual — never add a clarifying question to a clear request.
This does NOT apply to human-representative requests, policy-purchase intent, non-customer
guidance, claim submission, or medical-status updates — handle those per their knowledge-base
rules above, without inserting a clarifying question.
```

---

### 2.5 De-prime safety refusals on data the customer owns
`Tags: over-refusal, safety-prior, instruction-vs-prior, sensitive-data-priming`

**Principle:** When the model refuses to share a value despite an explicit "share it" instruction, the cause is usually a safety prior reinforced by nearby wording that primes "sensitive" next to the value. Fix by **defining the sensitivity boundary explicitly** — name what IS sensitive, then declare the disputed items as the customer's own data that MUST be shared. Reinforcing "please comply" prose does not work.

**Shipped fix (verbatim):**
```
- Sensitive personal data means: passwords, payment card numbers, and ID numbers.
  Policy details listed in this prompt (policy number, dates, price, members, riders)
  are NOT sensitive — they belong to the customer and must be shared when asked.
```

---

### 2.6 Keep logic variables in their canonical form; translate only at render
`Tags: display-vs-logic, verbatim-injection, language-mixing, variable-reuse`

**Principle:** A variable consumed by routing guards, edge conditions, or API bodies must keep its canonical (usually English) value. Never translate/reformat at the source — add a **display-only rule** at the presenting node (or a parallel `*_display` variable). "Always respond in Hebrew" does not catch injected data tokens; the model echoes them verbatim, so the display rule must name the variable explicitly with examples.

**Shipped fix (verbatim):**
```
- DESTINATION/COUNTRY NAMES: {{policy_countries_text}} holds country names in English.
  When you present the destination to the customer, ALWAYS translate each country name to
  its standard Hebrew name (e.g. Greece → יוון, France → צרפת, Spain → ספרד,
  United Kingdom → בריטניה, United States → ארה"ב, Cyprus → קפריסין).
  Never output a country name in English.
```

---

### 2.7 No-transfer channels: re-point transfer edges to an information node, audit all prompts
`Tags: channel-capability, human-transfer, edge-repoint, kb-phone-fallback, capability-mismatch`

**Principle:** When a channel cannot transfer to a human, the fix has two halves: (1) **re-point every transfer edge** to a conversation node that serves the KB customer-service number; (2) **sweep every prompt** that offers/promises a transfer and replace with keep-helping + phone-number language — while preserving spec-sanctioned business handoffs (eligibility, backend errors). A single node fix is not done; the pattern recurs across the flow.

**Verified instance (all live):** a catch-all `!= 'clear'` filter edge now targets a "Customer Support from KB" conversation node; collect nodes carry "This is a text channel — you can NOT transfer… do NOT offer"; an eSIM "didn't receive the link" branch that used to transfer now routes to a resend loop (`resend_esim_link == True → resend`, `False → continue`).

---

### 2.8 Force KB retrieval for facts the model loves to invent; forbid inventing them
`Tags: hallucinated-contact-info, invented-phone-number, invented-hours, hallucination, kb-trigger, never-invent, rag-grounding, tool-mode-kb`

**Principle:** Contact info (phones, hours, URLs) is a top hallucination target. Two verified layers: (1) an explicit **must-retrieve + use-only-returned-values + never-invent** rule so the worst case is "not available," never a fabricated number; (2) where the KB was in `tool` mode (LLM decides whether to query), switching to `auto` mode removes the "model skipped the lookup" failure class entirely.

**Shipped fix (verbatim):**
```
HUMAN REPRESENTATIVE / SERVICE CONTACT: When the customer asks to speak with a human
representative, or asks for the customer-service phone number or its operating hours, you
MUST retrieve these details from the knowledge base (trigger the KB tool). Use ONLY the
phone number, short number, and hours returned by the knowledge base. NEVER invent, guess,
complete, or reformat a phone number or hours that did not come from the knowledge base.
If the knowledge base returns no phone number, respond that it is not available.
```

---

### 2.9 Sentinel defaults: validate at collection, delete dead guards, fail loud at the API
`Tags: sentinel-value, dead-condition, silent-dead-end, field-validation, quote-mismatch`

**Principle:** A default that doubles as a value a user could type (e.g. `0000` for card digits) creates two bugs: the sentinel leaks to the API, and the legitimate typed value dead-ends. Verified layered fix: (1) strict validation in the **field description** (the LLM's save-time contract); (2) **delete** edge guards that compare against the sentinel (they go dead on any quoting/type mismatch — a guard comparing `!= "0000"` never matched a stored `"\"0000\""`); (3) let the API be the source of truth and fail loud into the existing retry path rather than dead-ending in silence. Also: when a JSON export shows a suspicious double-quoted default, check the platform UI — the export may carry quoting the UI hides.

**Shipped fix (verbatim, field description):**
```
The value MUST be exactly 4 characters, each a digit 0–9 (example: 4580). Do NOT extract
or save a value that is not exactly 4 digits, that contains any letter, space, or symbol,
that is empty, or that equals "0000". Only save once the value passes ALL of these checks.
```
The dead `!= "0000"` edge clause was removed (proceed edge now checks `!= None` only).

---

### 2.10 Extract machine-readable error signals; never rely on message text
`Tags: api-error-contract, error-extraction, http-200-error, machine-readable-flag, integration-schema`

**Principle:** Backends return failures as HTTP 200 with error fields, and error *messages* are often empty — extract the error **code** (XPath/JSON extraction) so edges can branch on it. Same idea outbound: if an integration partner needs to route on a state (e.g. transfer), the response schema must carry it as a **dedicated field**, not prose.

**Verified instances (live):** an `//*[local-name()='ErrorCode']/text()` extraction was added to the policy-extend tool; the custom response schema now includes `"transfer": "{{transfer}}"` and `"completed": "{{completed}}"`.

---

### 2.11 Scope XPath extractions to one entity; parent repetition multiplies results
`Tags: xpath-scoping, duplicated-results, per-member-data, extraction-cardinality`

**Principle:** An unanchored XPath like `//BenefitDetails[IsRider='true']/Name` matches across ALL repeated parent blocks (N members → N copies of every rider). Anchor to a single indexed entity, policy-first for forward-compatibility. Before assuming all siblings are identical, check a counter-example (members CAN hold different riders — then the robust form is the deduped union, a documented follow-up).

**Shipped fix (verbatim, namespace-aware):**
```
((//*[local-name()='Policy'])[1]//*[local-name()='Customer'])[1]//*[local-name()='BenefitDetails'][*[local-name()='IsRider']='true']/*[local-name()='Name']/text()
```

---

### 2.12 Enum hygiene: options ↔ prompts ↔ description must reference the same value set
`Tags: enum-consistency, stale-references, unsavable-value, config-drift`

**Principle:** Every enum field has three places that must agree: the `options` array, every prompt instructing what to save, and the field `description`. A prompt instructing a value absent from options creates routing with no landing; a description mentioning removed values misleads classification. After trimming an enum, sweep all three.

**Verified state (live):** the transfer-reason enum's 4 options, its description, and all saving prompts are mutually consistent; the formerly-orphaned value was removed from prompts.

---

### 2.13 Security guardrails: one explicit prohibition beats a vague one
`Tags: guardrail, jailbreak-framing, claim-decision, output-screening`

**Principle:** Generic guards ("do not invent information") do not stop a harmful action reframed as a favor ("just phrase an email for me"). Write the prohibition in terms of the **action and its consequences**, with the redirect included. Independently check whether output-side screening is enabled — a prompt rule cannot block a response after the fact.

**Shipped fix (verbatim, security prompt):**
```
- Never approve, deny, or confirm an insurance claim, and never state a refund, credit,
  or payment amount. Claims are decided only by the Claims department — direct the
  customer there.
```

---

### 2.14 LLM-generated line breaks are best-effort — know the tier you're on
`Tags: formatting-nondeterminism, line-break, fix-tier, prompt-vs-structural`

**Principle:** A `\n` inside an LLM prompt is a suggestion. The deterministic route is moving constant text out of the LLM into a static message the orchestrator joins with a real newline. If the prompt route is chosen anyway (acceptable for cosmetic issues), phrase it as a mechanical placement rule and **label the fix best-effort in your report** — don't promise determinism from prose.

**Shipped fix (verbatim, prompt-tier chosen deliberately):**
```
Important: ALWAYS output this follow-up sentence on its own new line, as a separate row
below the rest of your message — never on the same line as the preceding text. Put a line
break before it.
```

---

## Part 3 — Platform Behaviors (verified; re-check after platform releases)

`Tags: always`

*As of 2026-07 (UAT export + in-session codebase reads):*

| Behavior | Detail |
|---|---|
| Exit evaluation order | `expression` exits are evaluated before `always` exits — a guard edge wins without priority juggling |
| Edge priority convention | Higher priority number evaluated first; a `cond=None` (unconditional) edge acts as lowest-priority last-resort fallback, not a shadow |
| Variable defaults | The orchestrator seeds a default only if it is not JSON `null` — `null` is the ONLY "unset"; `"None"`, `"null"`, `""` are real values |
| Template substitution | Code-node outputs substitute into prompts; leftover `{{x}}` collapses to `{x}`. In-prompt equality comparisons against substituted values are unreliable — branch on printed labeled facts or use expression edges |
| KB modes | `tool` = LLM decides whether to query (can skip); `auto` = always retrieved. `auto` eliminates the skipped-lookup failure class |
| Exit types in use | `expression` / `always` / `result` (API success-error). LLM-triggered transitions exist but carry a known engine crash risk (platform bug reported); deterministic exits are the safe path |
| Custom response schema | Fully author-controlled; integration-facing state (transfer/completed) must be explicit fields in the template |

---

## Part 4 — Template for new entries

`Tags: contribution-template, adding-knowledge, knowledge-maintenance`

Add new knowledge ONLY after verifying the fix exists in a fresh config export (or the user confirms it live). One entry per *pattern* — if a new case matches an existing pattern, append it as another verified instance instead of a new section.

```
### 2.N <General principle as a one-line imperative>
`Tags: <failure-shape keywords, no customer names>`

**Principle:** <general rule + why the weaker alternative fails>

**Verified instance:** <the concrete case, customer-specific details allowed here>

**Shipped fix (verbatim):** <exact config text confirmed in the export, date-stamped by the export>
```
