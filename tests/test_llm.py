import pytest

from stagefinder.llm import BudgetExceeded, LLMClient, LLMError, LLMFatal, extract_json


class Resp:
    def __init__(self, status, content=None):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def make_post(responses):
    seq = list(responses)
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    post.calls = calls
    return post


def test_extract_json_plain_and_fenced_and_prefixed():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n[1, 2]\n```') == [1, 2]
    assert extract_json('Voici la réponse :\n{"a": [1]}\nMerci') == {"a": [1]}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("aucun")


def test_chat_returns_content_and_counts():
    post = make_post([Resp(200, "ok")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat("sys", "usr") == "ok"
    assert c.calls == 1
    assert post.calls[0]["model"] == "m"


def test_retry_on_429_then_success():
    post = make_post([Resp(429), Resp(500), Resp(200, "fin")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat("s", "u") == "fin"


def test_no_retry_on_401():
    post = make_post([Resp(401)])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.chat("s", "u")
    assert len(post.calls) == 1


def test_401_raises_llm_fatal():
    post = make_post([Resp(401)])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMFatal) as exc:
        c.chat("s", "u")
    assert isinstance(exc.value, LLMError)
    assert len(post.calls) == 1


class RespWithBody(Resp):
    def __init__(self, status, body):
        super().__init__(status)
        self._body = body

    def json(self):
        return self._body


def test_200_with_error_body_raises_llm_error():
    post = make_post([RespWithBody(200, {"error": {"message": "moderation"}})])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMError, match="moderation"):
        c.chat("s", "u")


def test_budget_exceeded():
    post = make_post([Resp(200, "a"), Resp(200, "b")])
    c = LLMClient("k", "m", max_calls=1, post=post, sleep=lambda s: None)
    c.chat("s", "u")
    with pytest.raises(BudgetExceeded):
        c.chat("s", "u")


def test_chat_json_retries_once_with_error_feedback():
    post = make_post([Resp(200, "pas du json"), Resp(200, '{"ok": true}')])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat_json("s", "u") == {"ok": True}
    assert "JSON" in post.calls[1]["messages"][-1]["content"]


def test_chat_json_fails_after_two_attempts():
    post = make_post([Resp(200, "non"), Resp(200, "toujours non")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.chat_json("s", "u")
