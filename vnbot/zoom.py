"""Zoom Team Chat beside Telegram, ported from the OKX bot's channel (``bot/notify.py``).

Two independent parts, either one or both:

1. Notifications. Every message the bot broadcasts (daily reports, the AI
   critic, intraday alerts) also goes to Zoom:
   - an Incoming Webhook into one channel: ``ZOOM_WEBHOOK_URL`` + ``ZOOM_WEBHOOK_TOKEN``;
   - or a chatbot message to ``ZOOM_TO_JID``: ``ZOOM_CLIENT_ID``, ``ZOOM_CLIENT_SECRET``,
     ``ZOOM_BOT_JID``, ``ZOOM_ACCOUNT_ID``. Sending needs only these credentials, so
     the OKX bot's chatbot app can be reused for notifications: put the same
     values and the JID the OKX bot is paired with.
2. Commands (``/webhook/zoom``): the same commands as Telegram, typed in Zoom.
   Needs ``ZOOM_SECRET_TOKEN`` (requests are checked by HMAC) and the senders
   allowed in ``ZOOM_ALLOWED_USERS`` (JIDs or emails). A Zoom app has a single
   bot endpoint URL, so commands need an app of this bot's own: an app already
   pointed at the OKX bot sends its commands there.

Zoom specifics kept from the OKX bot: a 1:1 reply goes to the sender's JID (the
message's toJid is the bot itself there, error 7004), Markdown is converted to
Zoom's dialect, messages are cut at 4096 characters and retried three times.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

API = "https://api.zoom.us/v2/im/chat/messages"
TOKEN_URL = "https://api.zoom.us/oauth/token"
MAX_CHARS = 4096


@dataclass(frozen=True)
class ZoomConfig:
    webhook_url: str = ""
    webhook_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    bot_jid: str = ""
    account_id: str = ""
    to_jid: str = ""
    secret_token: str = ""
    allowed: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> ZoomConfig:
        env = os.environ.get
        return cls(
            webhook_url=(env("ZOOM_WEBHOOK_URL") or "").strip(),
            webhook_token=(env("ZOOM_WEBHOOK_TOKEN") or "").strip(),
            client_id=(env("ZOOM_CLIENT_ID") or "").strip(),
            client_secret=(env("ZOOM_CLIENT_SECRET") or "").strip(),
            bot_jid=(env("ZOOM_BOT_JID") or "").strip(),
            account_id=(env("ZOOM_ACCOUNT_ID") or "").strip(),
            to_jid=(env("ZOOM_TO_JID") or "").strip(),
            secret_token=(env("ZOOM_SECRET_TOKEN") or "").strip(),
            allowed=tuple(u.strip().lower() for u in (env("ZOOM_ALLOWED_USERS") or "").split(",") if u.strip()),
        )


def to_zoom_markdown(text: str) -> str:
    out = []
    for line in text.split("\n"):
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            out.append(f"*{heading.group(2).strip()}*")
            continue
        row = re.match(r"^\s*\|(.+)\|\s*$", line)
        if row:
            cells = [c.strip() for c in row.group(1).split("|")]
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                continue
            out.append("• " + " — ".join(c for c in cells if c))
            continue
        out.append(line)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", "\n".join(out))
    return re.sub(r"~~(.+?)~~", r"~\1~", text)


def split(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Pieces of at most ``limit`` characters, cut at a paragraph, a line or a space."""
    if len(text) <= limit:
        return [text] if text else []
    pieces, rest = [], text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        pieces.append(rest)
    return pieces


