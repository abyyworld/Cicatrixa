"""
Optional human gate (APPROVAL_MODE=telegram): sends the verified diff to
Telegram with one-tap Approve / Reject inline buttons and long-polls for
the answer.
"""
import asyncio
import logging
import time
import uuid

import httpx

from config import APPROVAL_TIMEOUT_SEC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def request_approval(diff: str, explanation: str) -> bool:
    """Returns True if the patch may ship. Rejects on timeout or if the bot
    is unconfigured (fail safe). Only called when APPROVAL_MODE=telegram."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logger.warning("APPROVAL_MODE=telegram but Telegram not configured — rejecting")
        return False

    nonce = uuid.uuid4().hex[:8]
    text = (
        "🩹 *Self\\-healer: verified patch ready*\n"
        f"{_escape(explanation)}\n\n"
        f"```diff\n{diff[:3200]}\n```\n"
        "Reproduction test passes, full suite green\\. Ship it?"
    )
    async with httpx.AsyncClient(timeout=35.0) as http:
        r = await http.post(f"{API}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{nonce}"},
                {"text": "❌ Reject", "callback_data": f"reject:{nonce}"},
            ]]},
        })
        if r.status_code != 200:
            logger.error(f"Telegram sendMessage failed: {r.text}")
            return False
        message_id = r.json()["result"]["message_id"]

        decision = await _poll_for_decision(http, nonce)
        verdict = "✅ Approved — deploying canary" if decision else "❌ Rejected — patch discarded"
        await http.post(f"{API}/editMessageText", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": verdict,
        })
        return decision


async def _poll_for_decision(http: httpx.AsyncClient, nonce: str) -> bool:
    deadline = time.time() + APPROVAL_TIMEOUT_SEC
    offset = 0
    while time.time() < deadline:
        try:
            r = await http.get(f"{API}/getUpdates", params={
                "offset": offset,
                "timeout": 25,
                "allowed_updates": '["callback_query"]',
            })
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                data = cq.get("data", "")
                if data.endswith(nonce):
                    await http.post(f"{API}/answerCallbackQuery",
                                    json={"callback_query_id": cq["id"]})
                    return data.startswith("approve:")
        except Exception as exc:
            logger.warning(f"Telegram poll error: {exc}")
            await asyncio.sleep(3)
    logger.warning("Approval timed out — rejecting (fail safe)")
    return False


def _escape(text: str) -> str:
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
