"""The web page: ask for an analysis, follow the queue, read the reports.

Plain server-rendered HTML (no build step, no JavaScript framework) so the
upload stays small. With WEB_PASSWORD set, every page asks for it (HTTP Basic;
any user name), since each analysis spends LLM quota.
"""

from __future__ import annotations

import html
import secrets
import time
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from vnbot.jobs import Job, JobQueue
from vnbot.service import Watchlist, check_request, quick_text, today_vn
from vnbot.settings import Settings

_STATUS_VI = {
    "queued": "đang chờ",
    "quick": "đang tính quyết định hệ thống",
    "running": "các agent đang phân tích",
    "done": "xong",
    "failed": "lỗi",
}
_ACTION_COLOR = {"BUY": "#1a7f37", "SELL": "#cf222e", "HOLD": "#9a6700", "WATCH": "#9a6700", "NO_TRADE": "#57606a"}

STYLE = """
:root{--bg:#fff;--fg:#1f2328;--muted:#57606a;--line:#d0d7de;--card:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;--accent:#4493f8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0 4px}h2{font-size:17px;margin:24px 0 8px}
p.sub{color:var(--muted);margin:0 0 16px}form.ask{display:flex;flex-wrap:wrap;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
input,button{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input[name=ticker]{width:120px;text-transform:uppercase}button{background:var(--accent);border-color:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--accent)}table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:13px}.tag{font-weight:600}.muted{color:var(--muted)}pre{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;font:13px/1.5 ui-monospace,monospace}
a{color:var(--accent)}.err{color:#cf222e}.ok{color:#1a7f37}@media (max-width:560px){td:nth-child(3),th:nth-child(3){display:none}}
"""


# Module level: the dependency annotations below are strings (postponed
# evaluation), which FastAPI resolves in module scope.
_BASIC = HTTPBasic(auto_error=False)


