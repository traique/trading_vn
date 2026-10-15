"""The look of the web app: one stylesheet, a page shell and small components.

Server-rendered, no build step. The palette is "trà đào cam sả": peach tea,
orange and lemongrass on warm cream (light) or roasted brown (dark). The
theme follows the device and can be switched with the button in the header;
the choice is remembered in the browser (localStorage, best effort).

Chart series colours were checked with the dataviz palette validator
(lightness band, chroma, colour-blind separation, contrast) for each theme:
light #E07A45 / #3F6E2A on #FFFFFF, dark #D57446 / #4F721C on #1F1A16. Every
line also carries a direct label and the numbers are in the KPI tiles, so
identity never rests on colour alone.
"""

from __future__ import annotations

import html
import json

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700'
    '&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">'
)

THEME_BOOT = (
    "<script>(function(){try{var t=localStorage.getItem('ta-theme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
)

THEME_TOGGLE = """<script>
(function(){
  var b=document.getElementById('theme-toggle');if(!b)return;
  function cur(){var t=document.documentElement.getAttribute('data-theme');
    if(t)return t;return window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
  function label(){b.setAttribute('aria-label',cur()==='dark'?'Chuyển sang giao diện sáng':'Chuyển sang giao diện tối');}
  label();
  b.addEventListener('click',function(){var n=cur()==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',n);try{localStorage.setItem('ta-theme',n);}catch(e){}label();});
})();
</script>"""

_LIGHT = """
  --bg:#FAF5EF; --bg-glow:#FCE9DC; --surface:#FFFFFF; --surface-2:#F6EEE5; --surface-3:#EFE3D6;
  --ink:#2A211B; --ink-2:#5B4D44; --muted:#8B7B6F; --line:#EADDD0; --line-strong:#DCCAB8;
  --brand:#C4581F; --brand-hover:#A9481A; --brand-ink:#FFFFFF; --brand-soft:#FCE6D8; --brand-text:#A44A1A;
  --peach:#F3B38F; --sa:#5E7A1F; --sa-soft:#EDF2DA; --sa-text:#4A6118;
  --up:#2E7D3A; --up-soft:#E3F1E2; --down:#B83A2E; --down-soft:#FBE4E1; --warn:#A86C12; --warn-soft:#FBEFD9;
  --neutral-soft:#EFE8E1;
  --c-engine:#E07A45; --c-mom:#3F6E2A; --c-bench:#9A8B80;
  --shadow:0 1px 2px rgba(60,40,25,.06),0 8px 24px rgba(60,40,25,.06);
"""

_DARK = """
  --bg:#14100D; --bg-glow:#2A1A12; --surface:#1F1A16; --surface-2:#28211C; --surface-3:#322923;
  --ink:#F5ECE3; --ink-2:#D3C4B7; --muted:#A08F82; --line:#3A3029; --line-strong:#4A3D34;
  --brand:#EE9363; --brand-hover:#F4A87F; --brand-ink:#1C130E; --brand-soft:#3B2519; --brand-text:#F2A47C;
  --peach:#F0A57D; --sa:#B6C76C; --sa-soft:#2A311A; --sa-text:#C3D27E;
  --up:#86C98A; --up-soft:#1F3321; --down:#F08F84; --down-soft:#3C1F1C; --warn:#E6B45E; --warn-soft:#3A2D16;
  --neutral-soft:#2E2621;
  --c-engine:#D57446; --c-mom:#4F721C; --c-bench:#8E7F74;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.25);
"""

