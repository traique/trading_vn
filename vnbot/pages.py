"""The public pages a Zoom app needs, and the /zoom setup page, ported from the OKX bot (``bot/pages.py``).

    /zoom/oauth/callback   OAuth Redirect URL: Zoom comes back here after "Add App Now" / "Allow"
    /zoom/install          "Add to Zoom": redirects to Zoom's consent page
    /privacy               Privacy Policy URL
    /terms                 Terms of Use URL
    /support               Support URL
    /docs                  Documentation / Home URL
    /data-deletion         How to delete the data
    /zoom                  Setup (behind the web password): the URLs to paste in the Marketplace,
                           a check of the variables, the install and pairing state, the last requests

The public pages are bilingual Vietnamese / English (Zoom's reviewers read English)
and set no cookie. They wear the web app's look (``vnbot.ui``) with a public header.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from fastapi import Request

from vnbot import ui

logger = logging.getLogger(__name__)

APP_NAME = "TradingAgents VN"
UPDATED_VI, UPDATED_EN = "21/10/2026", "October 21, 2026"
SLASH = "vnbot"
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

PUBLIC_NAV = (("/docs", "Hướng dẫn"), ("/support", "Hỗ trợ"), ("/privacy", "Quyền riêng tư"),
              ("/terms", "Điều khoản"), ("/data-deletion", "Xoá dữ liệu"))

PUBLIC_CSS = """
.doc{max-width:860px;margin:0 auto}
.doc .card-b{padding:8px 30px 30px}
@media (max-width:620px){.doc .card-b{padding:6px 18px 22px}}
.doc h1{font-size:34px;margin:18px 0 4px}
.doc h2{font:600 19px/1.3 "Playfair Display",Georgia,serif;margin:26px 0 6px}
.doc p,.doc li{color:var(--ink-2)}
.doc code,.url{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--surface-2);
  border:1px solid var(--line);border-radius:7px;padding:2px 7px;word-break:break-all}
.lang{border-top:1px solid var(--line);margin-top:34px;padding-top:6px}
.box{background:var(--surface-2);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:14px 0;font-size:14px}
.kv{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:12px 0;border-top:1px solid var(--line)}
.kv:first-child{border-top:0}
.kv b{display:block;font-size:13.5px}.kv .url{display:inline-block;margin-top:5px}
.ok{color:var(--up);font-weight:700}.no{color:var(--down);font-weight:700}
.pubnav{display:flex;gap:2px;margin-left:auto;flex-wrap:wrap}
.pubnav a{padding:7px 11px;border-radius:10px;color:var(--ink-2);font-size:13px;font-weight:500}
.pubnav a:hover{text-decoration:none;background:var(--surface-2);color:var(--ink)}
.pubnav a[aria-current]{color:var(--ink);background:var(--surface-2)}
"""


def base_url(request: Request, public_url: str = "") -> str:
    if public_url:
        return public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _contact(email: str, en: bool) -> str:
    if email:
        e = html.escape(email)
        return f'email <a href="mailto:{e}">{e}</a>'
    return ("the contact details published on this app's Zoom App Marketplace listing" if en
            else "kênh liên hệ do chủ ứng dụng công bố trên trang Zoom App Marketplace của ứng dụng")


def public_page(title: str, body: str, current: str = "") -> str:
    nav = "".join(f'<a href="{h}"{" aria-current=page" if h == current else ""}>{t}</a>' for h, t in PUBLIC_NAV)
    return (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"<title>{html.escape(title)} · {APP_NAME}</title>{ui.THEME_BOOT}{ui.FONTS}"
        f"<style>{ui.STYLE}{PUBLIC_CSS}</style></head><body>"
        '<header class="top"><div class="top-in">'
        f'<a class="brand" href="/docs"><span class="mark">T</span><span class="brand-name">{APP_NAME}'
        "<small>Zoom Team Chat</small></span></a>"
        f'<nav class="pubnav">{nav}</nav>'
        f'<button class="icon-btn" id="theme-toggle" type="button" aria-label="Đổi giao diện sáng/tối">{ui.MOON}{ui.SUN}</button>'
        f'</div></header><main><div class="doc"><section class="card"><div class="card-b">{body}</div></section></div></main>'
        f"<footer><span>{APP_NAME} · Trang này không đặt cookie theo dõi và không thu thập dữ liệu người xem.</span>"
        "<span>This page sets no tracking cookie.</span></footer>"
        f"{ui.THEME_TOGGLE}</body></html>"
    )


# ─── Page bodies ─────────────────────────────────────────────────────────────


def privacy_body(email: str) -> str:
    return f"""
