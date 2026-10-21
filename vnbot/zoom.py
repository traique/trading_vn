"""Zoom Team Chat beside Telegram, ported from the OKX bot's channel (``bot/notify.py``).

Two independent parts, either one or both:

1. Notifications. Every message the bot broadcasts (daily reports, the AI
   critic, intraday alerts) also goes to Zoom:
   - an Incoming Webhook into one channel: ``ZOOM_WEBHOOK_URL`` + ``ZOOM_WEBHOOK_TOKEN``;
   - or a chatbot message (``ZOOM_CLIENT_ID``, ``ZOOM_CLIENT_SECRET``, ``ZOOM_BOT_JID``,
     ``ZOOM_ACCOUNT_ID``) to the paired JID, or to ``ZOOM_TO_JID`` when set. Sending needs
     only these credentials, so the OKX bot's chatbot app can be reused for
     notifications: put the same values and, as ``ZOOM_TO_JID``, the JID the OKX
     bot is paired with.
2. Commands (``/webhook/zoom``): the same commands as Telegram, typed in Zoom.
   Requests are checked by HMAC (``ZOOM_SECRET_TOKEN``). Exactly one JID may
   command the bot, paired like the OKX bot: a stranger who writes is refused,
   the owner is told their JID on Telegram and pairs it with ``/zoompair <JID>``
   or the button on ``/zoom``; ``ZOOM_ALLOWED_USERS`` (JIDs or emails) are let in
   and paired on their first message. Uninstalling the app deletes the pairing.
   A Zoom app has a single bot endpoint URL, so commands need an app of this
   bot's own: an app already pointed at the OKX bot sends its commands there.

Zoom specifics kept from the OKX bot: a 1:1 reply goes to the sender's JID (the
message's toJid is the bot itself there, error 7004), Markdown is converted to
Zoom's dialect, messages are cut at 4096 characters and retried three times,
the signature is also tried with the other app secrets (a token pasted into the
wrong variable is reported, not silently refused), and the last 20 requests
are kept for the ``/zoom`` page.

State (pairing, the pending stranger, the install, OAuth states, the request
log) lives in ``zoom_state.json`` under the data directory.
"""

from __future__ import annotations

import contextlib
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
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API = "https://api.zoom.us/v2/im/chat/messages"
TOKEN_URL = "https://api.zoom.us/oauth/token"
MAX_CHARS = 4096
REQUESTS_KEPT = 20


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
    verification_token: str = ""
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
            secret_token=(env("ZOOM_SECRET_TOKEN") or env("ZOOM_BOT_SECRET_TOKEN") or "").strip(),
            verification_token=(env("ZOOM_VERIFICATION_TOKEN") or "").strip(),
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
    """A chat message event as {event_id, sender_jid, text, reply_jid, channel, account_id, name, email}."""
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
        "channel": channel,
        "account_id": str(p.get("accountId") or p.get("account_id") or "").strip(),
        "name": str(p.get("userName") or p.get("user_name") or ""),
        "email": str(p.get("userEmail") or p.get("user_email") or "").lower(),
    }


