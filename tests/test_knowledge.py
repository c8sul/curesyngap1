import json

import httpx

from app.tools.knowledge import (
    DOCUMENT_URLS,
    SITE_ROOT,
    FixtureKnowledgeSource,
    KBChunk,
    TwilioKnowledgeSource,
    build_knowledge_tool,
    load_fixture_chunks,
)


def test_every_fixture_chunk_has_a_site_url():
    chunks = load_fixture_chunks()
    assert chunks
    assert all(chunk.url.startswith(SITE_ROOT) for chunk in chunks)


async def test_fixture_search_finds_the_fundraiser_page():
    results = await FixtureKnowledgeSource().search("how do I run a fundraiser?", top_k=3)

    assert results
    assert any("fundraise" in chunk.url for chunk in results)


async def test_fixture_search_returns_nothing_for_unrelated_questions():
    results = await FixtureKnowledgeSource().search("what is the weather in Denver", top_k=3)

    assert results == []


async def test_knowledge_tool_exposes_only_query_to_the_model():
    tool = build_knowledge_tool(FixtureKnowledgeSource(), top_k=2)
    schema = tool.to_openai_format()

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "search_knowledge"
    assert list(schema["function"]["parameters"]["properties"]) == ["query"]


async def test_knowledge_tool_returns_urls_with_each_passage():
    tool = build_knowledge_tool(FixtureKnowledgeSource(), top_k=2)

    results = await tool(query="how do I donate?")

    assert results
    assert all(result["url"] for result in results)


def _search_response(chunks: list[dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/Search")
        assert json.loads(request.content)["top"] == 5
        return httpx.Response(200, json={"chunks": chunks})

    return httpx.MockTransport(handle)


async def _search(chunks: list[dict], monkeypatch) -> list:
    """Run a search against a canned Search API response."""
    transport = _search_response(chunks)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    source = TwilioKnowledgeSource("know_kb_1", "SK1", "secret")
    return await source.search("fundraiser", 5)


async def test_a_chunks_own_document_url_is_used_when_the_api_supplies_one(monkeypatch):
    """Crawled content carries `documentUrl`, which beats every other source."""
    results = await _search(
        [
            {
                "content": "Fundraising guidance.",
                "documentUrl": f"{SITE_ROOT}fundraise/",
                "documentTitle": "03-family-resources",
                "score": 0.9,
            }
        ],
        monkeypatch,
    )

    assert results[0].url == f"{SITE_ROOT}fundraise/"
    assert results[0].title == "03-family-resources"
    assert results[0].score == 0.9


async def test_a_url_written_in_the_passage_is_used_when_the_api_supplies_none(monkeypatch):
    """Content uploaded as files has a null `documentUrl`; the bundles name the
    page each section came from, so the passage itself carries the link."""
    results = await _search(
        [
            {
                "content": (
                    "SYNGAP1 & Epilepsy \u2014 https://curesyngap1.org/syngap1-epilepsy/ "
                    "Seizures are common."
                ),
                "documentUrl": None,
                "documentTitle": "01-about-syngap1",
            }
        ],
        monkeypatch,
    )

    assert results[0].url == "https://curesyngap1.org/syngap1-epilepsy/"


async def test_a_passage_with_no_url_falls_back_to_its_documents_landing_page(monkeypatch):
    results = await _search(
        [{"content": "Seizures are common.", "documentUrl": None, "documentTitle": "02-treatment"}],
        monkeypatch,
    )

    assert results[0].url == DOCUMENT_URLS["02-treatment"]


async def test_an_unknown_document_still_yields_a_link_rather_than_none(monkeypatch):
    """A link to the front page is a weak answer, so it is the last resort
    rather than an error, and it keeps the always-include-a-link rule true."""
    results = await _search(
        [{"content": "Something new.", "documentUrl": None, "documentTitle": "99-unmapped"}],
        monkeypatch,
    )

    assert results[0].url == SITE_ROOT


def test_every_mapped_document_url_is_on_the_foundations_site():
    assert DOCUMENT_URLS
    assert all(url.startswith(SITE_ROOT) for url in DOCUMENT_URLS.values())


def test_kb_chunk_defaults():
    chunk = KBChunk(content="text", url=SITE_ROOT)

    assert chunk.title is None
    assert chunk.score is None


async def test_weakly_matching_passages_are_dropped(monkeypatch):
    """Semantic search returns its nearest matches for any question at all, so
    an off-topic question comes back with passages rather than with nothing."""
    results = await _search(
        [
            {"content": "Relevant.", "documentUrl": None, "documentTitle": "02-treatment",
             "score": 0.9},
            {"content": "Barely related.", "documentUrl": None, "documentTitle": "02-treatment",
             "score": 0.1},
        ],
        monkeypatch,
    )

    assert [chunk.content for chunk in results] == ["Relevant."]


async def test_a_passage_with_no_score_is_kept(monkeypatch):
    """`score` is optional on the API response; absent is not the same as low."""
    results = await _search(
        [{"content": "Unscored.", "documentUrl": None, "documentTitle": "02-treatment"}],
        monkeypatch,
    )

    assert len(results) == 1