<h1>Chính sách quyền riêng tư</h1><p class="muted">Cập nhật lần cuối: {UPDATED_VI}</p>
<p>{APP_NAME} là công cụ cá nhân do chủ ứng dụng tự vận hành để phân tích cổ phiếu Việt Nam và theo dõi một bot giao dịch
trên tài khoản ảo. Ứng dụng Zoom (Team Chat chatbot) chỉ dùng để chủ ứng dụng nhận thông báo và gửi lệnh cho bot.</p>
<h2>1. Dữ liệu được xử lý</h2>
<ul><li>Định danh Zoom của người nhắn cho bot: JID người dùng, tên hiển thị, Account ID, JID kênh (nếu nhắn trong kênh).</li>
<li>Nội dung các tin nhắn/lệnh gửi <b>trực tiếp cho bot</b> (ví dụ <code>nhanh FPT</code>). Bot không đọc cuộc họp, ghi âm,
danh bạ hay tin nhắn không gửi cho bot.</li>
<li>Thông tin kỹ thuật: thời điểm nhận sự kiện, mã sự kiện để chống xử lý trùng.</li></ul>
<h2>2. Mục đích sử dụng</h2>
<p>Xác thực người được phép dùng bot (ghép đôi đúng một JID), thực thi lệnh và gửi lại kết quả, gửi thông báo tới người đã ghép đôi.</p>
<h2>3. Chia sẻ</h2>
<p>Không bán, không chia sẻ dữ liệu cho bên thứ ba để quảng cáo. Dữ liệu chỉ được gửi tới Zoom (để trả lời qua Zoom API) và
nhà cung cấp hạ tầng chạy ứng dụng.</p>
<h2>4. Lưu trữ và bảo mật</h2>
<p>Lưu: thông tin ghép đôi (JID, tên, Account ID) cho tới khi gỡ ghép đôi hoặc gỡ ứng dụng; 20 request Zoom gần nhất
(có thể chứa nội dung lệnh) để chẩn đoán. Mọi yêu cầu từ Zoom được xác thực chữ ký HMAC; mã truy cập Zoom chỉ giữ trong
bộ nhớ, không ghi ra đĩa.</p>
<h2>5. Xoá dữ liệu</h2>
<p>Gỡ ứng dụng khỏi Zoom sẽ tự động xoá thông tin ghép đôi. Xem chi tiết tại <a href="/data-deletion">/data-deletion</a>.</p>
<h2>6. Liên hệ</h2><p>Mọi câu hỏi về quyền riêng tư: {_contact(email, False)}.</p>
<div class="lang">
<h1>Privacy Policy</h1><p class="muted">Last updated: {UPDATED_EN}</p>
<p>{APP_NAME} is a personal tool operated by its owner to analyse Vietnamese stocks and follow a paper-trading bot.
The Zoom app (Team Chat chatbot) is used only so the owner can receive notifications and send commands to the bot.</p>
<h2>Data processed</h2>
<ul><li>Zoom identifiers of whoever messages the bot: user JID, display name, account ID, channel JID (when used in a channel).</li>
<li>The text of messages/commands sent <b>directly to the bot</b>. The app does not access meetings, recordings, contacts or
any message not addressed to the bot.</li>
<li>Technical metadata: event time and event ID (for de-duplication).</li></ul>
<h2>Purpose</h2><p>To authorize the single paired user, execute their commands, reply with results and deliver notifications to that user.</p>
<h2>Sharing</h2><p>No data is sold or shared for advertising. Data is sent only to Zoom (to reply via the Zoom API) and to the
hosting provider that runs the app.</p>
<h2>Retention and security</h2><p>Pairing data (JID, name, account ID) is kept until unpaired or the app is uninstalled; the last
20 Zoom requests (which may contain command text) are kept for diagnosis. All Zoom requests are verified with HMAC signatures;
Zoom access tokens are kept in memory only.</p>
<h2>Deletion</h2><p>Uninstalling the app from Zoom automatically deletes the pairing data. See <a href="/data-deletion">/data-deletion</a>.</p>
<h2>Contact</h2><p>{_contact(email, True)}.</p></div>"""


def terms_body() -> str:
    return f"""