class ZoomState:
    """The channel's small persistent state, one JSON file."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._mem: dict = {}

    def _read(self) -> dict:
        if self.path is None:
            return dict(self._mem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            if self.path is None:
                self._mem = data
                return
            from vnbot.jobs import write_text_atomic

            write_text_atomic(self.path, json.dumps(data, ensure_ascii=False))


class ZoomChannel:
    def __init__(self, config: ZoomConfig | None = None, *, state_path: Path | None = None, post=None,
                 start: bool = True):
        self.config = config or ZoomConfig.from_env()
        c = self.config
        self.state = ZoomState(state_path)
        self.webhook_on = bool(c.webhook_url and c.webhook_token)
        self.chatbot_on = bool(c.client_id and c.client_secret and c.bot_jid)
        self.commands_on = self.chatbot_on and bool(c.secret_token or c.verification_token)
        self._post = post or requests.post
        self._token, self._token_exp = None, 0.0
        self._seen: dict[str, float] = {}
        self._targets: dict[str, dict] = {}         # "zoom:<n>" -> where a command's replies go
        self._next = 0
        self._lock = threading.Lock()
        self._alerted_at: dict[str, float] = {}     # stranger JID -> when the owner was last told
        self._notify_fail, self._notify_fail_ts = 0, 0.0
        self.queue: queue.Queue = queue.Queue(maxsize=500)
        self.handler = None                          # (target, text) -> None, set by the Telegram bot
        self.alert_owner = None                      # (text) -> None: tell the owner (Telegram)
        self.public_url = ""                         # for the links in the owner's alerts
        if start and (self.webhook_on or self.chatbot_on):
            threading.Thread(target=self._worker, name="zoom", daemon=True).start()
        if self.chatbot_on and not self.commands_on:
            logger.info("Zoom: no ZOOM_SECRET_TOKEN, commands from Zoom are refused (notifications still go out)")

    # ── pairing ──
    def pairing(self) -> dict:
        return self.state.get("pairing") or {}

    def pair(self, jid: str, name: str = "", account_id: str = "", reply_jid: str = "") -> dict:
        jid = jid.strip()
        p = {"jid": jid, "name": name, "account_id": account_id or self.config.account_id,
             "reply_jid": reply_jid or jid, "ts": time.time()}
        self.state.set("pairing", p)
        self._notify_fail = 0
        return p

    def unpair(self) -> None:
        self.state.set("pairing", {})

    def pending(self) -> dict:
        return self.state.get("pending") or {}

    def pair_pending(self) -> dict | None:
        """Pair the last stranger who wrote (the button on /zoom); tells them in Zoom."""
        pend = self.pending()
        if not pend.get("jid"):
            return None
        p = self.pair(pend["jid"], pend.get("name", ""), pend.get("account_id", ""), pend.get("reply_jid", ""))
        self.state.set("pending", {})
        self._put(("reply", "✅ Đã ghép đôi. Gõ trogiup để xem các lệnh.",
                   {"to_jid": p["reply_jid"], "user_jid": p["jid"], "account_id": p["account_id"]}))
        return p

    def notify_target(self) -> dict | None:
        c = self.config
        if c.to_jid:
            return {"to_jid": c.to_jid, "user_jid": c.to_jid, "account_id": c.account_id}
        p = self.pairing()
        if p.get("jid"):
            return {"to_jid": p.get("reply_jid") or p["jid"], "user_jid": p["jid"],
                    "account_id": p.get("account_id") or c.account_id}
        return None

    @property
    def notify_on(self) -> bool:
        return self.webhook_on or (self.chatbot_on and self.notify_target() is not None)

    @property
    def enabled(self) -> bool:
        return self.webhook_on or self.chatbot_on

    # ── sending ──
    def send(self, text: str) -> None:
        """A notification (broadcast); dropped silently when Zoom is off or the queue is full."""
        if self.webhook_on or self.chatbot_on:
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
        if kind != "notify":
            self.send_message(where["to_jid"], text, where["user_jid"], where["account_id"])
            return
        if self.webhook_on:
            self._post_webhook(text)
        target = self.notify_target() if self.chatbot_on else None
        if target is None:
            return
        if self._notify_fail >= 3 and time.time() - self._notify_fail_ts < 1800:
            return          # Zoom keeps refusing: pause 30 minutes (any message to the bot refreshes the pairing)
        if self.send_message(target["to_jid"], text, target["user_jid"], target["account_id"]):
            self._notify_fail = 0
        else:
            self._notify_fail += 1
            self._notify_fail_ts = time.time()
            if self._notify_fail == 3:
                logger.warning("Zoom: notifications refused 3 times in a row, paused 30 minutes. "
                               "Send the bot any command in Zoom to refresh the pairing.")

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
    @staticmethod
    def _fresh(ts: str) -> bool:
        if not ts or len(ts) > 16 or not ts.isdigit():
            return False
        stamp = int(ts) / 1000 if int(ts) > 10 ** 11 else int(ts)
        return abs(time.time() - stamp) <= 300

    def verify_detail(self, raw_body: bytes, headers) -> tuple[bool, str]:
        """(ok, why). The HMAC signature is tried with every secret of the app (Secret
        Token, Verification Token, Client Secret): a token pasted into the wrong
        variable still works and the page says which one matched. Safe, since all
        three are this app's own secrets."""
        c = self.config
        sig = headers.get("x-zm-signature", "")
        ts = headers.get("x-zm-request-timestamp", "")
        auth = headers.get("authorization", "")
        if sig:
            if not self._fresh(ts):
                return False, f"timestamp lệch giờ hoặc không hợp lệ ({ts[:20]})"
            msg = b"v0:" + ts.encode("ascii", "ignore") + b":" + raw_body
            for name, key in (("ZOOM_SECRET_TOKEN", c.secret_token), ("ZOOM_VERIFICATION_TOKEN", c.verification_token),
                              ("ZOOM_CLIENT_SECRET", c.client_secret)):
                if not key:
                    continue
                expected = "v0=" + hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
                if hmac.compare_digest(sig.encode(), expected.encode()):
                    if name != "ZOOM_SECRET_TOKEN":
                        logger.warning("Zoom: signature matched %s, not ZOOM_SECRET_TOKEN; fix the variables", name)
                        return True, f"chữ ký hợp lệ (khớp {name}; ZOOM_SECRET_TOKEN đang sai, nên sửa lại)"
                    return True, "chữ ký hợp lệ"
        if auth:
            for name, key in (("ZOOM_VERIFICATION_TOKEN", c.verification_token), ("ZOOM_SECRET_TOKEN", c.secret_token)):
                if key and hmac.compare_digest(auth.encode(), key.encode()):
                    return True, f"Authorization khớp {name}"
        if not sig and not auth:
            return False, "request không có chữ ký Zoom"
        return False, ("chữ ký không khớp ZOOM_SECRET_TOKEN / ZOOM_VERIFICATION_TOKEN / ZOOM_CLIENT_SECRET đang đặt. "
                       "Lấy lại Secret Token ở Features → Access của đúng app này")

    def verify(self, raw_body: bytes, headers) -> bool:
        return self.verify_detail(raw_body, headers)[0]

    def url_validation(self, plain: str) -> dict:
        encrypted = hmac.new(self.config.secret_token.encode(), plain.encode(), hashlib.sha256).hexdigest()
        return {"plainToken": plain, "encryptedToken": encrypted}

    def record(self, event: str | None, ok: bool, reason: str, data: dict | None = None) -> None:
        """Keep the last requests from Zoom for the /zoom page."""
        p = (data or {}).get("payload") or {}
        row = {"ts": time.time(), "event": event or "?", "ok": ok, "reason": reason,
               "user": str(p.get("userName") or p.get("userJid") or p.get("user_jid") or "")[:80],
               "cmd": str(p.get("cmd") or p.get("message") or "")[:60]}
        with contextlib.suppress(OSError):     # a full disk must not refuse the request itself
            self.state.set("requests", ([row] + (self.state.get("requests") or []))[:REQUESTS_KEPT])

    def requests_log(self) -> list[dict]:
        return self.state.get("requests") or []

    def lifecycle(self, event: str, payload: dict) -> None:
        """App events: uninstalling deletes the pairing (as /privacy promises)."""
        if event == "app_deauthorized":
            self.unpair()
            self.state.set("pending", {})
            self.state.set("install", {})
            logger.warning("Zoom: app removed from account %s, pairing deleted",
                           payload.get("account_id") or payload.get("accountId"))
            if self.alert_owner:
                self.alert_owner("⚠️ Zoom: ứng dụng đã bị gỡ khỏi Zoom, thông tin ghép đôi đã được xoá.")
        elif event == "bot_installed":
            logger.info("Zoom: bot installed (%s)", payload.get("userName") or payload.get("userJid") or "")

    def _allowed(self, ev: dict) -> bool:
        if self.pairing().get("jid") == ev["sender_jid"]:
            return True
        return bool({ev["sender_jid"].lower(), ev["email"]} & set(self.config.allowed))

    def handle(self, data: dict) -> None:
        """A verified chat event: run the command for the paired sender, else tell the owner."""
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
        if not self._allowed(ev):
            logger.warning("Zoom: message from unpaired %s (%s) ignored", ev["sender_jid"], ev["name"])
            self.state.set("pending", {"jid": ev["sender_jid"], "name": ev["name"], "account_id": ev["account_id"],
                                       "reply_jid": ev["reply_jid"], "ts": now})
            if self.alert_owner and now - self._alerted_at.get(ev["sender_jid"], 0) > 120:
                self._alerted_at[ev["sender_jid"]] = now
                page = f"{self.public_url}/zoom" if self.public_url else "/zoom"
                self.alert_owner(f"🔐 Zoom: {ev['name'] or 'ai đó'} vừa nhắn bot nhưng chưa được cấp quyền.\n"
                                 f"JID: {ev['sender_jid']}\nNếu là bạn, gõ trên Telegram: /zoompair {ev['sender_jid']}\n"
                                 f"hoặc bấm “Ghép đôi” trên trang {page}")
            self.reply(target, "⛔ Tài khoản Zoom này chưa được ghép đôi với bot. Chủ bot cần gõ /zoompair trên "
                               "Telegram hoặc bấm Ghép đôi trên trang /zoom.")
            return
        p = self.pairing()
        if not p.get("jid"):
            # let in by ZOOM_ALLOWED_USERS: paired now, so notifications reach them
            self.pair(ev["sender_jid"], ev["name"], ev["account_id"], ev["reply_jid"])
        elif p["jid"] == ev["sender_jid"] and not ev["channel"] and (
                p.get("reply_jid") != ev["reply_jid"] or (ev["account_id"] and p.get("account_id") != ev["account_id"])):
            # the latest 1:1 message has the right reply JID and account (a wrong account_id: error 7004)
            self.pair(p["jid"], p.get("name") or ev["name"], ev["account_id"] or p.get("account_id", ""), ev["reply_jid"])
        text = ev["text"] if ev["text"].startswith("/") else "/" + ev["text"]
        logger.info("Zoom command from %s: %s", ev["name"] or ev["sender_jid"], text)
        if self.handler is None:
            self.reply(target, "Bot chưa sẵn sàng nhận lệnh.")
            return
        self.handler(target, text)
