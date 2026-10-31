import json

import httpx
import pytest

from shortlist import notify
from shortlist.notify import TELEGRAM_LIMIT, format_digest, send_telegram


def row(n, score=90):
    return {"ats": "lever", "slug": "acme", "job_id": str(n), "company": "Acme", "title": f"ML Engineer {n}",
            "location": "NYC", "url": f"https://jobs.example.com/{n}", "score": score,
            "result": json.dumps({"one_line_reason": f"Strong ranking fit {n}."})}


def test_digest_lists_title_company_score_reason_and_link():
    [(text, included)] = format_digest([row(1, 95), row(2, 80)])
    assert included == [row(1, 95), row(2, 80)]
    assert text.startswith("Shortlist: 2 new matches\n")  # a single message has no (1/1) part marker
    assert "95 | ML Engineer 1 | Acme\nNYC\nStrong ranking fit 1.\nhttps://jobs.example.com/1" in text
    assert text.index("ML Engineer 1") < text.index("ML Engineer 2")


def test_long_digest_is_split_into_messages_within_the_limit():
    rows = [row(n) for n in range(100)]
    messages = format_digest(rows)
    assert len(messages) > 1
    assert all(len(text) <= TELEGRAM_LIMIT for text, _ in messages)
    assert [r for _, part in messages for r in part] == rows  # every match once, best first
    for i, (text, _) in enumerate(messages, 1):
        assert text.startswith(f"Shortlist: 100 new matches ({i}/{len(messages)})\n")


def test_empty_digest_has_no_messages():
    assert format_digest([]) == []


def test_send_telegram_posts_json_without_link_previews():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    send_telegram("hello", "123:SECRET", "42", transport=httpx.MockTransport(handler))
    [request] = seen
    assert request.method == "POST" and request.url.path == "/bot123:SECRET/sendMessage"
    assert json.loads(request.content) == {"chat_id": "42", "text": "hello", "link_preview_options": {"is_disabled": True}}


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}),
    httpx.Response(200, json={"ok": False, "description": "nope"}),
    httpx.Response(502, text="<html>bad gateway</html>"),
])
def test_send_telegram_errors_never_contain_the_token(response):
    with pytest.raises(RuntimeError) as err:
        send_telegram("hello", "123:SECRET", "42", transport=httpx.MockTransport(lambda request: response))
    assert "SECRET" not in str(err.value) and "Telegram sendMessage failed" in str(err.value)


def test_send_telegram_network_error_hides_url():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RuntimeError, match="ConnectError") as err:
        send_telegram("hello", "123:SECRET", "42", transport=httpx.MockTransport(handler))
    assert "SECRET" not in str(err.value) and err.value.__cause__ is None


def capture(response):
    seen = []

    def handler(request):
        seen.append(request)
        return response

    return seen, httpx.MockTransport(handler)


def test_digest_parts_fit_the_channel_limit():
    rows = [row(n) for n in range(60)]
    messages = format_digest(rows, notify.LIMITS["telegram"])
    assert len(messages) > 1 and all(len(text) <= notify.LIMITS["telegram"] for text, _ in messages)


def test_missing_env_and_send_by_channel(monkeypatch):
    assert notify.missing_env(["telegram"], {"TELEGRAM_BOT_TOKEN": "x"}) == ["TELEGRAM_CHAT_ID"]
    sent = []
    monkeypatch.setitem(notify.SENDERS, "telegram", lambda text, env: sent.append((text, env["TELEGRAM_CHAT_ID"])))
    notify.send("telegram", "hi", {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "42"})
    assert sent == [("hi", "42")]