STYLE = (
    ":root{" + _LIGHT + "color-scheme:light}"
    "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + _DARK + "color-scheme:dark}}"
    ":root[data-theme=dark]{" + _DARK + "color-scheme:dark}"
    + """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);background-image:radial-gradient(1200px 500px at 85% -10%,var(--bg-glow),transparent 60%);
  background-attachment:fixed;color:var(--ink);
  font:15px/1.6 "Be Vietnam Pro",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--brand-text);text-decoration:none}a:hover{text-decoration:underline}
.num,td.num,.kpi .v{font-variant-numeric:tabular-nums}
/* header */
.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 82%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
.top-in{max-width:1040px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:700;letter-spacing:-.01em}
.brand:hover{text-decoration:none}
.mark{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;color:#fff;font:700 13px/1 "Be Vietnam Pro",sans-serif;
  background:linear-gradient(135deg,#F3B38F 0%,#E07A45 55%,#7E9A32 100%);box-shadow:0 4px 12px rgba(224,122,69,.35)}
.brand small{display:block;font-weight:500;font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin-top:-2px}
.nav{display:flex;gap:4px;margin-left:auto;background:var(--surface-2);border:1px solid var(--line);padding:3px;border-radius:999px}
.nav a{padding:6px 14px;border-radius:999px;color:var(--ink-2);font-weight:500;font-size:14px}
.nav a:hover{text-decoration:none;color:var(--ink)}
.nav a.on{background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}
.icon-btn{width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
  display:grid;place-items:center;cursor:pointer;flex:none}
.icon-btn:hover{color:var(--ink);border-color:var(--line-strong)}
.icon-btn svg{width:18px;height:18px}
.i-sun{display:none}:root[data-theme=dark] .i-sun{display:block}:root[data-theme=dark] .i-moon{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .i-sun{display:block}:root:not([data-theme=light]) .i-moon{display:none}}
/* layout */
main{max-width:1040px;margin:0 auto;padding:22px 16px 48px}
.hero{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin:6px 0 18px}
h1{font:700 30px/1.15 "Playfair Display",Georgia,serif;letter-spacing:-.01em;margin:0}
h1 .tick{font-family:"Be Vietnam Pro",sans-serif;font-weight:700}
.lead{color:var(--ink-2);margin:6px 0 0;max-width:640px}
h2{font-size:16px;font-weight:600;margin:0;letter-spacing:-.005em}
.grid{display:grid;gap:16px;align-items:start}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
@media (max-width:820px){.g3,.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:620px){.g2{grid-template-columns:1fr}h1{font-size:25px}.nav a{padding:6px 10px;font-size:13px}
  .brand small{display:none}.brand-name{display:none}.top-in{gap:10px}}
.nav a{white-space:nowrap}
.stack>*+*{margin-top:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line)}
.card-h .sub{color:var(--muted);font-size:13px}
.card-b{padding:16px 18px}
.card-b.flush{padding:0}
.accent-peach{border-top:3px solid var(--c-engine)}.accent-sa{border-top:3px solid var(--sa)}
.muted{color:var(--muted)}.small{font-size:13px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:1px}
/* forms */
label.f{display:block;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.row>.fld{flex:1 1 140px}
input{width:100%;font:inherit;padding:11px 13px;border-radius:12px;border:1px solid var(--line-strong);background:var(--surface);
  color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-soft)}
input.ticker{text-transform:uppercase;font-weight:600;letter-spacing:.04em}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 14px/1 "Be Vietnam Pro",sans-serif;
  padding:13px 18px;border-radius:12px;border:1px solid transparent;cursor:pointer;white-space:nowrap;transition:background .15s,transform .05s}
.btn:active{transform:translateY(1px)}
.btn-primary{background:var(--brand);color:var(--brand-ink)}.btn-primary:hover{background:var(--brand-hover);text-decoration:none}
.btn-ghost{background:transparent;color:var(--brand-text);border-color:var(--line-strong)}.btn-ghost:hover{background:var(--brand-soft);text-decoration:none}
.btn-sa{background:var(--sa-soft);color:var(--sa-text);border-color:transparent}.btn-sa:hover{filter:brightness(.97);text-decoration:none}
.btn[disabled]{opacity:.55;cursor:not-allowed}
/* badges and pills */
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font:600 12px/1.6 "Be Vietnam Pro",sans-serif;
  letter-spacing:.02em;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.b-buy{background:var(--up-soft);color:var(--up)}.b-sell{background:var(--down-soft);color:var(--down)}
.b-hold{background:var(--brand-soft);color:var(--brand-text)}.b-watch{background:var(--warn-soft);color:var(--warn)}
.b-none{background:var(--neutral-soft);color:var(--muted)}
.b-ok{background:var(--sa-soft);color:var(--sa-text)}.b-err{background:var(--down-soft);color:var(--down)}
.b-run{background:var(--brand-soft);color:var(--brand-text)}.b-run::before{animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{padding:4px 10px;border-radius:999px;background:var(--surface-2);border:1px solid var(--line);font-weight:600;font-size:13px}
/* alerts */
.alert{padding:12px 16px;border-radius:14px;border:1px solid;margin-bottom:16px}
.alert-err{background:var(--down-soft);border-color:transparent;color:var(--down)}
.alert-ok{background:var(--sa-soft);border-color:transparent;color:var(--sa-text)}
.alert-info{background:var(--brand-soft);border-color:transparent;color:var(--brand-text)}
/* KPI */
.kpi{background:var(--surface-2);border-radius:14px;padding:12px 14px}
.kpi .l{font-size:12px;color:var(--muted);font-weight:500}
.kpi .v{font-size:20px;font-weight:700;letter-spacing:-.01em;margin-top:2px;white-space:nowrap}
.kpi .s{font-size:12px;color:var(--muted)}
.pos{color:var(--up)}.neg{color:var(--down)}
/* tables */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;text-align:left;
  padding:10px 14px;background:var(--surface-2);white-space:nowrap}
td{padding:11px 14px;border-top:1px solid var(--line);vertical-align:middle}
tbody tr:hover td{background:color-mix(in srgb,var(--surface-2) 60%,transparent)}
td.num,th.num{text-align:right;white-space:nowrap}
td code{font-size:12.5px;background:var(--surface-2);padding:2px 6px;border-radius:6px}
td.empty{color:var(--muted);text-align:center;padding:22px}
.sym{font-weight:700;letter-spacing:.02em}
tr.changed td{background:var(--sa-soft)}
/* report text */
.report{white-space:pre-wrap;font:14px/1.65 "Be Vietnam Pro",sans-serif;margin:0;color:var(--ink)}
.report.mono{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
/* stepper */
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 0}
.step{display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:var(--surface-2);color:var(--muted);font-size:13px;font-weight:500}
.step.done{background:var(--sa-soft);color:var(--sa-text)}.step.now{background:var(--brand-soft);color:var(--brand-text)}
.step.fail{background:var(--down-soft);color:var(--down)}
/* chart */
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto}
.chart .tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line-strong);border-radius:12px;
  box-shadow:var(--shadow);padding:8px 10px;font-size:12px;min-width:150px;opacity:0;transition:opacity .1s}
.chart .tip b{display:block;margin-bottom:4px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--ink-2);margin-bottom:8px}
.legend .ln{display:inline-block;width:18px;height:0;border-top:2.5px solid;vertical-align:4px;margin-right:6px}
.legend .ln.dash{border-top-style:dashed}
footer{max-width:1040px;margin:0 auto;padding:0 16px 32px;color:var(--muted);font-size:12px}
"""
)

