"""A stand-in for the OpenAI client, scripted turn by turn."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None
    role: str = "assistant"

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ]
        if exclude_none:
            data = {key: value for key, value in data.items() if value is not None}
        return data


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]


@dataclass
class FakeOpenAI:
    """Returns `turns` in order, recording the kwargs of each call.

    `error` raises instead of answering, to exercise the fallback path.
    """

    turns: list[FakeMessage] = field(default_factory=list)
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chat = _Chat(self)

    async def _create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if not self.turns:
            raise AssertionError("FakeOpenAI ran out of scripted turns")
        return FakeResponse(choices=[FakeChoice(message=self.turns.pop(0))])


class _Chat:
    def __init__(self, client: FakeOpenAI) -> None:
        self.completions = _Completions(client)


class _Completions:
    def __init__(self, client: FakeOpenAI) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> FakeResponse:
        return await self._client._create(**kwargs)


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> FakeToolCall:
    return FakeToolCall(id=call_id, function=FakeFunction(name=name, arguments=arguments))
