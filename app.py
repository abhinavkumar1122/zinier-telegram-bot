"""
Zinier Telegram Booking Bot — Webhook Server
---------------------------------------------
Production-mode entry point: Telegram pushes events to POST /webhook/telegram.
Drop-in replacement for bot_runner.py (polling) — same state machine, different entry point.

Environment variables (set in Railway dashboard):
  TELEGRAM_BOT_TOKEN   → from BotFather
  BOT_USERNAME         → e.g. zinier_test_bot
  WEBHOOK_SECRET       → any random string (used to validate Telegram calls)

Endpoints:
  POST /webhook/telegram  → receives Telegram updates (messages + callback_queries)
  POST /generate          → generate a new booking UID + deep link
  GET  /sessions          → debug: list all active sessions
  GET  /health            → Railway health check
"""

import os
import uuid
import httpx
from flask import Flask, request, jsonify
from datetime import datetime
from collections import deque

app = Flask(__name__)

TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME  = os.environ.get("BOT_USERNAME", "zinier_test_bot")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "zinier-demo-secret")
BASE          = f"https://api.telegram.org/bot{TOKEN}"

# In-memory log ring buffer — last 100 entries
_logs: deque = deque(maxlen=100)

def log(tag: str, msg: str):
    entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "tag": tag, "msg": msg}
    _logs.append(entry)
    print(f"[{entry['time']}] [{tag}] {msg}")

# ---------------------------------------------------------------------------
# In-memory session store
# Resets on restart — acceptable for demo; replace with Redis/MySQL in production
# Production replacement: UserNotification table (MySQL) with chatUserId + chatChannel
# ---------------------------------------------------------------------------
sessions: dict = {}

SLOTS = {
    "s1": {"display": "May 25 10:00 AM", "confirmText": "May 25, 10:00 AM", "location": "Downtown Office"},
    "s2": {"display": "May 25  2:00 PM", "confirmText": "May 25,  2:00 PM", "location": "Downtown Office"},
    "s3": {"display": "May 26 11:00 AM", "confirmText": "May 26, 11:00 AM", "location": "Downtown Office"},
}

# ---------------------------------------------------------------------------
# Telegram API helpers
# Production replacement: TelegramChannelAdapter.java (RestTemplate)
# ---------------------------------------------------------------------------

def tg(method: str, **kwargs) -> dict:
    try:
        r = httpx.post(f"{BASE}/{method}", json=kwargs, timeout=10)
        log("TELEGRAM", f"→ {method} ✅")
        return r.json()
    except Exception as e:
        log("TELEGRAM", f"→ {method} ❌ {e}")
        return {}

def send(chat_id: int, text: str, keyboard: list = None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = {"inline_keyboard": keyboard}
    tg("sendMessage", **data)

def answer(callback_id: str, text: str = ""):
    tg("answerCallbackQuery", callback_query_id=callback_id, text=text)

def clear_keyboard(chat_id: int, message_id: int):
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={})


# ---------------------------------------------------------------------------
# State machine handlers
# Production replacement: ChatBookingOrchestrator.java
# ---------------------------------------------------------------------------

def on_start(chat_id: int, uid: str, first_name: str):
    """Customer tapped deep link — link chatId to UID, send slot picker."""
    if uid not in sessions:
        send(chat_id, "⚠️ Booking session not found. Please use the link sent to you.")
        return
    s = sessions[uid]
    s["chatId"] = chat_id
    s["state"]  = "SLOT_SELECTION"
    log("WEBHOOK", f"/start received — uid={uid} chatId={chat_id}")
    log("DB",      f"UPDATE session uid={uid} → chatId={chat_id}, state=SLOT_SELECTION")
    log("BOT",     f"Sending slot selection keyboard to chatId={chat_id}")
    send(chat_id,
        f"Hi {first_name}! 👋\n\n"
        f"You have a service appointment request.\n"
        f"Please pick a slot:\n\n"
        f"📍 Location: Downtown Office",
        [
            [{"text": "📅 May 25 — 10:00 AM", "callback_data": f"slot:s1:{uid}"}],
            [{"text": "📅 May 25 —  2:00 PM", "callback_data": f"slot:s2:{uid}"}],
            [{"text": "📅 May 26 — 11:00 AM", "callback_data": f"slot:s3:{uid}"}],
        ]
    )


def on_slot(chat_id: int, uid: str, slot_id: str, callback_id: str, message_id: int):
    """Customer picked a slot — show confirm/cancel."""
    slot = SLOTS.get(slot_id)
    if not slot or uid not in sessions:
        return
    s = sessions[uid]
    s["selectedSlotId"] = slot_id
    s["state"] = "PENDING_CONFIRMATION"
    log("WEBHOOK", f"Slot selected — uid={uid} slot={slot_id} ({slot['confirmText']})")
    log("DB",      f"UPDATE session uid={uid} → selectedSlotId={slot_id}, state=PENDING_CONFIRMATION")
    log("BOT",     f"Sending confirm/cancel keyboard to chatId={chat_id}")
    answer(callback_id)
    clear_keyboard(chat_id, message_id)
    send(chat_id,
        f"You selected:\n"
        f"📅 {slot['confirmText']}\n"
        f"📍 {slot['location']}\n\n"
        f"Confirm your booking?",
        [
            [{"text": "✅ Confirm", "callback_data": f"confirm:{uid}"}],
            [{"text": "❌ Cancel",  "callback_data": f"cancel:{uid}"}],
        ]
    )


