from app.prompt import load_system_prompt


def test_system_prompt_forbids_medical_advice_and_requires_links():
    prompt = load_system_prompt().lower()

    assert "never give medical advice" in prompt
    assert "curesyngap1.org" in prompt
    assert "search_knowledge" in prompt
    assert "escalate_to_team" in prompt