SUN = ('<svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
       '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
       'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>')
MOON = ('<svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>')

e = html.escape


def page(title: str, body: str, *, active: str = "", refresh: int | None = None, show_bot: bool = True) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nav = [("home", "/", "Phân tích")] + ([("bot", "/bot", "Bot giao dịch")] if show_bot else [])
    links = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for key, href, label in nav)
    return (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#FAF5EF" media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#14100D" media="(prefers-color-scheme: dark)">'
        f"{meta}<title>{e(title)} · TradingAgents VN</title>{THEME_BOOT}{FONTS}<style>{STYLE}</style></head><body>"
        '<header class="top"><div class="top-in">'
        '<a class="brand" href="/"><span class="mark">TA</span><span class="brand-name">TradingAgents VN<small>Cổ phiếu Việt Nam</small></span></a>'
        f'<nav class="nav">{links}</nav>'
        f'<button class="icon-btn" id="theme-toggle" type="button" aria-label="Đổi giao diện sáng/tối">{MOON}{SUN}</button>'
        f"</div></header><main>{body}</main>"
        "<footer>Thông tin chỉ mang tính tham khảo, không phải khuyến nghị đầu tư. Bot không gửi lệnh tới công ty chứng khoán.</footer>"
        f"{THEME_TOGGLE}</body></html>"
    )


def hero(title: str, lead: str = "", right: str = "") -> str:
    return (f'<div class="hero"><div><h1>{title}</h1>'
            + (f'<p class="lead">{lead}</p>' if lead else "") + f"</div>{right}</div>")


def card(title: str, body: str, *, sub: str = "", action: str = "", flush: bool = False, cls: str = "") -> str:
    head = (f'<div class="card-h"><div><h2>{title}</h2>' + (f'<div class="sub">{sub}</div>' if sub else "")
            + f"</div>{action}</div>") if title else ""
    return f'<section class="card {cls}">{head}<div class="card-b{" flush" if flush else ""}">{body}</div></section>'


def alert(text: str, kind: str = "info") -> str:
    return f'<div class="alert alert-{kind}" role="status">{e(text)}</div>' if text else ""


