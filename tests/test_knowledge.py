from app.tools.knowledge import (
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


async def test_twilio_source_attaches_urls_to_chunks_that_carry_none():
    class FakeChunk:
        def __init__(self, knowledge_id):
            self.content = "Fundraising guidance."
            self.knowledge_id = knowledge_id
            self.score = 0.9

    class FakeClient:
        async def search_knowledge_base(self, knowledge_base_id, query, top_k):
            return [FakeChunk("know_source_known"), FakeChunk("know_source_unmapped")]

    from app.tools import knowledge

    knowledge.KNOWLEDGE_ID_URLS["know_source_known"] = f"{SITE_ROOT}fundraise/"
    try:
        results = await TwilioKnowledgeSource(FakeClient(), "know_kb_1").search("fundraiser", 5)
    finally:
        knowledge.KNOWLEDGE_ID_URLS.pop("know_source_known")

    assert results[0].url == f"{SITE_ROOT}fundraise/"
    # An unmapped source still yields a usable link rather than no link at all.
    assert results[1].url == SITE_ROOT


def test_kb_chunk_defaults():
    chunk = KBChunk(content="text", url=SITE_ROOT)

    assert chunk.title is None
    assert chunk.score is None
