"""The web page: ask for an analysis, follow the queue, read the reports.

Plain server-rendered HTML (no build step, no JavaScript framework) so the
upload stays small. Every page but /health asks for WEB_PASSWORD (HTTP Basic,
any user name), since each analysis spends LLM quota; without it set the pages
stay locked. A form posted from another site (the browser would send the saved
password with it) is refused. Repeated wrong passwords from one client lock it
out for a few minutes.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from typing import Annotated
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException

from vnbot import ui
from vnbot.jobs import Job, JobQueue
from vnbot.service import Watchlist, check_request, quick_text, today_vn
from vnbot.settings import Settings

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_WATCHLIST = 30

_STATUS_VI = {
    "queued": "đang chờ",
    "quick": "đang tính quyết định hệ thống",
    "running": "các agent đang phân tích",
    "done": "xong",
    "failed": "lỗi",
}
_SOURCE_VI = {"web": "Web", "telegram": "Telegram", "daily": "Tự động"}
_PHASE_VI = {"closed": "Nghỉ cuối tuần", "pre_open": "Chưa mở cửa", "ato": "Phiên ATO", "continuous": "Khớp lệnh liên tục",
             "lunch": "Nghỉ trưa", "atc": "Phiên ATC", "put_through": "Thoả thuận", "after_close": "Đã đóng cửa"}
_MODE_VI = {"critic": "AI phản biện", "full": "phân tích đầy đủ", "off": "không gọi AI"}
_T2_BADGE = '<span class="badge b-none">chờ T+2</span>'
_TUNED_BADGE = '<span class="badge b-ok">đã chỉnh</span>'
_CHANGED_BADGE = '<span class="badge b-ok">đổi</span> '
_KEPT_BADGE = '<span class="badge b-none">giữ nguyên</span>'
_UNSET_BADGE = '<span class="badge b-none">chưa cấu hình</span>'


def _switch_form(key: str, on: bool) -> str:
    """An on/off switch that posts to /channels/<key> (a form: works without JavaScript)."""
    label = "Đang bật, bấm để tắt" if on else "Đang tắt, bấm để bật"
    return (f'<form method="post" action="/channels/{key}" style="margin:0;display:inline-flex">'
            f'<input type="hidden" name="on" value="{0 if on else 1}">'
            f'<button class="switch{" on" if on else ""}" type="submit" role="switch" aria-checked="{"true" if on else "false"}" '
            f'aria-label="{label}" title="{label}"><span class="knob"></span>'
            f'<span class="st">{"Bật" if on else "Tắt"}</span></button></form>')


# Module level: the dependency annotations below are strings (postponed
# evaluation), which FastAPI resolves in module scope.
_BASIC = HTTPBasic(auto_error=False)


def _page(title: str, body: str, refresh: int | None = None, *, active: str = "", status_code: int = 200,
          show_bot: bool = True) -> HTMLResponse:
    return HTMLResponse(ui.page(title, body, active=active, refresh=refresh, show_bot=show_bot),
                        status_code=status_code)


def _when(ts: float | None) -> str:
    """An epoch time in Vietnam time, whatever the server's own zone (often UTC)."""
    return datetime.fromtimestamp(ts, VN_TZ).strftime("%d/%m %H:%M") if ts else ""


class LoginThrottle:
    """Wrong-password counter per client: after ``max_failures`` within ``window``
    seconds that client is refused for ``lockout`` seconds, right password or not.

    In memory only (one process); the table is bounded so a scan of many
    addresses cannot grow it without limit.
    """

    def __init__(self, max_failures: int = 8, window: float = 600.0, lockout: float = 300.0,
                 max_clients: int = 10_000, clock=time.monotonic):
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self.max_clients, self.clock = max_clients, clock
        self._failures: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int:
        """Seconds left on ``client``'s lockout, 0 when it may try."""
        with self._lock:
            until = self._locked.get(client)
            if until is None:
                return 0
            left = until - self.clock()
            if left <= 0:
                del self._locked[client]
                return 0
            return int(left) + 1

    def failure(self, client: str) -> None:
        with self._lock:
            now = self.clock()
            recent = [t for t in self._failures.get(client, []) if now - t < self.window] + [now]
            if len(recent) >= self.max_failures:
                self._failures.pop(client, None)
                self._locked[client] = now + self.lockout
            else:
                self._failures[client] = recent
            if len(self._failures) + len(self._locked) > self.max_clients:
                self._prune(now)

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, now: float) -> None:
        self._locked = {c: u for c, u in self._locked.items() if u > now}
        self._failures = {c: ts for c, ts in self._failures.items() if ts and now - ts[-1] < self.window}
        while self._failures and len(self._failures) + len(self._locked) > self.max_clients:
            self._failures.pop(next(iter(self._failures)))   # the oldest first seen


def _client(request: Request) -> str:
    # The socket peer. Uvicorn already replaces it with X-Forwarded-For when the
    # request comes from a proxy it trusts (FORWARDED_ALLOW_IPS, 127.0.0.1 by
    # default); the header itself is never trusted here, a client could forge it.
    return request.client.host if request.client else "?"


def _job_row(job: Job) -> str:
    return (f'<tr><td><a class="sym" href="/jobs/{job.id}">{html.escape(job.ticker)}</a></td>'
            f'<td class="num">{html.escape(job.date)}</td><td>{ui.action_badge(job.action)}</td>'
            f"<td>{ui.status_badge(job.status)}</td>"
            f'<td class="muted small">{_SOURCE_VI.get(job.source, html.escape(job.source))}</td>'
            f'<td class="muted small num">{_when(job.created)}</td></tr>')