<h1>Điều khoản sử dụng</h1><p class="muted">Cập nhật lần cuối: {UPDATED_VI}</p>
<h2>1. Phạm vi</h2><p>{APP_NAME} là công cụ cá nhân để chủ ứng dụng phân tích cổ phiếu và theo dõi bot giao dịch của chính mình
qua web, Telegram và Zoom Team Chat. Chỉ người đã được chủ ứng dụng ghép đôi mới dùng được bot. Bot không gửi lệnh tới công ty chứng khoán.</p>
<h2>2. Không phải tư vấn tài chính</h2><p>Mọi tín hiệu, phân tích AI và thông báo chỉ mang tính tham khảo, không phải khuyến nghị đầu tư.
Kết quả quá khứ không bảo đảm tương lai; người dùng tự chịu trách nhiệm về quyết định của mình.</p>
<h2>3. Giới hạn dịch vụ</h2><p>Dịch vụ phụ thuộc vào nguồn dữ liệu thị trường, Zoom, Telegram, nhà cung cấp AI và hạ tầng bên thứ ba;
không bảo đảm hoạt động liên tục hoặc không có lỗi.</p>
<h2>4. Bảo mật</h2><p>Không chia sẻ mật khẩu trang web hoặc thông tin xác thực Zoom. Chủ ứng dụng có thể gỡ ghép đôi hoặc thu hồi quyền bất cứ lúc nào.</p>
<h2>5. Quyền riêng tư</h2><p>Xem <a href="/privacy">Chính sách quyền riêng tư</a> và <a href="/data-deletion">Hướng dẫn xoá dữ liệu</a>.</p>
<div class="lang"><h1>Terms of Use</h1><p class="muted">Last updated: {UPDATED_EN}</p>
<h2>Scope</h2><p>{APP_NAME} is a personal tool that lets its owner analyse stocks and follow their own trading bot via web, Telegram and
Zoom Team Chat. Only the user paired by the owner can use the bot. The bot never sends orders to a broker.</p>
<h2>No financial advice</h2><p>All signals, AI analysis and notifications are for reference only, not investment advice. Past results do
not guarantee future ones; users are solely responsible for their decisions.</p>
<h2>Service limitations</h2><p>The service depends on market data sources, Zoom, Telegram, AI providers and third-party hosting;
continuous or error-free operation is not guaranteed.</p>
<h2>Security</h2><p>Do not share the web password or Zoom credentials. The owner may unpair or revoke access at any time.</p>
<h2>Privacy</h2><p>See the <a href="/privacy">Privacy Policy</a> and <a href="/data-deletion">Data Deletion</a> instructions.</p></div>"""


def deletion_body(email: str) -> str:
    return f"""
