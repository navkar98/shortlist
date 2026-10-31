"""Format the digest of new matches and send it by Telegram (plain httpx).

A channel takes its secrets from environment variables (or .env). The bot token is part of the request URL, so
errors never include a request URL.
"""
import json
import os

import httpx

TELEGRAM_LIMIT = 4096  # sendMessage `text` is 1-4096 characters (Bot API docs)
LIMITS = {"telegram": TELEGRAM_LIMIT}  # characters per message, by channel
REQUIRED_ENV = {"telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]}


def _header(n: int, part: int, parts: int) -> str:
    head = f"Shortlist: {n} new match{'' if n == 1 else 'es'}"
    return head + (f" ({part}/{parts})" if parts > 1 else "")


def _entry(row) -> str:
    reason = json.loads(row["result"])["one_line_reason"]
    return f"\n\n{row['score']} | {row['title'][:200]} | {row['company']}\n{row['location'][:150]}\n{reason[:300]}\n{row['url']}"


def format_digest(rows: list, limit: int = TELEGRAM_LIMIT) -> list[tuple[str, list]]:
    """The digest as one or more messages of at most `limit` characters: (text, rows in that message), best first."""
    header_room = len(_header(len(rows), 99, 99))
    chunks: list[list[tuple]] = [[]]
    size = header_room
    for row in rows:
        entry = _entry(row)
        if chunks[-1] and size + len(entry) > limit:
            chunks.append([])
            size = header_room
        chunks[-1].append((row, entry))
        size += len(entry)
    if not rows:
        return []
    return [(_header(len(rows), i, len(chunks)) + "".join(entry for _, entry in chunk), [row for row, _ in chunk])
            for i, chunk in enumerate(chunks, 1)]


def missing_env(channels: list[str], env=os.environ) -> list[str]:
    return [name for channel in channels for name in REQUIRED_ENV[channel] if not env.get(name)]


def _post(label: str, url: str, payload: dict, transport: httpx.BaseTransport | None) -> httpx.Response:
    with httpx.Client(timeout=20, transport=transport) as client:
        try:
            return client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError(f"{label} request failed ({type(e).__name__})") from None


def send_telegram(text: str, token: str, chat_id: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """Send one message. Errors never include the request URL, because it contains the bot token."""
    r = _post("Telegram", f"https://api.telegram.org/bot{token}/sendMessage",
              {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}, transport)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: HTTP {r.status_code} {body.get('description', '')}".strip())


# channel -> function(text, env) that sends one message or raises RuntimeError
SENDERS = {
    "telegram": lambda text, env: send_telegram(text, env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]),
}


def send(channel: str, text: str, env=os.environ) -> None:
    SENDERS[channel](text, env)
