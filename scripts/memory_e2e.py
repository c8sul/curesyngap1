"""Check the Conversation Memory round trip against the live Twilio account.

    docker compose run --rm memory-e2e --address whatsapp:+1... [--question "..."]

Answers one question: when this contact messages again, does the agent know who
it is talking to? Four steps, each reported pass or fail:

1. Identity resolution. Look the contact's phone number up in the Memory Store
   and get a profile id back. This is what links a returning family to what they
   said before, and it is what makes the same person's SMS and WhatsApp threads
   one conversation.
2. Profile read. Fetch that profile and show the traits on it.
3. Recall. Ask the store for observations, summaries and past communications.
4. Injection. Build the prompt `with_tac_memory` prepends to a model call, and
   confirm the profile reaches it.

Needs credentials and a Memory Store, so it is not part of `pytest`. The
prompt-building step of `tac`'s adapter is covered offline in
`tests/test_memory.py`.
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from tac import TAC, TACConfig
from tac.adapters.options import AdapterOptions
from tac.adapters.prompt_builder import MemoryPromptBuilder
from tac.models.session import AuthorInfo, ConversationSession

PASS = "PASS"
FAIL = "FAIL"


def identifier_for(address: str) -> tuple[str, str]:
    """The identifier a Memory Store profile is keyed on for this address.

    A WhatsApp profile is keyed on the whole channel address, prefix included,
    under idType `whatsapp`. An SMS number is keyed on the bare E.164 number
    under `phone`. The same person on both channels therefore has two
    identifiers, and looking one up does not find the other.
    """
    if address.startswith("whatsapp:"):
        return "whatsapp", address
    return "phone", address.split(":")[-1]


def _content(entry: object) -> str:
    return str(getattr(entry, "content", entry))


def report(step: str, ok: bool, detail: str) -> bool:
    print(f"[{PASS if ok else FAIL}] {step}: {detail}")
    return ok


async def run(address: str, question: str, conversation_id: str | None) -> int:
    load_dotenv()
    os.environ.setdefault("TWILIO_PHONE_NUMBER", "")

    store_id = os.environ.get("TWILIO_MEMORY_STORE_ID")
    if not store_id:
        print("TWILIO_MEMORY_STORE_ID is not set. Run provision first.", file=sys.stderr)
        return 1

    tac = TAC(config=TACConfig.from_env())
    memory_client = tac.conversation_memory_client
    if memory_client is None:
        print("TAC has no memory client; check TWILIO_MEMORY_STORE_ID.", file=sys.stderr)
        return 1

    id_type, value = identifier_for(address)
    print(f"Store {store_id}\nContact {value} (idType {id_type})\n")
    results = []

    lookup = await memory_client.lookup_profile(id_type=id_type, value=value)
    profile_ids = list(lookup.profiles or [])
    results.append(
        report(
            "identity resolution",
            bool(profile_ids),
            f"{value} normalized to {lookup.normalized_value!r} -> {profile_ids or 'no profile'}",
        )
    )
    if not profile_ids:
        print(
            "\nNo profile yet. A profile is created on the first inbound message from "
            "this number, so send one and re-run."
        )
        return 1

    profile_id = profile_ids[0]
    profile = await memory_client.get_profile(profile_id)
    traits = getattr(profile, "traits", None) or {}
    results.append(report("profile read", bool(profile), f"{profile_id} traits={traits}"))

    session = ConversationSession(
        conversation_id=conversation_id or "conv_conversation_memory_e2e",
        channel="WHATSAPP" if id_type == "whatsapp" else "SMS",
        profile_id=profile_id,
        author_info=AuthorInfo(address=address, participant_id="conv_participant_e2e"),
    )

    # Goes through TAC rather than the memory client directly, because this is
    # the same call the server makes on every inbound message.
    memory = await tac.retrieve_memory(conversation_context=session, query=question)
    counts = {
        name: len(getattr(memory, name, None) or [])
        for name in ("observations", "summaries", "communications")
    }
    results.append(report("recall", memory is not None, f"{counts}"))

    for observation in getattr(memory, "observations", None) or []:
        print(f"      observation: {_content(observation)}")
    for summary in getattr(memory, "summaries", None) or []:
        print(f"      summary: {_content(summary)}")

    prompt = MemoryPromptBuilder.build(
        memory_response=memory,
        context=session,
        options=AdapterOptions(),
    )
    injected = bool(prompt and prompt.strip())
    detail = (
        f"{len(prompt)} characters prepended to the model call" if injected else "nothing built"
    )
    results.append(report("injection", injected, detail))
    if injected:
        print("\n--- what the model is told about this contact ---")
        print(prompt)
        print("--- end ---")

    print(f"\n{sum(results)}/{len(results)} steps passed")
    return 0 if all(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True, help="Contact address, e.g. whatsapp:+1...")
    parser.add_argument(
        "--question",
        default="what did we talk about last time?",
        help="Recall query, which decides which memories are considered relevant",
    )
    parser.add_argument("--conversation-id", default=None, help="Scope recall to one conversation")
    args = parser.parse_args()
    return asyncio.run(run(args.address, args.question, args.conversation_id))


if __name__ == "__main__":
    raise SystemExit(main())
