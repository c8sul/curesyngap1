# Technical decisions

## Open decision 1: implementation stack and hosting

**Status:** Open  
**Decision owner:** Nathan  
**Decision timing:** Before application scaffolding begins

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

Open. Do not scaffold either stack until Nathan selects an option.