def on_confirm(chat_id: int, uid: str, callback_id: str):
    """Customer confirmed — fire booking (mock EventUtil call)."""
    if uid not in sessions:
        return
    s = sessions[uid]
    slot = SLOTS.get(s.get("selectedSlotId", ""), {})
    s["state"] = "CONFIRMED"
    log("WEBHOOK", f"Booking confirmed — uid={uid} slot={s.get('selectedSlotId')}")
    log("DB",      f"UPDATE session uid={uid} → state=CONFIRMED")
    log("PROD",    f"[MOCK] EventUtil.executeEvent() → SQS → Gryffindor workflow → booking complete. uid={uid}")
    answer(callback_id, "Booking confirmed! 🎉")
    send(chat_id,
        f"✅ Booking Confirmed!\n\n"
        f"📋 Reference: {uid}\n"
        f"📅 Date: {slot.get('confirmText', 'N/A')}\n"
        f"📍 Location: {slot.get('location', 'N/A')}\n\n"
        f"See you there! We'll send a reminder 24h before. 🎉"
    )


def on_cancel(chat_id: int, uid: str, callback_id: str):
    """Customer cancelled."""
    if uid in sessions:
        sessions[uid]["state"] = "CANCELLED"
    log("WEBHOOK", f"Booking cancelled — uid={uid}")
    log("DB",      f"UPDATE session uid={uid} → state=CANCELLED")
    answer(callback_id, "Booking cancelled")
    send(chat_id, "❌ Booking cancelled.\n\nContact support if you'd like to reschedule.")


# ---------------------------------------------------------------------------
# Webhook endpoint — Telegram pushes every update here
# Production replacement: ChatWebhookController.java POST /api/v1/webhook/telegram
# ---------------------------------------------------------------------------

@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    # Validate secret token header
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != WEBHOOK_SECRET:
        return "Unauthorized", 401

    update = request.get_json(silent=True) or {}

    # Handle regular messages (/start)
    if "message" in update:
        msg       = update["message"]
        text      = msg.get("text", "")
        chat_id   = msg["chat"]["id"]
        first_name = msg["from"].get("first_name", "Customer")

        if text.startswith("/start "):
            uid = text[7:].strip()
            log("WEBHOOK", f"Received /start — uid={uid} chatId={chat_id} user={first_name}")
            on_start(chat_id, uid, first_name)
        elif text == "/start":
            # bare /start — resend keyboard for existing session
            existing = next((s for s in sessions.values() if s.get("chatId") == chat_id and s["state"] == "SLOT_SELECTION"), None)
            if existing:
                on_start(chat_id, existing["uid"], first_name)
            else:
                send(chat_id, "👋 Please use the booking link sent to you via SMS or email.")

    # Handle button taps
    elif "callback_query" in update:
        cq          = update["callback_query"]
        data        = cq["data"]
        callback_id = cq["id"]
        chat_id     = cq["from"]["id"]
        message_id  = cq["message"]["message_id"]
        parts       = data.split(":")
        log("WEBHOOK", f"Callback received — data={data} chatId={chat_id}")

        if parts[0] == "slot" and len(parts) == 3:
            on_slot(chat_id, parts[2], parts[1], callback_id, message_id)
        elif parts[0] == "confirm" and len(parts) == 2:
            on_confirm(chat_id, parts[1], callback_id)
        elif parts[0] == "cancel" and len(parts) == 2:
            on_cancel(chat_id, parts[1], callback_id)

    # Always return 200 — Telegram retries on non-2xx
    return "ok", 200


# ---------------------------------------------------------------------------
# Generate UID endpoint — call this to start a booking session for a customer
# Production replacement: CustomerPortalHandler.generateUID() → lambda
# ---------------------------------------------------------------------------

@app.route("/generate", methods=["POST"])
def generate_uid():
    body        = request.get_json(silent=True) or {}
    customer    = body.get("customerName", "Customer")
    email       = body.get("email", "")
    uid         = "ZNR-" + str(uuid.uuid4())[:8].upper()
    deep_link   = f"https://t.me/{BOT_USERNAME}?start={uid}"

    sessions[uid] = {
        "uid":          uid,
        "customerName": customer,
        "email":        email,
        "chatId":       None,
        "state":        "PENDING",
        "selectedSlotId": None,
    }
    log("PORTAL", f"New booking session created — uid={uid} customer={customer}")
    log("DB",     f"INSERT session uid={uid} state=PENDING")
    log("PORTAL", f"Deep link generated → {deep_link}")
    log("MOCK",   f"[PROD would do] Send deep link to {email} via SMS/email — skipped in demo, share manually")

    return jsonify({"uid": uid, "deepLink": deep_link, "customerName": customer})


# ---------------------------------------------------------------------------
# Debug + health endpoints
# ---------------------------------------------------------------------------

@app.route("/logs", methods=["GET"])
def get_logs():
    return jsonify({"logs": list(_logs), "count": len(_logs)})


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    _logs.clear()
    return jsonify({"status": "cleared"})


@app.route("/sessions", methods=["GET"])
def list_sessions():
    return jsonify({"sessions": list(sessions.values()), "count": len(sessions)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": BOT_USERNAME, "sessions": len(sessions)})


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Zinier Telegram Booking Bot (Demo)",
        "bot": f"@{BOT_USERNAME}",
        "endpoints": {
            "POST /generate":           "Start a new booking session",
            "POST /webhook/telegram":   "Telegram webhook (set via setWebhook)",
            "GET  /sessions":           "List active sessions",
            "GET  /health":             "Health check",
        }
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)
