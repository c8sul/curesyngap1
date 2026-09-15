"""Knowledge search, behind one interface with two implementations.

`FixtureKnowledgeSource` serves a small hand-written set of curesyngap1.org
pages so the agent loop is testable before Enterprise Knowledge is populated.
`TwilioKnowledgeSource` calls Enterprise Knowledge and attaches a source URL to
each chunk.

The URL matters: every answer must link the family to the page they need next.
Enterprise Knowledge search returns `content`, `knowledge_id`, `created_at` and
`score` per chunk, with no URL, so the URL is resolved from `knowledge_id`
through `KNOWLEDGE_ID_URLS`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tac.tools.base import TACTool, function_tool

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "data" / "kb_fixture.json"

# knowledge_id -> page URL, for chunks returned by Enterprise Knowledge.
# Populate once the crawl of curesyngap1.org succeeds and the knowledge source
# IDs are known.
KNOWLEDGE_ID_URLS: dict[str, str] = {}

SITE_ROOT = "https://curesyngap1.org/"


@dataclass(frozen=True)
class KBChunk:
    """One retrieved passage and the page it came from."""

    content: str
    url: str
    title: str | None = None
    score: float | None = None


class KnowledgeSource(Protocol):
    """A searchable body of CURE SYNGAP1 content."""

    async def search(self, query: str, top_k: int) -> list[KBChunk]: ...


class FixtureKnowledgeSource:
    """Keyword search over a checked-in set of page summaries.

    Scores a chunk by how many query terms appear in its text, so it behaves
    enough like a retrieval step to exercise the agent loop and the
    always-include-the-link rule.
    """

    def __init__(self, chunks: list[KBChunk] | None = None) -> None:
        self._chunks = chunks if chunks is not None else load_fixture_chunks()

    async def search(self, query: str, top_k: int) -> list[KBChunk]:
        terms = _content_terms(query)
        if not terms:
            return []
        scored: list[tuple[float, KBChunk]] = []
        for chunk in self._chunks:
            haystack = _normalize(f"{chunk.title or ''} {chunk.content}")
            hits = sum(1 for term in terms if term in haystack)
            if hits:
                scored.append((hits / max(len(terms), 1), chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            KBChunk(content=c.content, url=c.url, title=c.title, score=round(score, 3))
            for score, c in scored[:top_k]
        ]


class TwilioKnowledgeSource:
    """Enterprise Knowledge search, with source URLs attached."""

    def __init__(self, knowledge_client: object, knowledge_base_id: str) -> None:
        self._client = knowledge_client
        self._knowledge_base_id = knowledge_base_id

    async def search(self, query: str, top_k: int) -> list[KBChunk]:
        results = await self._client.search_knowledge_base(  # type: ignore[attr-defined]
            knowledge_base_id=self._knowledge_base_id,
            query=query,
            top_k=top_k,
        )
        return [
            KBChunk(
                content=result.content,
                url=KNOWLEDGE_ID_URLS.get(result.knowledge_id, SITE_ROOT),
                score=result.score,
            )
            for result in results
        ]


def load_fixture_chunks(path: Path = FIXTURE_PATH) -> list[KBChunk]:
    """Load the checked-in page summaries."""
    raw = json.loads(path.read_text())
    return [
        KBChunk(content=entry["content"], url=entry["url"], title=entry.get("title"))
        for entry in raw
    ]


# Question words and filler carry no topic, so matching on them would make any
# question look like a hit against any page.
_STOPWORDS = frozenset(
    """
    about also anything are ask been can could did does doing for from get give
    had has have how into its like more most much need only our out please
    should some tell than that the their them then there these they this those
    was were what when where which who whom why will with would you your
    """.split()
)


def _content_terms(query: str) -> set[str]:
    """The topic-bearing words of a query."""
    return {
        term for term in _normalize(query).split() if len(term) > 2 and term not in _STOPWORDS
    }


def _normalize(text: str) -> str:
    return "".join(char if char.isalnum() else " " for char in text.lower())


def build_knowledge_tool(source: KnowledgeSource, top_k: int = 5) -> TACTool:
    """Build the LLM-facing knowledge search tool over `source`."""

    @function_tool(
        name="search_knowledge",
        description=(
            "Search CURE SYNGAP1 website content for information about SYNGAP1, "
            "clinical trials, research, donating, and running a fundraiser. Returns "
            "passages, each with the URL of the page it came from. The input MUST be "
            "a question in the form of a string."
        ),
    )
    async def search_knowledge(query: str) -> list[dict[str, object]]:
        """Search CURE SYNGAP1 content.

        Args:
            query: The question to search for.

        Returns:
            Passages, each with `content`, `url`, `title` and `score`.
        """
        chunks = await source.search(query, top_k)
        return [
            {"content": c.content, "url": c.url, "title": c.title, "score": c.score} for c in chunks
        ]

    return search_knowledge
