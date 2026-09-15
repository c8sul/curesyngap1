"""The agent loop: retrieve, answer, escalate.

One turn is `respond()`: give the model the conversation so far and two tools,
let it call them, and return the text it settles on. The loop is capped and
time-bounded so a messaging channel is never left waiting.
"""

import asyncio
import json
from typing import Any

from tac.core.logging import get_logger
from tac.tools.base import TACTool

from app.config import AgentSettings
from app.prompt import FALLBACK_REPLY
from app.tools.escalation import Escalation, EscalationContext, build_escalation_tool
from app.tools.knowledge import KnowledgeSource, build_knowledge_tool

logger = get_logger(__name__)


class Agent:
    """Answers one message at a time from a knowledge source.

    `openai_client` is whatever the caller wants the model call to go through.
    On a live channel that is TAC's memory-injecting wrapper from
    `with_tac_memory`; in tests and the local harness it is a plain client.
    """

    def __init__(
        self,
        openai_client: Any,
        knowledge_source: KnowledgeSource,
        escalation: Escalation,
        system_prompt: str,
        settings: AgentSettings,
    ) -> None:
        self._client = openai_client
        self._knowledge_source = knowledge_source
        self._escalation = escalation
        self._system_prompt = system_prompt
        self._settings = settings

    async def respond(
        self,
        history: list[dict[str, Any]],
        escalation_context: EscalationContext,
        client: Any | None = None,
    ) -> str:
        """Produce a reply to the last message in `history`.

        `history` is the running conversation in OpenAI message form, ending
        with the user's newest message. On success, the assistant message plus
        any tool calls and their results are appended to it, so a caller that
        keeps `history` across turns gives the model the tool results it
        already saw.

        The turn runs against a copy and is committed only once it completes.
        A round that is abandoned partway leaves behind an assistant message
        carrying `tool_calls` with no matching tool results, which the
        Chat Completions API rejects; committing whole turns keeps a timeout
        from breaking every later turn of the same conversation.

        `client` replaces the default client for this turn, which is how a
        per-conversation memory-injecting wrapper is supplied.

        Returns the reply text, or a fallback if the model errors or times out.
        """
        turn = list(history)
        try:
            reply = await asyncio.wait_for(
                self._run_tool_loop(turn, escalation_context, client or self._client),
                timeout=self._settings.timeout_seconds,
            )
        except TimeoutError:
            logger.error(
                f"Agent timed out after {self._settings.timeout_seconds}s on "
                f"conversation {escalation_context.conversation_id}"
            )
            return FALLBACK_REPLY
        except Exception:
            logger.error(
                f"Agent failed on conversation {escalation_context.conversation_id}",
                exc_info=True,
            )
            return FALLBACK_REPLY
        history[:] = turn
        return reply

    async def _run_tool_loop(
        self,
        history: list[dict[str, Any]],
        escalation_context: EscalationContext,
        client: Any,
    ) -> str:
        tools = {
            tool.name: tool
            for tool in (
                build_knowledge_tool(self._knowledge_source, top_k=self._settings.top_k),
                build_escalation_tool(self._escalation, escalation_context),
            )
        }
        schemas = [tool.to_openai_format() for tool in tools.values()]

        for _ in range(self._settings.max_tool_iterations):
            message = await self._complete(client, history, schemas)
            history.append(message)

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return message.get("content") or FALLBACK_REPLY

            for call in tool_calls:
                history.append(await self._run_tool(tools, call))

        # Out of tool iterations. Ask once more, with no tools, for a final answer.
        message = await self._complete(client, history, tools=None)
        history.append(message)
        return message.get("content") or FALLBACK_REPLY

    async def _complete(
        self,
        client: Any,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Call the model once and return its message as a plain dict."""
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [{"role": "system", "content": self._system_prompt}, *history],
        }
        if tools:
            kwargs["tools"] = tools
        if self._settings.reasoning_effort:
            kwargs["reasoning_effort"] = self._settings.reasoning_effort

        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.model_dump(exclude_none=True)

    async def _run_tool(
        self,
        tools: dict[str, TACTool],
        call: Any,
    ) -> dict[str, Any]:
        """Execute one tool call and return the message carrying its result."""
        call_id = _get(call, "id")
        function = _get(call, "function")
        name = _get(function, "name")
        raw_arguments = _get(function, "arguments") or "{}"

        try:
            arguments = json.loads(raw_arguments)
            tool = tools[name]
            result = await tool(**arguments)
            content = json.dumps(result, default=str)
        except KeyError:
            logger.error(f"Model called unknown tool {name!r}")
            content = json.dumps({"error": f"unknown tool {name}"})
        except Exception as error:
            logger.error(f"Tool {name!r} failed", exc_info=True)
            content = json.dumps({"error": str(error)})

        return {"role": "tool", "tool_call_id": call_id, "content": content}


def _get(obj: Any, key: str) -> Any:
    """Read `key` from a dict or an attribute of the same name."""
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