LOCKED_MESSAGE = (
    "Trang web đang khoá vì chưa đặt WEB_PASSWORD. Thêm WEB_PASSWORD vào file .env "
    "rồi khởi động lại server (bot Telegram vẫn chạy bình thường)."
)


def _host(value: str) -> str:
    return (urlsplit(value).netloc if "//" in value else value).strip().lower()


def _money(value) -> str:
    return ui.money(value)


def _book_card(title: str, book, *, accent: str, sub: str = "") -> str:
    st = book.stats()
    nav_sub = f"từ {html.escape(book.start_date)}" if book.start_date else "chưa chạy phiên nào"
    kpis = "".join([
        ui.kpi("NAV", ui.money_short(st["nav"]), nav_sub),
        ui.kpi("Lợi nhuận", ui.pct(st["return_pct"]),
               f"VNINDEX {ui.pct(st['benchmark_pct'])}" if st["benchmark_pct"] is not None else "", ui.tone(st["return_pct"])),
        ui.kpi("Sụt giảm tối đa", ui.pct(st["max_drawdown_pct"], signed=False), "từ đỉnh NAV"),
        ui.kpi("Lệnh mua", str(st["trades"]),
               f"thắng {st['win_rate_pct']:.0f}%".replace(".", ",") if st["win_rate_pct"] is not None else "chưa đóng lệnh nào"),
    ])
    positions = [
        f'<tr><td class="sym">{html.escape(p.symbol)}</td><td class="num">{ui.money(p.qty)}</td>'
        f'<td class="num">{ui.money(p.avg_price)}</td><td class="num">{ui.money(p.last_price)}</td>'
        f'<td class="num {ui.tone(p.last_price - p.avg_price if p.avg_price else None)}">'
        f'{ui.pct((p.last_price / p.avg_price - 1) * 100) if p.avg_price else "—"}</td>'
        f'<td class="num">{ui.money(p.stop) if p.stop else "—"}</td>'
        f'<td>{"" if p.sellable else _T2_BADGE}</td></tr>'
        for p in book.positions.values()]
    orders = [
        f'<tr><td>{ui.action_badge("BUY" if o.side == "BUY" else "SELL")}</td><td class="sym">{html.escape(o.symbol)}</td>'
        f'<td class="num">{ui.money(o.qty)}</td><td class="num">{ui.money(o.limit) if o.limit else "giá mở cửa"}</td>'
        f'<td class="muted small">{html.escape(o.reason)}</td></tr>'
        for o in book.orders]
    fills = [
        f'<tr><td class="num muted">{html.escape(f.date)}</td><td>{ui.action_badge("BUY" if f.side == "BUY" else "SELL")}</td>'
        f'<td class="sym">{html.escape(f.symbol)}</td><td class="num">{ui.money(f.qty)}</td><td class="num">{ui.money(f.price)}</td>'
        f'<td class="num {ui.tone(f.pnl)}">{ui.money(f.pnl)}</td><td class="muted small">{html.escape(f.reason)}</td></tr>'
        for f in reversed(book.fills[-12:])]
    body = (
        f'<div class="grid g4">{kpis}</div>'
        '<h2 style="margin:20px 0 10px">Đang giữ</h2>'
        + ui.table([("Mã", ""), ("KL", "num"), ("Giá vốn", "num"), ("Giá", "num"), ("Lãi/lỗ", "num"),
                    ("Stop", "num"), ("", "")], positions, "Không giữ mã nào.")
        + '<h2 style="margin:20px 0 10px">Lệnh cho phiên tới</h2>'
        + ui.table([("Chiều", ""), ("Mã", ""), ("KL", "num"), ("Giá", "num"), ("Lý do", "")], orders, "Không có lệnh chờ.")
        + '<h2 style="margin:20px 0 10px">Giao dịch gần đây</h2>'
        + ui.table([("Ngày", ""), ("Chiều", ""), ("Mã", ""), ("KL", "num"), ("Giá", "num"), ("Lãi/lỗ", "num"),
                    ("Lý do", "")], fills, "Chưa có giao dịch.")
    )
    return ui.card(title, body, sub=sub, cls=accent)


def _common_start(*books) -> str | None:
    """The first session every book with a history has: the date the chart indexes on."""
    starts = [min(h["date"] for h in b.history) for b in books if b.history]
    return max(starts) if starts else None


def _chart(trader) -> str:
    paper, mom, fb = trader.paper(), trader.momentum_book(), trader.factor_book()
    dates = sorted({h["date"] for h in paper.history} | {h["date"] for h in mom.history}
                   | {h["date"] for h in fb.history})
    # Every line is 100 on the same session (the books started on different days):
    # the gaps between the lines after it are returns over the same period.
    base = _common_start(paper, mom, fb) or ""

    def indexed(history, key):
        by_day = {h["date"]: h.get(key) for h in history}
        first = next((by_day[d] for d in dates if d >= base and by_day.get(d)), None)
        return [round(by_day[d] / first * 100, 3) if first and by_day.get(d) else None for d in dates]

    bench_hist = max((paper.history, mom.history, fb.history), key=len)
    series = [
        {"name": "Phòng thủ", "color": "var(--c-factor)", "values": indexed(fb.history, "nav")},
        {"name": "Quant Engine", "color": "var(--c-engine)", "values": indexed(paper.history, "nav")},
        {"name": "Động lượng", "color": "var(--c-mom)", "values": indexed(mom.history, "nav")},
        {"name": "VNINDEX", "color": "var(--c-bench)", "values": indexed(bench_hist, "benchmark"), "dashed": True},
    ]
    return ui.nav_chart(dates, series, run_time=_run_time(trader))


