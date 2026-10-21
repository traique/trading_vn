"""Open the web port at once, before the app is ready (as the OKX bot does).

A host such as Botkeep reports "This application is not responding" while
nothing listens on the port it assigned, and may restart a process that is
only slow to import its libraries. ``LateApp`` is served from the first
second: ``/health`` answers 200 and the other pages say the bot is starting
(503 with ``Retry-After``, refreshed every few seconds), until the real app is
handed over with ``ready``. If the start fails, every page says so and points
to the log, and ``/health`` answers 500, instead of the port going quiet.
"""

from __future__ import annotations

import html
import json
import logging

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>{title} · TradingAgents VN</title>
<style>:root{{--bg:#F6F4EF;--ink:#14161B;--mut:#6F737D;--gold:#B8963E;--gold2:#E3C979;--card:#fff;--line:#E7E2D7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0B0D12;--ink:#ECEEF2;--mut:#8A909C;--card:#14171F;--line:#252A36}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;box-sizing:border-box}}
.c{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:28px 26px;max-width:420px;text-align:center}}
.m{{display:block;width:124px;height:88px;margin:0 auto 10px;object-fit:contain}}
h1{{font:600 22px/1.3 Georgia,serif;margin:0 0 6px}}p{{color:var(--mut);margin:0}}</style></head>
<body><div class="c"><img class="m" src="/static/logo.png" alt="LCTA"><h1>{title}</h1><p>{text}</p></div></body></html>"""


class LateApp:
    """An ASGI app that answers by itself until ``ready(app)`` hands it the real one."""

    def __init__(self, retry_seconds: int = 5):
        self.app = None
        self.error = ""
        self.retry = retry_seconds

    def ready(self, app) -> None:
        self.app = app
        logger.info("Web: the app is ready")

    def failed(self, error: str) -> None:
        self.error = error or "lỗi chưa xác định"

    async def __call__(self, scope, receive, send):
        if self.app is not None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if path.startswith("/static/"):
            from vnbot import ui  # light: no framework import

            name = path.rsplit("/", 1)[-1]
            if name in ui.STATIC_FILES:
                await _respond(send, 200, (ui.STATIC_DIR / name).read_bytes(), "image/png")
                return
        if path == "/health":
            status = 500 if self.error else 200
            body = json.dumps({"ok": not self.error, "booting": not self.error,
                               **({"error": self.error} if self.error else {})}).encode()
            await _respond(send, status, body, "application/json")
            return
        if self.error:
            page = _PAGE.format(refresh="", title="Khởi động không thành công",
                                text=f"Chi tiết: {html.escape(self.error)}. Vui lòng xem nhật ký (log) trên Botkeep.")
            await _respond(send, 500, page.encode(), "text/html; charset=utf-8")
            return
        page = _PAGE.format(refresh=f'<meta http-equiv="refresh" content="{self.retry}">', title="Đang khởi động",
                            text="Hệ thống đang khởi động, trang sẽ tự tải lại sau giây lát.")
        await _respond(send, 503, page.encode(), "text/html; charset=utf-8",
                       [(b"retry-after", str(self.retry).encode())])


async def _respond(send, status: int, body: bytes, content_type: str, extra=()) -> None:
    headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
