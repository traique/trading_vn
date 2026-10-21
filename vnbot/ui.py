"""The look of the web app: one stylesheet, a page shell and small components.

Server-rendered, no build step. "Champagne on obsidian": ivory paper and ink
with a champagne-gold accent (light), obsidian with the same gold (dark),
Playfair Display headings over Be Vietnam Pro. The theme follows the device
and can be switched with the button in the header; the choice is remembered in
the browser (localStorage, best effort).

Chart series colours were checked with the dataviz palette validator
(lightness band, chroma, colour-blind separation, contrast) for each theme:
light #2A6FC9 / #B8860B / #1A9E78 on #FFFFFF, dark #4A88D8 / #B88A22 / #1C9A72
on #14171F. VNINDEX is a dashed neutral line. Every line also carries a direct
label and the numbers are in the KPI tiles, so identity never rests on colour
alone.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = ("logo.png", "favicon.png")
LOGO = '<img class="logo" src="/static/logo.png" alt="LCTA" width="62" height="44">'
FAVICON = '<link rel="icon" type="image/png" href="/static/favicon.png">'

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700'
    '&family=Playfair+Display:wght@500;600;700&display=swap" rel="stylesheet">'
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
  --bg:#F6F4EF; --bg-glow:#EFE4C8; --surface:#FFFFFF; --surface-2:#F4F1EA; --surface-3:#ECE7DC;
  --ink:#14161B; --ink-2:#464A53; --muted:#6F737D; --line:#E7E2D7; --line-strong:#D5CDBC;
  --gold:#B8963E; --gold-2:#E3C979; --gold-ink:#1A1505; --brand-text:#7E6014; --brand-soft:#F5EDD8;
  --up:#1E7A4C; --up-soft:#E2F2E9; --down:#B3362B; --down-soft:#FAE5E2; --warn:#8A5D08; --warn-soft:#F7EDD4;
  --neutral-soft:#EFECE6; --info:#2A5FA8; --info-soft:#E6EEF8;
  --c-factor:#2A6FC9; --c-engine:#B8860B; --c-mom:#1A9E78; --c-bench:#8A8F99;
  --shadow:0 1px 2px rgba(20,22,27,.04),0 12px 32px -12px rgba(20,22,27,.12);
  --hair:linear-gradient(90deg,transparent,rgba(184,150,62,.55),transparent);
"""

_DARK = """
  --bg:#0B0D12; --bg-glow:#1D1A10; --surface:#14171F; --surface-2:#1A1E28; --surface-3:#222734;
  --ink:#ECEEF2; --ink-2:#B9BEC8; --muted:#8A909C; --line:#252A36; --line-strong:#333A49;
  --gold:#C9A445; --gold-2:#EBD38A; --gold-ink:#17130A; --brand-text:#E2C470; --brand-soft:#2A2414;
  --up:#5CC795; --up-soft:#13281E; --down:#F0857A; --down-soft:#2E1716; --warn:#E6B65A; --warn-soft:#2B2414;
  --neutral-soft:#1F232D; --info:#7FB0EE; --info-soft:#16223A;
  --c-factor:#4A88D8; --c-engine:#B88A22; --c-mom:#1C9A72; --c-bench:#7D828C;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 18px 40px -18px rgba(0,0,0,.7);
  --hair:linear-gradient(90deg,transparent,rgba(212,180,90,.5),transparent);
"""