def _run_time(trader) -> str:
    """When the scheduler really runs the bot: ``trade_time``, never before the bar is final."""
    from vnbot.settings import MIN_TRADE_TIME

    return max(trader.settings.trade_time, MIN_TRADE_TIME)


def _capital_card(trader) -> str:
    """The form that sets the paper books' capital (each chosen book starts over)."""
    current = trader.paper_capital()
    boxes = "".join(
        f'<label class="check"><input type="checkbox" name="books" value="{key}" checked> {html.escape(name)}</label>'
        for key, name in trader.BOOK_NAMES.items())
    form = f"""
<form method="post" action="/bot/capital">
  <div class="row">
    <div class="fld" style="flex:1 1 220px"><label class="f" for="capital">Vốn mỗi tài khoản (đồng)</label>
      <input id="capital" name="capital" inputmode="numeric" value="{ui.money(current)}" required></div>
    <button class="btn btn-primary" type="submit">Đổi vốn</button>
  </div>
  <div class="checks">{boxes}</div>
  <label class="check warn"><input type="checkbox" name="confirm" value="1" required> Tôi hiểu: tài khoản được chọn
  bắt đầu lại từ đầu với vốn mới (sổ cũ được giữ lại trong data/trading).</label>
</form>"""
    return ui.card("Vốn tài khoản ảo", form,
                   sub=f"Đang dùng {ui.money(current)} đồng cho tài khoản mới · tối thiểu 10 triệu", cls="accent-gold")


def _bot_page(trader, msg: str = "") -> str:
    from tradingagents.quant import params as qp
    from tradingagents.trading import calibrate as cal

    mom, fb = trader.momentum_book(), trader.factor_book()
    basket = fb.meta.get("fb_basket") or []
    last = trader.last_report()
    running = trader.running
    run_btn = (
        '<form method="post" action="/bot/run" style="margin:0">'
        f'<button class="btn btn-primary" type="submit"{" disabled" if running else ""}>'
        f'{"Đang chạy…" if running else "▶ Chạy phiên ngay"}</button></form>'
    )
    mode = {"paper": "Tài khoản ảo", "signal": "Ảo + tín hiệu thật", "off": "Tắt"}.get(trader.settings.trade_mode, "")
    lead = (f'Chế độ <b>{mode}</b> · tín hiệu thật theo <b>{html.escape(cal.STRATEGIES.get(trader.settings.signal_strategy, ""))}</b>'
            f' · tự chạy lúc {html.escape(_run_time(trader))} mỗi ngày (chạy bù phiên bị lỡ)'
            + (f' · phiên gần nhất {html.escape(last["day"])}' if last.get("day") else ""))
    parts = [ui.hero("Bot giao dịch", lead, run_btn, eyebrow="Ba tài khoản ảo · không gửi lệnh"), ui.alert(msg, "info")]
    base = _common_start(trader.paper(), mom, fb)
    parts.append(ui.card("So sánh ba chiến lược", _chart(trader),
                         sub="NAV chỉ số hoá về 100 tại phiên " + html.escape(base or "đầu tiên")
                             + " (phiên đầu tiên chung của ba tài khoản), cùng VNINDEX"))
    parts.append('<div class="mt">' + _capital_card(trader) + "</div>")
    basket_html = ('<div class="chips">' + "".join(f'<span class="chip">{html.escape(s)}</span>' for s in basket) + "</div>"
                   if basket else "tiền mặt")
    current = qp.active()
    parts.append('<div class="stack" style="margin-top:16px">'
                 + _book_card("Phòng thủ (mặc định)", fb, accent="accent-factor",
                              sub=f"Top {current.fb_top_k} trong {current.fb_universe} mã vốn hoá lớn nhất (ít biến động, "
                                  f"gần đỉnh 52 tuần), về tiền mặt khi VNINDEX dưới MA{current.fb_market_ma}")
                 + _book_card("Quant Engine", trader.paper(), accent="accent-peach",
                              sub="Kế hoạch BUY của engine: vùng mua, stop, T1/T2")
                 + _book_card("Động lượng VN30", mom, accent="accent-sa",
                              sub="Top 5 VN30 theo đà tăng, về tiền mặt khi VNINDEX dưới MA100 (để so sánh)")
                 + "</div>")
    parts.append('<div class="mt"></div>')
    parts.append(ui.card("Rổ phòng thủ hiện tại", basket_html if basket else
                         '<span class="muted">Đang giữ tiền mặt (VNINDEX dưới đường trung bình) hoặc chưa xếp hạng.</span>'))
    prow = []
    groups = [(qp.FACTOR_FIXED, "Phòng thủ (cố định)"), (qp.MOMENTUM_TUNABLE, "Động lượng"), (qp.TUNABLE, "Engine")]
    for k in list(qp.TUNABLE) + list(qp.MOMENTUM_TUNABLE) + list(qp.FACTOR_FIXED):
        default, now = getattr(qp.DEFAULT, k), getattr(current, k)
        changed = default != now
        group = next(name for keys, name in groups if k in keys)
        prow.append(f'<tr class="{"changed" if changed else ""}"><td><code>{html.escape(k)}</code></td>'
                    f'<td class="muted small">{group}</td><td class="num muted">{default}</td>'
                    f'<td class="num"><b>{now}</b></td>'
                    f'<td>{_TUNED_BADGE if changed else ""}</td></tr>')
    reports = cal.last_reports(14)
    crow = [
        f'<tr><td class="num">{html.escape(r["as_of"])}</td>'
        f'<td>{html.escape(cal.STRATEGIES.get(r.get("strategy", "engine"), ""))}</td>'
        f'<td>{(_CHANGED_BADGE + html.escape(r["accepted"]["name"])) if r.get("accepted") else _KEPT_BADGE}</td>'
        f'<td class="muted small">{html.escape(r.get("note") or "")}</td></tr>'
        for r in reversed(reports)]
    parts.append('<div class="stack" style="margin-top:16px">'
                 + ui.card("Tự hiệu chỉnh hằng đêm",
                           ui.table([("Ngày", ""), ("Chiến lược", ""), ("Kết quả", ""), ("Ghi chú", "")], crow,
                                    f"Chưa chạy hiệu chỉnh - chạy sau phiên đầu tiên lúc {html.escape(_run_time(trader))}."),
                           sub="Quant Engine và Động lượng: mỗi đêm tối đa một thay đổi nhỏ; Phòng thủ giữ bộ đã kiểm định", flush=True)
                 + ui.card("Ngưỡng đang dùng",
                           ui.table([("Ngưỡng", ""), ("Nhóm", ""), ("Mặc định", "num"), ("Đang dùng", "num"), ("", "")],
                                    prow, ""),
                           sub="Dòng tô xanh là ngưỡng bot đã tự chỉnh", flush=True)
                 + "</div>")
    if last.get("text"):
        parts.append('<div style="margin-top:16px">'
                     + ui.card(f"Báo cáo phiên {html.escape(last['day'])}", f'<pre class="report">{html.escape(last["text"])}</pre>')
                     + "</div>")
    return "".join(parts)


