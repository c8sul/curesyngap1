# CURE SYNGAP1 SMS Agent

A volunteer Global Impact Week project for [CURE SYNGAP1](https://curesyngap1.org/), a rare-disease nonprofit.

Families will be able to text a phone number and ask plain-language questions such as:

- "Tell me about SYNGAP1."
- "How do I run a fundraiser?"
- "How do I donate?"

The agent should answer briefly and link the family to the most relevant page on curesyngap1.org.

## Intended flow

```text
Twilio inbound webhook
  -> retrieve relevant content from the knowledge base
  -> call an OpenAI model
  -> reply through Twilio Conversations
```

## Current status

- The Twilio account exists and is funded.
- The Twilio Enterprise Knowledge crawl of curesyngap1.org is currently blocked. The site sits behind bot protection that returns HTTP 403 to the crawler.
- We are asking for the Twilio crawler to be allowlisted. The fallback is to export the WordPress content, convert it, and import it into the knowledge base.
- **Do not assume the knowledge base is populated yet.**
- A phone number has not been provisioned.
- Carrier registration, through toll-free verification or 10DLC, takes approximately 1–2+ weeks. Plan for local testing first.

## Open technical decision

The implementation stack is intentionally undecided. See [docs/decisions.md](docs/decisions.md) for the two options and their hosting implications.

Nathan will make the decision before application scaffolding begins. This repository currently contains documentation and configuration examples only.

## Local setup

Application setup instructions will be added after the implementation stack is selected and scaffolded.

Copy `.env.example` to `.env` when local development begins. Never commit credentials or secrets.

## Ownership

Assign the accountable owner for each external account before deployment.

| Account | Owner |
| --- | --- |
| Twilio | TBD |
| OpenAI | TBD |
| Hosting | TBD |

## Safety and response principles

The draft agent behavior is documented in [prompts/system.md](prompts/system.md). In short, the agent should provide concise, source-linked information and must not give medical advice or interpret symptoms.

## License

This project is licensed under the [MIT License](LICENSE).
