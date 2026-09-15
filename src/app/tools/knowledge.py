"""Knowledge search, behind one interface with two implementations.

`FixtureKnowledgeSource` serves a small hand-written set of curesyngap1.org
pages so the agent loop is testable before Enterprise Knowledge is populated.
It is the stub, and it is what runs unless `TWILIO_KNOWLEDGE_BASE_ID` is set.
`TwilioKnowledgeSource` calls Enterprise Knowledge and attaches a source URL to
each chunk.

The URL matters: every answer must link the family to the page they need next.
Enterprise Knowledge returns `documentUrl` per chunk, but it is null for content
uploaded as files rather than crawled, which is how this knowledge base is
populated. `_resolve_url` therefore tries, in order: the chunk's own
`documentUrl`, a curesyngap1.org URL written inside the passage text, and the
document's landing page from `DOCUMENT_URLS`.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from tac.tools.base import TACTool, function_tool

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "data" / "kb_fixture.json"

KNOWLEDGE_API = "https://knowledge.twilio.com"

# Semantic search always returns its closest matches, so an off-topic question
# comes back with passages rather than with nothing. Dropping the weak tail
# keeps unrelated content out of the model's context. It cannot decide
# relevance on its own: a question about the weather in Denver matches the
# Colorado clinic page strongly, which is why the prompt makes the model judge
# whether the passages answer the question and escalate when they do not.
DEFAULT_MIN_SCORE = 0.3

# Document title -> the page a reader should land on, for chunks whose own text
# carries no URL. Keyed on `documentTitle` rather than `knowledgeId`, because a
# re-upload of the same content changes the id and not the title. Each bundle
# covers several pages; these are the one to send someone to first.
DOCUMENT_URLS: dict[str, str] = {
    "01-about-syngap1": "https://curesyngap1.org/what-is-syngap1/",
    "02-treatment": "https://curesyngap1.org/syngap1-treatment/",
    "03-family-resources": (
        "https://curesyngap1.org/syngap1-resources-for-newly-diagnosed-families/"
    ),
    "04-clinical-care": "https://curesyngap1.org/doctors/",
    "05-research-grants": "https://curesyngap1.org/resources/grants/",
    "06-about-the-organization": "https://curesyngap1.org/mission-and-values/",
}

SITE_ROOT = "https://curesyngap1.org/"

# A curesyngap1.org URL inside a passage. The uploaded documents list the page
# each section came from, so a chunk often carries its own exact link.
_URL_IN_TEXT = re.compile(r"https://curesyngap1\.org/[\w./-]*[\w/]")


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

    A stand-in for Enterprise Knowledge, selected whenever
    `TWILIO_KNOWLEDGE_BASE_ID` is unset. The passages are hand-written, not
    crawled from curesyngap1.org, and some of their URLs are unverified, so
    answers drawn from them demonstrate the loop rather than inform anyone.

    Scoring counts how many query terms appear in a chunk's title and content,
    which is enough to exercise the agent loop and the always-include-the-link
    rule. It is not retrieval: it cannot match a paraphrase, has no notion of
    similarity, and ranks by term count rather than relevance.
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
    """Enterprise Knowledge search, with a source URL attached to each chunk.

    Calls the Search API directly rather than through `tac`'s
    `search_knowledge_base()`, because that helper parses responses into
    `KnowledgeChunkResult`, which drops `documentTitle` and `documentUrl`.
    Both are needed to link an answer to a page.
    """

    SEARCH_TIMEOUT_SECONDS = 8.0

    def __init__(
        self,
        knowledge_base_id: str,
        api_key: str,
        api_secret: str,
        min_score: float = DEFAULT_MIN_SCORE,
        base_url: str = KNOWLEDGE_API,
    ) -> None:
        self._knowledge_base_id = knowledge_base_id
        self._auth = (api_key, api_secret)
        self._min_score = min_score
        self._base_url = base_url

    async def search(self, query: str, top_k: int) -> list[KBChunk]:
        async with httpx.AsyncClient(timeout=self.SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self._base_url}/v2/KnowledgeBases/{self._knowledge_base_id}/Search",
                json={"query": query[:2048], "top": top_k},
                auth=self._auth,
            )
        response.raise_for_status()
        chunks = [_to_chunk(chunk) for chunk in response.json().get("chunks", [])]
        return [chunk for chunk in chunks if chunk.score is None or chunk.score >= self._min_score]


def _to_chunk(chunk: dict[str, object]) -> KBChunk:
    content = str(chunk.get("content") or "")
    title = chunk.get("documentTitle")
    return KBChunk(
        content=content,
        url=_resolve_url(content, chunk.get("documentUrl"), title),
        title=str(title) if title else None,
        score=chunk.get("score"),  # type: ignore[arg-type]
    )


def _resolve_url(content: str, document_url: object, title: object) -> str:
    """The most specific page this passage can be attributed to.

    Falling back to the site root is a poor answer, so it is last: a family
    given the front page has to search the site themselves.
    """
    if document_url:
        return str(document_url)
    found = _URL_IN_TEXT.search(content)
    if found:
        return found.group(0)
    return DOCUMENT_URLS.get(str(title), SITE_ROOT)


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