STYLE = (
    ":root{" + _LIGHT + "color-scheme:light}"
    "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + _DARK + "color-scheme:dark}}"
    ":root[data-theme=dark]{" + _DARK + "color-scheme:dark}"
    + """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  background-image:radial-gradient(900px 420px at 90% -8%,var(--bg-glow),transparent 62%),
    radial-gradient(700px 360px at -10% 0%,color-mix(in srgb,var(--bg-glow) 45%,transparent),transparent 60%);
  background-attachment:fixed;font:15px/1.6 "Be Vietnam Pro",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--brand-text);text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
.num,td.num,.kpi .v,.stat .v{font-variant-numeric:tabular-nums}
::selection{background:var(--brand-soft)}
/* header */
.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 78%,transparent);
  backdrop-filter:saturate(1.3) blur(14px);-webkit-backdrop-filter:saturate(1.3) blur(14px)}
.top::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;background:var(--hair)}
.top-in{max-width:1120px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:16px}
.brand{display:flex;align-items:center;gap:12px;color:var(--ink)}
.brand:hover{text-decoration:none}
.logo{width:62px;height:44px;object-fit:contain;flex:none;filter:drop-shadow(0 4px 10px rgba(184,150,62,.25))}
.brand-name{font:600 17px/1.1 "Playfair Display",Georgia,serif;letter-spacing:.01em}
.brand small{display:block;font:600 10px/1.4 "Be Vietnam Pro",sans-serif;color:var(--muted);letter-spacing:.18em;
  text-transform:uppercase;margin-top:3px}
.nav{display:flex;gap:2px;margin-left:auto;min-width:0;overflow-x:auto;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}
.nav a{position:relative;padding:8px 14px;border-radius:10px;color:var(--ink-2);font-weight:500;font-size:14px;white-space:nowrap}
.nav a:hover{text-decoration:none;color:var(--ink);background:var(--surface-2)}
.nav a.on{color:var(--ink)}
.nav a.on::after{content:"";position:absolute;left:14px;right:14px;bottom:2px;height:2px;border-radius:2px;
  background:linear-gradient(90deg,var(--gold),var(--gold-2))}
.icon-btn{width:38px;height:38px;border-radius:11px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
  display:grid;place-items:center;cursor:pointer;flex:none;transition:border-color .15s,color .15s}
.icon-btn:hover{color:var(--brand-text);border-color:var(--gold)}
.icon-btn svg{width:18px;height:18px}
.i-sun{display:none}:root[data-theme=dark] .i-sun{display:block}:root[data-theme=dark] .i-moon{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .i-sun{display:block}:root:not([data-theme=light]) .i-moon{display:none}}
/* layout */
main{max-width:1120px;margin:0 auto;padding:30px 16px 56px}
.hero{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;margin:0 0 24px}
.eyebrow{font:600 11px/1 "Be Vietnam Pro",sans-serif;letter-spacing:.2em;text-transform:uppercase;color:var(--brand-text);
  display:flex;align-items:center;gap:10px;margin-bottom:12px}
.eyebrow::before{content:"";width:28px;height:1px;background:linear-gradient(90deg,var(--gold),transparent)}
h1{font:600 40px/1.1 "Playfair Display",Georgia,serif;letter-spacing:-.015em;margin:0}
h1 .tick{font-family:"Be Vietnam Pro",sans-serif;font-weight:700;letter-spacing:.02em}
.lead{color:var(--ink-2);margin:10px 0 0;max-width:680px;font-size:15.5px}
h2{font-size:15px;font-weight:600;margin:0;letter-spacing:.005em}
h3{font:600 11px/1 "Be Vietnam Pro",sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:22px 0 10px}
.grid{display:grid;gap:18px;align-items:start}
.grid>*{min-width:0}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}.g-wide{grid-template-columns:minmax(0,1.35fr) minmax(0,1fr)}
@media (max-width:880px){.g3,.g4{grid-template-columns:repeat(2,minmax(0,1fr))}.g-wide{grid-template-columns:1fr}}
@media (max-width:620px){.g2{grid-template-columns:1fr}h1{font-size:30px}.nav a{padding:8px 8px;font-size:13px}
  .brand small,.brand-name{display:none}.top-in{gap:8px}main{padding-top:22px}
  .stat{padding:14px 16px}.stat .v{font-size:20px;white-space:normal}.card-h{padding:16px 18px 12px}.card-b{padding:4px 18px 18px}
  .row .btn{flex:1 1 auto}}
.stack>*+*{margin-top:18px}
.mt{margin-top:18px}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden}
.card::before{content:"";position:absolute;left:24px;right:24px;top:0;height:1px;background:var(--hair);opacity:.8}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 22px 14px}
.card-h h2{font:600 18px/1.25 "Playfair Display",Georgia,serif;letter-spacing:.005em}
.card-h .sub{color:var(--muted);font-size:13px;margin-top:3px}
.card-b{padding:4px 22px 20px}
.card-b.flush{padding:0;border-top:1px solid var(--line)}
.accent-peach::after,.accent-sa::after,.accent-factor::after,.accent-gold::after{content:"";position:absolute;left:0;top:18px;
  bottom:18px;width:3px;border-radius:0 3px 3px 0}
.accent-peach::after{background:var(--c-engine)}.accent-sa::after{background:var(--c-mom)}
.accent-factor::after{background:var(--c-factor)}.accent-gold::after{background:linear-gradient(var(--gold),var(--gold-2))}
.muted{color:var(--muted)}.small{font-size:13px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:1px}
/* forms */
label.f{display:block;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.14em;margin-bottom:7px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.row>.fld{flex:1 1 150px}
input{width:100%;font:inherit;padding:12px 14px;border-radius:12px;border:1px solid var(--line-strong);background:var(--surface-2);
  color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s,background .15s}
input:focus{border-color:var(--gold);background:var(--surface);box-shadow:0 0 0 4px var(--brand-soft)}
input.ticker{text-transform:uppercase;font-weight:700;letter-spacing:.08em;font-size:17px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 14px/1 "Be Vietnam Pro",sans-serif;
  letter-spacing:.01em;padding:13px 20px;border-radius:12px;border:1px solid transparent;cursor:pointer;white-space:nowrap;
  transition:filter .15s,background .15s,border-color .15s,transform .05s}
.btn:active{transform:translateY(1px)}
.btn-primary{background:linear-gradient(135deg,var(--gold),var(--gold-2));color:var(--gold-ink);
  box-shadow:0 8px 20px -10px rgba(184,150,62,.8),inset 0 1px 0 rgba(255,255,255,.35)}
.btn-primary:hover{filter:brightness(1.05);text-decoration:none}
.btn-ghost{background:transparent;color:var(--ink);border-color:var(--line-strong)}
.btn-ghost:hover{border-color:var(--gold);color:var(--brand-text);text-decoration:none}
.btn-sa{background:var(--ink);color:var(--bg)}.btn-sa:hover{filter:brightness(1.15);text-decoration:none}
.btn[disabled]{opacity:.55;cursor:not-allowed}
/* on/off switch */
.switch{display:inline-flex;align-items:center;gap:10px;border:0;background:none;padding:4px 0;cursor:pointer;
  font:600 13px/1 "Be Vietnam Pro",sans-serif;color:var(--muted)}
.switch .knob{position:relative;width:44px;height:26px;border-radius:999px;background:var(--surface-3);
  border:1px solid var(--line-strong);transition:background .2s,border-color .2s;flex:none}
.switch .knob::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
  background:var(--surface);box-shadow:0 1px 3px rgba(0,0,0,.3);transition:transform .2s}
.switch.on{color:var(--up)}
.switch.on .knob{background:linear-gradient(135deg,var(--gold),var(--gold-2));border-color:transparent}
.switch.on .knob::after{transform:translateX(18px)}
.switch:focus-visible .knob{box-shadow:0 0 0 4px var(--brand-soft)}
.switch .st{min-width:24px;text-align:left}
.checks{display:flex;flex-wrap:wrap;gap:8px 18px;margin:14px 0 8px}
label.check{display:inline-flex;align-items:center;gap:8px;font-size:14px;color:var(--ink-2);cursor:pointer}
label.check input{width:18px;height:18px;padding:0;accent-color:var(--gold);flex:none}
label.check.warn{display:flex;align-items:flex-start;color:var(--warn);font-size:13px;margin-top:6px}
label.check.warn input{margin-top:1px}
/* badges and chips */
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
  font:600 11.5px/1.7 "Be Vietnam Pro",sans-serif;letter-spacing:.04em;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.b-buy{background:var(--up-soft);color:var(--up)}.b-sell{background:var(--down-soft);color:var(--down)}
.b-hold{background:var(--brand-soft);color:var(--brand-text)}.b-watch{background:var(--warn-soft);color:var(--warn)}
.b-none{background:var(--neutral-soft);color:var(--muted)}
.b-ok{background:var(--up-soft);color:var(--up)}.b-err{background:var(--down-soft);color:var(--down)}
.b-info{background:var(--info-soft);color:var(--info)}
.b-run{background:var(--brand-soft);color:var(--brand-text)}.b-run::before{animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{padding:5px 11px;border-radius:999px;background:var(--surface-2);border:1px solid var(--line);font-weight:700;font-size:12.5px;
  letter-spacing:.05em}
/* alerts */
.alert{padding:13px 16px;border-radius:14px;border:1px solid transparent;margin-bottom:18px;font-size:14px}
.alert-err{background:var(--down-soft);color:var(--down)}
.alert-ok{background:var(--up-soft);color:var(--up)}
.alert-info{background:var(--brand-soft);color:var(--brand-text)}
/* KPI tiles and the stat strip */
.kpi{position:relative;background:var(--surface-2);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.kpi .l{font-size:11px;color:var(--muted);font-weight:600;letter-spacing:.12em;text-transform:uppercase}
.kpi .v{font:600 24px/1.2 "Playfair Display",Georgia,serif;letter-spacing:-.01em;margin-top:6px;white-space:nowrap}
.kpi .s{font-size:12.5px;color:var(--muted);margin-top:2px}
.strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:var(--surface);border:1px solid var(--line);
  border-radius:20px;box-shadow:var(--shadow);margin-bottom:18px;overflow:hidden}
.stat{padding:16px 20px;position:relative;min-width:0}
.stat+.stat::before{content:"";position:absolute;left:0;top:18px;bottom:18px;width:1px;background:var(--line)}
.stat .l{font-size:11px;color:var(--muted);font-weight:600;letter-spacing:.12em;text-transform:uppercase}
.stat .v{font:600 26px/1.2 "Playfair Display",Georgia,serif;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .s{font-size:12.5px;color:var(--muted)}
@media (max-width:880px){.strip{grid-template-columns:repeat(2,minmax(0,1fr))}.stat:nth-child(3)::before{display:none}
  .stat:nth-child(n+3){border-top:1px solid var(--line)}}
.meter{height:8px;border-radius:999px;background:var(--surface-3);overflow:hidden;margin-top:10px}
.meter>span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--gold),var(--gold-2))}
.meter.hot>span{background:var(--down)}
.pos{color:var(--up)}.neg{color:var(--down)}
/* tables */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-size:10.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.14em;text-align:left;
  padding:11px 16px;white-space:nowrap;border-bottom:1px solid var(--line);background:var(--surface-2)}
td{padding:12px 16px;border-top:1px solid var(--line);vertical-align:middle}
tbody tr:first-child td{border-top:0}
tbody tr{transition:background .12s}
tbody tr:hover td{background:color-mix(in srgb,var(--surface-2) 70%,transparent)}
td.num,th.num{text-align:right;white-space:nowrap}
td code{font-size:12.5px;background:var(--surface-2);padding:2px 7px;border-radius:6px;border:1px solid var(--line)}
td.empty{color:var(--muted);text-align:center;padding:26px}
.sym{font-weight:700;letter-spacing:.05em;color:var(--ink)}
a.sym:hover{color:var(--brand-text)}
tr.changed td{background:var(--brand-soft)}
/* feeds */
.feed{list-style:none;margin:0;padding:0}
.feed li{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--line);font-size:14px}
.feed li:first-child{border-top:0;padding-top:2px}
.feed .t{flex:none;color:var(--muted);font-size:12.5px;width:52px;padding-top:1px;font-variant-numeric:tabular-nums}
.feed .empty{color:var(--muted);border:0}
/* report text */
.report{white-space:pre-wrap;font:14px/1.7 "Be Vietnam Pro",sans-serif;margin:0;color:var(--ink)}
.report.mono{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
/* stepper */
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 18px}
.step{display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;background:var(--surface);border:1px solid var(--line);
  color:var(--muted);font-size:13px;font-weight:500}
.step.done{background:var(--up-soft);border-color:transparent;color:var(--up)}
.step.now{background:var(--brand-soft);border-color:var(--gold);color:var(--brand-text)}
.step.fail{background:var(--down-soft);border-color:transparent;color:var(--down)}
/* chart */
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto}
.chart .tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line-strong);border-radius:12px;
  box-shadow:var(--shadow);padding:9px 11px;font-size:12px;min-width:150px;opacity:0;transition:opacity .1s;color:var(--ink)}
.chart .tip b{display:block;margin-bottom:4px}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;color:var(--ink-2);margin-bottom:10px}
.legend .ln{display:inline-block;width:18px;height:0;border-top:2.5px solid;vertical-align:4px;margin-right:6px}
.legend .ln.dash{border-top-style:dashed}
footer{max-width:1120px;margin:0 auto;padding:0 16px 36px;color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;
  justify-content:space-between;gap:8px}
footer::before{content:"";flex-basis:100%;height:1px;background:var(--hair);margin-bottom:14px}
"""
)

