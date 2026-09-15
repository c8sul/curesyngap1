# Technical decisions

## Decision 1: implementation stack and hosting

**Status:** Decided — Option A, Python with `twilio-agent-connect`  
**Decision owner:** Nathan  
**Decided:** 2026-09-15

### Context

The service must receive inbound Twilio messages, retrieve relevant CURE SYNGAP1 content, call an OpenAI model, and reply through Twilio Conversations.

The knowledge base is not yet populated because the Twilio Enterprise Knowledge crawler receives HTTP 403 responses from curesyngap1.org. The team is pursuing crawler allowlisting, with a converted WordPress content export as the fallback.

### Option A: Python with `twilio-agent-connect`

Use Python and the `twilio-agent-connect` package, also referred to as TAC core and available on PyPI.

Benefits:

- Enterprise Knowledge search is included.
- Conversation memory is included.
- Multi-channel routing is included.
- Twilio signature validation is included.
- Less custom integration work may be required.

Tradeoffs and hosting implications:

- TAC is FastAPI-based.
- It expects a container-capable host, such as Render or Fly.io.
- It is not a natural fit for a Vercel-only deployment.
- The package classifiers still identify it as Alpha, which introduces maturity and change risk.

### Option B: Node.js with TypeScript, without TAC

Use Node.js and TypeScript and integrate directly with the required Twilio services.

Benefits:

- Fits Vercel cleanly.
- Uses a common TypeScript serverless development and deployment model.
- Avoids depending on an Alpha-classified framework package.

Tradeoffs and hosting implications:

- The team must implement knowledge-base querying.
- The team must implement conversation handling and memory behavior.
- The team must implement the necessary Twilio request validation and routing.
- Vercel is the straightforward hosting choice, but the custom integration surface is larger.

### Decision

Option A. Python with `twilio-agent-connect`, pinned to 2.4.0, running in a
container. Docker is the unit of local development and deployment, so the host
can be Render, Fly.io, or anything else that runs a container.

What TAC supplies and the application therefore does not implement: the FastAPI
server and its `/webhook`, `/twiml` and `/ws` routes; Twilio signature
validation on every route; SMS, WhatsApp, RCS, Chat and Voice channels;
Conversation Memory retrieval and injection into the model call; conversation
session tracking; and reply routing back to the channel the message arrived on.

What the application implements: the tool-calling loop, the knowledge search
tool and its source URLs, the escalation tool, the system prompt, and the
timeout fallback.

Accepted costs:

- TAC is pinned. Read the upstream diff before any version bump; the surfaces
  this application depends on are `TACConfig.from_env`, `TAC.on_message_ready`,
  `TACTool`, and `with_tac_memory`.
- A container host is required. Vercel is not an option.
- Conversation Memory behavior and cross-channel linking are Console
  configuration plus SDK internals rather than application code, so debugging
  recall means reading TAC and the Conversation Configuration.

What this buys later: `VoiceChannel` and ConversationRelay are already
implemented, and voice is the last item in the launch plan.

## Open decision 2: escalation transport

**Status:** Open  
**Decision owner:** Caitlyn Olmer, with Nathan on implementation

### Context

When the agent cannot answer, the question and the conversation so far go to a
person at CURE SYNGAP1 who can reply to the family directly. There is no live
handoff, because no one is guaranteed to be available.

The build plan proposed Twilio Email, on the grounds that it reuses the Twilio
Account SID and Auth Token already configured. That needs a verified sending
domain, and CURE SYNGAP1's domain is administered by the volunteer who runs
their WordPress site, so verifying it is a request to a third party rather than
a configuration step.

Two things are unresolved: which address receives escalations, and which
transport delivers them.

### Options

- Twilio Email, sending from a verified CURE SYNGAP1 subdomain. Reuses existing
  credentials, but blocked on DNS access the team does not have.
- Twilio Email, sending from a domain the team already controls. Same product,
  no dependency on the foundation's DNS, but the mail arrives from an unrelated
  sender.
- SMS to a staff number. No domain verification at all, and the team already has
  a Twilio number. Poor fit for a question plus transcript.
- A Studio Flow or a webhook into whatever the foundation already uses for
  inbound contact.

### Interim state

`src/app/tools/escalation.py` defines the `Escalation` interface and a
`LoggingEscalation` implementation that records the escalation and logs it. The
agent, the prompt rule, and the tests are complete; only the transport is
missing. Choosing one means writing a second implementation of `Escalation`.

That implementation also has to decide what happens to the question text.
`LoggingEscalation` masks the phone number and then logs the question verbatim,
which is fine for a stub read by one developer and not fine for logs shipped to
an aggregator, because a family's question can itself contain health details.
Whoever picks the transport picks the redaction with it.

## Open decision 3: observation extraction

**Status:** Open  
**Decision owner:** Caitlyn Olmer, with Nathan on implementation

### Context

Conversation Memory writes two kinds of memory. Identity traits are written
whenever `memoryExtractionEnabled` is true on the Conversation Configuration:
a profile is created for the sender and matched by phone number on their next
message, which is what lets a returning family skip re-introducing themselves.
Observations, the free-text summaries of what was discussed, are produced by
Conversational Intelligence operators, and an operator only runs if its
intelligence configuration is listed in the configuration's
`intelligenceConfigurationIds`. With that list empty, conversations close and no
observation is written.

### The conflict to resolve first

An operator extracts whatever the conversation contains. Families describe
seizures, medications and diagnoses, so a general-purpose observation operator
would write exactly the health details the agent is forbidden to retain. Turning
observations on is therefore a data-handling decision, not a configuration step.

Resolving it means answering: what an observation may contain, which operator
enforces that, how long observations are retained, and what the foundation tells
families about it.

### What switching it on looks like

Each Memory Store is created with its own `intelligenceServiceId`. Attaching it
to the Conversation Configuration is one PATCH of
`intelligenceConfigurationIds`, which returns HTTP 202 and an operation to poll.
Whether that service alone yields observations, or a purpose-built operator has
to be defined first, is unverified.
