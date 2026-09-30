# ADR-0004: LLMs only interpret changed fragments; tiered models from configuration

Status: accepted

Checked 2026-09-29 (claude-api reference): Haiku 4.5 $1/$5, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20, Fable 5.1
$10/$50 per MTok; structured outputs via `messages.parse(output_format=...)`; prompt caching available but our
prompts are below the minimum cacheable size, so result caching by input hash is used instead.

Decision: monitoring is deterministic up to "a primary page's rule-topic block changed". Only then, and only
for the changed hunks (+1 block context, ≤ 4 kB, redacted), the cheap tier proposes a structured
interpretation; ambiguous/low-confidence/invalid output escalates to the strong tier. Model ids are env
config (`PROPGUARD_LLM_MODEL_CHEAP/STRONG`), never code. Output is a PENDING proposal; confirmation is a human
action. Budget cap and any API error/refusal fail closed. LLM disabled by default.

Development-time: research ran on a cheaper model (Sonnet tier) as a subagent; the orchestrator (Opus tier)
wrote risk/execution/security code; an independent review ran on the most capable model (Fable tier).