def create_app(settings: Settings, jobs: JobQueue, watchlist: Watchlist, trader=None, zoom=None,
               switches=None) -> FastAPI:
    from vnbot.channels import Switches

    app = FastAPI(title="TradingAgents VN", docs_url=None, redoc_url=None, openapi_url=None)
    if switches is None:
        switches = Switches(settings.data_dir, {"intraday": settings.intraday_alerts})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    def guard(request: Request,
              credentials: Annotated[HTTPBasicCredentials | None, Depends(_BASIC)] = None) -> None:
        if not settings.web_password:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, LOCKED_MESSAGE)
        client = _client(request)
        wait = throttle.retry_after(client)
        if wait:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Sai mật khẩu quá nhiều lần. Thử lại sau {wait // 60 + 1} phút.",
                                headers={"Retry-After": str(wait)})
        if credentials is None:   # the browser's first request, before it asks for the password
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
        if not secrets.compare_digest(credentials.password.encode("utf-8"), settings.web_password.encode("utf-8")):
            throttle.failure(client)
            logger.warning("Web: wrong password from %s", client)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
        throttle.success(client)

    def same_origin(request: Request) -> None:
        """Refuse a form posted from another site (CSRF): Basic auth rides along with it."""
        allowed = {_host(h) for h in (
            request.headers.get("host", ""),
            request.headers.get("x-forwarded-host", "").split(",")[0],
            settings.public_url,
        ) if h}
        origin = request.headers.get("origin")
        if origin is not None:
            if origin == "null" or _host(origin) not in allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")
            return
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")
        referer = request.headers.get("referer")
        if referer and _host(referer) not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Yêu cầu từ trang khác bị từ chối.")

    post_guard = [Depends(guard), Depends(same_origin)]

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "time": time.time()})

    @app.get("/static/{name}")
    def static_file(name: str):
        """The logo and favicon (public: the Zoom pages show them too)."""
        if name not in ui.STATIC_FILES:
            raise HTTPException(404, "Không tìm thấy trang.")
        return FileResponse(ui.STATIC_DIR / name, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def zoom_webhook(request: Request) -> JSONResponse:
        """Zoom chatbot events (Bot endpoint, Event notification and Deauthorization endpoint).
        Signed by Zoom (HMAC), no password; the last requests are kept for the /zoom page."""
        if zoom is None or not zoom.chatbot_on:
            return JSONResponse({"error": "zoom is off"}, status_code=404)
        raw = await request.body()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "bad json"}, status_code=400)
        event = data.get("event")
        ok, why = zoom.verify_detail(raw, request.headers)
        if event == "endpoint.url_validation":
            plain = str((data.get("payload") or {}).get("plainToken", ""))
            if not ok or not plain or not zoom.config.secret_token:
                zoom.record(event, False, why if not ok else "thiếu plainToken hoặc ZOOM_SECRET_TOKEN")
                return JSONResponse({"error": "invalid"}, status_code=401 if not ok else 400)
            zoom.record(event, True, "đã trả lời Validate")
            return JSONResponse(zoom.url_validation(plain))
        zoom.record(event, ok, why, data)
        if not ok:
            logger.warning("Zoom webhook from %s refused: %s", _client(request), why)
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if event in ("app_deauthorized", "bot_installed"):
            zoom.lifecycle(event, data.get("payload") or {})
            return JSONResponse({"ok": True})
        if not zoom.commands_on:
            return JSONResponse({"error": "zoom commands are off"}, status_code=404)
        if not switches.on("zoom_commands"):
            zoom.record(event, True, "bỏ qua: nhận lệnh Zoom đang tắt trên trang web", data)
            return JSONResponse({"ok": True, "ignored": "zoom commands switched off"})
        zoom.handle(data)
        return JSONResponse({"ok": True})

    app.add_api_route("/webhook/zoom", zoom_webhook, methods=["POST"])
    app.add_api_route("/zoom/bot", zoom_webhook, methods=["POST"])     # the OKX bot's other path

    from vnbot import pages

    pages.register(app, settings, zoom, guard=guard, post_guard=post_guard,
                   notify=getattr(zoom, "alert_owner", None))

    critic_llm: list = []        # the provider chain, built on the first web /critic

    def _ai_today() -> tuple[dict, list[dict]]:
        from tradingagents.llm_clients.fallback_client import provider_status, usage_today

        return usage_today(), provider_status()

    def _ai_stat(usage: dict) -> tuple[str, str, str]:
        cap = usage["cap"]
        value = f'{usage["total"]}<span class="muted" style="font-size:.6em"> / {cap}</span>' if cap else str(usage["total"])
        return ("AI hôm nay", value, (ui.meter(usage["total"], cap) or "không giới hạn"))

    def _alerts_today() -> list[dict]:
        from vnbot.intraday import read_alerts

        return read_alerts(settings.data_dir / "intraday_alerts.json", today_vn())

    @app.get("/", dependencies=[Depends(guard)])
    def index(error: str = "") -> HTMLResponse:
        from tradingagents.market import vn_rules

        usage, providers = _ai_today()
        rows = [_job_row(j) for j in jobs.recent(30)]
        codes = watchlist.get()
        today = today_vn()
        phase = vn_rules.session_phase()
        scope = {"vn100": "VN100", "vn30": "VN30"}.get(settings.scan_universe, "Watchlist")
        alerts = _alerts_today()
        ready = sum(1 for p in providers if not p["resting_seconds"])
        stats = ui.strip([
            _ai_stat(usage),
            ("Thị trường", html.escape(_PHASE_VI.get(phase, phase)), f"{html.escape(today)} · giờ Việt Nam"),
            ("Cảnh báo hôm nay", str(len(alerts)),
             "trong phiên, không gọi AI" if settings.intraday_alerts else "đang tắt (INTRADAY_ALERTS)"),
            ("Quét 15:30", html.escape(scope + (" + watchlist" if scope != "Watchlist" else "")),
             f"tối đa {settings.daily_ai_max} mã gửi AI · {_MODE_VI.get(settings.daily_ai_mode, '')}"),
        ])
        ask = f"""
<form method="post" action="/analyze">
  <div class="row">
    <div class="fld"><label class="f" for="ticker">Mã cổ phiếu</label>
      <input class="ticker" id="ticker" name="ticker" placeholder="FPT" required maxlength="20" autocomplete="off" autocapitalize="characters"></div>
    <div class="fld"><label class="f" for="date">Ngày phân tích</label>
      <input id="date" name="date" type="date" value="{today}" max="{today}"></div>
  </div>
  <div class="row" style="margin-top:14px">
    <button class="btn btn-primary" type="submit" formaction="/critic">AI phản biện · 1 lượt</button>
    <button class="btn btn-ghost" type="submit" formaction="/quick">Chỉ quyết định hệ thống</button>
    <button class="btn btn-ghost" type="submit">Phân tích đầy đủ · ~25 lượt</button>
  </div>
  <p class="muted small" style="margin:12px 0 0">Quyết định hệ thống ra trong vài giây, không tốn lượt AI.
  AI phản biện hỏi AI một lần: đồng ý hay phản đối, vì sao. Phân tích đầy đủ chạy cả chuỗi agent (vài phút).</p>
</form>"""
        chips = ('<div class="chips" style="margin-bottom:14px">'
                 + "".join(f'<span class="chip">{html.escape(c)}</span>' for c in codes) + "</div>") if codes else ""
        watch = f"""{chips}
<form method="post" action="/watchlist" class="row">
  <div class="fld" style="flex:1 1 220px"><label class="f" for="codes">Danh sách mã</label>
    <input id="codes" name="codes" value="{html.escape(",".join(codes))}" placeholder="FPT,HPG,VCB"></div>
  <button class="btn btn-sa" type="submit">Lưu</button>
</form>
<p class="muted small" style="margin:12px 0 0">Mỗi ngày giao dịch lúc {html.escape(settings.daily_report_time)}: Quant Engine
quét {html.escape(scope)} cùng watchlist, các mã mạnh nhất được AI phản biện, kết quả gửi qua Telegram/Zoom.</p>"""
        bot_link = ""
        if trader is not None and settings.trade_mode != "off":
            books = [("Phòng thủ", trader.factor_book()), ("Quant Engine", trader.paper()),
                     ("Động lượng", trader.momentum_book())]

            def kpi(title, book):
                st = book.stats()
                since = f' · từ {html.escape(book.start_date)}' if book.start_date else ""
                return ui.kpi(title, ui.pct(st["return_pct"]), f'NAV {ui.money_short(st["nav"])}{since}',
                              ui.tone(st["return_pct"]))

            bot_link = ui.card(
                "Bot giao dịch",
                '<div class="grid g3">' + "".join(kpi(t, b) for t, b in books) + "</div>",
                sub="Ba tài khoản ảo, mỗi tài khoản tính lãi/lỗ từ ngày nó bắt đầu",
                action='<a class="btn btn-ghost" href="/bot">Chi tiết →</a>', cls="accent-gold",
            )
        ai_card = ui.card(
            "Nhà cung cấp AI",
            f'<p class="small" style="margin:0">{ready}/{len(providers)} sẵn sàng · '
            f'hôm nay đã gọi <b>{usage["total"]}</b>' + (f' / {usage["cap"]} lượt' if usage["cap"] else " lượt") + "</p>"
            + ui.meter(usage["total"], usage["cap"]),
            sub="Theo thứ tự dự phòng" if providers else
                "Chưa cấu hình nhà cung cấp AI nào (ROUTER9_*, GROQ_API_KEY, OPENROUTER_API_KEY).",
            action='<a class="btn btn-ghost" href="/ai">Xem →</a>')
        body = (
            ui.hero("Phân tích cổ phiếu",
                    "Hành động và mọi mức giá do hệ thống định lượng chốt bằng code; AI chỉ phản biện và diễn giải.",
                    eyebrow="Bảng điều khiển")
            + ui.alert(error, "err")
            + stats
            + '<div class="grid g-wide">'
            + '<div class="stack">' + ui.card("Phân tích một mã", ask, cls="accent-gold")
            + ui.card("Lượt phân tích gần đây",
                      ui.table([("Mã", ""), ("Ngày", "num"), ("Hành động", ""), ("Trạng thái", ""), ("Nguồn", ""),
                                ("Tạo lúc", "num")], rows, "Chưa có lượt phân tích nào."),
                      sub="Trang tự tải lại khi có lượt đang chạy" if jobs.is_active() else "", flush=True)
            + "</div>"
            + '<div class="stack">' + bot_link + ui.card("Theo dõi hằng ngày", watch) + ai_card + "</div>"
            + "</div>"
        )
        return _page("Phân tích", body, refresh=20 if jobs.is_active() else None, active="home",
                     show_bot=trader is not None and settings.trade_mode != "off")

    @app.get("/ai", dependencies=[Depends(guard)])
    def ai_page() -> HTMLResponse:
        from vnbot.critic import CriticLog

        usage, providers = _ai_today()
        log = CriticLog(settings.data_dir)
        st = log.stats()
        alerts = _alerts_today()
        prow = []
        for p in providers:
            state = (f'<span class="badge b-watch">nghỉ {p["resting_seconds"]}s</span>'
                     if p["resting_seconds"] else '<span class="badge b-ok">sẵn sàng</span>')
            calls = f'{p["calls_today"]} / {p["cap"]}' if p["cap"] else str(p["calls_today"])
            prow.append(f'<tr><td><b>{html.escape(p["label"])}</b><div class="muted small">{html.escape(p["model"])}</div>'
                        + (f'<div class="small neg">{html.escape(p["last_error"])}</div>' if p["last_error"] else "")
                        + f'</td><td>{state}</td><td class="num">{calls}{ui.meter(p["calls_today"], p["cap"])}</td></tr>')
        budget = ui.card(
            "Hạn mức AI hôm nay",
            ui.table([("Nhà cung cấp", ""), ("Trạng thái", ""), ("Lượt", "num")], prow,
                     "Chưa cấu hình nhà cung cấp AI nào (ROUTER9_*, GROQ_API_KEY, OPENROUTER_API_KEY)."),
            sub=f"Tổng {usage['total']}" + (f" / {usage['cap']} lượt (LLM_MAX_CALLS_PER_DAY)" if usage["cap"] else " lượt")
                + " · tính theo ngày giờ Việt Nam", flush=True)
        feed = "".join(f'<li><span class="t">{html.escape(a.get("at", ""))}</span><span>{html.escape(a.get("text", ""))}</span></li>'
                       for a in reversed(alerts))
        if not feed:
            when = (f"mỗi {settings.intraday_interval_min} phút trong giờ khớp lệnh liên tục"
                    if settings.intraday_alerts else "đang tắt (INTRADAY_ALERTS=0)")
            feed = f'<li class="empty">Chưa có cảnh báo nào hôm nay. Kiểm tra {html.escape(when)}.</li>'
        alert_card = ui.card("Cảnh báo trong phiên", f'<ul class="feed">{feed}</ul>',
                             sub="Stop, mục tiêu, vùng mua, VNINDEX giảm mạnh · không gọi AI")
        crow = []
        for r in reversed(log.rows()[-30:]):
            out = r.get("outcome")
            if out is None:
                result = '<span class="badge b-none">chờ</span>'
            elif r["action"] == "BUY":
                result = f'<span class="num {ui.tone(out["r"])}">{out["r"]:+.2f}R</span> <span class="muted small">{html.escape(out["label"])}</span>'
            else:
                result = f'<span class="num">{ui.pct(out.get("pct"))}</span> <span class="muted small">{html.escape(out["label"])}</span>'
            verdict = ('<span class="badge b-ok">đồng ý</span>' if r["verdict"] == "AGREE"
                       else '<span class="badge b-err">phản đối</span>')
            crow.append(f'<tr><td class="num muted">{html.escape(r["day"])}</td><td class="sym">{html.escape(r["code"])}</td>'
                        f'<td>{ui.action_badge(r["action"])}</td><td>{verdict} <span class="muted small">{r["confidence"]}%</span></td>'
                        f'<td class="small">{html.escape(r.get("reason", ""))}</td><td>{result}</td></tr>')
        agree, reject = st["AGREE"], st["REJECT"]

        def r_of(x):
            return f'{x["avg_r"]:+.2f}R'.replace(".", ",") if x["n"] else "—"

        stats = ui.strip([
            _ai_stat(usage),
            ("AI phản đối đúng", f'{st["veto_right_pct"]}%' if st["veto_right_pct"] is not None else "—",
             f'{reject["n"]} lệnh MUA bị phản đối đã có kết quả'),
            ("Kết quả bóng", f'{r_of(agree)} <span class="muted" style="font-size:.6em">/ {r_of(reject)}</span>',
             "TB lệnh AI đồng ý / phản đối"),
            ("Cảnh báo hôm nay", str(len(alerts)), "trong phiên"),
        ])
        channels = [
            ("telegram", "Telegram · thông báo", settings.telegram_token != "",
             "báo cáo hằng ngày, AI phản biện, cảnh báo (lệnh Telegram vẫn trả lời khi tắt)"),
            ("zoom_notify", "Zoom · thông báo", bool(zoom and zoom.notify_on),
             "Incoming Webhook hoặc chatbot tới tài khoản đã ghép đôi"),
            ("zoom_commands", "Zoom · nhận lệnh", bool(zoom and zoom.commands_on), "lệnh gõ trong Zoom (/webhook/zoom)"),
            ("intraday", "Cảnh báo trong phiên", trader is not None and settings.trade_mode != "off",
             f"mỗi {settings.intraday_interval_min} phút trong giờ khớp lệnh, không gọi AI"),
        ]
        chrow = [f'<tr><td><b>{label}</b><div class="muted small">{html.escape(note)}</div></td>'
                 f"<td class='num'>{_switch_form(key, switches.on(key)) if ready else _UNSET_BADGE}</td></tr>"
                 for key, label, ready, note in channels]
        body = (
            ui.hero("AI & cảnh báo",
                    "AI chỉ được hỏi khi đáng hỏi: mỗi mã một lượt, trong hạn mức ngày. Bot ghi lại mọi lần AI phản biện "
                    "và chấm kết quả trên các phiên sau, để bạn biết có nên tin AI hay không.",
                    eyebrow="Giám sát")
            + stats
            + '<div class="grid g2">' + budget + alert_card + "</div>"
            + '<div class="mt">'
            + ui.card("AI phản biện gần đây",
                      ui.table([("Ngày", ""), ("Mã", ""), ("Hệ thống", ""), ("AI", ""), ("Lý do", ""), ("Kết quả", "")],
                               crow, "Chưa có lần phản biện nào. Lượt quét 15:30 hoặc nút “AI phản biện” sẽ tạo."),
                      sub="Lệnh MUA: chạm stop (−1R), chạm mục tiêu, hoặc sau 20 phiên · lệnh BÁN: giá sau 10 phiên",
                      flush=True)
            + "</div>"
            + '<div class="mt">' + ui.card("Kênh thông báo", ui.table([("Kênh", ""), ("", "")], chrow, ""), flush=True,
                                            action='<a class="btn btn-ghost" href="/zoom">Cấu hình Zoom →</a>') + "</div>"
        )
        return _page("AI & cảnh báo", body, active="ai", show_bot=trader is not None and settings.trade_mode != "off")

    @app.post("/critic", dependencies=post_guard)
    def critic_web(ticker: str = Form(...), date: str = Form("")):
        from tradingagents.quant import engine
        from vnbot.critic import CriticLog, default_llm

        try:
            code, day = check_request(ticker, date)
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        try:
            result = engine.analyze(code, day)
            if not critic_llm:
                critic_llm.append(default_llm(settings))
            log = CriticLog(settings.data_dir)
            log.settle_now()
            verdict = log.review(day, result, critic_llm[0])
        except Exception:  # noqa: BLE001 - shown to the user, not raised
            logger.exception("Web critic for %r failed", code)
            return RedirectResponse(f"/?error={quote('Không phản biện được (lỗi dữ liệu hoặc AI). Thử lại sau ít phút.')}",
                                    status_code=303)
        body = (ui.hero(f'<span class="tick">{html.escape(code)}</span> {ui.action_badge(result.action)}',
                        f"Ngày {html.escape(day)} · AI phản biện, 1 lượt gọi",
                        '<a class="btn btn-ghost" href="/">← Mã khác</a>', eyebrow="AI phản biện")
                + '<div class="stack">'
                + ui.card("Ý kiến của AI", f'<pre class="report">{html.escape(verdict)}</pre>',
                          sub="AI không đổi hành động hay mức giá; kết quả được chấm trên các phiên sau (trang AI & cảnh báo)",
                          cls="accent-gold")
                + ui.card("Quyết định hệ thống định lượng", f'<pre class="report">{html.escape(result.deterministic_summary)}</pre>',
                          sub="Quant Engine, bằng code")
                + "</div>")
        return _page(f"{code} - AI phản biện", body, active="home")

    @app.post("/analyze", dependencies=post_guard)
    def analyze(ticker: str = Form(...), date: str = Form("")):
        try:
            code, day = check_request(ticker, date)
            job = jobs.submit(code, day, "web")
        except ValueError as exc:   # a bad request, or QueueFull
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/quick", dependencies=post_guard)
    def quick(ticker: str = Form(...), date: str = Form("")):
        try:
            text = quick_text(ticker, date)
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        except Exception:  # noqa: BLE001 - shown to the user, not raised
            # The details go to the log; the page only says what failed.
            logger.exception("Quick decision for %r failed", ticker)
            text = "Không tính được quyết định hệ thống (lỗi đọc dữ liệu hoặc tính toán). Thử lại sau ít phút."
        code = ticker.strip().upper()[:20]
        body = (ui.hero(f'<span class="tick">{html.escape(code)}</span> · quyết định hệ thống',
                        "Do Quant Engine tính bằng code, không gọi AI.",
                        '<a class="btn btn-ghost" href="/">← Mã khác</a>', eyebrow="Quant Engine")
                + ui.card("", f'<pre class="report">{html.escape(text)}</pre>'))
        return _page(f"{code} - quyết định hệ thống", body, active="home")   # page() escapes the title

    @app.post("/watchlist", dependencies=post_guard)
    def save_watchlist(codes: str = Form("")):
        try:
            wanted = [c for c in codes.replace(" ", ",").split(",") if c.strip()]
            if len(wanted) > MAX_WATCHLIST:
                raise ValueError(f"Tối đa {MAX_WATCHLIST} mã trong danh sách theo dõi.")
            watchlist.set(wanted)
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.get("/bot", dependencies=[Depends(guard)])
    def bot_page(msg: str = "") -> HTMLResponse:
        if trader is None or settings.trade_mode == "off":
            raise HTTPException(404, "Bot giao dịch đang tắt (TRADE_MODE=off).")
        return _page("Bot giao dịch", _bot_page(trader, msg), active="bot", refresh=15 if trader.running else None)

    @app.post("/bot/run", dependencies=post_guard)
    def bot_run():
        if trader is None or settings.trade_mode == "off":
            raise HTTPException(404, "Bot giao dịch đang tắt (TRADE_MODE=off).")
        return RedirectResponse(f"/bot?msg={quote(trader.start_run())}", status_code=303)

    @app.post("/bot/capital", dependencies=post_guard)
    def bot_capital(books: Annotated[list[str] | None, Form()] = None, capital: str = Form(""), confirm: str = Form("")):
        if trader is None or settings.trade_mode == "off":
            raise HTTPException(404, "Bot giao dịch đang tắt (TRADE_MODE=off).")
        try:
            if confirm != "1":
                raise ValueError("Đánh dấu ô xác nhận: các tài khoản được chọn sẽ bắt đầu lại từ đầu.")
            amount = float("".join(ch for ch in capital if ch.isdigit()) or 0)
            msg = trader.reset_paper(amount, books or [])
        except ValueError as exc:
            msg = f"⚠️ {exc}"
        return RedirectResponse(f"/bot?msg={quote(msg)}", status_code=303)

    @app.post("/channels/{name}", dependencies=post_guard)
    def channel_switch(name: str, on: str = Form("1")):
        try:
            switches.set(name, on == "1")
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        logger.info("Web: channel %s switched %s", name, "on" if on == "1" else "off")
        return RedirectResponse("/ai#kenh", status_code=303)

    @app.get("/jobs/{job_id}", dependencies=[Depends(guard)])
    def job_page(job_id: str) -> HTMLResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không có lượt phân tích này")
        live = job.status in ("queued", "quick", "running")
        right = (f'<a class="btn btn-primary" href="/reports/{job.id}">Báo cáo đầy đủ →</a>' if job.html_path
                 else '<a class="btn btn-ghost" href="/">← Trang chính</a>')
        parts = [ui.hero(f'<span class="tick">{html.escape(job.ticker)}</span> {ui.action_badge(job.action)}',
                         f"Ngày phân tích {html.escape(job.date)} · {_SOURCE_VI.get(job.source, '')}", right,
                         eyebrow="Phân tích đầy đủ"),
                 ui.stepper(job.status)]
        if job.status == "queued":
            parts.append(ui.alert(f"Còn {jobs.position(job.id)} lượt chờ phía trước. Trang tự tải lại.", "info"))
        if job.status == "running":
            parts.append(ui.alert("Các agent đang phân tích (thường vài phút). Trang tự tải lại.", "info"))
        if job.error:
            parts.append(ui.alert(job.error, "err"))
        blocks = []
        if job.quick_summary:
            blocks.append(ui.card("Quyết định hệ thống định lượng", f'<pre class="report">{html.escape(job.quick_summary)}</pre>',
                                  sub="Quant Engine - đã chốt, các agent không được đổi", cls="accent-peach"))
        if job.final_decision:
            blocks.append(ui.card("Quyết định cuối", f'<pre class="report">{html.escape(job.final_decision)}</pre>',
                                  sub="Portfolio Manager, sau Output Guard", cls="accent-sa"))
        if job.guard_notes:
            blocks.append(ui.card("Output Guard đã chặn", f'<pre class="report">{html.escape(job.guard_notes)}</pre>',
                                  sub="Những đoạn AI nói trái với hệ thống"))
        if blocks:
            parts.append('<div class="stack" style="margin-top:16px">' + "".join(blocks) + "</div>")
        return _page(f"{job.ticker} {job.date}", "".join(parts), refresh=15 if live else None, active="home")

    @app.get("/reports/{job_id}", dependencies=[Depends(guard)])
    def report(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.html_path is None:
            raise HTTPException(404, "Chưa có báo cáo")
        return FileResponse(job.html_path, media_type="text/html; charset=utf-8")

    # Starlette's class: also the 404/405 the router raises for unknown paths/methods.
    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return HTMLResponse("Cần mật khẩu", status_code=401, headers=exc.headers)
        message = {"Not Found": "Không tìm thấy trang.",
                   "Method Not Allowed": "Phương thức không được hỗ trợ."}.get(exc.detail, str(exc.detail))
        title = {503: "Trang web đang khoá", 429: "Tạm khoá đăng nhập"}.get(exc.status_code, "Có lỗi")
        body = (ui.hero(title, "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert(message, "err"))
        return HTMLResponse(ui.page("Lỗi", body, show_bot=False), status_code=exc.status_code,
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    def bad_form(request: Request, exc: RequestValidationError):
        body = (ui.hero("Có lỗi", "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert("Thiếu hoặc sai dữ liệu trong biểu mẫu.", "err"))
        return _page("Lỗi", body, status_code=422, show_bot=False)

    @app.exception_handler(Exception)
    def server_error(request: Request, exc: Exception):
        # Never a traceback or an exception text on the page; Starlette re-raises
        # the exception after this response, so the server still logs it.
        body = (ui.hero("Có lỗi", "", '<a class="btn btn-ghost" href="/">← Trang chính</a>')
                + ui.alert("Lỗi máy chủ khi xử lý yêu cầu. Chi tiết đã ghi vào log.", "err"))
        return _page("Lỗi", body, status_code=500, show_bot=False)

    return app
