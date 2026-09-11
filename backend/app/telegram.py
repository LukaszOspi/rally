"""Telegram bot: deep-link binding of saved searches, plus alert delivery.

Long polling rather than webhooks - it needs no inbound route, no extra Caddy
block and no public secret, and the message volume here is a handful a week.
"""

import html
import logging
import os
import threading
import time

import requests

from . import store
from .matcher import describe, matches

log = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://rally.ospi.link").rstrip("/")
RALLY_URL = "https://booking.roadsurfer.com/en/rally"

API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

HELP = (
    "<b>Roadsurfer Rally Finder</b>\n\n"
    "I watch every one-way rally route in Europe and ping you when something "
    "matching your saved search appears.\n\n"
    "<b>Commands</b>\n"
    "/list - your saved searches\n"
    "/pause &lt;n&gt; - stop alerts for search n\n"
    "/resume &lt;n&gt; - restart alerts for search n\n"
    "/delete &lt;n&gt; - remove search n\n"
    "/help - this message\n\n"
    f"Create searches at {PUBLIC_URL}"
)


def enabled() -> bool:
    return bool(TOKEN)


def _call(method: str, **payload):
    if not API:
        return None
    try:
        resp = requests.post(f"{API}/{method}", json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data.get("description"))
            return None
        return data.get("result")
    except (requests.RequestException, ValueError) as exc:
        log.warning("telegram %s error: %s", method, exc)
        return None


def send(chat_id, text: str, preview: bool = False):
    return _call(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=not preview,
    )


def deep_link(watch_id: str) -> str | None:
    """Link that binds a freshly created search to whoever taps it."""
    if not BOT_USERNAME:
        return None
    return f"https://t.me/{BOT_USERNAME}?start={watch_id}"


# ── formatting ───────────────────────────────────────────────────────────────

def format_offer(offer: dict) -> str:
    e = html.escape
    start = f"{e(offer['start_name'])} ({e(offer['start_country'] or '')})"
    end = f"{e(offer['end_name'])} ({e(offer['end_country'] or '')})"

    lines = [f"🚐 <b>{start} → {end}</b>"]

    days = offer.get("window_days") or 0
    window = f"{offer['start_date']} → {offer['end_date']}"
    if days > 7:
        lines.append(f"📅 {window} <i>({days}-day window, pick any 7)</i>")
    else:
        lines.append(f"📅 {window} <i>({days} days)</i>")

    if offer.get("distance_km"):
        lines.append(f"📍 {offer['distance_km']:.0f} km apart")

    pickup, dropoff = offer.get("pickup"), offer.get("dropoff")
    if pickup and pickup.get("address"):
        window_txt = ""
        if pickup.get("from") and pickup.get("to"):
            window_txt = f" ({pickup['from'][:5]}-{pickup['to'][:5]})"
        lines.append(f"🅿️ Pick up: {e(pickup['address'])}{window_txt}")
    if dropoff and dropoff.get("address"):
        window_txt = ""
        if dropoff.get("from") and dropoff.get("to"):
            window_txt = f" ({dropoff['from'][:5]}-{dropoff['to'][:5]})"
        lines.append(f"🏁 Drop off: {e(dropoff['address'])}{window_txt}")

    vehicles = [v for v in (offer.get("vehicles") or []) if v.get("available")]
    if vehicles:
        parts = []
        for v in vehicles[:4]:
            bits = e(v.get("name") or "Camper")
            if v.get("seats"):
                bits += f" · {v['seats']} seats"
            if v.get("beds"):
                bits += f"/{v['beds']} beds"
            parts.append(bits)
        lines.append("🛏 " + "\n   ".join(parts))

    if offer.get("price"):
        lines.append(f"💶 <b>{offer['price']:.2f} {offer.get('currency') or 'EUR'}</b>")

    lines.append(f'\n<a href="{RALLY_URL}">Book on roadsurfer.com</a>')
    return "\n".join(lines)