<h1>Hướng dẫn xoá dữ liệu</h1><p class="muted">Cập nhật lần cuối: {UPDATED_VI}</p>
<h2>Cách 1: Gỡ ứng dụng (tự động)</h2><p>Trong Zoom: <b>Zoom App Marketplace → Manage → Added Apps → {APP_NAME} → Remove</b>.
Zoom gửi sự kiện huỷ cấp quyền, ứng dụng xoá ngay thông tin ghép đôi (JID, tên, Account ID).</p>
<h2>Cách 2: Gỡ ghép đôi</h2><p>Chủ ứng dụng gõ <code>/zoomxoa</code> trên Telegram để xoá thông tin ghép đôi Zoom.</p>
<h2>Cách 3: Yêu cầu qua liên hệ</h2><p>Liên hệ {_contact(email, False)}, ghi rõ JID hoặc email Zoom. Không gửi mật khẩu hay mã OTP.
Yêu cầu hợp lệ được xử lý trong tối đa 7 ngày.</p>
<h2>Dữ liệu còn lại</h2><p>Nhật ký 20 request Zoom gần nhất được thay dần bằng request mới. Tin nhắn đã gửi trong Zoom nằm trên hệ thống
của Zoom và cần xoá trực tiếp trong Zoom.</p>
<div class="lang"><h1>Data Deletion</h1><p class="muted">Last updated: {UPDATED_EN}</p>
<h2>Option 1: Uninstall (automatic)</h2><p>In Zoom: <b>Zoom App Marketplace → Manage → Added Apps → {APP_NAME} → Remove</b>.
Zoom sends a deauthorization event and the app immediately deletes the pairing data (JID, name, account ID).</p>
<h2>Option 2: Unpair</h2><p>The owner sends <code>/zoomxoa</code> on Telegram to delete the Zoom pairing.</p>
<h2>Option 3: Request</h2><p>Contact {_contact(email, True)} with your Zoom JID or email. Never send passwords or OTP codes.
Valid requests are handled within 7 days.</p>
<h2>Remaining data</h2><p>The log of the last 20 Zoom requests is overwritten by newer ones. Messages already sent in Zoom are stored by
Zoom and must be deleted in Zoom.</p></div>"""


COMMANDS = (
    ("nhanh FPT", "Quyết định của Quant Engine, không gọi AI", "Quant engine decision, no AI"),
    ("phanbien FPT", "AI phản biện quyết định (1 lượt gọi AI)", "AI critique of the decision (one AI request)"),
    ("phanbien", "AI phản đối đúng bao nhiêu (kết quả bóng)", "How often the AI's objections were right"),
    ("phantich FPT", "Phân tích đầy đủ bằng các agent (~25 lượt AI)", "Full multi-agent analysis (~25 AI requests)"),
    ("theodoi FPT,HPG", "Đặt / xem watchlist", "Set / show the watchlist"),
    ("danhmuc", "Danh mục các tài khoản", "Portfolios"),
    ("lenh", "Lệnh cho phiên tới", "Orders for the next session"),
    ("hieuchinh", "Ngưỡng và tự hiệu chỉnh", "Thresholds and self-calibration"),
    ("trangthai", "Hàng đợi, nhà cung cấp AI, hạn mức", "Queue, AI providers, budget"),
    ("trogiup", "Danh sách lệnh", "Command list"),
)


def docs_body() -> str:
    rows = "".join(f"<tr><td><code>{c}</code></td><td>{vi}</td><td class='muted'>{en}</td></tr>" for c, vi, en in COMMANDS)
    return f"""
<h1>Hướng dẫn sử dụng</h1>
<p>{APP_NAME} gửi báo cáo, ý kiến AI phản biện và cảnh báo trong phiên, và nhận lệnh qua Zoom Team Chat.</p>
<h2>1. Cài ứng dụng</h2><p>Bấm <a class="btn btn-primary" href="/zoom/install" style="padding:7px 12px;font-size:13px">Thêm vào Zoom</a>
và cho phép quyền. Sau đó mở Zoom → Team Chat → mục <b>Apps</b> → chọn <b>{APP_NAME}</b>.</p>
<h2>2. Ghép đôi</h2><p>Nhắn bất kỳ lệnh nào cho bot (ví dụ <code>trangthai</code>). Lần đầu bot báo cho chủ ứng dụng qua Telegram kèm JID của
bạn; chủ ứng dụng gõ <code>/zoompair &lt;JID&gt;</code> hoặc bấm “Ghép đôi” trên trang /zoom. Chỉ một tài khoản Zoom được dùng bot.</p>
<h2>3. Lệnh</h2><p>Gõ trực tiếp trong chat với bot, hoặc trong kênh: <code>/{SLASH} nhanh FPT</code>.</p>
<div class="table-wrap"><table>{rows}</table></div>
<div class="lang"><h1>Documentation</h1>
<p><b>Install:</b> click <a href="/zoom/install">Add to Zoom</a> and authorize, then open Zoom → Team Chat → Apps → {APP_NAME}.<br>
<b>Pairing:</b> send any command (e.g. <code>trangthai</code>); the owner receives your JID on Telegram and pairs it with
<code>/zoompair &lt;JID&gt;</code>. Only one Zoom user can use the bot.<br>
<b>Commands:</b> type them directly in the bot chat, or <code>/{SLASH} &lt;command&gt;</code> in a channel (see the table above).<br>
<b>Uninstall:</b> Zoom App Marketplace → Manage → Added Apps → Remove. Pairing data is deleted automatically.</p></div>"""


def support_body(email: str) -> str:
    return f"""
