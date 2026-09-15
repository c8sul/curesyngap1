from app.prompt import FALLBACK_REPLY, MAX_REPLY_CHARS, TOO_LONG_REPLY, load_system_prompt


def test_system_prompt_forbids_medical_advice_and_requires_links():
    prompt = load_system_prompt().lower()

    assert "never give medical advice" in prompt
    assert "curesyngap1.org" in prompt
    assert "search_knowledge" in prompt
    assert "escalate_to_team" in prompt


def test_system_prompt_declines_off_topic_requests_and_limits_length():
    prompt = load_system_prompt().lower()

    assert "only help with syngap1 and cure syngap1" in prompt
    assert "under 600 characters" in prompt


def test_canned_replies_fit_the_delivery_limit():
    # Twilio's hard limit is 1600; MAX_REPLY_CHARS leaves headroom below it.
    assert MAX_REPLY_CHARS < 1600
    assert len(FALLBACK_REPLY) <= MAX_REPLY_CHARS
    assert len(TOO_LONG_REPLY) <= MAX_REPLY_CHARS
