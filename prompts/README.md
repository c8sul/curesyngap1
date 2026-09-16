# Prompts

Prompt assets for the CURE SYNGAP1 SMS agent. Stack-agnostic — the files here are text and JSONL data, not runtime code.

## File map

| File | Purpose |
|---|---|
| `system.md` | The system prompt. Navigator-first, 10 blocks: identity, mission, scope, safety rules, routing taxonomy, response format, context, refusal patterns, when unsure, response envelope. |
| `refusal-templates.md` | Canned openers for clinical refusal, emergency (911), self-harm (988), off-topic, and KB-miss cases. Referenced by `system.md`. |
| `few-shot.jsonl` | Six worked user/assistant pairs. Feed to the model as chat history before the real user turn. Assistant responses include the full envelope. |
| `evals/README.md` | Assertion schema for `fixtures.jsonl`. Documents `must_contain` / `must_not_contain` / `expected_meta` / `forbidden_meta` / `topics_allowed`. |
| `evals/fixtures.jsonl` | 24 test cases across all categories, including de-escalation boundary cases and an envelope-resilience case. |

## Composition for Option A (Python `twilio-agent-connect`)

TAC loads the system prompt via its prompt config. Few-shot pairs are pre-loaded as `messages[]` before the inbound turn. KB chunks arrive as tool/context messages.

Reference sketch (not runtime code — implementation lands in a follow-up PR):

```python
from pathlib import Path
import json

PROMPT_DIR = Path("prompts")

system_prompt = (PROMPT_DIR / "system.md").read_text()
few_shot = [json.loads(line) for line in (PROMPT_DIR / "few-shot.jsonl").read_text().splitlines()]

def build_request(user_turn: str, sources: list[dict]) -> list[dict]:
    sources_block = "<sources>\n" + "\n".join(
        f'  <source url="{s["url"]}">{s["text"]}</source>' for s in sources
    ) + "\n</sources>"
    return [
        {"role": "system", "content": system_prompt},
        *few_shot,
        {"role": "user", "content": f"{sources_block}\n\n{user_turn}"},
    ]
```

## Composition for Option B (Node TS, direct integration)

Same shape. System prompt as `system` role message; few-shot pairs pre-loaded into `messages[]`; KB chunks in the user message or as a tool result.

```ts
import fs from "node:fs";
import path from "node:path";

const PROMPT_DIR = "prompts";

const systemPrompt = fs.readFileSync(path.join(PROMPT_DIR, "system.md"), "utf8");
const fewShot = fs
  .readFileSync(path.join(PROMPT_DIR, "few-shot.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((l) => JSON.parse(l));

function buildRequest(userTurn: string, sources: { url: string; text: string }[]) {
  const sourcesBlock =
    "<sources>\n" +
    sources.map((s) => `  <source url="${s.url}">${s.text}</source>`).join("\n") +
    "\n</sources>";
  return [
    { role: "system", content: systemPrompt },
    ...fewShot,
    { role: "user", content: `${sourcesBlock}\n\n${userTurn}` },
  ];
}
```

## Response envelope parsing

The model returns:

```
META: {...json...}
---
SMS: <message body>
```

Parse rule:

1. Find the FIRST line whose content is exactly `---` (no leading or trailing characters, no whitespace).
2. Everything before that line: strip leading `META: `, parse the remainder as JSON.
3. Everything after that line: strip leading `SMS: `. This is the message body sent to Twilio.
4. If step 1 or 2 fails: log a `envelope_parse_failure` warning event, send the RAW model output as the SMS body. Never drop the message.

Python reference:

```python
def parse_envelope(raw: str) -> tuple[dict | None, str]:
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line == "---":
            meta_text = "\n".join(lines[:i]).removeprefix("META: ").strip()
            sms_text = "\n".join(lines[i+1:]).removeprefix("SMS: ").strip()
            try:
                return json.loads(meta_text), sms_text
            except json.JSONDecodeError:
                return None, raw
    return None, raw
```

TypeScript reference:

```ts
function parseEnvelope(raw: string): { meta: unknown | null; sms: string } {
  const lines = raw.split("\n");
  const sep = lines.findIndex((l) => l === "---");
  if (sep < 0) return { meta: null, sms: raw };
  const metaText = lines.slice(0, sep).join("\n").replace(/^META: /, "").trim();
  const smsText = lines.slice(sep + 1).join("\n").replace(/^SMS: /, "").trim();
  try {
    return { meta: JSON.parse(metaText), sms: smsText };
  } catch {
    return { meta: null, sms: raw };
  }
}
```

## Controlled vocab for `META.topics`

The model MUST emit values only from this list. Any intent not covered by the list is tagged `other`, and the app layer logs a warning so the vocab can be extended intentionally.

- `fundraising`
- `global_impact_week`
- `doctors`
- `registry`
- `EMERALD_trial`
- `DEEp_OCEAN_trial`
- `CAMP4_trial`
- `ICD10`
- `donation`
- `newly_diagnosed`
- `adult_transition`
- `siblings`
- `advocacy`
- `financial_planning`
- `self_advocate`
- `other`

## Graceful degradation

Every failure mode has a defined fallback:

- Envelope malformed → send raw model output as SMS, log `envelope_parse_failure`.
- META category unknown → log `unknown_category`, ship SMS anyway.
- Topics contain unknown values → downgrade unknowns to `other` in logged META, ship SMS unchanged, log `unknown_topic`.
- `<sources>` empty → prompt should trigger KB-miss template; if model still tries to answer, `must_not_contain` fixtures will catch hallucinated URLs at test time.

## What's not here yet

- Runner code — lands post-stack-decision.
- Chunker / KB ingest script — lands post-stack-decision. Raw corpus is at `kb/source/`.
- Language handling beyond English — v2.
- STOP / HELP keyword handling — TwiML-layer concern.