def parse_event(data: dict) -> dict | None:
    """A chat message event as {event_id, sender_jid, text, reply_jid, account_id, name, email}."""
    p = data.get("payload")
    if not isinstance(p, dict):
        return None
    text = str(p.get("cmd") or p.get("message") or "").strip()
    sender = str(p.get("userJid") or p.get("user_jid") or "").strip()
    to_jid = str(p.get("toJid") or p.get("to_jid") or sender).strip()
    channel = str(p.get("channelName") or p.get("channel_name") or "").strip()
    if not sender or not text:
        return None
    return {
        "event_id": str(p.get("messageId") or p.get("message_id") or f"{sender}:{data.get('event_ts', '')}"),
        "sender_jid": sender,
        "text": text,
        # in a channel the reply goes to the channel; in a 1:1 chat to the sender
        "reply_jid": to_jid if channel else sender,
        "account_id": str(p.get("accountId") or p.get("account_id") or "").strip(),
        "name": str(p.get("userName") or p.get("user_name") or ""),
        "email": str(p.get("userEmail") or p.get("user_email") or "").lower(),
    }


class ZoomChannel:
    def __init__(self, config: ZoomConfig | None = None, *, post=None, start: bool = True):
        self.config = config or ZoomConfig.from_env()
        c = self.config
        self.webhook_on = bool(c.webhook_url and c.webhook_token)
        self.chatbot_on = bool(c.client_id and c.client_secret and c.bot_jid)
        self.notify_on = self.webhook_on or (self.chatbot_on and bool(c.to_jid))
        self.commands_on = self.chatbot_on and bool(c.secret_token)
        self._post = post or requests.post
        self._token, self._token_exp = None, 0.0
        self._seen: dict[str, float] = {}
        self._targets: dict[str, dict] = {}         # "zoom:<n>" -> where a command's replies go
        self._next = 0
        self._lock = threading.Lock()
        self.queue: queue.Queue = queue.Queue(maxsize=500)
        self.handler = None                          # (target, text) -> None, set by the Telegram bot
        self.alert_owner = None                      # (text) -> None: a stranger wrote to the bot
        if start and (self.notify_on or self.chatbot_on):
            threading.Thread(target=self._worker, name="zoom", daemon=True).start()
        if self.chatbot_on and not c.secret_token:
            logger.info("Zoom: no ZOOM_SECRET_TOKEN, commands from Zoom are refused (notifications still go out)")

    @property
    def enabled(self) -> bool:
        return self.notify_on or self.commands_on

    # ── sending ──
    def send(self, text: str) -> None:
        """A notification (broadcast); dropped silently when Zoom is off or the queue is full."""
        if self.notify_on:
            self._put(("notify", text, None))

    def reply(self, target: str, text: str) -> None:
        """The answer to a command typed in Zoom (``target`` from ``handle``)."""
        with self._lock:
            where = self._targets.get(target)
        if where is None:
            logger.warning("Zoom: reply to unknown target %s dropped (restarted since the command?)", target)
            return
        self._put(("reply", text, where))

    def _put(self, item) -> None:
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            logger.warning("Zoom: queue full, message dropped")

    def _worker(self) -> None:
        while True:
            item = self.queue.get()
            try:
                self.deliver(item)
            except Exception:  # noqa: BLE001 - one failed message must not stop the worker
                logger.exception("Zoom: send failed")

    def deliver(self, item) -> None:
        kind, text, where = item
        c = self.config
        if kind == "notify":
            if self.webhook_on:
                self._post_webhook(text)
            if self.chatbot_on and c.to_jid:
                self.send_message(c.to_jid, text, c.to_jid, c.account_id)
        else:
            self.send_message(where["to_jid"], text, where["user_jid"], where["account_id"])

    def _post_webhook(self, text: str) -> None:
        url = self.config.webhook_url
        if "format=" not in url:
            url += ("&" if "?" in url else "?") + "format=message"
        for piece in split(text, 3800):
            r = self._post(url, data=json.dumps(piece, ensure_ascii=False).encode("utf-8"), timeout=15,
                           headers={"Authorization": self.config.webhook_token, "Content-Type": "application/json"})
            if r.status_code >= 300:
                logger.warning("Zoom webhook %s: %s", r.status_code, r.text[:200])
                return

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        r = self._post(TOKEN_URL, params={"grant_type": "client_credentials"},
                       auth=(self.config.client_id, self.config.client_secret), timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"Zoom OAuth token {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + max(60, int(data.get("expires_in", 3600)) - 60)
        return self._token

    def send_message(self, to_jid: str, text: str, user_jid: str | None = None, account_id: str | None = None) -> bool:
        for piece in split(to_zoom_markdown(text)):
            body = {"robot_jid": self.config.bot_jid, "to_jid": to_jid, "user_jid": user_jid or to_jid,
                    "account_id": account_id or self.config.account_id, "is_markdown_support": True,
                    "content": {"body": [{"type": "message", "text": piece}]}}
            sent = False
            for attempt in range(1, 4):
                try:
                    r = self._post(API, json=body, timeout=15,
                                   headers={"Authorization": f"Bearer {self._access_token()}"})
                    if r.status_code < 400:
                        sent = True
                        break
                    logger.error("Zoom message %s to %s: %s", r.status_code, to_jid, r.text[:300])
                    if r.status_code == 401:
                        self._token = None
                    elif r.status_code != 429:
                        break                   # a fixed error (wrong JID or account): retrying is useless
                except (requests.RequestException, RuntimeError) as exc:
                    logger.warning("Zoom message retry %d/3: %s", attempt, exc)
                time.sleep(1.5 * attempt)
            if not sent:
                return False
        return True

    # ── receiving ──
    def verify(self, raw_body: bytes, headers) -> bool:
        """The request's HMAC signature with ZOOM_SECRET_TOKEN, sent within the last 5 minutes."""
        key = self.config.secret_token
        sig = headers.get("x-zm-signature", "")
        ts = headers.get("x-zm-request-timestamp", "")
        if not key or not sig or not ts.isdigit() or len(ts) > 16:
            return False
        stamp = int(ts) / 1000 if int(ts) > 10 ** 11 else int(ts)
        if abs(time.time() - stamp) > 300:
            return False
        expected = "v0=" + hmac.new(key.encode(), b"v0:" + ts.encode() + b":" + raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode(), expected.encode())

    def url_validation(self, plain: str) -> dict:
        encrypted = hmac.new(self.config.secret_token.encode(), plain.encode(), hashlib.sha256).hexdigest()
        return {"plainToken": plain, "encryptedToken": encrypted}

    def handle(self, data: dict) -> None:
        """A verified chat event: run the command for an allowed sender, else tell the owner."""
        ev = parse_event(data)
        if ev is None:
            return
        now = time.time()
        with self._lock:
            if ev["event_id"] in self._seen:
                return                              # Zoom sent it again
            self._seen[ev["event_id"]] = now
            if len(self._seen) > 2000:
                self._seen = {k: v for k, v in self._seen.items() if now - v < 3600}
            self._next += 1
            target = f"zoom:{self._next}"
            self._targets[target] = {"to_jid": ev["reply_jid"], "user_jid": ev["sender_jid"],
                                     "account_id": ev["account_id"]}
            if len(self._targets) > 200:
                for old in list(self._targets)[:100]:
                    self._targets.pop(old, None)
        if not ({ev["sender_jid"].lower(), ev["email"]} & set(self.config.allowed)):
            logger.warning("Zoom: message from %s (%s) not in ZOOM_ALLOWED_USERS", ev["sender_jid"], ev["name"])
            self.reply(target, f"⛔ Tài khoản Zoom này chưa được phép dùng bot. JID: {ev['sender_jid']}")
            if self.alert_owner:
                self.alert_owner(f"🔐 Zoom: {ev['name'] or 'ai đó'} vừa nhắn bot. Nếu là bạn, thêm "
                                 f"{ev['sender_jid']} vào ZOOM_ALLOWED_USERS rồi khởi động lại.")
            return
        text = ev["text"] if ev["text"].startswith("/") else "/" + ev["text"]
        logger.info("Zoom command from %s: %s", ev["name"] or ev["sender_jid"], text)
        if self.handler is None:
            self.reply(target, "Bot chưa sẵn sàng nhận lệnh.")
            return
        self.handler(target, text)
