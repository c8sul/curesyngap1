"""Talk to the agent from a terminal, with no Twilio account involved.

    docker compose run --rm chat

Needs OPENAI_API_KEY only. Exercises the same `Agent.respond` the SMS webhook
calls, against the checked-in knowledge fixture, so prompt and tool changes can
be judged before a phone number exists.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.agent import Agent  # noqa: E402
from app.config import AgentSettings  # noqa: E402
from app.prompt import load_system_prompt  # noqa: E402
from app.tools.escalation import EscalationContext, LoggingEscalation  # noqa: E402
from app.tools.knowledge import FixtureKnowledgeSource  # noqa: E402

CONVERSATION_ID = "local-chat"


async def main() -> None:
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY in .env first.")

    escalation = LoggingEscalation()
    agent = Agent(
        openai_client=AsyncOpenAI(),
        knowledge_source=FixtureKnowledgeSource(),
        escalation=escalation,
        system_prompt=load_system_prompt(),
        settings=AgentSettings.from_env(),
    )
    history: list[dict[str, object]] = []

    print("Ask a question. Ctrl-C to quit.\n")
    while True:
        try:
            question = input("you: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue

        history.append({"role": "user", "content": question})
        reply = await agent.respond(
            history=history,
            escalation_context=EscalationContext(
                conversation_id=CONVERSATION_ID,
                channel="CHAT",
                contact_address="+15555550100",
            ),
        )
        print(f"agent: {reply}\n")

    if escalation.sent:
        print(f"{len(escalation.sent)} question(s) escalated this session.")


if __name__ == "__main__":
    asyncio.run(main())
