"""Unit tests for IntentClassifier — mocked Anthropic client, no live API calls."""
import pytest
from unittest.mock import MagicMock

from app.brain.intent import IntentClassifier


# ── _fmt_history (pure string formatting) ────────────────────────────────────

def test_fmt_history_empty_list():
    assert IntentClassifier._fmt_history([]) == ""


def test_fmt_history_single_user_turn():
    history = [{"role": "user", "content": "What's my schedule?"}]
    result = IntentClassifier._fmt_history(history)
    assert "User: What's my schedule?" in result


def test_fmt_history_includes_assistant_turn():
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result = IntentClassifier._fmt_history(history)
    assert "User: Hello" in result
    assert "Assistant: Hi there!" in result


def test_fmt_history_truncates_long_content():
    long_msg = "x" * 400
    history = [{"role": "user", "content": long_msg}]
    result = IntentClassifier._fmt_history(history)
    # Content is capped at 300 chars — the 301st character must not appear
    assert "x" * 301 not in result


def test_fmt_history_respects_max_turns():
    """Only the last max_turns pairs should appear in the output."""
    history = [
        {"role": "user",      "content": "msg1"},
        {"role": "assistant", "content": "resp1"},
        {"role": "user",      "content": "msg2"},
        {"role": "assistant", "content": "resp2"},
        {"role": "user",      "content": "msg3"},
        {"role": "assistant", "content": "resp3"},
    ]
    result = IntentClassifier._fmt_history(history, max_turns=2)
    assert "msg3" in result
    assert "resp3" in result
    assert "msg1" not in result
    assert "resp1" not in result


def test_fmt_history_contains_header():
    history = [{"role": "user", "content": "Hi"}]
    result = IntentClassifier._fmt_history(history)
    assert "Recent conversation" in result


# ── classify() — happy path ───────────────────────────────────────────────────

def _make_classifier(response_text: str) -> IntentClassifier:
    """Return a classifier whose Anthropic client is mocked to return response_text."""
    classifier = IntentClassifier()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_client.messages.create.return_value = mock_response
    classifier._client = mock_client
    return classifier


def test_classify_returns_parsed_intent():
    classifier = _make_classifier(
        '{"intent": "gmail_read", "confidence": 0.95, "params": {"action": "list"}}'
    )
    result = classifier.classify("Check my email")
    assert result["intent"] == "gmail_read"
    assert result["confidence"] == 0.95
    assert result["params"]["action"] == "list"


def test_classify_chat_intent():
    classifier = _make_classifier(
        '{"intent": "chat", "confidence": 0.9, "params": {}}'
    )
    result = classifier.classify("Hello there")
    assert result["intent"] == "chat"
    assert result["params"] == {}


def test_classify_strips_json_markdown_fence():
    json_in_fence = '```json\n{"intent": "calendar_read", "confidence": 0.88, "params": {}}\n```'
    classifier = _make_classifier(json_in_fence)
    result = classifier.classify("What's on my calendar?")
    assert result["intent"] == "calendar_read"


def test_classify_passes_history_to_client():
    """Verify that conversation history is forwarded when provided."""
    classifier = _make_classifier('{"intent": "chat", "confidence": 0.8, "params": {}}')
    history = [
        {"role": "user",      "content": "Earlier message"},
        {"role": "assistant", "content": "Earlier reply"},
    ]
    classifier.classify("yes", history=history)
    call_kwargs = classifier._client.messages.create.call_args
    prompt_content = call_kwargs[1]["messages"][0]["content"]
    assert "Earlier message" in prompt_content


# ── classify() — error / fallback paths ──────────────────────────────────────

def test_classify_falls_back_on_api_error():
    classifier = IntentClassifier()
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("Connection error")
    classifier._client = mock_client

    result = classifier.classify("Hello")
    assert result == {"intent": "chat", "confidence": 0.5, "params": {}}


def test_classify_falls_back_on_invalid_json():
    classifier = _make_classifier("This is not JSON at all")
    result = classifier.classify("Tell me something")
    assert result["intent"] == "chat"
    assert result["confidence"] == 0.5


def test_classify_falls_back_on_empty_response():
    classifier = _make_classifier("")
    result = classifier.classify("Hi")
    assert result["intent"] == "chat"