_ACTION = {
    "BUY": ("b-buy", "MUA"), "SELL": ("b-sell", "BÁN"), "HOLD": ("b-hold", "GIỮ"),
    "WATCH": ("b-watch", "THEO DÕI"), "NO_TRADE": ("b-none", "ĐỨNG NGOÀI"),
}


def action_badge(action: str) -> str:
    if not action:
        return '<span class="muted">—</span>'
    cls, label = _ACTION.get(action, ("b-none", action))
    return f'<span class="badge {cls}" title="{e(action)}">{e(label)}</span>'


_STATUS = {
    "queued": ("b-none", "Đang chờ"), "quick": ("b-run", "Tính quyết định"),
    "running": ("b-run", "Agent đang phân tích"), "done": ("b-ok", "Xong"), "failed": ("b-err", "Lỗi"),
}


def status_badge(status: str) -> str:
    cls, label = _STATUS.get(status, ("b-none", status))
    return f'<span class="badge {cls}">{e(label)}</span>'


def stepper(status: str) -> str:
    order = ["queued", "quick", "running", "done"]
    labels = {"queued": "Xếp hàng", "quick": "Quyết định hệ thống", "running": "Agent phân tích", "done": "Hoàn tất"}
    if status == "failed":
        return '<div class="steps"><span class="step fail">Lỗi - xem chi tiết bên dưới</span></div>'
    at = order.index(status) if status in order else 0
    items = []
    for i, key in enumerate(order):
        cls = "done" if i < at or status == "done" else ("now" if i == at else "")
        items.append(f'<span class="step {cls}">{labels[key]}</span>')
    return f'<div class="steps">{"".join(items)}</div>'


def money(value) -> str:
    return "—" if value is None else f"{value:,.0f}".replace(",", ".")


def money_short(value) -> str:
    """1.012.300.000 -> "1,012 tỷ"; 532.600.000 -> "532,6 tr" (KPI tiles)."""
    if value is None:
        return "—"
    if abs(value) >= 1e9:
        return f"{value / 1e9:,.3f} tỷ".replace(".", ",")
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.1f} tr".replace(",", "X").replace(".", ",").replace("X", ".")
    return money(value)


def pct(value, signed: bool = True) -> str:
    if value is None:
        return "—"
    text = f"{value:+.2f}%" if signed else f"{value:.2f}%"
    return text.replace(".", ",")


def tone(value) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="kpi"><div class="l">{label}</div><div class="v {cls}">{value}</div>'
            + (f'<div class="s">{sub}</div>' if sub else "") + "</div>")


def table(headers: list[tuple[str, str]], rows: list[str], empty: str) -> str:
    """``headers`` as (label, css class); ``rows`` already rendered <tr>...</tr>."""
    head = "".join(f'<th class="{c}">{h}</th>' for h, c in headers)
    body = "".join(rows) or f'<tr><td class="empty" colspan="{len(headers)}">{empty}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


# ─── NAV chart ────────────────────────────────────────────────────────────────

_CHART_JS = """<script>
(function(){
  document.querySelectorAll('.chart[data-series]').forEach(function(box){
    var d=JSON.parse(box.getAttribute('data-series')),svg=box.querySelector('svg'),tip=box.querySelector('.tip'),
        hair=svg.querySelector('.hair'),hit=svg.querySelector('.hit');
    function fmt(v){return v==null?'—':(v-100>=0?'+':'')+(v-100).toFixed(2).replace('.',',')+'%';}
    function move(ev){
      var r=svg.getBoundingClientRect(),x=(ev.clientX-r.left)/r.width*d.w;
      var i=Math.round((x-d.l)/(d.w-d.l-d.r)*(d.dates.length-1));i=Math.max(0,Math.min(d.dates.length-1,i));
      var px=d.l+(d.dates.length>1?i/(d.dates.length-1):0)*(d.w-d.l-d.r);
      hair.setAttribute('x1',px);hair.setAttribute('x2',px);hair.style.opacity=1;
      var html='<b>'+d.dates[i]+'</b>';
      d.series.forEach(function(s){html+='<div><span class="dot" style="background:'+s.color+'"></span>'+s.name+': '+fmt(s.values[i])+'</div>';});
      tip.innerHTML=html;tip.style.opacity=1;
      var left=px/d.w*r.width+12;if(left+170>r.width)left=px/d.w*r.width-180;tip.style.left=left+'px';tip.style.top='8px';
    }
    function out(){tip.style.opacity=0;hair.style.opacity=0;}
    hit.addEventListener('pointermove',move);hit.addEventListener('pointerdown',move);hit.addEventListener('pointerleave',out);
  });
})();
</script>"""


