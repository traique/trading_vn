"""Telegram bot over the Bot API with long polling (no webhook, no extra library).

Commands (Vietnamese, like lananh-main's bot):

    /phantich MÃ [YYYY-MM-DD]  full multi-agent analysis (minutes); the system
                               decision arrives first, the report after
    /nhanh MÃ                  the quantitative engine's decision only (seconds)
    /theodoi [MÃ1,MÃ2,...]     show or set the daily watchlist
    /trangthai                 queue and AI provider status
    /danhmuc /lenh /hieuchinh  the trading bot: books, next session's orders, calibration
    /khop mua|ban MÃ KL GIÁ [NGÀY]  record a fill in the real book (signal mode)
    /von SỐ_TIỀN               set the real book's cash (signal mode)
    /id                        your Telegram id (to put in TELEGRAM_ALLOWED_IDS)

Only ids in TELEGRAM_ALLOWED_IDS are served; the analyses cost LLM quota.

Only new messages run commands: an edited message is ignored, so editing an
old "/khop ..." never records the fill twice. Updates already handed out are
remembered across restarts for the same reason. The bot token sits in every
Bot API URL, so it is scrubbed from this module's log records and replies.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from vnbot.jobs import Job, JobQueue
from vnbot.service import Watchlist, check_request, quick_text
from vnbot.settings import Settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 3900
COMMAND_LANES = 3          # commands run on this many worker threads at most
MAX_QUEUED_COMMANDS = 50   # beyond this a burst is dropped, not queued
# Messages from ids not in TELEGRAM_ALLOWED_IDS never take one of those slots: at
# most this many are queued at once, and each chat gets one reply per window.
MAX_STRANGER_COMMANDS = 3
STRANGER_REPLY_SECONDS = 300
SEEN_KEPT = 1000           # update ids remembered across restarts
# A Bot API token: "<bot id>:<35-ish url-safe characters>".
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])\d{5,}:[A-Za-z0-9_-]{30,}")
_SECRETS: set[str] = set()


def redact(text) -> str:
    """``text`` without any Telegram bot token (requests puts the URL in its errors)."""
    text = str(text)
    for secret in _SECRETS:
        text = text.replace(secret, "<TOKEN>")
    return _TOKEN_RE.sub("<TOKEN>", text)


class _RedactFilter(logging.Filter):
    """Scrubs the token from a record's message and traceback before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact(record.getMessage()), ()
        except Exception:  # noqa: BLE001 - a bad format string still gets logged
            record.msg = redact(record.msg)
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_REDACT = _RedactFilter()


def _install_redaction() -> None:
    # This module's records, and urllib3's request lines ("POST /bot<token>/...") at DEBUG.
    for name in (__name__, "urllib3.connectionpool"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactFilter) for f in target.filters):
            target.addFilter(_REDACT)


_install_redaction()


class TelegramNetworkError(requests.RequestException):
    """A Bot API request that did not get through, with the token removed from the message."""
_MD_NOISE = re.compile(r"(\*\*|__|^#+\s*)", re.MULTILINE)

HELP = (
    "TradingAgents VN - phân tích cổ phiếu HOSE/HNX/UPCoM.\n\n"
    "/phantich FPT - phân tích đầy đủ bằng các agent AI (vài phút)\n"
    "/phantich FPT 2026-10-01 - phân tích tại một ngày trong quá khứ\n"
    "/nhanh FPT - chỉ quyết định của hệ thống định lượng (vài giây)\n"
    "/phanbien FPT - AI phản biện quyết định của hệ thống (1 lượt gọi AI); /phanbien - AI phản biện đúng bao nhiêu\n"
    "/theodoi FPT,HPG,VCB - đặt danh sách báo cáo tự động mỗi ngày\n"
    "/theodoi - xem danh sách đang theo dõi\n"
    "/trangthai - hàng đợi và trạng thái các nhà cung cấp AI\n"
    "/danhmuc - danh mục bot (paper, và tài khoản thật nếu bật chế độ tín hiệu)\n"
    "/lenh - lệnh bot đặt cho phiên tới\n"
    "/chay - chạy ngay phiên đã đóng gần nhất (không đợi {trade_time})\n"
    "/hieuchinh - ngưỡng đang dùng và kết quả tự hiệu chỉnh gần đây\n"
    "/khop mua FPT 100 56300 - báo đã khớp lệnh thật (bán: /khop ban ...; thêm ngày YYYY-MM-DD nếu khớp hôm trước)\n"
    "/von 500000000 - đặt tiền mặt tài khoản thật\n"
    "/zoompair JID [tên] · /zoomxoa · /zoomstatus - ghép đôi / gỡ / xem tài khoản Zoom được dùng bot\n\n"
    "Hành động và mọi mức giá do hệ thống định lượng chốt; các agent AI chỉ diễn giải."
)