<h1>Hỗ trợ</h1>
<p>Liên hệ: {_contact(email, False)}. Thời gian phản hồi: trong 2 ngày làm việc.</p>
<h2>Câu hỏi thường gặp</h2>
<p><b>Bot không trả lời trên Zoom?</b> Tài khoản Zoom của bạn chưa được ghép đôi: chủ ứng dụng cần gõ <code>/zoompair &lt;JID&gt;</code>
trên Telegram.<br>
<b>Muốn ngừng nhận thông báo?</b> Gỡ ứng dụng trong Zoom App Marketplace hoặc nhờ chủ ứng dụng gõ <code>/zoomxoa</code>.<br>
<b>Xoá dữ liệu?</b> Xem <a href="/data-deletion">/data-deletion</a>.</p>
<p>Danh sách lệnh: <a href="/docs">/docs</a>.</p>
<div class="lang"><h1>Support</h1>
<p>Contact: {_contact(email, True)}. Response time: within 2 business days.</p>
<p><b>The bot does not reply?</b> Your Zoom account is not paired yet: the owner must run <code>/zoompair &lt;JID&gt;</code> on Telegram.<br>
<b>Stop notifications?</b> Remove the app in Zoom App Marketplace, or ask the owner to run <code>/zoomxoa</code>.<br>
<b>Delete data?</b> See <a href="/data-deletion">/data-deletion</a>. Commands: <a href="/docs">/docs</a>.</p></div>"""


# ─── OAuth ───────────────────────────────────────────────────────────────────


def authorize_url(client_id: str, base: str, state: str) -> str:
    return "https://zoom.us/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": base + "/zoom/oauth/callback", "state": state})


def exchange_code(zoom, code: str, redirect_uri: str, post=None) -> tuple[int, dict]:
    r = (post or requests.post)("https://zoom.us/oauth/token", timeout=15,
                                data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                                auth=(zoom.config.client_id, zoom.config.client_secret))
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:200]}
    return r.status_code, data


def _installed_page() -> str:
    return public_page("Đã cài thành công", f"""<h1>✅ Đã cài {APP_NAME} vào Zoom</h1>
<p>Mở Zoom → <b>Team Chat</b> → mục <b>Apps</b> → chọn <b>{APP_NAME}</b>, nhắn <code>trangthai</code>.</p>
<p>Lần đầu bot sẽ báo JID của bạn cho chủ ứng dụng qua Telegram; chủ ứng dụng gõ <code>/zoompair &lt;JID&gt;</code> để cấp quyền.</p>
<p><a class="btn btn-primary" href="zoommtg://zoom.us/">Mở Zoom</a> &nbsp; <a class="btn btn-ghost" href="/docs">Xem hướng dẫn</a></p>
<div class="lang"><p class="muted">The app has been installed. Open Zoom → Team Chat → Apps → {APP_NAME} and send <code>trangthai</code>.</p></div>""")


def _when(ts) -> str:
    return datetime.fromtimestamp(float(ts), VN_TZ).strftime("%d/%m %H:%M") if ts else ""


def register(app, settings, zoom, *, guard, post_guard, notify=None, post=None) -> None:
    """Add the public pages, the OAuth routes and the /zoom setup page to the web app.
    ``guard``/``post_guard``: the web password (and same-origin check) dependencies."""
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    email = settings.support_email

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return public_page("Chính sách quyền riêng tư / Privacy Policy", privacy_body(email), "/privacy")

    @app.get("/terms", response_class=HTMLResponse)
    def terms():
        return public_page("Điều khoản sử dụng / Terms of Use", terms_body(), "/terms")

    @app.get("/data-deletion", response_class=HTMLResponse)
    def deletion():
        return public_page("Xoá dữ liệu / Data Deletion", deletion_body(email), "/data-deletion")

    @app.get("/docs", response_class=HTMLResponse)
    def docs():
        return public_page("Hướng dẫn / Documentation", docs_body(), "/docs")

    @app.get("/support", response_class=HTMLResponse)
    def support():
        return public_page("Hỗ trợ / Support", support_body(email), "/support")

    @app.get("/zoom/install")
    def install(request: Request):
        if zoom is None or not zoom.config.client_id:
            return HTMLResponse(public_page("Chưa cấu hình", "<h1>Chưa cấu hình Zoom</h1><p>Thiếu <code>ZOOM_CLIENT_ID</code>.</p>"))
        state = secrets.token_urlsafe(16)
        states = {k: v for k, v in (zoom.state.get("oauth_states") or {}).items() if time.time() - v < 900}
        states[state] = time.time()
        zoom.state.set("oauth_states", states)
        return RedirectResponse(authorize_url(zoom.config.client_id, base_url(request, settings.public_url), state),
                                status_code=302)

    @app.get("/zoom/oauth/callback", response_class=HTMLResponse)
    def callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        if error:
            logger.warning("Zoom OAuth refused: %s %s", error, error_description)
            return public_page("Chưa cài được", f"""<h1>Chưa cài được ứng dụng</h1>
