"""Alerts during the session: code only, no AI request.

The books still trade on the day's closed bar after 15:05 (as the backtests
did); this only tells the owner, while the market is open, what deserves a look
now rather than at the evening report. Every ``intraday_interval_min`` minutes
of continuous trading (9:15-11:30, 13:00-14:30) it reads the last matched price
of:

- the real book's holdings (signal mode): price at or under the stop (with how
  many shares T+2 lets you sell), at T1 or T2, locked at the floor;
- the Quant Engine paper book: its holdings (same checks) and its limit buy
  orders for today (the price is in the buy zone, or already under the planned stop);
- VNINDEX down ``intraday_vni_drop_pct`` % (and twice that) from the last close.

Each alert goes out once a day (kept in ``intraday_alerts.json``, so a restart
does not repeat them). A price checked every few minutes can miss a brief dip
through a stop between two reads.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vnbot.jobs import write_text_atomic

logger = logging.getLogger(__name__)

ACTIVE_PHASES = ("continuous",)


def _fmt(value) -> str:
    return f"{value:,.0f}".replace(",", ".") if value is not None else "-"


def live_price(code: str, day: str) -> float | None:
    from tradingagents.dataflows.news import get_live_match_price

    return get_live_match_price(code, day)


def vnindex_change(day: str) -> float | None:
    """VNINDEX's change from the last close in percent, when today's bar is out."""
    from tradingagents.dataflows.ohlcv import load_ohlcv
    from tradingagents.market import vn_rules

    frame = load_ohlcv(vn_rules.BENCHMARK, day)
    if len(frame) < 2 or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
        return None
    return (float(frame["Close"].iloc[-1]) / float(frame["Close"].iloc[-2]) - 1) * 100


def exchange(code: str) -> str | None:
    from tradingagents.dataflows.symbols import exchange_of

    try:
        return exchange_of(code)
    except Exception:  # noqa: BLE001 - HOSE bands are assumed
        return None


class IntradayMonitor:
    def __init__(self, settings, trader, notify: Callable[[str], None], *,
                 price=live_price, index_change=vnindex_change, exchange_of=exchange):
        self.settings, self.trader, self.notify = settings, trader, notify
        self.price, self.index_change, self.exchange_of = price, index_change, exchange_of
        self.path = Path(settings.data_dir) / "intraday_alerts.json"
        self._last_check = 0.0
        self._thread = threading.Thread(target=self._loop, name="intraday", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # ── once-a-day memory ──
    def _sent(self, day: str) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return set(data.get("keys", [])) if data.get("day") == day else set()

    def _remember(self, day: str, keys: set[str]) -> None:
        write_text_atomic(self.path, json.dumps({"day": day, "keys": sorted(keys)}))

    # ── what to watch ──
    def _books(self, day: str):
        """(label, book) of the books whose holdings are watched."""
        out = []
        if self.settings.trade_mode == "signal":
            try:
                real = self.trader.real()
                real.settle_to(day)          # T+2 as of today (the file is not changed)
                out.append(("💼 Thật", real))
            except Exception as exc:  # noqa: BLE001 - a damaged real book is reported by /danhmuc
                logger.warning("Intraday: real book unavailable: %s", exc)
        out.append(("🤖 Ảo (Quant Engine)", self.trader.paper()))
        return out

    def check(self, day: str) -> list[str]:
        """One pass: the alerts not sent yet today, sent and returned."""
        from tradingagents.market import vn_rules

        sent = self._sent(day)
        alerts: list[tuple[str, str]] = []
        prices: dict[str, float | None] = {}

        def price_of(code):
            if code not in prices:
                try:
                    prices[code] = self.price(code, day)
                except Exception as exc:  # noqa: BLE001 - one quote must not stop the pass
                    logger.warning("Intraday price %s: %s", code, exc)
                    prices[code] = None
            return prices[code]

        for label, book in self._books(day):
            for pos in book.positions.values():
                p = price_of(pos.symbol)
                if not p:
                    continue
                where = f"{label} {pos.symbol}: giá {_fmt(p)}"
                if pos.stop and p <= pos.stop:
                    sellable = pos.settled_qty
                    t2 = (f"bán được {_fmt(sellable)}/{_fmt(pos.qty)} cp (T+2)" if sellable < pos.qty
                          else f"{_fmt(pos.qty)} cp bán được")
                    alerts.append((f"stop:{label}:{pos.symbol}", f"⛔ {where} ≤ stop {_fmt(pos.stop)}, {t2}"))
                if pos.target1 and not pos.t1_done and p >= pos.target1:
                    alerts.append((f"t1:{label}:{pos.symbol}", f"🎯 {where} chạm T1 {_fmt(pos.target1)}"))
                if pos.target2 and p >= pos.target2:
                    alerts.append((f"t2:{label}:{pos.symbol}", f"🏁 {where} chạm T2 {_fmt(pos.target2)}"))
                if pos.last_price:
                    _, floor = vn_rules.ceiling_floor(pos.last_price, self.exchange_of(pos.symbol))
                    if p <= floor:
                        alerts.append((f"floor:{label}:{pos.symbol}",
                                       f"🔒 {where} = giá sàn {_fmt(floor)}, có thể không bán được"))
        for order in self.trader.paper().orders:
            if order.side != "BUY" or not order.limit:
                continue
            p = price_of(order.symbol)
            if not p:
                continue
            where = f"🤖 Lệnh mua {order.symbol} giá ≤ {_fmt(order.limit)}"
            if order.stop and p <= order.stop:
                alerts.append((f"order-stop:{order.symbol}",
                               f"⚠️ {where}: giá {_fmt(p)} đã dưới stop dự kiến {_fmt(order.stop)}, nên bỏ lệnh"))
            elif p <= order.limit:
                alerts.append((f"order-zone:{order.symbol}", f"🛒 {where}: giá {_fmt(p)} đang trong vùng mua"))
        try:
            change = self.index_change(day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Intraday VNINDEX: %s", exc)
            change = None
        drop = self.settings.intraday_vni_drop_pct
        if change is not None and drop > 0:
            for level in (drop, 2 * drop):
                if change <= -level:
                    alerts.append((f"vni:{level:g}", f"📉 VNINDEX {change:+.2f}% so với phiên trước (ngưỡng −{level:g}%)"))
        new = [(k, text) for k, text in alerts if k not in sent]
        if new:
            self._remember(day, sent | {k for k, _ in new})
            self.notify(f"🔔 Cảnh báo trong phiên {vn_rules.now_vn().strftime('%H:%M')} (không gọi AI)\n"
                        + "\n".join(text for _, text in new)
                        + "\nBot không tự đặt lệnh; các tài khoản ảo vẫn khớp theo nến ngày sau 15:05.")
        return [text for _, text in new]

    def tick(self) -> None:
        from tradingagents.market import vn_rules

        now = vn_rules.now_vn()
        if vn_rules.session_phase(now) not in ACTIVE_PHASES:
            return
        if time.time() - self._last_check < self.settings.intraday_interval_min * 60:
            return
        self._last_check = time.time()
        self.check(now.strftime("%Y-%m-%d"))

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the monitor must keep ticking
                logger.exception("Intraday check failed")
            time.sleep(30)