SUN = ('<svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
       '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
       'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>')
MOON = ('<svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
        '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>')

e = html.escape


NAV = (("home", "/", "Phân tích"), ("ai", "/ai", "AI & cảnh báo"), ("bot", "/bot", "Bot giao dịch"))


def page(title: str, body: str, *, active: str = "", refresh: int | None = None, show_bot: bool = True) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    links = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
                    for key, href, label in NAV if show_bot or key != "bot")
    return (
        '<!doctype html><html lang="vi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="theme-color" content="#F6F4EF" media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#0B0D12" media="(prefers-color-scheme: dark)">'
        f"{meta}<title>{e(title)} · TradingAgents VN</title>{FAVICON}{THEME_BOOT}{FONTS}<style>{STYLE}</style></head><body>"
        '<header class="top"><div class="top-in">'
        f'<a class="brand" href="/">{LOGO}<span class="brand-name">TradingAgents VN'
        "<small>HOSE · HNX · UPCoM</small></span></a>"
        f'<nav class="nav">{links}</nav>'
        f'<button class="icon-btn" id="theme-toggle" type="button" aria-label="Đổi giao diện sáng/tối">{MOON}{SUN}</button>'
        f"</div></header><main>{body}</main>"
        "<footer><span>Thông tin chỉ mang tính tham khảo, không phải khuyến nghị đầu tư.</span>"
        "<span>Bot không gửi lệnh tới công ty chứng khoán.</span></footer>"
        f"{THEME_TOGGLE}</body></html>"
    )


def hero(title: str, lead: str = "", right: str = "", *, eyebrow: str = "") -> str:
    top = f'<div class="eyebrow">{eyebrow}</div>' if eyebrow else ""
    return (f'<div class="hero"><div>{top}<h1>{title}</h1>'
            + (f'<p class="lead">{lead}</p>' if lead else "") + f"</div>{right}</div>")


def strip(items: list[tuple[str, str, str]]) -> str:
    """The headline numbers of a page: (label, value html, sub html)."""
    return '<div class="strip">' + "".join(
        f'<div class="stat"><div class="l">{label}</div><div class="v">{value}</div>'
        + (f'<div class="s">{sub}</div>' if sub else "") + "</div>" for label, value, sub in items) + "</div>"


def meter(used: int, cap: int | None) -> str:
    """A bar of ``used`` out of ``cap`` (none without a cap); red from 85%."""
    if not cap:
        return ""
    share = max(0.0, min(1.0, used / cap))
    return (f'<div class="meter{" hot" if share >= 0.85 else ""}" role="img" aria-label="{used}/{cap}">'
            f'<span style="width:{share * 100:.1f}%"></span></div>')


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