<p>Zoom trả về lỗi: <code>{html.escape(error)}</code> {html.escape(error_description)}</p>
<p><a class="btn btn-ghost" href="/zoom/install">Thử lại</a></p>""")
        if not code or zoom is None:
            return public_page("Zoom OAuth callback", f"""<h1>Zoom OAuth Redirect URL</h1>
<p>Đây là địa chỉ Zoom chuyển về sau khi cài ứng dụng. Hãy cài từ Zoom Marketplace hoặc bấm nút dưới đây.</p>
<p class="muted">This is the OAuth redirect endpoint of {APP_NAME}.</p>
<p><a class="btn btn-primary" href="/zoom/install">Thêm vào Zoom / Add to Zoom</a></p>""")
        states = zoom.state.get("oauth_states") or {}
        if state in states:
            states.pop(state, None)
            zoom.state.set("oauth_states", states)
        elif state:
            logger.warning("Zoom OAuth: unknown state (an old link?), exchanging the code anyway")
        redirect_uri = base_url(request, settings.public_url) + "/zoom/oauth/callback"
        inst = zoom.state.get("install") or {}
        if inst.get("code") == code[-12:]:
            return _installed_page()          # the same callback twice (a code works once): already installed
        status, data = 0, {"error": "ZOOM_CLIENT_ID/ZOOM_CLIENT_SECRET chưa cấu hình"}
        if zoom.config.client_id and zoom.config.client_secret:
            try:
                status, data = exchange_code(zoom, code, redirect_uri, post)
            except requests.RequestException as exc:
                status, data = 0, {"error": str(exc)}
        if status == 200 and "access_token" in data:
            # No token is kept: the chatbot uses its own client_credentials token.
            zoom.state.set("install", {"ts": time.time(), "scope": data.get("scope", ""), "code": code[-12:]})
            logger.info("Zoom: app installed (scope %s)", data.get("scope", ""))
            if notify:
                notify(f"✅ Zoom: ứng dụng đã được cài. Mở Team Chat → Apps → {APP_NAME} và nhắn 'trangthai' để ghép đôi.")
            return _installed_page()
        if data.get("error") == "invalid_grant" and time.time() - float(inst.get("ts") or 0) < 600:
            return _installed_page()          # the code was spent by the previous, successful call
        logger.error("Zoom OAuth code exchange failed (%s): %s", status, data)
        reason = html.escape(str(data.get("reason") or data.get("error_description") or data.get("error") or data))
        return public_page("Lỗi cài đặt", f"""<h1>Chưa hoàn tất cài đặt</h1>
