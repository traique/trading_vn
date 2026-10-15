"""Telegram bot over the Bot API with long polling (no webhook, no extra library).

Commands (Vietnamese, like lananh-main's bot):

    /phantich MÃ [YYYY-MM-DD]  full multi-agent analysis (minutes); the system
                               decision arrives first, the report after
    /nhanh MÃ                  the quantitative engine's decision only (seconds)
    /theodoi [MÃ1,MÃ2,...]     show or set the daily watchlist
    /trangthai                 queue and AI provider status
    /danhmuc /lenh /hieuchinh  the trading bot: books, next session's orders, calibration
    /khop mua|ban MÃ KL GIÁ    record a fill in the real book (signal mode)
    /von SỐ_TIỀN               set the real book's cash (signal mode)
    /id                        your Telegram id (to put in TELEGRAM_ALLOWED_IDS)

Only ids in TELEGRAM_ALLOWED_IDS are served; the analyses cost LLM quota.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import requests

from vnbot.jobs import Job, JobQueue
from vnbot.service import Watchlist, check_request, quick_text
from vnbot.settings import Settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 3900
_MD_NOISE = re.compile(r"(\*\*|__|^#+\s*)", re.MULTILINE)

HELP = (
    "TradingAgents VN - phân tích cổ phiếu HOSE/HNX/UPCoM.\n\n"
    "/phantich FPT - phân tích đầy đủ bằng các agent AI (vài phút)\n"
    "/phantich FPT 2026-10-01 - phân tích tại một ngày trong quá khứ\n"
    "/nhanh FPT - chỉ quyết định của hệ thống định lượng (vài giây)\n"
    "/theodoi FPT,HPG,VCB - đặt danh sách báo cáo tự động mỗi ngày\n"
    "/theodoi - xem danh sách đang theo dõi\n"
    "/trangthai - hàng đợi và trạng thái các nhà cung cấp AI\n"
    "/danhmuc - danh mục bot (paper, và tài khoản thật nếu bật chế độ tín hiệu)\n"
    "/lenh - lệnh bot đặt cho phiên tới\n"
    "/chay - chạy ngay phiên đã đóng gần nhất (không đợi 15:30)\n"
    "/hieuchinh - ngưỡng đang dùng và kết quả tự hiệu chỉnh gần đây\n"
    "/khop mua FPT 100 56300 - báo đã khớp lệnh thật (bán: /khop ban ...)\n"
    "/von 500000000 - đặt tiền mặt tài khoản thật\n\n"
    "Hành động và mọi mức giá do hệ thống định lượng chốt; các agent AI chỉ diễn giải."
)


def _amount(text: str) -> float:
    """A number typed the Vietnamese way: 56.300 or 56300 (dots and commas as thousands)."""
    digits = re.sub(r"[.,\s_]", "", text or "")
    if not digits.isdigit():
        raise ValueError(f"Số không hợp lệ: {text!r}")
    return float(digits)


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
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._poll, name="telegram-bot", daemon=True)
        self.trader = None   # vnbot.trader.TradingBot, set by main_botkeep
        jobs.add_listener(self.on_job_event)

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
            response = self._session.post(url, data=params, files=files, timeout=http_timeout)
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
        for part in chunks(text):
            try:
                self._call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview="true")
            except Exception:  # noqa: BLE001 - delivery failures are logged, not fatal
                logger.exception("Telegram sendMessage to %s failed", chat_id)

    def broadcast(self, text: str) -> None:
        """Send to the report chats (the daily bot report, the calibration result)."""
        if not self.enabled:
            return
        for chat_id in sorted(self.settings.telegram_report_chat_ids):
            self.send(chat_id, text)

    def send_file(self, chat_id: int, path, caption: str = "") -> None:
        try:
            with open(path, "rb") as handle:
                self._call("sendDocument", http_timeout=60, chat_id=chat_id, caption=caption[:1000],
                           files={"document": (path.name, handle, "text/html")})
        except Exception:  # noqa: BLE001
            logger.exception("Telegram sendDocument to %s failed", chat_id)

    # ── polling ──
    def _poll(self) -> None:
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message","edited_message"]'}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", http_timeout=70, **params)
                for update in updates or []:
                    offset = update["update_id"] + 1
                    message = update.get("message") or update.get("edited_message")
                    if message and message.get("text"):
                        threading.Thread(target=self._handle, args=(message,), daemon=True).start()
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

        if command == "/id":
            self.send(chat_id, f"Telegram id của bạn: {user_id}")
            return
        if user_id not in self.settings.telegram_allowed_ids:
            self.send(chat_id, f"Bot chưa cho phép tài khoản này. Thêm {user_id} vào TELEGRAM_ALLOWED_IDS.")
            return
        try:
            if command in ("/start", "/help", "/trogiup"):
                self.send(chat_id, HELP)
            elif command == "/phantich":
                self._analyze(chat_id, args)
            elif command == "/nhanh":
                self.send(chat_id, quick_text(*(args.split()[:2] or [""])))
            elif command == "/theodoi":
                self._watch(chat_id, args)
            elif command == "/trangthai":
                self.send(chat_id, self._status())
            elif command in ("/danhmuc", "/lenh", "/hieuchinh", "/khop", "/von", "/chay"):
                self._trading(chat_id, command, args)
            else:
                self.send(chat_id, HELP)
        except ValueError as exc:
            self.send(chat_id, f"⚠️ {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Command %s failed", command)
            self.send(chat_id, f"⚠️ Lỗi: {exc}")

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
            if len(parts) != 4:
                raise ValueError("Dùng: /khop mua FPT 100 56300 (hoặc /khop ban ...)")
            side = {"mua": "BUY", "buy": "BUY", "ban": "SELL", "bán": "SELL", "sell": "SELL"}.get(parts[0].lower())
            if side is None:
                raise ValueError("Chiều lệnh là mua hoặc ban.")
            code, _ = check_request(parts[1])
            from vnbot.service import today_vn

            self.send(chat_id, self.trader.record_fill(side, code, int(_amount(parts[2])), _amount(parts[3]), today_vn()))

    def _watch(self, chat_id: int, args: str) -> None:
        if args:
            codes = self.watchlist.set([c for c in re.split(r"[,\s]+", args) if c])
            self.send(chat_id, f"✅ Theo dõi: {', '.join(codes)}. Báo cáo tự động lúc {self.settings.daily_report_time} các ngày giao dịch.")
        else:
            codes = self.watchlist.get()
            self.send(chat_id, "Đang theo dõi: " + (", ".join(codes) if codes else "(trống)"))

    def _status(self) -> str:
        from tradingagents.llm_clients.fallback_client import provider_status

        lines = ["Hàng đợi:"]
        for job in self.jobs.recent(8):
            lines.append(f"- {job.ticker} {job.date}: {job.status} {job.action}".rstrip())
        lines.append("\nNhà cung cấp AI (theo thứ tự):")
        for row in provider_status():
            state = f"nghỉ {row['resting_seconds']}s ({row['last_error']})" if row["resting_seconds"] else "sẵn sàng"
            lines.append(f"- {row['label']} [{row['model']}]: {state}")
        return "\n".join(lines)

    # ── job events ──
    def on_job_event(self, job: Job, event: str) -> None:
        if not self.enabled:
            return
        # Telegram askers, plus the report chats when the daily run asked for it;
        # a web request is followed on the web page.
        targets = list(job.chat_ids)
        if "daily" in job.sources:
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
