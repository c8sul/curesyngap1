# CURE SYNGAP1 messaging agent

A volunteer Global Impact Week project for [CURE SYNGAP1](https://curesyngap1.org/), a rare-disease nonprofit run almost entirely by volunteers.

Families and supporters message a number and ask plain-language questions:

- "Tell me about SYNGAP1."
- "How do I run a fundraiser?"
- "How do I donate?"

The agent answers briefly and links to the most relevant page on curesyngap1.org. It does not give medical advice. When it cannot answer from the knowledge base, it escalates the question to the team rather than guessing.

## How a message flows

```text
WhatsApp or SMS
  -> Twilio Conversation Orchestrator captures it (capture rules)
  -> POST /webhook on this service, Twilio signature verified
  -> Twilio Conversation Memory supplies who is asking
  -> OpenAI model, with two tools: search the knowledge base, escalate
  -> reply routed back to whichever channel the message arrived on
```

## Quickstart

Docker is the only prerequisite. Everything runs in a container.

```bash
git clone https://github.com/c8sul/curesyngap1.git
cd curesyngap1
cp .env.example .env
docker compose run --rm test
```

Tests and lint pass with no credentials at all. To talk to the agent, put an
`OPENAI_API_KEY` in `.env` and run:

```bash
docker compose run --rm chat
```

That is the fastest way to judge a prompt or tool change. It exercises the same
agent loop the live webhook calls, against the checked-in knowledge fixture, and
needs no Twilio account.

## Serving a real channel

Four things have to line up: credentials, a sender, a public URL, and the Twilio
resources that tie them together.

### 1. Credentials

Ask Caitlyn for access to the Twilio account and the OpenAI project, then fill in
`.env`:

| Variable | Where it comes from |
| --- | --- |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio Console home page |
| `OPENAI_API_KEY` | the SYNGAP OpenAI project |

Everything else in `.env` is either optional or created for you in step 4.

### 2. A sender

WhatsApp through Twilio's sandbox is the fastest path, because it needs no
Meta Business verification, no approved sender, and no carrier registration.
`.env.example` ships configured for it.

1. Open the **Try WhatsApp** page in the legacy Console:
   <https://www.twilio.com/console/sms/whatsapp/sandbox>. Acknowledge the terms,
   click **Confirm**, and note the join code.

   Do not use Messaging > Senders > WhatsApp Senders > Create new sender. That is
   sender self-signup, which requires a Facebook login and a Meta Business
   Portfolio, and takes weeks.

2. From the phone you want to test with, send `join <code>` over WhatsApp to
   **+1 415 523 8886**. It replies to confirm. That code is what routes your
   messages to this Twilio account's sandbox.

3. Leave `TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886` in `.env`.

4. On that same Try WhatsApp page, set the sandbox's **Inbound URL** to
   `https://<your-public-host>/whatsapp-sandbox-silence`. Left at its default,
   the sandbox echoes "You said ..." to every user alongside the real answer.
   That endpoint returns empty TwiML, which sends the user nothing.

For SMS instead, set `TWILIO_PHONE_NUMBER` to an SMS-capable Twilio number in
E.164. Messaging US numbers additionally requires toll-free verification or
10DLC registration, which takes one to three weeks.

At least one sender is required. Configure both and a returning contact's SMS and
WhatsApp threads merge automatically, since both carry the same phone number.

### 3. A public URL

Twilio has to reach this service, so it needs a hostname that resolves from the
internet. Either works:

```bash
# Local, in one terminal
docker compose up agent

# and a tunnel in another
ngrok http 8000
```

Or deploy the container to a host such as Render or Fly.io and use its URL. A
deployed host is stable; a free ngrok host changes every restart, which means
repeating step 4.

### 4. The Twilio resources

```bash
docker compose run --rm provision --webhook-domain <your-public-host>
```

Pass the host only, with no scheme and no path. The script creates, or reuses, a
scoped API key, a Memory Store, and a Conversation Configuration, then prints the
`.env` lines to save. Re-running is safe: it reuses what already exists and
patches the configuration in place, so changing `--webhook-domain` or adding a
sender keeps the same configuration id.

`--list` shows what exists on the account.

### 5. Send a message

Message your sender and watch the `docker compose up agent` terminal. A healthy
round trip logs a started conversation and a sent response:

```text
CONVERSATION | Started WHATSAPP conversation [conversation_id=conv_conversation_...]
Sent WHATSAPP response via Actions API [conversation_id=conv_conversation_..., to_address=wh***NNNN]
```

## Layout

| Path | Contains |
| --- | --- |
| `src/app/main.py` | TAC wiring, channel registration, and the `on_message_ready` callback |
| `src/app/agent.py` | The agent loop: model call, tool calls, iteration cap, timeout fallback |
| `src/app/tools/knowledge.py` | Knowledge search, over the fixture or Enterprise Knowledge |
| `src/app/tools/escalation.py` | Sending an unanswered question to a person |
| `src/app/data/kb_fixture.json` | Stand-in page summaries, used until the site crawl succeeds |
| `src/app/config.py` | Environment-derived settings |
| `prompts/system.md` | The live system prompt. Edit this file, not the code |
| `scripts/provision.py` | Creates the Twilio resources, idempotently |
| `scripts/chat.py` | Terminal conversation with the agent, no Twilio account needed |
| `tests/` | Agent loop, tools, and prompt guarantees. No network calls |

## Making changes

```bash
docker compose run --rm test     # pytest and ruff
docker compose up agent          # reloads on edits to src/
```

**Agent behavior** lives in `prompts/system.md`. It is loaded at runtime and
mounted into the container, so editing it and restarting is enough. A test
asserts the medical-advice prohibition and both tool rules are still present, so
they cannot be dropped by accident.

**A new tool** is one `@function_tool` function in `src/app/tools/`, added to the
`tools` mapping in `src/app/agent.py`. Tools are built per turn, which is what
lets the escalation tool carry conversation details the model is never asked for.

**Knowledge** comes from whatever satisfies the `KnowledgeSource` protocol in
`src/app/tools/knowledge.py`. Setting `TWILIO_KNOWLEDGE_BASE_ID` switches from
the fixture to Enterprise Knowledge with no other change.

## What is stubbed, and who owns it

- **The knowledge base.** The Enterprise Knowledge crawl of curesyngap1.org is
  blocked by the site's bot protection, which returns HTTP 403 to the crawler.
  Ryan owns unblocking it, with a WordPress export as the fallback. Until then
  the agent searches `src/app/data/kb_fixture.json`, eight hand-written page
  summaries. Those URLs are plausible but only partly verified against the live
  site, so treat answers as illustrative.

  Enterprise Knowledge search returns `content`, `knowledge_id`, `created_at` and
  `score` per chunk, with **no page URL**. Since every answer has to link
  somewhere, `TwilioKnowledgeSource` maps `knowledge_id` to a URL through
  `KNOWLEDGE_ID_URLS`. Populating that map is part of switching the crawl on.

- **Escalation delivery.** `LoggingEscalation` records and logs the question
  instead of sending it. The agent, the prompt rule, and the tests are complete;
  only the transport is missing. See
  [docs/decisions.md](docs/decisions.md) for the options and why Twilio Email is
  not the obvious choice.

- **Observation extraction.** Memory extraction is on, so a contact's profile is
  created and matched by phone number, which is what stops a returning family
  repeating themselves. Only the contact address is recorded. Observations, the
  summaries of what was discussed, need a Conversational Intelligence operator
  listed in the configuration's `intelligenceConfigurationIds`, and that list is
  deliberately empty: an operator would extract the diagnoses and medications the
  agent is forbidden to retain. See [docs/decisions.md](docs/decisions.md).

- **Voice.** Not wired. TAC supplies `VoiceChannel` and ConversationRelay when
  text is proven.

## Safety

The agent must not give medical advice, diagnose, interpret symptoms or test
results, or guarantee fundraising outcomes, and must not store a family's health
details. Those rules live in [prompts/system.md](prompts/system.md) and a
Foundation content reviewer signs off before launch.

The adversarial test set — off-topic questions, attempts to extract medical
advice, prompt injection — is still to be built, and is the gate before real
families are on it.

## Ownership

| Account | Owner |
| --- | --- |
| Twilio | Caitlyn Olmer |
| OpenAI | Caitlyn Olmer |
| Hosting | TBD |

## License

This project is licensed under the [MIT License](LICENSE).
