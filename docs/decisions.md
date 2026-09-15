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

## Open decision 3: what memory may retain about a family

**Status:** Open. Extraction and recall are both enabled in the meantime.  
**Decision owner:** Caitlyn Olmer, with Nathan on implementation  
**Interim choice made:** 2026-09-15

### Interim choice

Recall is left on, with `MEMORY_MODE=always`, while the team is the only group
messaging the agent. The reasoning is that the behavior has to be visible to be
decided on: with retrieval off, the agent reported having no memory of a
contact while observations were accumulating in the store regardless. Nothing
about that choice makes it the right setting for real families.

Revisit before anyone outside the team is given the number.

### What is being written today

`memoryExtractionEnabled` is true on the Conversation Configuration, and that
alone drives the full extraction pipeline. The Memory Store carries its own
`intelligenceServiceId`, created with the store, and its operators run without
anything being listed in the configuration's `intelligenceConfigurationIds`.

Three kinds of memory are therefore written and read back:

- **Traits.** The contact address, keyed by identifier type.
- **Observations.** Short statements about the person, each carrying a `source`
  of `intelligence_operatorresult_...`.
- **Conversation summaries.** A paragraph per closed conversation, describing
  what was asked and what the agent answered.

All three are injected into the model's context on the next message, as roughly
1.5 KB of prompt. `docker compose run --rm memory-e2e --address <address>`
prints exactly what the model is told about a contact.

### The decision this forces

Extraction is not selective. Observations and summaries are written from
whatever the family said, and families describe seizures, medications and
diagnoses. Nothing in the pipeline distinguishes "wants to run a fundraiser"
from "my daughter has twenty seizures a day", and the second would be retained
the same way.

The agent's prompt forbids repeating a family's health details back to them,
but that rule governs the model's output, not what the platform stores. A
prompt rule cannot constrain extraction.

So, before real families are on this:

1. What may be retained about a family, and for how long.
2. Whether observations and summaries are kept, restricted to non-clinical
   topics, or turned off.
3. What families are told about what is remembered.
4. Who can read the store, and who reviews what accumulates in it.

### The levers

- `memoryExtractionEnabled: false` on the Conversation Configuration stops
  observations and summaries. Identity resolution and traits are unaffected, so
  a returning family is still recognized; they simply repeat their question.
- Attaching a purpose-built operator to `intelligenceConfigurationIds`
  constrains what is extracted, and is the option that keeps memory useful.
  Whether it can be scoped tightly enough is unverified.
- Removing what has accumulated is a data-plane call per observation.

The content in the store was produced by test messages from a team member's own
phone, so nothing sensitive has been retained. That is a property of who has
messaged it so far, not of the configuration.