def _page(title: str, body: str, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(
        f'<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def _action(action: str) -> str:
    if not action:
        return '<span class="muted">-</span>'
    color = _ACTION_COLOR.get(action, "inherit")
    return f'<span class="tag" style="color:{color}">{html.escape(action)}</span>'


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else ""


def _job_row(job: Job) -> str:
    link = f'<a href="/jobs/{job.id}">{html.escape(job.ticker)}</a>'
    state = _STATUS_VI.get(job.status, job.status)
    cls = "err" if job.status == "failed" else ("ok" if job.status == "done" else "muted")
    return (f"<tr><td>{link}</td><td>{job.date}</td><td>{html.escape(job.source)}</td>"
            f"<td>{_action(job.action)}</td><td class='{cls}'>{state}</td><td class='muted'>{_when(job.created)}</td></tr>")


def create_app(settings: Settings, jobs: JobQueue, watchlist: Watchlist) -> FastAPI:
    app = FastAPI(title="TradingAgents VN", docs_url=None, redoc_url=None, openapi_url=None)

    def guard(credentials: Annotated[HTTPBasicCredentials | None, Depends(_BASIC)] = None) -> None:
        if not settings.web_password:
            return
        if credentials is None or not secrets.compare_digest(credentials.password, settings.web_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "time": time.time()})

    @app.get("/", dependencies=[Depends(guard)])
    def index(error: str = "") -> HTMLResponse:
        from tradingagents.llm_clients.fallback_client import provider_status

        rows = "".join(_job_row(j) for j in jobs.recent(30)) or '<tr><td colspan="6" class="muted">Chưa có lượt phân tích nào.</td></tr>'
        providers = "".join(
            f"<tr><td>{html.escape(p['label'])}</td><td class='muted'>{html.escape(p['model'])}</td>"
            f"<td>{'nghỉ ' + str(p['resting_seconds']) + 's - ' + html.escape(p['last_error']) if p['resting_seconds'] else '<span class=ok>sẵn sàng</span>'}</td></tr>"
            for p in provider_status()
        ) or '<tr><td colspan="3" class="err">Chưa cấu hình nhà cung cấp AI nào (ROUTER9_*, GROQ_API_KEY, OPENROUTER_API_KEY).</td></tr>'
        watch = ", ".join(watchlist.get()) or "(trống)"
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""
<h1>TradingAgents VN</h1>
<p class="sub">Phân tích cổ phiếu HOSE/HNX/UPCoM bằng nhiều agent AI. Hành động và mọi mức giá do hệ thống định lượng chốt; các agent chỉ diễn giải.</p>
{err}
<form class="ask" method="post" action="/analyze">
  <input name="ticker" placeholder="FPT" required maxlength="20" autocomplete="off">
  <input name="date" type="date" value="{today_vn()}" max="{today_vn()}">
  <button type="submit">Phân tích đầy đủ</button>
  <button class="ghost" type="submit" formaction="/quick">Chỉ quyết định hệ thống</button>
</form>
<h2>Lượt phân tích gần đây</h2>
<table><tr><th>Mã</th><th>Ngày</th><th>Nguồn</th><th>Hành động</th><th>Trạng thái</th><th>Tạo lúc</th></tr>{rows}</table>
<h2>Theo dõi hằng ngày</h2>
<form class="ask" method="post" action="/watchlist">
  <input name="codes" value="{html.escape(watch if watch != '(trống)' else '')}" placeholder="FPT,HPG,VCB" style="flex:1;min-width:200px">
  <button type="submit">Lưu</button>
</form>
<p class="muted">Báo cáo tự động lúc {settings.daily_report_time} các ngày giao dịch, gửi qua Telegram.</p>
<h2>Nhà cung cấp AI (theo thứ tự dự phòng)</h2>
<table><tr><th>Nhà cung cấp</th><th>Model</th><th>Trạng thái</th></tr>{providers}</table>
"""
        busy = any(j.status in ("queued", "quick", "running") for j in jobs.recent(10))
        return _page("TradingAgents VN", body, refresh=20 if busy else None)

    @app.post("/analyze", dependencies=[Depends(guard)])
    def analyze(ticker: str = Form(...), date: str = Form("")):
        try:
            code, day = check_request(ticker, date)
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        job = jobs.submit(code, day, "web")
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/quick", dependencies=[Depends(guard)])
    def quick(ticker: str = Form(...), date: str = Form("")) -> HTMLResponse:
        try:
            text = quick_text(ticker, date)
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        except Exception as exc:  # noqa: BLE001 - shown to the user, not raised
            text = f"Không tính được: {exc}"
        return _page(f"{ticker.upper()} - quyết định hệ thống",
                     f'<p><a href="/">← Trang chính</a></p><h1>Quyết định hệ thống định lượng</h1><pre>{html.escape(text)}</pre>')

    @app.post("/watchlist", dependencies=[Depends(guard)])
    def save_watchlist(codes: str = Form("")):
        try:
            watchlist.set([c for c in codes.replace(" ", ",").split(",") if c.strip()])
        except ValueError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.get("/jobs/{job_id}", dependencies=[Depends(guard)])
    def job_page(job_id: str) -> HTMLResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không có lượt phân tích này")
        parts = [f'<p><a href="/">← Trang chính</a></p><h1>{html.escape(job.ticker)} · {job.date} {_action(job.action)}</h1>',
                 f'<p class="sub">Trạng thái: {_STATUS_VI.get(job.status, job.status)}</p>']
        if job.status == "queued":
            parts.append(f"<p>Còn {jobs.position(job.id)} lượt chờ phía trước.</p>")
        if job.quick_summary:
            parts.append(f"<h2>Quyết định hệ thống định lượng</h2><pre>{html.escape(job.quick_summary)}</pre>")
        if job.status == "running":
            parts.append('<p class="muted">Các agent đang phân tích (thường vài phút). Trang tự tải lại.</p>')
        if job.guard_notes:
            parts.append(f'<h2>Output Guard</h2><pre class="err">{html.escape(job.guard_notes)}</pre>')
        if job.final_decision:
            parts.append(f"<h2>Quyết định cuối</h2><pre>{html.escape(job.final_decision)}</pre>")
        if job.html_path:
            parts.append(f'<p><a href="/reports/{job.id}">Mở báo cáo đầy đủ của các agent →</a></p>')
        if job.error:
            parts.append(f'<p class="err">{html.escape(job.error)}</p>')
        live = job.status in ("queued", "quick", "running")
        return _page(f"{job.ticker} {job.date}", "".join(parts), refresh=15 if live else None)

    @app.get("/reports/{job_id}", dependencies=[Depends(guard)])
    def report(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.html_path is None:
            raise HTTPException(404, "Chưa có báo cáo")
        return FileResponse(job.html_path, media_type="text/html; charset=utf-8")

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return HTMLResponse("Cần mật khẩu", status_code=401, headers=exc.headers)
        return _page("Lỗi", f'<p><a href="/">← Trang chính</a></p><p class="err">{html.escape(str(exc.detail))}</p>')

    return app