<p>Zoom đã chuyển về nhưng đổi mã xác thực thất bại (HTTP {status}): <code>{reason}</code></p>
<div class="box">Kiểm tra: <b>OAuth Redirect URL</b> trên Marketplace phải đúng <code>{html.escape(redirect_uri)}</code> và nằm trong
<b>OAuth Allow List</b>; <code>ZOOM_CLIENT_ID</code>/<code>ZOOM_CLIENT_SECRET</code> phải là cặp <b>Development</b> nếu đang Local Test
(Production nếu đã publish). Biến <code>PUBLIC_URL</code> nên là <code>{html.escape(base_url(request, settings.public_url))}</code>.</div>
<p><a class="btn btn-ghost" href="/zoom/install">Thử lại</a></p>""")

    @app.post("/zoom/pair", dependencies=post_guard)
    def zoom_pair():
        if zoom is not None:
            p = zoom.pair_pending()
            if p:
                logger.info("Zoom: paired %s from the /zoom page", p["jid"])
        return RedirectResponse("/zoom", status_code=303)

    @app.post("/zoom/unpair", dependencies=post_guard)
    def zoom_unpair():
        if zoom is not None:
            zoom.unpair()
        return RedirectResponse("/zoom", status_code=303)

    @app.get("/zoom", response_class=HTMLResponse, dependencies=[Depends(guard)])
    def zoom_setup(request: Request):
        b = base_url(request, settings.public_url)
        c = zoom.config if zoom is not None else None
        p = zoom.pairing() if zoom else {}
        inst = (zoom.state.get("install") or {}) if zoom else {}
        pend = zoom.pending() if zoom else {}

        def chk(v) -> str:
            return '<span class="ok">✓</span>' if v else '<span class="no">✗</span>'

        def copy_btn(u: str) -> str:
            return (f'<button class="btn btn-ghost" type="button" style="padding:7px 12px;font-size:12px" '
                    f'data-copy="{html.escape(u)}">Chép</button>')

        urls = [("OAuth Redirect URL", b + "/zoom/oauth/callback"), ("OAuth Allow List", b + "/zoom/oauth/callback"),
                ("Bot endpoint URL (Development & Production)", b + "/webhook/zoom"),
                ("Event notification endpoint URL", b + "/webhook/zoom"),
                ("Deauthorization Notification Endpoint", b + "/webhook/zoom"),
                ("Home URL / Direct landing URL", b + "/docs"), ("Privacy Policy URL", b + "/privacy"),
                ("Terms of Use URL", b + "/terms"), ("Support URL", b + "/support"), ("Documentation URL", b + "/docs")]
        url_rows = "".join(f'<div class="kv"><div><b>{n}</b><span class="url">{html.escape(u)}</span></div>{copy_btn(u)}</div>'
                           for n, u in urls)
        checks = [("PUBLIC_URL", bool(settings.public_url)), ("ZOOM_CLIENT_ID", bool(c and c.client_id)),
                  ("ZOOM_CLIENT_SECRET", bool(c and c.client_secret)), ("ZOOM_BOT_JID", bool(c and c.bot_jid)),
                  ("ZOOM_SECRET_TOKEN", bool(c and c.secret_token)), ("ZOOM_ACCOUNT_ID", bool(c and c.account_id)),
                  ("SUPPORT_EMAIL", bool(settings.support_email)),
                  ("ZOOM_WEBHOOK_URL + ZOOM_WEBHOOK_TOKEN (tuỳ chọn)", bool(c and c.webhook_url and c.webhook_token))]
        check_rows = "".join(f'<div class="kv"><div><b>{k}</b></div><div>{chk(v)}</div></div>' for k, v in checks)
        state_rows = (
            f'<div class="kv"><div><b>Đã cài vào Zoom (OAuth)</b><span class="muted small">{_when(inst.get("ts"))}</span></div>'
            f"<div>{chk(inst)}</div></div>"
            f'<div class="kv"><div><b>JID đã ghép đôi</b><span class="muted small">'
            f'{html.escape(((p.get("name") or "") + " " + (p.get("jid") or "chưa có")).strip())}</span></div>'
            f"<div>{chk(p.get('jid'))}</div></div>"
            f'<div class="kv"><div><b>Gửi thông báo tới</b><span class="muted small">'
            f'{html.escape((c.to_jid + " (ZOOM_TO_JID)") if c and c.to_jid else ("JID đã ghép đôi" if p.get("jid") else "chưa có"))}'
            f'{" · Incoming Webhook" if zoom and zoom.webhook_on else ""}</span></div><div>{chk(zoom and zoom.notify_on)}</div></div>'
            f'<div class="kv"><div><b>Nhận lệnh từ Zoom</b><span class="muted small">/webhook/zoom</span></div>'
            f"<div>{chk(zoom and zoom.commands_on)}</div></div>")
        if p.get("jid"):
            state_rows += ('<form method="post" action="/zoom/unpair" style="margin-top:12px">'
                           '<button class="btn btn-ghost" type="submit">Gỡ ghép đôi</button></form>')
        pend_html = ""
        if pend.get("jid") and pend.get("jid") != p.get("jid"):
            pend_html = (
                f'<div class="box"><b>Có người vừa nhắn bot nhưng chưa được ghép đôi</b><br>'
                f'{html.escape(pend.get("name") or "")} · <span class="url">{html.escape(pend["jid"])}</span> · '
                f'{_when(pend.get("ts"))}'
                '<form method="post" action="/zoom/pair" style="margin-top:12px">'
                '<button class="btn btn-primary" type="submit">Ghép đôi tài khoản này</button></form></div>')
        reqs = zoom.requests_log() if zoom else []
        req_rows = "".join(
            f'<div class="kv"><div><b>{datetime.fromtimestamp(r["ts"], VN_TZ).strftime("%d/%m %H:%M:%S")} · '
            f'{html.escape(r["event"])}</b><span class="muted small">{html.escape(r.get("user") or "")} '
            f'{html.escape(r.get("cmd") or "")} — {html.escape(r["reason"])}</span></div><div>{chk(r["ok"])}</div></div>'
            for r in reqs) or (
            '<p class="box">Chưa nhận được request nào từ Zoom, nghĩa là Zoom chưa gọi tới bot. Kiểm tra <b>Bot endpoint URL</b>'
            " trong Features → Surface → Team Chat Subscription của đúng app này (bản Development khi Local Test), và bạn đang"
            " nhắn đúng bot của app này chứ không phải bot OKX.</p>")
        steps = f"""<ol>