def notify_matches(new_keys: list[str]) -> int:
    """Push every new offer that matches an active, linked saved search."""
    if not enabled() or not new_keys:
        return 0

    watches = store.active_linked_watches()
    if not watches:
        return 0

    offers = store.offers_by_keys(new_keys)
    # Expired-but-just-discovered windows are not worth a push.
    offers = [o for o in offers if o.get("active")]
    sent = 0

    for watch in watches:
        hits = [
            o for o in offers
            if matches(o, watch["criteria"])
            and not store.already_notified(watch["id"], o["key"])
        ]
        if not hits:
            continue

        hits.sort(key=lambda o: o["start_date"])
        header = (
            f"🔔 <b>{html.escape(watch['name'])}</b> - "
            f"{len(hits)} new {'match' if len(hits) == 1 else 'matches'}"
        )
        # Telegram caps messages at 4096 chars; batch conservatively.
        chunks, current = [], [header]
        for offer in hits[:20]:
            block = format_offer(offer)
            if sum(len(c) for c in current) + len(block) > 3500:
                chunks.append("\n\n".join(current))
                current = []
            current.append(block)
        if current:
            chunks.append("\n\n".join(current))

        for chunk in chunks:
            if send(watch["chat_id"], chunk):
                sent += 1

        for offer in hits:
            store.record_hit(watch["id"], offer["key"])

    return sent


# ── command handling ─────────────────────────────────────────────────────────

def _watch_lines(chat_id) -> str:
    watches = store.watches_for_chat(chat_id)
    if not watches:
        return f"No saved searches yet. Create one at {PUBLIC_URL}"
    names = store.station_names()
    ccs = store.country_names()
    out = ["<b>Your saved searches</b>"]
    for i, w in enumerate(watches, 1):
        state = "active" if w["active"] else "paused"
        out.append(
            f"\n<b>{i}. {html.escape(w['name'])}</b> ({state})\n"
            f"   {html.escape(describe(w['criteria'], names, ccs))}"
        )
    return "\n".join(out)


def _resolve_index(chat_id, arg: str):
    watches = store.watches_for_chat(chat_id)
    try:
        idx = int(arg)
    except (TypeError, ValueError):
        return None
    if 1 <= idx <= len(watches):
        return watches[idx - 1]
    return None


def handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()
    arg = arg.strip()

    if command == "/start":
        if arg:
            watch = store.get_watch(arg)
            if not watch:
                send(chat_id, "That search link is not valid or has expired.")
                return
            if watch.get("chat_id") and str(watch["chat_id"]) != str(chat_id):
                send(chat_id, "That search is already linked to another chat.")
                return
            store.link_watch(arg, chat_id)
            send(
                chat_id,
                f"✅ Linked <b>{html.escape(watch['name'])}</b>\n"
                f"{html.escape(describe(watch['criteria'], store.station_names(), store.country_names()))}\n\n"
                "I'll message you when a matching rally appears.",
            )
        else:
            send(chat_id, HELP)
        return

    if command == "/help":
        send(chat_id, HELP)
    elif command == "/list":
        send(chat_id, _watch_lines(chat_id))
    elif command in ("/pause", "/resume"):
        watch = _resolve_index(chat_id, arg)
        if not watch:
            send(chat_id, "Usage: <code>/pause 1</code> - see /list for numbers.")
            return
        store.update_watch(watch["id"], active=(command == "/resume"))
        verb = "paused" if command == "/pause" else "resumed"
        send(chat_id, f"Search <b>{html.escape(watch['name'])}</b> {verb}.")
    elif command == "/delete":
        watch = _resolve_index(chat_id, arg)
        if not watch:
            send(chat_id, "Usage: <code>/delete 1</code> - see /list for numbers.")
            return
        store.delete_watch(watch["id"])
        send(chat_id, f"Deleted <b>{html.escape(watch['name'])}</b>.")
    else:
        send(chat_id, HELP)


def poll_loop(stop: threading.Event) -> None:
    """Long-poll getUpdates until asked to stop."""
    if not enabled():
        log.info("telegram disabled (no TELEGRAM_BOT_TOKEN)")
        return

    me = _call("getMe")
    if me:
        log.info("telegram bot @%s connected", me.get("username"))
        global BOT_USERNAME
        if not BOT_USERNAME:
            BOT_USERNAME = me.get("username") or ""
    _call("setMyCommands", commands=[
        {"command": "list", "description": "Your saved searches"},
        {"command": "pause", "description": "Pause a search"},
        {"command": "resume", "description": "Resume a search"},
        {"command": "delete", "description": "Delete a search"},
        {"command": "help", "description": "How this works"},
    ])

    offset = None
    while not stop.is_set():
        try:
            updates = _call("getUpdates", offset=offset, timeout=25) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception:  # noqa: BLE001 - one bad update must not kill polling
                    log.exception("failed handling update")
        except Exception:  # noqa: BLE001
            log.exception("telegram poll error")
            time.sleep(5)
