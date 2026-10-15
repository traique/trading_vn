"""The trading bot: two paper accounts traded every session, signals for a real one.

Two strategies run side by side, each on its own paper book:

- ``engine``: the Quant Engine's BUY plans (entry zone, stop, T1/T2).
- ``momentum``: the momentum rotation over VN30 (trading/momentum.py).

The report compares them every day; ``SIGNAL_STRATEGY`` picks which one the
real-account signals follow. Each trading day after the close (``run_day``):

1. The paper account settles the session: yesterday's orders fill or lapse,
   stops and targets are checked on today's bar (ledger rules, T+2, fees).
2. The engine decides for the basket (VN30 + watchlist + anything held), with
   the paper book as its portfolio so held stocks are judged as holdings.
3. The paper account places tomorrow's orders; the NAV is recorded beside VNINDEX.
4. In ``signal`` mode, the same decisions are turned into suggested orders
   sized on the real book, and today's stop/target hits on real holdings are
   flagged. The real book changes only when its owner reports a fill (/khop).

Then, if enabled, the nightly calibration may move one threshold (see
``tradingagents.trading.calibrate``). No order is ever sent to a broker.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

from vnbot.settings import Settings

logger = logging.getLogger(__name__)


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}".replace(",", ".")


def _bn(value: float) -> str:
    return f"{value / 1e6:,.1f} tr".replace(",", "X").replace(".", ",").replace("X", ".")


class TradingBot:
    def __init__(self, settings: Settings, watchlist, notify=None):
        self.settings, self.watchlist = settings, watchlist
        self.notify = notify or (lambda text: None)
        self.dir = settings.data_dir / "trading"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._running = threading.Event()

    # ── books ──
    @property
    def paper_path(self) -> Path:
        return self.dir / "paper.json"

    @property
    def real_path(self) -> Path:
        return self.dir / "real.json"

    @property
    def momentum_path(self) -> Path:
        return self.dir / "paper_momentum.json"

    def momentum_book(self):
        from tradingagents.trading.ledger import Ledger

        return Ledger.load(self.momentum_path, self.settings.paper_capital)

    def paper(self):
        from tradingagents.trading.ledger import Ledger

        return Ledger.load(self.paper_path, self.settings.paper_capital)

    def real(self):
        from tradingagents.trading.ledger import Ledger

        return Ledger.load(self.real_path, 0.0)

    def last_signals(self) -> dict:
        try:
            return json.loads((self.dir / "signals.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ── the basket ──
    def universe(self) -> list[str]:
        from tradingagents.dataflows import money_flows as mf

        codes: list[str] = []
        if self.settings.universe_vn30:
            codes += mf.index_members("VN30")
        codes += self.watchlist.get()
        out = []
        for code in codes:
            if code not in out:
                out.append(code)
        return out

    # ── one session ──
    def run_day(self, day: str) -> str:
        """Settle ``day`` on the paper book, decide, place tomorrow's orders. Returns the report."""
        from tradingagents.dataflows.errors import VendorError
        from tradingagents.dataflows.ohlcv import load_ohlcv
        from tradingagents.market import vn_rules
        from tradingagents.quant import engine
        from tradingagents.quant.params import active
        from tradingagents.trading.ledger import Bar, Signal

        with self._lock:
            params = active()
            paper, real, mom_book = self.paper(), self.real(), self.momentum_book()
            last = self.last_report()
            if paper.meta.get("last_day") == day and last.get("day") == day:
                # Already settled: running a session twice would count its bar twice.
                return last["text"]
            frames: dict = {}

            def frame_of(symbol):
                if symbol not in frames:
                    try:
                        frames[symbol] = load_ohlcv(symbol, day)
                    except VendorError as exc:
                        logger.warning("Bot: no bars for %s on %s: %s", symbol, day, exc)
                        frames[symbol] = None
                return frames[symbol]

            def bar_of(symbol):
                frame = frame_of(symbol)
                if frame is None or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
                    return None            # suspended today
                row, prev = frame.iloc[-1], (float(frame["Close"].iloc[-2]) if len(frame) > 1 else None)
                return Bar(day, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), prev)

            touched = ({o.symbol for o in paper.orders} | set(paper.positions) | set(real.positions)
                       | {o.symbol for o in mom_book.orders} | set(mom_book.positions))
            bars = {s: b for s in touched if (b := bar_of(s)) is not None}
            fills = paper.process_session({s: bars[s] for s in bars}, params=params)
            vni = frame_of(vn_rules.BENCHMARK)
            bench = float(vni["Close"].iloc[-1]) if vni is not None else None
            paper.end_of_session(day, bench)

            # Decisions: the paper book decides holdings; a stock held only in the
            # real book is judged again as a holding for the real signals.
            symbols = list(dict.fromkeys(self.universe() + list(paper.positions) + list(real.positions)))
            signals, real_only, errors = [], {}, []
            book = paper.portfolio_context()
            for symbol in symbols:
                try:
                    result = engine.analyze(symbol, day, portfolio=book, params=params)
                    signals.append(Signal.from_result(result))
                    if symbol in real.positions and symbol not in paper.positions:
                        real_only[symbol] = Signal.from_result(
                            engine.analyze(symbol, day, portfolio=real.portfolio_context(), params=params))
                except Exception as exc:  # noqa: BLE001 - one stock must not stop the session
                    errors.append(f"{symbol}: {exc}")
                    logger.warning("Bot: %s on %s failed: %s", symbol, day, exc)
            orders = paper.plan_orders(day, signals, max_positions=params.max_positions)
            paper.meta["last_day"] = day
            paper.save(self.paper_path)
            (self.dir / "signals.json").write_text(json.dumps(
                {"day": day, "signals": {s.symbol: s.__dict__ for s in signals}}, ensure_ascii=False), encoding="utf-8")

            text = self._paper_report(day, paper, fills, orders, signals, errors)
            mom_text, mom_decision = self._momentum_session(day, mom_book, bars, frame_of, vni, bench, params)
            text += "\n\n" + mom_text + "\n\n" + self._comparison(paper, mom_book)
            if self.settings.trade_mode == "signal":
                if self.settings.signal_strategy == "momentum":
                    text += "\n\n" + self._real_momentum_report(day, real, bars, mom_decision, frame_of, params)
                else:
                    text += "\n\n" + self._real_report(day, real, bars, signals, real_only, params)
            from datetime import datetime

            (self.dir / "last_report.json").write_text(json.dumps(
                {"day": day, "text": text, "at": datetime.now().isoformat(timespec="minutes")},
                ensure_ascii=False), encoding="utf-8")
            return text

    def last_report(self) -> dict:
        try:
            return json.loads((self.dir / "last_report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ── on demand (/chay, the web button) ──
    def session_day(self) -> str:
        """The last closed session: today after the close, else the previous one (holidays skipped)."""
        from tradingagents.dataflows.ohlcv import load_ohlcv
        from tradingagents.market import vn_rules

        today = vn_rules.now_vn().strftime("%Y-%m-%d")
        frame = load_ohlcv(vn_rules.BENCHMARK, today)     # drops today's bar while the session runs
        return frame["Date"].iloc[-1].strftime("%Y-%m-%d")

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start_run(self, notify=None) -> str:
        """Run the last closed session in the background; the report goes to ``notify``."""
        notify = notify or self.notify
        if self._running.is_set():
            return "⏳ Bot đang chạy một phiên, đợi vài phút."
        try:
            day = self.session_day()
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ Không đọc được dữ liệu VNINDEX: {exc}"
        if self.paper().meta.get("last_day") == day and self.last_report().get("day") == day:
            return f"✅ Phiên {day} đã chạy rồi - xem /danhmuc, /lenh hoặc trang /bot."
        self._running.set()

        def work():
            try:
                notify(self.run_day(day))
            except Exception as exc:  # noqa: BLE001
                logger.exception("On-demand session %s failed", day)
                notify(f"❌ Bot giao dịch lỗi phiên {day}: {exc}")
            finally:
                self._running.clear()

        threading.Thread(target=work, name="bot-run-now", daemon=True).start()
        return f"▶️ Đang chạy phiên {day} cho hai tài khoản ảo (khoảng 1 phút); báo cáo sẽ gửi khi xong."

    # ── the momentum book ──
    def _momentum_session(self, day, book, bars, frame_of, vni, bench, params):
        from tradingagents.dataflows import money_flows as mf
        from tradingagents.trading import momentum

        fills = book.process_session({s: b for s, b in bars.items() if s in book.positions or
                                      any(o.symbol == s for o in book.orders)}, rules=momentum.RULES)
        book.end_of_session(day, bench)
        need = params.mom_lookback + 1
        closes = {}
        for symbol in mf.index_members("VN30"):
            frame = frame_of(symbol)
            if frame is None or frame["Date"].iloc[-1].strftime("%Y-%m-%d") != day:
                continue
            closes[symbol] = [float(c) for c in frame["Close"].tail(need)]
        vni_closes = [float(c) for c in vni["Close"].tail(params.mom_market_ma)] if vni is not None else []
        decision = momentum.decide(book, closes, vni_closes, params)
        orders = momentum.plan(book, day, decision, {s: c[-1] for s, c in closes.items()}, params)
        book.save(self.momentum_path)
        st = book.stats()
        bench_txt = f" | VNINDEX {st['benchmark_pct']:+.2f}%" if st["benchmark_pct"] is not None else ""
        lines = [f"🚀 BOT ĐỘNG LƯỢNG {day}",
                 f"NAV {_fmt(st['nav'])} ({st['return_pct']:+.2f}% từ {book.start_date or day}){bench_txt} | "
                 f"sụt giảm tối đa {st['max_drawdown_pct']:.2f}%",
                 ("Thị trường: VNINDEX trên MA" if decision.market_ok else "Thị trường: VNINDEX dưới MA")
                 + f"{params.mom_market_ma} | rổ: " + (", ".join(decision.basket) or "tiền mặt")
                 + (f" ({decision.note})" if decision.rebalanced or not decision.market_ok else "")]
        if fills:
            lines += [f"- Khớp {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      + (f", lãi/lỗ {_bn(f.pnl)}" if f.pnl is not None else "") for f in fills]
        if orders:
            lines += [f"- Lệnh phiên tới: {'MUA' if o.side == 'BUY' else 'BÁN'} {o.symbol} {_fmt(o.qty)} giá mở cửa ({o.reason})"
                      for o in orders]
        return "\n".join(lines), decision

    @staticmethod
    def _comparison(engine_book, mom_book) -> str:
        a, b = engine_book.stats(), mom_book.stats()
        return (f"📊 So sánh: Quant Engine {a['return_pct']:+.2f}% (DD {a['max_drawdown_pct']:.1f}%) | "
                f"Động lượng {b['return_pct']:+.2f}% (DD {b['max_drawdown_pct']:.1f}%)")

    def _real_momentum_report(self, day, real, bars, decision, frame_of, params) -> str:
        from tradingagents.trading import momentum
        from tradingagents.trading.ledger import Ledger

        lines = ["📣 TÍN HIỆU THẬT - ĐỘNG LƯỢNG (tự đặt lệnh, rồi báo /khop)"]
        for symbol, pos in real.positions.items():
            bar = bars.get(symbol)
            if bar is not None and pos.stop and bar.low <= pos.stop:
                lines.append(f"- ⛔ {symbol}: chạm stop {_fmt(pos.stop)} - đề xuất BÁN {_fmt(pos.qty)} cp")
        if real.cash <= 0 and not real.positions:
            lines.append("Chưa có vốn tài khoản thật: gửi /von 500000000 để bot tính khối lượng.")
            return "\n".join(lines)
        draft = Ledger.from_dict(copy.deepcopy(real.to_dict()))
        draft.orders = []
        last = {}
        for symbol in set(decision.basket) | set(real.positions):
            frame = frame_of(symbol)
            if frame is not None:
                last[symbol] = float(frame["Close"].iloc[-1])
        orders = momentum.plan(draft, day, decision, last, params)
        lines += [f"- {'MUA' if o.side == 'BUY' else 'BÁN'} {o.symbol} {_fmt(o.qty)} cp giá mở cửa ({o.reason})"
                  for o in orders]
        if len(lines) == 1:
            lines.append("Không đổi danh mục phiên tới.")
        return "\n".join(lines)

    def _paper_report(self, day, paper, fills, orders, signals, errors) -> str:
        s = paper.stats()
        bench = f" | VNINDEX {s['benchmark_pct']:+.2f}%" if s["benchmark_pct"] is not None else ""
        lines = [f"🤖 BOT PAPER {day}",
                 f"NAV {_fmt(s['nav'])} ({s['return_pct']:+.2f}% từ {paper.start_date or day}){bench} | "
                 f"sụt giảm tối đa {s['max_drawdown_pct']:.2f}%"]
        if fills:
            lines.append("Khớp hôm nay:")
            lines += [f"- {f.side} {f.symbol} {_fmt(f.qty)} @ {_fmt(f.price)} ({f.reason})"
                      + (f", lãi/lỗ {_bn(f.pnl)}" if f.pnl is not None else "") for f in fills]
        if paper.positions:
            lines.append("Đang giữ:")
            for p in paper.positions.values():
                pnl = (p.last_price / p.avg_price - 1) * 100 if p.avg_price else 0
                lines.append(f"- {p.symbol} {_fmt(p.qty)} cp, vốn {_fmt(p.avg_price)}, giá {_fmt(p.last_price)} "
                             f"({pnl:+.1f}%), stop {_fmt(p.stop)}, T1 {_fmt(p.target1)}"
                             + ("" if p.sellable else " [chưa về T+2]"))
        if orders:
            lines.append("Lệnh cho phiên tới:")
            lines += [f"- {o.side} {o.symbol} {_fmt(o.qty)}"
                      + (f" giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}, T1 {_fmt(o.target1)}" if o.limit else " giá mở cửa")
                      for o in orders]
        else:
            buys = [s for s in signals if s.action == "BUY"]
            lines.append("Không có lệnh mới." + (f" ({len(buys)} tín hiệu BUY nhưng hết chỗ/tiền)" if buys else
                                                  " Không mã nào đạt điều kiện BUY."))
        if errors:
            lines.append(f"⚠️ {len(errors)} mã lỗi dữ liệu: " + "; ".join(errors[:3]))
        return "\n".join(lines)

    def _real_report(self, day, real, bars, signals, real_only, params) -> str:
        from tradingagents.trading.ledger import Ledger

        lines = ["📣 TÍN HIỆU CHO TÀI KHOẢN THẬT (tự đặt lệnh, rồi báo /khop)"]
        for symbol, pos in real.positions.items():
            bar = bars.get(symbol)
            sig = real_only.get(symbol) or next((s for s in signals if s.symbol == symbol), None)
            if bar is not None and pos.stop and bar.low <= pos.stop:
                lines.append(f"- ⛔ {symbol}: chạm stop {_fmt(pos.stop)} hôm nay - đề xuất BÁN {_fmt(pos.qty)} cp")
            elif bar is not None and pos.target1 and not pos.t1_done and bar.high >= pos.target1:
                lines.append(f"- 🎯 {symbol}: chạm T1 {_fmt(pos.target1)} - đề xuất bán 1/2, dời stop về hoà vốn")
            elif sig is not None and sig.action == "SELL":
                lines.append(f"- 🔻 {symbol}: hệ thống chuyển SELL - đề xuất bán {_fmt(pos.qty)} cp phiên tới")
        if real.cash <= 0 and not real.positions:
            lines.append("Chưa có vốn tài khoản thật: gửi /von 500000000 để bot tính khối lượng.")
            return "\n".join(lines)
        draft = Ledger.from_dict(copy.deepcopy(real.to_dict()))
        draft.orders = []
        orders = draft.plan_orders(day, signals, max_positions=params.max_positions)
        buys = [o for o in orders if o.side == "BUY"]
        if buys:
            lines += [f"- MUA {o.symbol} {_fmt(o.qty)} cp, giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}, "
                      f"T1 {_fmt(o.target1)}" + (f", T2 {_fmt(o.target2)}" if o.target2 else "") for o in buys]
        elif len(lines) == 1:
            lines.append("Không có lệnh đề xuất cho phiên tới.")
        return "\n".join(lines)

    # ── the real book, updated by its owner ──
    def record_fill(self, side: str, symbol: str, qty: int, price: float, day: str) -> str:
        from tradingagents.market import vn_rules
        from tradingagents.trading.ledger import Fill, Position, _fee

        side = side.upper()
        if qty <= 0 or qty % vn_rules.LOT_SIZE:
            raise ValueError("Khối lượng phải là bội số của 100.")
        with self._lock:
            real = self.real()
            if side == "BUY":
                sig = (self.last_signals().get("signals") or {}).get(symbol, {})
                if self.settings.signal_strategy == "momentum":
                    from tradingagents.market import vn_rules
                    from tradingagents.quant.params import active

                    pct = active().mom_stop_pct
                    sig = {"stop": vn_rules.round_to_tick(price * (1 - pct / 100)) if pct else 0}
                value = qty * price
                fee = _fee(value, "BUY")
                old = real.positions.get(symbol)
                if old:
                    total = old.qty + qty
                    old.avg_price = (old.avg_price * old.qty + value + fee) / total
                    old.qty = total
                else:
                    real.positions[symbol] = Position(
                        symbol=symbol, qty=qty, avg_price=(value + fee) / qty, buy_date=day,
                        stop=float(sig.get("stop") or 0), target1=sig.get("target1"), target2=sig.get("target2"),
                        last_price=price)
                real.cash -= value + fee
                real.fills.append(Fill(day, symbol, "BUY", qty, price, round(fee), "báo khớp"))
                pos = real.positions[symbol]
                if self.settings.signal_strategy == "momentum":
                    note = (f"stop {_fmt(pos.stop)}" if pos.stop else "giữ tới khi rời rổ động lượng")
                else:
                    note = (f"stop {_fmt(pos.stop)}, T1 {_fmt(pos.target1)}" if pos.stop
                            else "chưa có stop (không có tín hiệu BUY gần nhất)")
                text = f"✅ Đã ghi MUA {symbol} {_fmt(qty)} @ {_fmt(price)}; {note}."
            elif side == "SELL":
                pos = real.positions.get(symbol)
                if pos is None or pos.qty < qty:
                    raise ValueError(f"Tài khoản thật không có đủ {symbol} để bán.")
                fill = real._sell(pos, qty, price, day, "báo khớp")
                text = f"✅ Đã ghi BÁN {symbol} {_fmt(qty)} @ {_fmt(price)}; lãi/lỗ {_bn(fill.pnl)}."
            else:
                raise ValueError("Dùng: /khop mua|ban MÃ KHỐI_LƯỢNG GIÁ")
            real.save(self.real_path)
            return text

    def set_capital(self, cash: float) -> str:
        if cash <= 0:
            raise ValueError("Vốn phải lớn hơn 0.")
        with self._lock:
            real = self.real()
            real.cash = cash
            if not real.positions:
                real.start_cash = cash
            real.save(self.real_path)
        return f"✅ Tiền mặt tài khoản thật: {_fmt(cash)} VND."

    # ── status ──
    def portfolio_text(self) -> str:
        paper = self.paper()
        s = paper.stats()
        lines = [f"🤖 Paper: NAV {_fmt(s['nav'])} ({s['return_pct']:+.2f}%), tiền {_fmt(paper.cash)}, "
                 f"{s['trades']} lệnh mua, thắng {s['win_rate_pct'] if s['win_rate_pct'] is not None else '-'}%"]
        lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)} → {_fmt(p.last_price)}, stop {_fmt(p.stop)}"
                  for p in paper.positions.values()] or ["- (không giữ mã nào)"]
        mom = self.momentum_book()
        m = mom.stats()
        lines.append(f"🚀 Động lượng: NAV {_fmt(m['nav'])} ({m['return_pct']:+.2f}%), tiền {_fmt(mom.cash)}, "
                     f"rổ {', '.join(mom.meta.get('mom_basket') or []) or 'tiền mặt'}")
        lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)} → {_fmt(p.last_price)}"
                  for p in mom.positions.values()] or ["- (không giữ mã nào)"]
        if self.settings.trade_mode == "signal":
            real = self.real()
            lines.append(f"💼 Thật: tiền {_fmt(real.cash)}")
            lines += [f"- {p.symbol} {_fmt(p.qty)} @ {_fmt(p.avg_price)}, stop {_fmt(p.stop)}, T1 {_fmt(p.target1)}"
                      for p in real.positions.values()] or ["- (chưa ghi vị thế nào)"]
        return "\n".join(lines)

    def orders_text(self) -> str:
        lines = []
        for title, book in (("Quant Engine", self.paper()), ("Động lượng", self.momentum_book())):
            lines.append(f"{title}:")
            lines += [f"- {o.side} {o.symbol} {_fmt(o.qty)}"
                      + (f" giá ≤ {_fmt(o.limit)}, stop {_fmt(o.stop)}" if o.limit else " giá mở cửa")
                      for o in book.orders] or ["- không có lệnh chờ"]
        return "Lệnh cho phiên tới\n" + "\n".join(lines)

    def calibration_text(self) -> str:
        from tradingagents.quant import params as qp
        from tradingagents.trading import calibrate as cal

        current = qp.active()
        diff = qp.changed(current)
        lines = ["⚙️ Ngưỡng đang dùng: " + (", ".join(f"{k} {a}→{b}" for k, (a, b) in diff.items())
                                         if diff else "mặc định (chưa đổi lần nào)")]
        reports = cal.last_reports(10)
        for row in reversed(reports):
            acc = row.get("accepted")
            who = cal.STRATEGIES.get(row.get("strategy", "engine"), "")
            lines.append(f"- {row['as_of']} {who}: " + (f"ĐỔI {acc['name']}" if acc else f"giữ nguyên. {row.get('note', '')}"))
        if not reports:
            lines.append("Chưa chạy hiệu chỉnh lần nào.")
        return "\n".join(lines)

    # ── nightly ──
    def calibrate(self, day: str) -> str:
        from tradingagents.dataflows import money_flows as mf
        from tradingagents.trading import calibrate as cal, simulator as sim

        symbols = list(dict.fromkeys(self.universe() + mf.index_members("VN30")))
        data = sim.build_dataset(symbols, cal.TRAIN_SESSIONS + cal.HOLDOUT_SESSIONS, day)
        out = []
        for strategy in ("engine", "momentum"):
            if strategy == "momentum":
                members = set(mf.index_members("VN30"))
                data.symbols = [s for s in data.symbols if s in members]
                data.bars = {s: b for s, b in data.bars.items() if s in members}
            out.append("⚙️ " + cal.calibrate(symbols, day, apply=True, data=data, strategy=strategy).summary())
        return "\n\n".join(out)