<li>Basic Information → dán <b>OAuth Redirect URL</b> và <b>OAuth Allow List</b>.</li>
<li>Features → Surface → bật <b>Team Chat</b> → Team Chat Subscription: dán <b>Bot endpoint URL</b>, slash command
<code>{SLASH}</code>. Chép <b>Bot JID</b> (Development) vào <code>ZOOM_BOT_JID</code>.</li>
<li>Features → Access → <b>Secret Token</b> → <code>ZOOM_SECRET_TOKEN</code>. Bật Event Subscription với URL ở trên, thêm sự kiện
<i>App Deauthorized</i>, bấm <b>Validate</b>.</li>
<li>Basic Information → App Credentials (Development) → <code>ZOOM_CLIENT_ID</code>, <code>ZOOM_CLIENT_SECRET</code>. Account ID lấy ở
zoom.us/account → <code>ZOOM_ACCOUNT_ID</code>.</li>
<li>Scopes: <code>imchat:bot</code> (Zoom tự thêm khi bật chatbot).</li>
<li>Restart bot, rồi Local Test → <b>Add App Now</b> (hoặc nút dưới) → Allow → về trang callback báo thành công.</li>
<li>Zoom → Team Chat → Apps → nhắn <code>trangthai</code> → Telegram báo JID → gõ <code>/zoompair &lt;JID&gt;</code> hoặc bấm
“Ghép đôi” ở trên.</li></ol>
<div class="box"><b>Chỉ muốn nhận thông báo bằng app chatbot của bot OKX?</b> Điền cùng <code>ZOOM_CLIENT_ID</code>,
<code>ZOOM_CLIENT_SECRET</code>, <code>ZOOM_BOT_JID</code>, <code>ZOOM_ACCOUNT_ID</code> như bot OKX và đặt <code>ZOOM_TO_JID</code> =
JID bot OKX đang ghép đôi (<code>/zoomstatus</code> trên Telegram của bot OKX). Không cần các bước trên: lệnh gõ vào app đó vẫn tới bot OKX.</div>"""
        script = """<script>document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
try{navigator.clipboard.writeText(b.getAttribute('data-copy'));b.textContent='Đã chép';}catch(e){}});});</script>"""
        body = (
            ui.hero("Cấu hình Zoom",
                    'Dán các URL dưới đây vào app trên <a href="https://marketplace.zoom.us/user/build" target="_blank" '
                    'rel="noopener">Zoom Marketplace</a>, rồi theo các bước bên dưới. <a href="/zoom">Tải lại</a>',
                    '<a class="btn btn-primary" href="/zoom/install">Thêm vào Zoom (Local Test)</a>', eyebrow="Zoom Team Chat")
            + pend_html
            + '<div class="grid g2">'
            + ui.card("Trạng thái", state_rows)
            + ui.card("Biến môi trường", check_rows, sub="✓ là đã đặt; giá trị không hiện ra")
            + "</div>"
            + '<div class="grid g2 mt">'
            + ui.card("URL cần dán vào Marketplace", url_rows)
            + '<div class="stack">' + ui.card("Request Zoom gần nhất", req_rows, sub="20 request gần nhất, mới nhất ở trên")
            + ui.card("Các bước Local Test", steps) + "</div>"
            + "</div>" + script
        )
        return HTMLResponse(ui.page("Cấu hình Zoom", f"<style>{PUBLIC_CSS}</style>" + body, active="ai"))
