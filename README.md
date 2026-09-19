# CURE SYNGAP1 messaging agent

A volunteer Global Impact Week project for [CURE SYNGAP1](https://curesyngap1.org/), a rare-disease nonprofit run almost entirely by volunteers.

Families and supporters message a number and ask plain-language questions:

- "Tell me about SYNGAP1."
- "How do I run a fundraiser?"
- "How do I donate?"

The agent answers briefly and links to the most relevant page on curesyngap1.org. It does not give medical advice. When it cannot answer from the knowledge base, it escalates the question to the team rather than guessing.

It runs at <https://curesyngap1-agent.onrender.com> and serves WhatsApp through Twilio's sandbox. [Text the agent](#text-the-agent) is how to try it; [Quickstart](#quickstart) is how to run it locally.

> **Decision needed before real families use this: what memory may retain.**
> Conversation Memory is enabled, and it writes observations and conversation
> summaries from whatever a family says. Extraction is not selective, so a
> family describing seizures or medications would have that retained and read
> back on their next message. This is on deliberately, to make the behavior
> visible rather than to settle the question. It applies to anyone holding the
> sandbox join code, on a public host, so widen who can text it only once the
> question is answered. See [Memory](#memory),
> [Who can text it](#who-can-text-it), and
> [Open decision 3](docs/decisions.md).

## How a message flows

```text
WhatsApp or SMS
  -> Twilio Conversation Orchestrator captures it (capture rules)
  -> POST /webhook on this service, Twilio signature verified
  -> Twilio Conversation Memory supplies who is asking and what they asked before
  -> OpenAI model, with two tools: search the knowledge base, escalate
  -> reply routed back to whichever channel the message arrived on
```

## Text the agent

No setup, no account, no clone. The agent runs at
<https://curesyngap1-agent.onrender.com> and answers over two channels: SMS on
its own toll-free number, and WhatsApp through Twilio's sandbox.

By SMS, text **+1 855 770 5019**. There is no join code and nothing expires, so
this is the shortest path to a working conversation — and the reason to read
[Who can text it](#who-can-text-it) before passing the number on.

By WhatsApp:

1. Ask a maintainer for the sandbox join code. It routes your messages to this
   Twilio account, so it is shared deliberately rather than published here; see
   [Who can text it](#who-can-text-it).
2. From WhatsApp, send `join <code>` to **+1 415 523 8886**. It replies to
   confirm. Any number of people can join the same sandbox with the same code,
   and each session expires three days after joining, so a returning tester
   sends `join <code>` again.
3. Ask it something the site covers: "What is the SYNGAP1 ICD-10 code?", "How do
   I start a fundraiser?", "What should I do if I need medical information?"

What to expect:

- A short answer with a link to the page on curesyngap1.org it came from.
- The first message after a quiet spell can take ten seconds or return the
  fallback reply, because the service sleeps when idle. Send a second one.
- No medical advice, ever. A clinical question is answered with a pointer to a
  qualified clinician; see [Safety](#safety).
- It remembers earlier conversations with you. Asking "what did we talk about
  before?" is the fastest way to see that, and
  [Retention is not settled](#retention-is-not-settled) is the reason to think
  before testing it with real health details.

### Who can text it

The two channels gate access differently, and SMS barely gates it at all.

The join code belongs to this Twilio account's sandbox, and the sandbox number
is shared across every Twilio account: the code is what routes a message to this
agent rather than someone else's. Anyone who has it can reach the agent, and what
they say is retained (see [Memory](#memory)), which is why it is not in this
file.

Anyone with access to the Twilio account reads it off the **Try WhatsApp** page
of the Console and needs nothing from anyone. Everybody else needs a maintainer
to pass it on.

The SMS number has no equivalent: anyone who knows it can text the agent, and
what they say is retained the same way. Knowing the number is the whole of the
access control, so who it is given to is the decision that matters — and it is
the decision [Open decision 3](docs/decisions.md) says to settle before the
number reaches real families.

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
agent loop the live webhook calls, against the checked-in knowledge fixture (see
[Knowledge](#knowledge)), and needs no Twilio account.

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

At least one sender is required. Configuring both does not merge a contact's two
threads: Conversation Memory keys a profile on the identifier type, so
`whatsapp:+1...` under `whatsapp` and `+1...` under `phone` are two identifiers
for one person, and the conversation grouping is per channel type as well. Someone
who switches channels starts over.

### 3. A public URL

Twilio has to reach this service, so it needs a hostname that resolves from the
internet. Either works:

```bash
# Local, in one terminal
docker compose up agent

# and a tunnel in another
ngrok http 8000
```

Or deploy to Render for a stable hostname, which a free ngrok host is not: it
changes every restart, and each change means repeating step 4. See
[Deploying to Render](#deploying-to-render).

### 4. The Twilio resources

```bash
docker compose run --rm provision --webhook-domain <your-public-host>
```

Pass the host only, with no scheme and no path. The script creates, or reuses, a
scoped API key, a Memory Store with the `Engagement` and `Interests` trait groups
declared on it, and a Conversation Configuration, then prints the
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
| `src/app/data/kb_fixture.json` | Offline page summaries, for tests and credential-free runs |
| `src/app/config.py` | Environment-derived settings |
| `render.yaml` | The deployed service definition, read by Render's Blueprints |
| `prompts/system.md` | The live system prompt. Edit this file, not the code |
| `scripts/provision.py` | Creates the Twilio resources, idempotently |
| `scripts/chat.py` | Terminal conversation with the agent, no Twilio account needed |
| `scripts/memory_e2e.py` | Checks the Conversation Memory round trip against the live account |
| `scripts/deploy.py` | Triggers and watches a Render deploy |
| `tests/` | Agent loop, tools, and prompt guarantees. No network calls |

## Deploying to Render

`render.yaml` defines the service, so a deploy is reviewable in the repository
rather than living only in the dashboard. Render reads it through Blueprints.

**Create it once.** With the repository connected (below), choose New →
Blueprint in the Render dashboard and select it. Render prompts for every
variable marked `sync: false`, which is all of the secrets and the
account-specific ids; take them from your `.env`. Pushes to the branch named in
`render.yaml` deploy automatically afterwards.

### Connecting the repository is the repo owner's job

Render's GitHub app has to be installed on the account that **owns**
`curesyngap1`, and that account is a personal one rather than an organization.
Only its owner can install a GitHub app on it; push access to the repository is
not enough, and installing the app on a contributor's own account exposes only
that account's repositories.

For the owner, once: dashboard.render.com → the workspace picker at the top
left → New → Blueprint. With no connection yet the page offers **Connect
GitHub** rather than a repository list, which redirects to
`github.com/apps/render/installations/new`. Install it on the account that owns
the repository, choosing either all repositories or just `curesyngap1`. GitHub
returns to Render with the repository now selectable.

**Point Twilio at the new hostname.** The service comes up at
`https://<name>.onrender.com`, and two places have to name it:

```bash
# The Conversation Configuration's status callback. Patches in place, so the
# configuration keeps its id.
docker compose run --rm provision --webhook-domain <name>.onrender.com
```

and the WhatsApp Sandbox Inbound URL, set to
`https://<name>.onrender.com/whatsapp-sandbox-silence` at
<https://www.twilio.com/console/sms/whatsapp/sandbox>. That field has no API, so
it is a manual step every time the hostname changes.

**Changing an environment variable does not deploy.** Render stores the new
value and the running instance keeps the old one until something deploys, so a
corrected credential appears to have no effect. Trigger one:

```bash
docker compose run --rm deploy
```

**Deploying and checking on it.** A push to the branch in `render.yaml` deploys
on its own. For the cases a push does not cover — redeploying after an
environment variable changes, or recovering a failed deploy — and to read the
service URL back:

```bash
docker compose run --rm deploy --status    # report, change nothing
docker compose run --rm deploy             # deploy and wait for it to go live
docker compose run --rm deploy --logs 50   # the service's recent output
docker compose run --rm deploy --sync-env  # push changed .env values, then deploy
```

`--sync-env` is how a credential is rotated: put the new value in `.env`, run it,
and it pushes what differs from the service's current values and deploys, since
Render keeps the running instance on the old value until something does. It
prints key names and lengths, never values.

These need `RENDER_API_KEY` in `.env`, and `--logs` also needs `RENDER_OWNER_ID`.

Keep trailing whitespace out of `.env` values. `python-dotenv` strips it, so a
local run is unaffected, but `docker --env-file` passes the value through
verbatim. A credential that picks up a stray space that way is wrong only where
it was copied to, and an auth token one character too long fails every webhook
signature with a 403 that looks nothing like a bad secret.
A deployed agent's logs are the only place a failing recall or a cold-start
timeout is visible, so `--logs` is the deployed equivalent of
`docker compose logs agent`. Build logs are in the dashboard.

**The serving command is not the Dockerfile's.** `render.yaml` sets
`dockerCommand` to `uvicorn --factory app.main:create_app`, because the
Dockerfile's `python -m app.main` serves TAC's app directly and so carries
neither `/healthz` nor `/whatsapp-sandbox-silence`, both of which `create_app()`
adds. It also binds `$PORT`, which is what Render routes to.

**`/healthz` is the only route without a signature check.** Every other route
validates a Twilio signature and would fail a health check that is not a signed
Twilio request, so the health check has nowhere else to go. It reports that the
process started and found its configuration, which is what separates a bad
deploy from a working one.

### The free plan sleeps

A free service spins down when idle, and a cold start can exceed
`AGENT_TIMEOUT_SECONDS`, so the first message after a quiet period gets the
fallback reply instead of an answer. Warm it with a request to `/healthz` before
a demo, or move to a paid plan before real families text it.

The workspace's own plan does not change this: Render's workspace tiers and
per-service compute plans are independent, and a free instance sleeps in a paid
workspace too. Changing `plan: free` to `plan: starter` in `render.yaml` is what
removes it. That re-deploys onto the new instance type and costs nothing else,
since this service keeps no state of its own: conversation history is
in-process and rebuilt from the next message, and what persists lives in
Twilio's Memory Store.

## Moving off the sandbox

The sandbox is a shared Twilio number with a join code, which makes it a testing
tool rather than something to give families. Two things replace it, independently.

### A WhatsApp sender of your own

1. Get a Meta Business Portfolio verified and a WhatsApp sender approved for a
   Twilio number. Twilio's sender self-signup drives this, and it takes weeks
   rather than minutes, which is the reason the sandbox exists.
2. Set `TWILIO_WHATSAPP_NUMBER=whatsapp:+1<your number>` in `.env` and in the
   Render service's environment.
3. Re-run provisioning so the capture rules name the new sender:

   ```bash
   docker compose run --rm provision --webhook-domain curesyngap1-agent.onrender.com
   ```

   It patches the Conversation Configuration in place, so it keeps its id.
4. Deploy, because an environment variable alone does not:
   `docker compose run --rm deploy`.

An approved sender needs no Inbound URL and no `/whatsapp-sandbox-silence`: that
route exists only because the sandbox has its own echoing webhook. Leaving the
route registered is harmless.

### An SMS number

CURE SYNGAP1's SMS sender is the toll-free number **+1 855 770 5019**, whose
toll-free verification is approved. It is set as `TWILIO_PHONE_NUMBER` in
`.env` and in `render.yaml`, so both channels are live and nothing has to be
done to serve SMS on a fresh checkout.

To point the agent at a different number instead:

1. Buy an SMS-capable Twilio number. Messaging US numbers requires toll-free
   verification or 10DLC registration first, which takes one to three weeks.
2. Set `TWILIO_PHONE_NUMBER` to it in E.164 in `.env` and in `render.yaml`.
   `build_server()` registers the SMS channel whenever that value starts with
   `+`, so nothing else has to change for the agent to serve it. That key
   carries a literal `value:` rather than `sync: false`, so
   `deploy.py --sync-env` does not push it: it reaches the service through a
   Blueprint sync or the Render dashboard.
3. Re-run provisioning and deploy, as above.

Inbound SMS is captured by the Conversation Configuration's capture rules, so the
number's own Messaging webhook stays empty. Leaving a webhook or an auto-replying
Messaging Service on the number is what makes a family get two answers.

Giving the number out is what Decision 3 in [docs/decisions.md](docs/decisions.md)
is gated on: with `MEMORY_MODE=always`, extraction is not selective, so a family
describing seizures or medications has that retained. Settle that before the
number goes anywhere public.

A family who texts after using WhatsApp starts over, because Conversation Memory
keys a profile on the identifier type: `whatsapp:+1...` and `+1...` are two
identifiers for one person. Running both channels is fine; merging their history
is not something this app can do.

## Troubleshooting

Symptoms seen while getting this working end to end, and what each one means.

| Symptom | Cause |
| --- | --- |
| `POST /webhook` returns 403 within a few ms | The auth token the service holds is not the one Twilio signs with. Compare lengths before contents: trailing whitespace in a `.env` value survives `docker --env-file` and makes a 32-character token 33. |
| Messages arrive, no reply, no application log | Same 403. Twilio's delivery is fine and the request never reaches the callback. |
| The user gets "You said ..." alongside the real answer | The WhatsApp Sandbox Inbound URL is still the stock Twilio Function every new sandbox ships with. Point it at `<host>/whatsapp-sandbox-silence`. |
| Two replies to one message | A local container and the deployed service are both serving the same Conversation Configuration. Only one webhook host can be current. |
| A texter gets the agent's answer plus a stock or canned one | The SMS number still has its own Messaging webhook, or belongs to a Messaging Service that replies. Capture rules deliver the message to `/webhook` either way, so both answer. Clear the number's messaging configuration in the Console. |
| A corrected credential changes nothing | Render keeps the running instance until something deploys. Run `docker compose run --rm deploy`. |
| One tester gets no replies while others are answered | They went over the rate limit: `RATE_LIMIT_MESSAGES` per minute per contact. The log says `Rate limit: ...`. It clears itself a minute after their last message. |
| A message gets no reply at all, but the logs show `Sent WHATSAPP response` | The body was over Twilio's 1600-character limit, which Twilio rejects after accepting the send; the Console's Messaging logs show the failure. `Agent.respond()` now replaces any reply over `MAX_REPLY_CHARS` with a short one and logs a warning, so look for that line first. |
| `GET /` returns 404 | Expected. The app registers `/webhook`, `/twiml`, `/ws`, `/healthz` and the sandbox silencer, and no root route. Use `/healthz`. |
| The first message is slow or returns the fallback | The free instance spun down. See [The free plan sleeps](#the-free-plan-sleeps). |
| The agent says it has no memory of earlier conversations | Look for a `Recall:` line in the logs. `observations=0` means retrieval found nothing; no line at all means recall was skipped or the contact has no profile yet. |
| A sender set in `render.yaml` is still empty on the service | A literal `value:` in the blueprint is applied when Render creates the env var, not on every push, and `--sync-env` skips it because it is not `sync: false`. Read it back with `GET /v1/services/<id>/env-vars`, then set it with `PUT .../env-vars/<KEY>` or in the dashboard, and deploy. |
| A deploy ends `update_failed` right after creation | The service started before its credentials existed. `build_server()` refuses to start without them, by design. Set them, then deploy. |

`docker compose run --rm deploy --logs 50` is the first move for all of these on
a deployed service, and `docker compose logs agent` locally.

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
`src/app/tools/knowledge.py`, either Enterprise Knowledge or the offline
fixture. See [Knowledge](#knowledge).

## Limits the agent holds itself to

Constants in `src/app/main.py` and `src/app/prompt.py`, not environment
variables: each one exists to stop a specific failure, and none of them is a
setting a deployment should want to differ on.

| Limit | Why |
| --- | --- |
| `MAX_REPLY_CHARS` (1500) | Twilio rejects a body over 1600 characters *after* accepting the send, so an oversized reply reaches nobody and reports nothing. Over this, the family gets a short reply instead. |
| `MAX_INBOUND_CHARS` (2000) | Whatever is sent is what the model reads. Longer costs tokens and is where an injection attempt would hide. |
| `RATE_LIMIT_MESSAGES` (12/minute, per contact) | One sender cannot spend an OpenAI call per message. The contact is told once, then not answered until the window rolls. In-process, so it is per replica. |
| `MAX_CONVERSATIONS` (500) | A conversation's history is dropped when TAC reports it closed, but that report never arrives for one that closes across a restart, so the oldest are dropped rather than kept for the life of the process. |
| `MAX_HISTORY_MESSAGES` (40) | The turns of one conversation sent back to the model. |
| `LOGGED_CHARS` (200) | How much of a family's question reaches a log line. A question can itself be a health detail; an escalation's transport still gets the whole text. |

## Knowledge

Answers come from the Enterprise Knowledge base **Syngap1**
(`know_knowledgebase_01m2gmmj26e35ty0xf2kkgfk16`), which holds a content
snapshot of curesyngap1.org uploaded as six documents:

| Document | Covers | Answers link to |
| --- | --- | --- |
| `01-about-syngap1` | The condition, epilepsy, autism, life expectancy, census | `/what-is-syngap1/` |
| `02-treatment` | Treatment status and the therapeutic pipeline | `/syngap1-treatment/` |
| `03-family-resources` | Newly diagnosed, adulthood, siblings, undiagnosed, getting involved | `/syngap1-resources-for-newly-diagnosed-families/` |
| `04-clinical-care` | ICD codes, clinicians, clinical trials, registries, studies | `/doctors/` |
| `05-research-grants` | Grants, the grant program, iPSC models | `/resources/grants/` |
| `06-about-the-organization` | Mission, team, finances, impact | `/mission-and-values/` |

Set the base to search, and nothing else:

```bash
TWILIO_KNOWLEDGE_BASE_ID=know_knowledgebase_01m2gmmj26e35ty0xf2kkgfk16
```

### How a passage becomes a link

Every answer has to link the family to a page, and the Search API does not
reliably supply one: `documentUrl` is null for content uploaded as files rather
than crawled, which is how this base is populated. `_resolve_url` in
`src/app/tools/knowledge.py` tries three things in order:

1. The chunk's own `documentUrl`. Populated only for crawled content.
2. A curesyngap1.org URL written inside the passage text. The uploaded bundles
   name the page each section came from, so about one chunk in six carries its
   exact link.
3. The document's landing page, from `DOCUMENT_URLS`. All twenty URLs embedded
   in the six documents were checked against the live site and return 200.

Step 3 is approximate by nature, and it is the common case. A bundle covers
several pages, so a passage about clinical trials inside `04-clinical-care` is
linked to `/doctors/` rather than to `/clinical-trials/`. The answer text stays
correct and the link lands the reader on a real, related page, but it is not
always the page the passage came from. Crawling the site instead of uploading
files would populate `documentUrl` and remove the guess; the crawler is blocked
by the site's bot protection, which Ryan owns unblocking.

This is also why `TwilioKnowledgeSource` calls the Search API directly rather
than through `tac`'s `search_knowledge_base()`: that helper parses responses
into `KnowledgeChunkResult`, which keeps `content`, `knowledgeId`, `createdAt`
and `score` and discards `documentTitle` and `documentUrl`.

### Relevance

Semantic search returns its nearest matches for any question at all, so an
off-topic question comes back with passages rather than with nothing. `KB_MIN_SCORE`
(default `0.3`) drops the weak tail, and it cannot do more than that: "what is
the weather in Denver" matches the Colorado clinic page at 0.8. Judging whether
the passages actually answer the question is the model's job, which is what
prompt rules 6 and 12 are for. Verified: that question gets a refusal, not an
invented answer.

### Known content gaps

The base has no donate, fundraise, events or contact page. Two of the three
questions this agent exists to answer are affected: "How do I donate?" is
escalated rather than answered, and "How do I run a fundraiser?" returns the
`giving@cureSYNGAP1.org` address from a resources page instead of the
fundraising page. Adding those pages to the knowledge base is the single highest
-value change available, and it needs no code.

### Answering with no Twilio account

Leave `TWILIO_KNOWLEDGE_BASE_ID` unset and the agent searches
`src/app/data/kb_fixture.json`: eight hand-written page summaries matched by
keyword overlap. That is what `docker compose run --rm chat` and the whole test
suite use, so both run with no credentials. It is a test double, not content
anyone should act on, and it behaves differently from the real thing in one way
that matters: keyword matching returns nothing for an unrelated question, where
semantic search returns weak matches.

## Memory

A returning contact is recognized without re-introducing themselves. Twilio
Conversation Memory does the work; this application only passes the retrieved
memory into the model call.

Check it against the live account:

```bash
docker compose run --rm memory-e2e --address whatsapp:+1...
```

Four steps, each reported pass or fail: identity resolution (the address to a
profile id), the profile read, recall (observations, summaries, past
communications), and injection (the prompt prepended to the model call). The
last step prints exactly what the model is told about that contact, which is the
only reliable way to see what has accumulated.

A profile appears on the contact's first inbound message, so the check reports
no profile until one has been sent.

Two things are worth knowing before reading the output:

- **The identifier type matters.** A WhatsApp profile is keyed on the full
  address, `whatsapp:+1...`, under identifier type `whatsapp`; an SMS one on the
  bare E.164 number under `phone`. Looking up the wrong type returns no profile,
  which is indistinguishable from a first-time contact. Valid types are `email`,
  `phone`, `pushUserID`, `whatsapp` and `chat`.
- **`GET /Profiles/{id}` returns traits only.** Observations and summaries come
  back from `/Recall`, so a profile that looks empty may not be. That is why the
  check uses recall rather than the profile read alone.

### Traits written when a conversation closes

When Conversation Orchestrator closes a conversation, `src/app/traits.py` writes
two trait groups to the contact's profile. Unlike observations and summaries,
every trait is declared on the Memory Store with a type and a validation rule,
so only what is listed here can be stored.

| Group | Traits | Written by | In the prompt |
| --- | --- | --- | --- |
| `Engagement` | `conversationCount`, `firstContactAt`, `lastContactAt`, `lastChannel`, `lastConversationId`, `escalationCount`, `lastEscalatedAt` | The app, from what it already knows | No |
| `Interests` | `role`, `topics`, `preferredLanguage` | One model call per conversation, limited to fixed vocabularies by a strict JSON schema | Yes |

`role` is one of `parent_caregiver`, `family_member`, `clinician`, `researcher`,
`donor_supporter`, `other` or `unknown`, and `unknown` never replaces a known
value. `topics` accumulates across conversations from `research`,
`clinical_trials`, `family_support`, `getting_started`, `events`, `fundraising`,
`donating`, `advocacy` and `other`. Nothing about the child and no email
address is stored: the schema has nowhere to put them, and anything outside the
vocabularies is dropped before the write.

`PROFILE_TRAITS` controls it: `all` (the default), `engagement` for the
counters alone with no model call, or `off`.

It is best effort. TAC reports a close only for conversations it still holds in
memory, so one that closes while the free instance is asleep, or across a
redeploy, writes nothing and `conversationCount` undercounts. A write that fails
is logged as `Traits: writing failed` and never affects a reply. A successful
one logs `Traits: profile=...` with the names of the traits written.

The groups must exist on the store before the app can write them; the profile
PATCH refuses undeclared traits. Provisioning declares them, and re-running it
adds any trait added to `TRAIT_GROUPS` since. A trait cannot move between
groups once written, so treat the names as permanent.

### Retention is not settled

Recall is on, and that is a deliberate interim choice rather than a policy: the
team is testing with its own phones, and turning it off would hide behavior that
has to be understood before it is decided. Keep both facts in mind while working
on this:

- **What it buys.** A returning family does not repeat themselves. Ask "do you
  have any memories about me" and the agent recites what it holds.
- **What it costs.** Observations and summaries are written from whatever the
  family said. Nothing distinguishes "wants to run a fundraiser" from "my
  daughter has twenty seizures a day". The prompt forbids the agent from
  repeating health details back, and that governs the model's output, not what
  the platform stores.

`MEMORY_MODE=never` turns recall off in one line, and `memoryExtractionEnabled:
false` on the Conversation Configuration stops the writing. Identity resolution
survives either, so a returning family is still recognized.

Settle this before real families are on it. The questions, and the levers, are
in [Open decision 3](docs/decisions.md).

## What is stubbed, and who owns it

- **Escalation delivery.** `LoggingEscalation` records and logs the question
  instead of sending it. The agent, the prompt rule, and the tests are complete;
  only the transport is missing. See
  [docs/decisions.md](docs/decisions.md) for the options and why Twilio Email is
  not the obvious choice.

- **Memory retention policy.** Memory itself works. The policy is what is
  missing. Identity resolution, traits, observations and conversation
  summaries are all live, and all are injected into the next message's context.
  Extraction is not selective, so a family describing seizures or medications
  would have that retained the same way as a fundraising question. What may be
  kept, for how long, and what families are told about it is an open decision:
  see [docs/decisions.md](docs/decisions.md). Run
  `docker compose run --rm memory-e2e --address <address>` to see exactly what is
  stored about a contact.

- **Voice.** Not wired. TAC supplies `VoiceChannel` and ConversationRelay when
  text is proven.

## Safety

The agent must not give medical advice, diagnose, or interpret symptoms or test
results. Those rules live in [prompts/system.md](prompts/system.md), a test
asserts they are still present, and a Foundation content reviewer signs off
before launch. Spot-checked: asked whether to increase a child's Keppra dose,
the agent declines, directs the family to the prescribing clinician, and names
the emergency signs to act on.

Two gates remain before real families are on it:

- **The adversarial test set.** Off-topic questions, attempts to extract medical
  advice, and prompt injection. Not yet built.
- **A retention decision.** The prompt stops the agent repeating a family's
  health details back to them, and does nothing about what Conversation Memory
  stores. Extraction and recall are both on, deliberately and provisionally, and
  extraction is not selective. See [Retention is not settled](#retention-is-not-settled).

## Known upstream issues

**WhatsApp contacts cannot be resolved to a memory profile
(`twilio-agent-connect` 2.4.0).** `tac.retrieve_memory()` resolves a profile
itself when the session carries no `profile_id`, deriving the identifier type as
`"email" if "@" in address else "phone"` and passing the address through
unchanged (`tac/core/tac.py:180`). A WhatsApp address is `whatsapp:+1...` and
its profile is keyed on identifier type `whatsapp` with the prefix intact, so
the lookup matches nothing and a returning family is met as a stranger.

`src/app/memory.py` works around it by resolving the profile and setting it on
the session before retrieval, which is why the channels keep TAC's default
`memory_mode` of `never`: retrieval happens in `_recall`, not in TAC. Report it
upstream and delete `app.memory` when a fixed version is pinned.

**`READ` delivery status fails validation (`twilio-agent-connect` 2.4.0).** Every
WhatsApp read receipt logs a `ValidationError` for
`recipients.0.deliveryStatus`. The SDK hardcodes
`Literal["INITIATED", "IN_PROGRESS", "DELIVERED", "COMPLETED", "FAILED"]` in
three models (`tac/models/conversation.py`, `tac/models/memory.py`,
`tac/models/tac.py`) and WhatsApp sends `READ`.

It is log noise, not lost messages. The events that fail are delivery-status
updates for the agent's own outbound messages, which TAC's `_is_own_message`
check would discard anyway; validation simply happens first. Confirmed against
the logs: every inbound message received a reply.

Left alone rather than worked around, because patching a literal inside three
SDK models to silence a log line is the more fragile choice. Report it upstream
at <https://github.com/twilio/twilio-agent-connect-python> and drop this section
when a fixed version is pinned.

## Ownership

| Account | Owner |
| --- | --- |
| Twilio | Caitlyn Olmer |
| OpenAI | Caitlyn Olmer |
| Hosting | TBD |

## License

This project is licensed under the [MIT License](LICENSE).