def nav_chart(dates: list[str], series: list[dict], *, run_time: str | None = None) -> str:
    """Indexed (start = 100) lines on one axis. ``series``: {name, color (css), values, dashed};
    ``run_time``: when the bot runs each day (HH:MM), for the empty-chart hint."""
    if len(dates) < 2:
        when = f"mỗi ngày lúc {html.escape(run_time)}" if run_time else "sau mỗi phiên"
        return ('<p class="muted small" style="margin:0">Biểu đồ NAV xuất hiện sau 2 phiên giao dịch của bot '
                f'({when}, hoặc bấm “Chạy phiên ngay”).</p>')
    w, h, left, right, top, bottom = 680, 270, 48, 172, 16, 32
    values = [v for s in series for v in s["values"] if v is not None]
    lo, hi = min(values + [100.0]), max(values + [100.0])
    pad = max((hi - lo) * 0.12, 0.5)
    lo, hi = lo - pad, hi + pad

    def x(i):
        return left + i / (len(dates) - 1) * (w - left - right)

    def y(v):
        return top + (hi - v) / (hi - lo) * (h - top - bottom)

    grid = []
    for k in range(4):
        v = lo + (hi - lo) * k / 3
        grid.append(f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
                    f'style="stroke:var(--line);stroke-width:1"/>'
                    f'<text x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end" '
                    f'style="fill:var(--muted);font-size:13px">{v - 100:+.0f}%</text>')
    base = (f'<line x1="{left}" x2="{w - right}" y1="{y(100):.1f}" y2="{y(100):.1f}" '
            'style="stroke:var(--line-strong);stroke-width:1"/>')
    lines, labels = [], []
    used_y: list[float] = []
    for s in series:
        pts = [(x(i), y(v)) for i, v in enumerate(s["values"]) if v is not None]
        if len(pts) < 2:
            continue
        dash = "stroke-dasharray:5 4;" if s.get("dashed") else ""
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        lines.append(f'<polyline points="{path}" fill="none" style="stroke:{s["color"]};stroke-width:2;'
                     f'{dash}stroke-linejoin:round;stroke-linecap:round"/>')
        last = next(v for v in reversed(s["values"]) if v is not None)
        ly = pts[-1][1]
        while any(abs(ly - u) < 18 for u in used_y):
            ly += 18
        used_y.append(ly)
        lines.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="4" '
                     f'style="fill:{s["color"]};stroke:var(--surface);stroke-width:2"/>')
        change = f"{last - 100:+.1f}%".replace(".", ",")
        labels.append(f'<text x="{w - right + 10}" y="{ly + 5:.1f}" style="fill:var(--ink-2);font-size:14px;font-weight:600">'
                      f'{e(s["name"])} {change}</text>')
    axis = (f'<text x="{left}" y="{h - 8}" style="fill:var(--muted);font-size:13px">{e(dates[0])}</text>'
            f'<text x="{w - right}" y="{h - 8}" text-anchor="end" style="fill:var(--muted);font-size:13px">{e(dates[-1])}</text>')
    hair = (f'<line class="hair" x1="0" x2="0" y1="{top}" y2="{h - bottom}" '
            'style="stroke:var(--muted);stroke-width:1;opacity:0"/>')
    hit = f'<rect class="hit" x="{left}" y="0" width="{w - left - right}" height="{h}" fill="transparent"/>'
    data = {"w": w, "l": left, "r": right, "dates": dates,
            "series": [{"name": s["name"], "color": s["color"], "values": s["values"]} for s in series]}
    legend = "".join(
        f'<span><span class="ln{" dash" if s.get("dashed") else ""}" style="border-color:{s["color"]}"></span>{e(s["name"])}</span>'
        for s in series)
    return (f'<div class="legend">{legend}</div>'
            f'<div class="chart" data-series="{e(json.dumps(data))}">'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="NAV các tài khoản so với VNINDEX, chỉ số hoá 100">'
            f'{"".join(grid)}{base}{"".join(lines)}{"".join(labels)}{axis}{hair}{hit}</svg>'
            f'<div class="tip"></div></div>{_CHART_JS}')