# The sender of a command typed in Zoom (vnbot.zoom checked it against ZOOM_ALLOWED_USERS).
ZOOM_USER = "zoom"


def _amount(text: str) -> float:
    """A number typed the Vietnamese way: 56.300 or 56300 (dots and commas as thousands).

    Separators must split groups of three digits: "56.3" (thousands shorthand, or a
    decimal) is refused rather than read as 563 đồng.
    """
    raw = re.sub(r"[\s_]", "", text or "")
    groups = re.split(r"[.,]", raw)
    if not raw or not all(g.isdigit() for g in groups) or any(len(g) != 3 for g in groups[1:]):
        raise ValueError(f"Số không hợp lệ: {text!r}. Nhập số đầy đủ theo đồng, ví dụ 56300 hoặc 56.300.")
    return float("".join(groups))


def plain(text: str) -> str:
    """Markdown from the reports as plain text, so Telegram shows it without parse errors."""
    return _MD_NOISE.sub("", text or "").strip()


def chunks(text: str, size: int = MAX_MESSAGE) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            parts.append(current + line[: size - len(current)])
            line, current = line[size - len(current):], ""
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current.strip():
        parts.append(current)
    return parts or [""]


class TelegramBot:
    def __init__(self, settings: Settings, jobs: JobQueue, watchlist: Watchlist):
        self.settings, self.jobs, self.watchlist = settings, jobs, watchlist
        if len(settings.telegram_token or "") >= 10:
            _SECRETS.add(settings.telegram_token)
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._poll, name="telegram-bot", daemon=True)
        self.trader = None   # vnbot.trader.TradingBot, set by main_botkeep
        self._critic_llm = None   # the provider chain, built on the first /phanbien
        self.zoom = None          # vnbot.zoom.ZoomChannel: broadcasts go there too, its commands come here
        self.switches = None      # vnbot.channels.Switches: the on/off of each channel (all on when None)
        # Commands of one chat run in order on one lane; a burst waits instead of
        # spawning a thread per message (the host has 2 GB).
        self._lanes = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"telegram-cmd-{i}")
                       for i in range(COMMAND_LANES)]
        self._queued = 0
        self._queued_lock = threading.Lock()
        self._strangers_queued = 0
        self._stranger_replied: dict = {}      # chat id -> time.monotonic() of the last reply
        self._seen_path = settings.data_dir / "telegram_seen.json"
        self._seen: deque[int] = deque(self._load_seen(), maxlen=SEEN_KEPT)
        jobs.add_listener(self.on_job_event)

    def _help(self) -> str:
        from vnbot.settings import MIN_TRADE_TIME

        return HELP.format(trade_time=max(self.settings.trade_time, MIN_TRADE_TIME))

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_token)

    def start(self) -> None:
        if self.enabled:
            self._thread.start()
        else:
            logger.info("TELEGRAM_TOKEN not set; the Telegram bot is off")

    # ── Bot API ──
    def _call(self, method: str, http_timeout: float = 30, **params):
        url = API.format(token=self.settings.telegram_token, method=method)
        files = params.pop("files", None)
        for attempt in range(3):
            if files:
                for value in files.values():   # re-send the file from its start
                    handle = value[1] if isinstance(value, tuple) else value
                    if hasattr(handle, "seek"):
                        handle.seek(0)
            try:
                response = self._session.post(url, data=params, files=files, timeout=http_timeout)
            except requests.RequestException as exc:
                # ``from None``: the original carries the URL, so the token, in its text.
                raise TelegramNetworkError(f"Telegram {method}: {redact(exc)}") from None
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError(f"Telegram {method}: HTTP {response.status_code}") from None
            if payload.get("ok"):
                return payload.get("result")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if payload.get("error_code") == 429 and retry_after and attempt < 2:
                # Flood control: a long report is several messages in a row.
                time.sleep(min(float(retry_after), 60) + 0.5)
                continue
            raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
        raise RuntimeError(f"Telegram {method}: still rate limited")

    def send(self, chat_id: int, text: str) -> None:
        if isinstance(chat_id, str) and chat_id.startswith("zoom:"):
            if self.zoom is not None:
                self.zoom.reply(chat_id, redact(text))
            return
        for part in chunks(redact(text)):
            try:
                self._call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview="true")
            except Exception:  # noqa: BLE001 - delivery failures are logged, not fatal
                logger.exception("Telegram sendMessage to %s failed", chat_id)

    def broadcast(self, text: str) -> None:
        """Send to the report chats (the daily bot report, the calibration result) and Zoom,
        each when its switch on the web page is on."""
        if self.zoom is not None and self._switch("zoom_notify"):
            self.zoom.send(redact(text))
        if not self.enabled or not self._switch("telegram"):
            return
        for chat_id in sorted(self.settings.telegram_report_chat_ids):
            self.send(chat_id, text)

    def _switch(self, name: str) -> bool:
        return self.switches is None or self.switches.on(name)

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        if isinstance(chat_id, str):
            link = f": {self.settings.public_url}" if self.settings.public_url else ""
            self.send(chat_id, f"{caption} - file HTML chỉ gửi qua Telegram, xem trên trang web{link}")
            return
        try:
            with open(path, "rb") as handle:
                self._call("sendDocument", http_timeout=60, chat_id=chat_id, caption=caption[:1000],
                           files={"document": (path.name, handle, "text/html")})
        except Exception:  # noqa: BLE001
            logger.exception("Telegram sendDocument to %s failed", chat_id)

    # ── polling ──
    def _load_seen(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self._seen_path.read_text(encoding="utf-8"))][-SEEN_KEPT:]
        except (OSError, ValueError, TypeError):
            return []

    def _save_seen(self) -> None:
        try:
            self._seen_path.write_text(json.dumps(list(self._seen)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Telegram: cannot save handled update ids: %s", exc)

    def _accept(self, updates) -> tuple[int | None, list[dict]]:
        """The next offset and the new messages of a getUpdates batch.

        Every update id is marked handled (and saved) before any command runs:
        a crash or restart mid-batch must not run "/khop" or "/von" twice when
        Telegram hands the unconfirmed batch out again.
        """
        offset, messages = None, []
        seen = set(self._seen)
        for update in updates or []:
            try:
                update_id = int(update["update_id"])
            except (KeyError, TypeError, ValueError):
                logger.warning("Telegram: update without an id skipped")
                continue
            offset = max(offset or 0, update_id + 1)
            if update_id in seen:
                continue
            seen.add(update_id)
            self._seen.append(update_id)
            message = update.get("message")
            if message is None:
                # An edited message is not a new command (editing "/khop" would
                # record the fill again); channel posts and the rest are ignored.
                logger.info("Telegram: update %s ignored (%s)", update_id,
                            ",".join(k for k in update if k != "update_id"))
                continue
            if isinstance(message, dict) and message.get("text") and message.get("chat"):
                messages.append(message)
        if offset is not None:
            self._save_seen()
        return offset, messages

    def _dispatch(self, message: dict) -> None:
        """Queue a command on its chat's lane, bounded so a burst cannot pile up threads.

        Only the allowed ids share the command budget: a stranger flooding a public
        bot must not make it drop the owner's "/khop" or "/von"."""
        chat_id = message["chat"].get("id")
        user_id = (message.get("from") or {}).get("id")
        if user_id not in self.settings.telegram_allowed_ids:
            self._dispatch_stranger(message, chat_id)
            return
        with self._queued_lock:
            full = self._queued >= MAX_QUEUED_COMMANDS
            if not full:
                self._queued += 1
        lane = self._lanes[hash(chat_id) % len(self._lanes)]
        if full:
            logger.warning("Telegram: %d commands waiting; message from chat %s dropped", self._queued, chat_id)
            # The owner is told, so a dropped "/khop" is not mistaken for a recorded one.
            lane.submit(self.send, chat_id, f"⚠️ Bot đang bận ({MAX_QUEUED_COMMANDS} lệnh chờ): chưa thực hiện "
                                            f"\"{message['text'][:60]}\". Gửi lại sau ít phút.")
            return
        try:
            lane.submit(self._run, message)
        except Exception:
            with self._queued_lock:
                self._queued -= 1
            raise

    def _dispatch_stranger(self, message: dict, chat_id) -> None:
        """A message from an id not allowed: at most one reply per chat per window (the
        /id answer or the refusal), on a small budget of its own; the rest is ignored."""
        now = time.monotonic()
        with self._queued_lock:
            last = self._stranger_replied.get(chat_id)
            if (last is not None and now - last < STRANGER_REPLY_SECONDS) \
                    or self._strangers_queued >= MAX_STRANGER_COMMANDS:
                logger.info("Telegram: message from unauthorised chat %s ignored", chat_id)
                return
            if len(self._stranger_replied) > 1000:
                self._stranger_replied = {k: t for k, t in self._stranger_replied.items()
                                          if now - t < STRANGER_REPLY_SECONDS}
            self._stranger_replied[chat_id] = now
            self._strangers_queued += 1
        lane = self._lanes[hash(chat_id) % len(self._lanes)]
        try:
            lane.submit(self._run, message, stranger=True)
        except Exception:
            with self._queued_lock:
                self._strangers_queued -= 1
            raise

    def zoom_command(self, target: str, text: str) -> None:
        """A command typed in Zoom by an allowed sender; runs on a lane like a Telegram one."""
        message = {"chat": {"id": target}, "from": {"id": ZOOM_USER}, "text": text}
        with self._queued_lock:
            if self._queued >= MAX_QUEUED_COMMANDS:
                full = True
            else:
                full = False
                self._queued += 1
        if full:
            self.send(target, f"⚠️ Bot đang bận ({MAX_QUEUED_COMMANDS} lệnh chờ). Gửi lại sau ít phút.")
            return
        try:
            self._lanes[hash(target) % len(self._lanes)].submit(self._run, message)
        except Exception:
            with self._queued_lock:
                self._queued -= 1
            raise

    def _run(self, message: dict, stranger: bool = False) -> None:
        try:
            self._handle(message)
        except Exception:  # noqa: BLE001 - one bad message must not stop its lane
            logger.exception("Telegram message handling failed")
        finally:
            with self._queued_lock:
                if stranger:
                    self._strangers_queued -= 1
                else:
                    self._queued -= 1

    def _poll(self) -> None:
        offset = None
        while True:
            try:
                # Edited messages are not asked for: only new messages run commands.
                params = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", http_timeout=70, **params)
                next_offset, messages = self._accept(updates)
                if next_offset is not None:
                    # Advanced before any command runs: whatever happens below,
                    # the next getUpdates confirms this batch and never repeats it.
                    offset = next_offset
                for message in messages:
                    try:
                        self._dispatch(message)
                    except Exception:  # noqa: BLE001
                        logger.exception("Telegram: cannot queue a message")
            except requests.RequestException as exc:
                logger.warning("Telegram polling error: %s", exc)
                time.sleep(5)
            except RuntimeError as exc:
                text = str(exc)
                if "webhook is active" in text:
                    # The token still has a webhook from another service or an
                    # earlier deploy; long polling only works once it is removed.
                    self._drop_webhook()
                    time.sleep(1)
                elif "terminated by other getUpdates" in text:
                    logger.warning("Telegram: another process is polling with this bot token. "
                                   "Stop the other copy (another server or a local run).")
                    time.sleep(30)
                else:
                    logger.warning("Telegram polling failed: %s", text)
                    time.sleep(10)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram polling failed")
                time.sleep(10)

    def _drop_webhook(self) -> None:
        try:
            self._call("deleteWebhook", drop_pending_updates="false")
            logger.warning("Telegram: removed the webhook set on this bot token, switching to polling.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram deleteWebhook failed: %s", exc)
            time.sleep(30)

    # ── commands ──
    def _handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        user_id = (message.get("from") or {}).get("id")
        text = message["text"].strip()
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        args = rest.strip()

        if command == "/id" and user_id != ZOOM_USER:
            self.send(chat_id, f"Telegram id của bạn: {user_id}")
            return
        if user_id != ZOOM_USER and user_id not in self.settings.telegram_allowed_ids:
            self.send(chat_id, f"Bot chưa cho phép tài khoản này. Thêm {user_id} vào TELEGRAM_ALLOWED_IDS.")
            return
        try:
            if command in ("/start", "/help", "/trogiup"):
                self.send(chat_id, self._help())
            elif command == "/phantich":
                self._analyze(chat_id, args)
            elif command == "/phanbien":
                self._critic(chat_id, args)
            elif command in ("/zoompair", "/zoomxoa", "/zoomstatus"):
                self.send(chat_id, self._zoom_pairing(command, args, from_zoom=user_id == ZOOM_USER))
            elif command == "/nhanh":
                self.send(chat_id, quick_text(*(args.split()[:2] or [""])))
            elif command == "/theodoi":
                self._watch(chat_id, args)
            elif command == "/trangthai":
                self.send(chat_id, self._status())
            elif command in ("/danhmuc", "/lenh", "/hieuchinh", "/khop", "/von", "/chay"):
                self._trading(chat_id, command, args)
            else:
                self.send(chat_id, self._help())
        except ValueError as exc:
            self.send(chat_id, f"⚠️ {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Command %s failed", command)
            self.send(chat_id, f"⚠️ Lỗi: {redact(exc)}")

    def _analyze(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if not parts:
            raise ValueError("Nhập mã cổ phiếu, ví dụ: /phantich FPT")
        code, day = check_request(parts[0], parts[1] if len(parts) > 1 else None)
        job = self.jobs.submit(code, day, "telegram", chat_id)
        if job.status != "queued":
            self.send(chat_id, f"⏳ {code} ngày {day} đang được phân tích; bạn sẽ nhận kết quả khi xong.")
            if job.quick_summary:
                self.send(chat_id, f"📊 Quyết định hệ thống định lượng\n\n{job.quick_summary}")
            return
        ahead = self.jobs.position(job.id)
        wait = f" (đang có {ahead} lượt chờ trước)" if ahead else ""
        self.send(chat_id, f"⏳ Đã nhận {code} ngày {day}{wait}. Quyết định hệ thống sẽ tới trước, báo cáo đầy đủ của các agent tới sau vài phút.")

    def _zoom_pairing(self, command: str, args: str, *, from_zoom: bool) -> str:
        """Pair, unpair or show the one Zoom account allowed to use the bot (from Telegram only)."""
        if from_zoom:
            return "Lệnh ghép đôi Zoom chỉ dùng được từ Telegram."
        z = self.zoom
        if z is None or not z.chatbot_on:
            return "Chưa cấu hình Zoom chatbot (ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET / ZOOM_BOT_JID)."
        if command == "/zoomstatus":
            p = z.pairing()
            return (f"Zoom đang ghép đôi: {p.get('name') or ''} {p['jid']}".replace("  ", " ") if p.get("jid")
                    else "Chưa ghép đôi JID Zoom nào.")
        if command == "/zoomxoa":
            z.unpair()
            return "Đã gỡ ghép đôi Zoom."
        parts = args.split(maxsplit=1)
        if not parts:
            return "Cú pháp: /zoompair JID [tên]. JID có trong tin báo khi bạn nhắn bot trên Zoom lần đầu."
        pend = z.pending()
        same = pend.get("jid") == parts[0]
        p = z.pair(parts[0], parts[1] if len(parts) > 1 else (pend.get("name", "") if same else ""),
                   pend.get("account_id", "") if same else "", pend.get("reply_jid", "") if same else "")
        return f"✅ Đã ghép đôi Zoom: {p['jid']}. Từ giờ bạn gõ lệnh và nhận thông báo trên Zoom."

    def _critic(self, chat_id: int, args: str) -> None:
        """One AI request about the engine's decision (runs on the chat's command lane)."""
        from tradingagents.quant import engine
        from vnbot.critic import CriticLog, default_llm

        log = CriticLog(self.settings.data_dir)
        parts = args.split()
        if not parts:
            self.send(chat_id, log.stats_text())
            return
        code, day = check_request(parts[0], parts[1] if len(parts) > 1 else None)
        self.send(chat_id, f"⏳ AI đang phản biện {code} ngày {day} (1 lượt gọi)...")
        result = engine.analyze(code, day)
        if self._critic_llm is None:
            self._critic_llm = default_llm(self.settings)
        log.settle_now()
        self.send(chat_id, f"📊 {result.deterministic_summary}\n\n{log.review(day, result, self._critic_llm)}")

    def _trading(self, chat_id: int, command: str, args: str) -> None:
        if self.trader is None or self.settings.trade_mode == "off":
            raise ValueError("Bot giao dịch đang tắt (TRADE_MODE=off).")
        if command == "/chay":
            self.send(chat_id, self.trader.start_run(notify=lambda text: self.send(chat_id, text)))
        elif command == "/danhmuc":
            self.send(chat_id, self.trader.portfolio_text())
        elif command == "/lenh":
            self.send(chat_id, self.trader.orders_text())
        elif command == "/hieuchinh":
            self.send(chat_id, self.trader.calibration_text())
        elif command == "/von":
            self.send(chat_id, self.trader.set_capital(_amount(args)))
        else:
            if self.settings.trade_mode != "signal":
                raise ValueError("/khop chỉ dùng ở chế độ tín hiệu (TRADE_MODE=signal).")
            parts = args.split()
            if len(parts) not in (4, 5):
                raise ValueError("Dùng: /khop mua FPT 100 56300 [YYYY-MM-DD] (hoặc /khop ban ...)")
            side = {"mua": "BUY", "buy": "BUY", "ban": "SELL", "bán": "SELL", "sell": "SELL"}.get(parts[0].lower())
            if side is None:
                raise ValueError("Chiều lệnh là mua hoặc ban.")
            # The fill's day (default today) sets when the shares become sellable (T+2).
            code, day = check_request(parts[1], parts[4] if len(parts) == 5 else None)
            if datetime.strptime(day, "%Y-%m-%d").weekday() >= 5:
                raise ValueError(f"{day} là cuối tuần, không có phiên giao dịch.")
            qty, price = int(_amount(parts[2])), _amount(parts[3])
            if qty <= 0:
                raise ValueError("Khối lượng phải lớn hơn 0.")
            if price < 100:
                raise ValueError(f"Giá {parts[3]} không hợp lệ. Nhập giá theo đồng, ví dụ 56300 hoặc 56.300.")
            self.send(chat_id, self.trader.record_fill(side, code, qty, price, day))

    def _watch(self, chat_id: int, args: str) -> None:
        if args:
            wanted = [c for c in re.split(r"[,\s]+", args) if c]
            if len(wanted) > 30:
                raise ValueError("Tối đa 30 mã trong danh sách theo dõi.")
            codes = self.watchlist.set(wanted)
            self.send(chat_id, f"✅ Theo dõi: {', '.join(codes)}. Báo cáo tự động lúc {self.settings.daily_report_time} các ngày giao dịch.")
        else:
            codes = self.watchlist.get()
            self.send(chat_id, "Đang theo dõi: " + (", ".join(codes) if codes else "(trống)"))

    def _status(self) -> str:
        from tradingagents.llm_clients.fallback_client import provider_status, usage_today

        lines = ["Hàng đợi:"]
        for job in self.jobs.recent(8):
            lines.append(f"- {job.ticker} {job.date}: {job.status} {job.action}".rstrip())
        usage = usage_today()
        cap = f"/{usage['cap']}" if usage["cap"] else ""
        lines.append(f"\nNhà cung cấp AI (theo thứ tự), hôm nay đã gọi {usage['total']}{cap} lượt:")
        for row in provider_status():
            state = f"nghỉ {row['resting_seconds']}s ({row['last_error']})" if row["resting_seconds"] else "sẵn sàng"
            calls = f"{row['calls_today']}/{row['cap']}" if row["cap"] else str(row["calls_today"])
            lines.append(f"- {row['label']} [{row['model']}]: {state}, {calls} lượt")
        return "\n".join(lines)

    # ── job events ──
    def on_job_event(self, job: Job, event: str) -> None:
        # Telegram and Zoom askers, plus the report chats when the daily run asked for it;
        # a web request is followed on the web page.
        targets = [c for c in job.chat_ids if self.enabled or isinstance(c, str)]
        if "daily" in job.sources and self.enabled:
            targets += [c for c in sorted(self.settings.telegram_report_chat_ids) if c not in targets]
        for chat_id in targets:
            if event == "quick":
                self.send(chat_id, f"📊 Quyết định hệ thống định lượng\n\n{job.quick_summary}")
            elif event == "done":
                link = f"\n\nXem báo cáo: {self.settings.public_url}/reports/{job.id}" if self.settings.public_url else ""
                notes = f"\n\n⚠️ Output Guard: {job.guard_notes}" if job.guard_notes else ""
                self.send(chat_id, f"✅ {job.ticker} {job.date} - {job.action}\n\n{plain(job.final_decision)}{notes}{link}")
                if job.html_path:
                    self.send_file(chat_id, job.html_path, caption=f"Báo cáo đầy đủ {job.ticker} {job.date}")
            elif event == "failed":
                self.send(chat_id, f"❌ {job.ticker} {job.date}: {job.error}")
