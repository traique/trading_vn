"""Deterministic analysis of one Vietnamese stock at one date - the system's decision.

Port of lananh-main ``stock/analysis.build_context`` for a point-in-time run.
Python decides everything numeric here: the action (BUY / HOLD / WATCH / SELL
/ NO_TRADE), entry zone, stop, targets, risk/reward, position size and
scenarios. The LLM agents downstream only explain, debate and stress-test this
decision; they may not change it (see ``agents/output_guard.py``).

Every input is read as of the run date: price bars up to the last closed
session on or before it, VNINDEX the same, news inside the 30 days before it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.ohlcv import load_ohlcv
from tradingagents.dataflows.symbols import exchange_of, normalize_symbol
from tradingagents.market import vn_rules
from tradingagents.quant import (
    features as feat,
    policy,
    price_adjust,
    report_format as rfmt,
    sector,
    validation,
)

logger = logging.getLogger(__name__)

HISTORY_BARS = 260
NEWS_LOOKBACK_DAYS = 30

ACTION_LABEL_VI = {
    "BUY": "MUA",
    "HOLD": "GIỮ",
    "WATCH": "THEO DÕI",
    "SELL": "BÁN (thoát vị thế)",
    "NO_TRADE": "ĐỨNG NGOÀI",
}
# Labels a narrative may use without contradicting the system action: WATCH and
# NO_TRADE both mean "no new position", HOLD usually comes with "watch".
COMPATIBLE_ACTIONS = {
    "BUY": frozenset({"BUY"}),
    "SELL": frozenset({"SELL"}),
    "HOLD": frozenset({"HOLD", "WATCH"}),
    "WATCH": frozenset({"WATCH", "NO_TRADE"}),
    "NO_TRADE": frozenset({"NO_TRADE", "WATCH"}),
}


@dataclass
class QuantResult:
    """The system decision and the report the agents read. Plain data, checkpoint-safe."""

    symbol: str
    as_of_date: str
    last_bar_date: str
    price: float
    exchange: str | None
    action: str
    confidence: float
    setup_type: str
    market_regime: str
    risk_level: str
    data_quality: str
    reasons: list[str]
    stop_price: float | None
    target_price: float | None
    rr_ratio: float | None
    invalidation_reason: str | None
    entry_low: float | None = None
    entry_high: float | None = None
    target2: float | None = None
    position_size_pct: float | None = None
    suggested_shares: int | None = None
    ceiling: float | None = None
    floor: float | None = None
    is_holding: bool = False
    report: str = ""
    deterministic_summary: str = ""
    scenarios: list[dict] = field(default_factory=list)

    def to_state(self) -> dict:
        return asdict(self)


def _series(symbol: str, as_of_date: str) -> pd.DataFrame:
    return load_ohlcv(symbol, as_of_date).tail(HISTORY_BARS).reset_index(drop=True)


def _lists(frame: pd.DataFrame) -> tuple[list[float], list[float], list[float], list[float], list[str]]:
    return (
        [float(v) for v in frame["Close"]],
        [float(v) for v in frame["High"]],
        [float(v) for v in frame["Low"]],
        [float(v) for v in frame["Volume"]],
        [d.strftime("%Y-%m-%d") for d in pd.to_datetime(frame["Date"])],
    )


def _align_to_benchmark(
    closes: list[float], dates: list[str], vni_closes: list[float], vni_dates: list[str]
) -> tuple[list[float], list[float]]:
    """The stock's closes paired by DATE with the VNINDEX close of the same session.

    Pairing the two tails by array position compares different sessions when
    the stock was suspended, listed later, or one source misses a day; each
    stock bar takes the last VNINDEX close on or before its date instead, and
    bars older than the VNINDEX series are dropped.
    """
    if not closes or not vni_closes:
        return closes, []
    ends = feat.benchmark_end_indices(dates, vni_dates, len(closes), len(vni_closes))
    pairs = [(c, vni_closes[e - 1]) for c, e in zip(closes, ends, strict=True) if e > 0]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _news_impact(symbol: str, as_of_date: str) -> tuple[float, list]:
    from tradingagents.dataflows.news import collect_company_news, sentiment_score

    start = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        items, _ = collect_company_news(symbol, start, as_of_date, limit=8)
    except Exception as exc:  # noqa: BLE001 - news only nudges a projected target
        logger.warning("News unavailable for %s: %s", symbol, exc)
        return 0.0, []
    scored = [(n.title, sentiment_score(n.title), n.confirmed) for n in items]
    return rfmt.relevant_news_impact(scored, symbol), items


def _live_quote_line(symbol: str, as_of_date: str, price: float) -> str | None:
    """The last matched price during today's session, shown for reference only."""
    now = vn_rules.now_vn()
    if as_of_date != now.strftime("%Y-%m-%d") or not vn_rules.is_market_hours(now):
        return None
    from tradingagents.dataflows.news import get_live_match_price

    live = get_live_match_price(symbol, as_of_date)
    if not live:
        return None
    diff = live - price
    return (
        f"Giá khớp gần nhất trong phiên hôm nay: {rfmt.fmt_price(live)} VND, chênh "
        f"{'+' if diff > 0 else ''}{rfmt.fmt_price(diff)} VND ({rfmt.fmt_signed_pct(diff / price * 100)}) "
        "so với giá dùng để tính toán - CHỈ để tham khảo, KHÔNG dùng cho stop/target/R:R."
    )


def _estimated_nav(portfolio, price: float, symbol: str | None = None) -> float | None:
    """Cash plus holdings: the analysed symbol at its current close ``price``,
    other positions at cost (the only price the portfolio carries for them)."""
    if portfolio is None or getattr(portfolio, "cash", None) is None:
        return None
    nav = float(portfolio.cash)
    held = portfolio.position_in(symbol) if symbol and hasattr(portfolio, "position_in") else None
    for position in portfolio.positions:
        if held is not None and position is held and price > 0:
            nav += float(position.quantity) * float(price)
        elif position.average_price:
            nav += float(position.quantity) * float(position.average_price)
    return nav if nav > 0 else None


def _suggested_shares(portfolio, nav: float | None, position_pct: float, entry_high: float) -> int | None:
    """Board-lot shares for the plan's % NAV, capped by the free cash: a VN cash
    account cannot buy more than its cash at the top of the entry zone."""
    if not nav:
        return None
    shares = vn_rules.shares_for(nav, position_pct, entry_high)
    cash = getattr(portfolio, "cash", None)
    if cash is not None and entry_high > 0:
        shares = min(shares, vn_rules.round_lot(max(float(cash), 0.0) / entry_high))
    return shares


@dataclass
class Prepared:
    """Everything the policy needs for one stock at one date, and what the report shows beside it."""

    inputs: policy.PolicyInputs
    closes: list[float]
    dates: list[str]
    stats: feat.SignalStats
    enhanced: feat.EnhancedIndicators | None
    ma_alignment: feat.MAAlignment | None
    support_resistance: feat.SupportResistance | None
    key_levels: feat.KeyLevels
    liquidity: feat.Liquidity | None
    relative_strength: float
    vni_closes: list[float]
    adjustment_note: str | None


def build_inputs(
    code: str,
    as_of_date: str,
    frame: pd.DataFrame,
    vni: pd.DataFrame | None,
    *,
    exchange: str | None,
    source: str = "",
    news_impact: float = 0.0,
    is_holding: bool = False,
    stock_flows=None,
    market_flows=None,
    etf_member: bool = False,
    params=None,
) -> Prepared:
    """The policy inputs from bars already cut at ``as_of_date`` - no network.

    Shared by the live analysis and the backtest/calibration, so both decide
    on exactly the same features.
    """
    from tradingagents.quant.params import active as active_params

    frame = frame.tail(HISTORY_BARS).reset_index(drop=True)
    closes, highs, lows, volumes, dates = _lists(frame)
    if vni is not None and not vni.empty:
        vni = vni.tail(HISTORY_BARS).reset_index(drop=True)
        vni_closes, vni_highs, vni_lows, vni_volumes, vni_dates = _lists(vni)
    else:
        vni_closes = vni_highs = vni_lows = vni_volumes = vni_dates = []

    audit = price_adjust.audit_series(code, source, closes, dates, exchange=exchange)
    window_gap = price_adjust.last_gap_in_window(audit.gaps, len(closes))
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d").replace(hour=15, tzinfo=vn_rules.VN_TZ)
    quality = validation.validate_ohlcv(closes, highs, lows, volumes, dates, now=as_of_dt)
    if window_gap is not None and quality.usable:
        gap_reason = (
            f"chuỗi giá có gap {rfmt.fmt_signed_pct(window_gap.move_pct)} ngày {window_gap.date or 'không rõ'}"
            + (f" (khớp {window_gap.ratio_hint})" if window_gap.ratio_hint else "")
            + " nghi ngày không hưởng quyền CHƯA điều chỉnh"
        )
        quality = replace(quality, status="degraded", reasons=[*quality.reasons, gap_reason])

    price = closes[-1]
    stats = feat.calc_signal_stats(closes, volumes, price)
    rs_closes, rs_vni = _align_to_benchmark(closes, dates, vni_closes, vni_dates)
    relative_strength = feat.calc_relative_strength(rs_closes, rs_vni, lookback=65) if rs_vni else 0.0
    enhanced = feat.build_enhanced_indicators(closes, price, highs, lows) if quality.usable and len(closes) >= 20 else None

    level_start = window_gap.index if window_gap is not None else 0
    level_highs, level_lows, level_closes = highs[level_start:], lows[level_start:], closes[level_start:]
    ma_alignment = feat.calc_ma_alignment(closes) if len(closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(level_highs, level_lows, price, 30) if level_highs else None
    key_levels = feat.find_key_levels(level_highs, level_lows, level_closes) if level_highs else feat.KeyLevels([], [])
    liquidity = feat.calc_liquidity(volumes)
    session = feat.calc_session_metrics(closes, highs, lows, volumes)
    trend_score = (
        feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram)
        if ma_alignment and ma_alignment.alignment != "unknown" and enhanced
        else None
    )
    vni_multi_tf = feat.calc_multi_timeframe(vni_closes) if vni_closes else None
    vni_adx = feat.calc_adx(vni_closes, vni_highs, vni_lows) if vni_closes else None
    vni_distribution = feat.calc_distribution_days(vni_closes, vni_volumes) if vni_closes else 0
    vni_dist_events = feat.distribution_events(vni_closes, vni_volumes) if vni_closes else None

    inputs = policy.PolicyInputs(
        price=price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment,
        support_resistance=support_resistance, liquidity=liquidity, session=session,
        relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact,
        quality=quality, vnindex_multi_tf=vni_multi_tf, vnindex_adx=vni_adx,
        vnindex_distribution_days=vni_distribution, key_levels=key_levels, exchange=exchange,
        is_holding=is_holding, unadjusted_gap=window_gap is not None,
        stock_flows=stock_flows, market_flows=market_flows, etf_member=etf_member,
        vnindex_dist_events=vni_dist_events,
        params=params or active_params(),
    )
    return Prepared(
        inputs=inputs, closes=closes, dates=dates, stats=stats, enhanced=enhanced,
        ma_alignment=ma_alignment, support_resistance=support_resistance, key_levels=key_levels,
        liquidity=liquidity, relative_strength=relative_strength, vni_closes=vni_closes,
        adjustment_note=audit.note,
    )


def load_flow_signals(code: str, as_of_date: str, vni_closes: list[float]):
    """(stock flows, market flows, VN30 member) as of the date; Nones when unavailable."""
    from tradingagents.dataflows import money_flows as mf
    from tradingagents.quant import flow_features as ff

    stock = market = None
    member = False
    try:
        stock = ff.stock_flows(mf.load_stock_flows(code, as_of_date), as_of_date)
    except Exception as exc:  # noqa: BLE001 - flows only adjust; the decision stands without them
        logger.warning("Stock flows unavailable for %s: %s", code, exc)
    try:
        market = ff.market_flows(
            mf.load_market_flows(as_of_date), mf.load_etf_flows(as_of_date),
            mf.load_breadth(as_of_date), vni_closes, as_of_date,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Market flows unavailable for %s: %s", as_of_date, exc)
    try:
        member = code in mf.index_members("VN30")
    except Exception:  # noqa: BLE001
        member = False
    return stock, market, member


def analyze(symbol: str, as_of_date: str, *, portfolio=None, params=None, flows: bool = True) -> QuantResult:
    """Run the full deterministic pipeline for ``symbol`` at ``as_of_date``.

    Raises NoMarketDataError when the stock itself has no usable prices; every
    other input (VNINDEX, money flows, news, sector, exchange) degrades gracefully.
    """
    code = normalize_symbol(symbol)
    if vn_rules.is_index(code):
        raise NoMarketDataError(symbol, code, "an index has no trading decision; analyse a stock")
    frame = _series(code, as_of_date)
    try:
        vni = _series(vn_rules.BENCHMARK, as_of_date)
    except VendorError as exc:
        logger.warning("VNINDEX unavailable for %s: %s", as_of_date, exc)
        vni = None
    exchange = exchange_of(code)
    held = portfolio.position_in(code) if portfolio is not None else None
    is_holding = bool(held and held.quantity > 0)
    news_impact, news = _news_impact(code, as_of_date)
    vni_list = [float(c) for c in vni["Close"]] if vni is not None else []
    stock_flows, market_flows, member = load_flow_signals(code, as_of_date, vni_list) if flows else (None, None, False)

    prep = build_inputs(
        code, as_of_date, frame, vni, exchange=exchange, source=frame.attrs.get("source", ""),
        news_impact=news_impact, is_holding=is_holding, stock_flows=stock_flows,
        market_flows=market_flows, etf_member=member, params=params,
    )
    decision = policy.evaluate_policy(prep.inputs)
    price, dates, stats, enhanced = prep.closes[-1], prep.dates, prep.stats, prep.enhanced
    ma_alignment, support_resistance, key_levels = prep.ma_alignment, prep.support_resistance, prep.key_levels
    liquidity, relative_strength, vni_closes = prep.liquidity, prep.relative_strength, prep.vni_closes

    ceiling, floor = vn_rules.ceiling_floor(price, exchange) if exchange else (None, None)
    plan = decision.trade_plan
    shares = None
    if plan is not None:
        shares = _suggested_shares(portfolio, _estimated_nav(portfolio, price, code), plan.position_size_pct, plan.entry_high)
    sector_perf = sector.sector_performance(code, as_of_date, vni_closes)

    result = QuantResult(
        symbol=code, as_of_date=as_of_date, last_bar_date=dates[-1], price=price, exchange=exchange,
        action=decision.action, confidence=decision.confidence, setup_type=decision.setup_type,
        market_regime=decision.market_regime, risk_level=decision.risk_level,
        data_quality=decision.data_quality, reasons=list(decision.reasons),
        stop_price=decision.stop_price, target_price=decision.target_price, rr_ratio=decision.rr_ratio,
        invalidation_reason=decision.invalidation_reason,
        entry_low=plan.entry_low if plan else None, entry_high=plan.entry_high if plan else None,
        target2=plan.target2 if plan else None,
        position_size_pct=plan.position_size_pct if plan else None,
        suggested_shares=shares, ceiling=ceiling, floor=floor, is_holding=is_holding,
        scenarios=[{"name": s.name, "trigger": s.trigger, "action": s.action} for s in decision.scenarios],
    )
    result.deterministic_summary = render_summary(result, stats)
    result.report = render_report(
        result, decision=decision, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment,
        support_resistance=support_resistance, key_levels=key_levels, liquidity=liquidity,
        relative_strength=relative_strength, sector_perf=sector_perf, news=news,
        adjustment_note=prep.adjustment_note, held=held,
        live_quote_line=_live_quote_line(code, as_of_date, price),
        stock_flows=stock_flows, market_flows=market_flows,
    )
    return result


# ─── Rendering ───────────────────────────────────────────────────────────────


def flow_lines(stock_flows, market_flows) -> list[str]:
    """Money-flow facts for the report, each number as the system measured it."""
    from tradingagents.quant.flow_features import fmt_bn

    lines = []
    f = stock_flows
    if f is not None:
        streak = (f"mua ròng {f.foreign_streak} phiên liên tiếp" if f.foreign_streak > 0
                  else f"bán ròng {-f.foreign_streak} phiên liên tiếp" if f.foreign_streak < 0 else "không có chuỗi")
        ratio = f" ({f.foreign_ratio_20:+.1f}% giá trị khớp)" if f.foreign_ratio_20 is not None else ""
        lines.append(f"Khối ngoại 20 phiên: {fmt_bn(f.foreign_net_20)}{ratio}; {streak}; "
                     f"bán ròng {f.foreign_sell_days_10}/10 phiên gần nhất"
                     + (f"; room còn {f.room_pct:.2f}%" if f.room_pct is not None else ""))
        if f.prop_ratio_10 is not None:
            lines.append(f"Tự doanh 10 phiên: {f.prop_ratio_10:+.1f}% giá trị khớp")
    else:
        lines.append("Khối ngoại/tự doanh của mã: chưa có dữ liệu.")
    m = market_flows
    if m is not None:
        parts = []
        if m.foreign_net_20 is not None:
            parts.append(f"khối ngoại toàn thị trường 20 phiên {fmt_bn(m.foreign_net_20)} "
                         f"(bán ròng {m.foreign_sell_days_15}/15 phiên)")
        if m.prop_net_20 is not None:
            parts.append(f"tự doanh 20 phiên {fmt_bn(m.prop_net_20)}")
        if m.etf_net_20 is not None:
            parts.append(f"ETF nội 20 phiên {fmt_bn(m.etf_net_20)}")
        if m.adv_ratio_10 is not None:
            parts.append(f"tỷ lệ mã tăng 10 phiên HOSE {m.adv_ratio_10:.0f}%")
        if m.pct_above_ma50 is not None:
            parts.append(f"{m.pct_above_ma50:.0f}% mã trên MA50")
        if m.breadth_divergence:
            parts.append("PHÂN KỲ: VNINDEX đỉnh 20 phiên nhưng độ rộng yếu")
        lines.append("Thị trường: " + "; ".join(parts))
    else:
        lines.append("Dòng tiền toàn thị trường: chưa có dữ liệu.")
    return lines




def _p(value: float | None) -> str:
    return rfmt.fmt_price(value) if value is not None else "N/A"


def render_summary(r: QuantResult, stats: feat.SignalStats | None = None) -> str:
    """The short, rule-based decision block - the fallback whenever a narrative is dropped."""
    label = ACTION_LABEL_VI.get(r.action, r.action)
    if r.action == "BUY" and r.entry_low is not None:
        plan = (
            f"Vùng mua {_p(r.entry_low)}-{_p(r.entry_high)} | Tỷ trọng tham khảo "
            f"{rfmt.fmt_number(r.position_size_pct, 1)}% NAV"
            + (f" (~{rfmt.fmt_price(r.suggested_shares)} cp, lô 100)" if r.suggested_shares else "")
            + f" | Stop {_p(r.stop_price)} | T1 {_p(r.target_price)}"
            + (f" | T2 {_p(r.target2)}" if r.target2 else "")
            + f" | R:R ~{rfmt.fmt_number(r.rr_ratio)} (tại giá mua cao nhất của vùng)"
        )
    elif r.action == "SELL":
        plan = (
            f"Không phải vùng mua. Đang giữ: tín hiệu nghiêng về giảm/thoát vị thế theo kỷ luật rủi ro; "
            f"tín hiệu vô hiệu nếu giá đóng cửa vượt {_p(r.stop_price)}"
            + (f"; hỗ trợ tham chiếu {rfmt.format_level(r.price, r.target_price)}" if r.target_price else "")
        )
    elif r.action == "HOLD" and r.target_price is not None and r.stop_price is not None:
        plan = (
            f"Đang giữ, tín hiệu còn thuận lợi | Tham khảo chốt lời {_p(r.target_price)} | "
            f"Cân nhắc cắt lỗ nếu đóng cửa dưới {_p(r.stop_price)} | R:R ~{rfmt.fmt_number(r.rr_ratio)}"
        )
    elif r.action == "NO_TRADE":
        plan = "Hệ thống chưa đủ lợi thế để đề xuất vùng giá - ưu tiên đứng ngoài quan sát."
    else:
        plan = "Chưa đủ rõ xu hướng để đề xuất vùng giá cụ thể - theo dõi thêm."
    lines = [
        f"{r.symbol} - {_p(r.price)} VND (đóng cửa phiên {r.last_bar_date})",
        f"Tín hiệu hệ thống: {label} ({r.action}) | confidence {rfmt.fmt_number(r.confidence)} | "
        f"setup {r.setup_type} | VNINDEX {r.market_regime} | rủi ro {r.risk_level} | dữ liệu {r.data_quality}",
        plan,
    ]
    if stats is not None:
        lines.append(
            f"RSI14 {rfmt.fmt_number(stats.rsi14, 1) if stats.rsi14 is not None else 'chưa đủ dữ liệu'} | "
            f"Trend ~3 tháng {rfmt.fmt_signed_pct(stats.trend_3m)}"
        )
    if r.reasons:
        lines.append("Lý do: " + "; ".join(r.reasons[:4]))
    if r.invalidation_reason:
        lines.append(f"Điều kiện vô hiệu/theo dõi: {r.invalidation_reason}")
    return "\n".join(lines)


def render_summary_from_state(decision: dict) -> str:
    """``render_summary`` for a decision read back from graph state, however partial."""
    if decision.get("price"):
        fields = {k: v for k, v in decision.items() if k in QuantResult.__dataclass_fields__}
        try:
            return render_summary(QuantResult(**fields))
        except TypeError:
            pass
    reasons = "; ".join(decision.get("reasons") or []) or "không có dữ liệu"
    return f"Tín hiệu hệ thống: {decision.get('action') or 'NO_TRADE'} - {reasons}"


def render_report(
    r: QuantResult, *, decision, stats, enhanced, ma_alignment, support_resistance, key_levels,
    liquidity, relative_strength, sector_perf, news, adjustment_note, held, live_quote_line,
    stock_flows=None, market_flows=None,
) -> str:
    """Everything the agents may cite, with every price level and its % distance pre-computed."""
    price = r.price
    lines = [
        f"=== QUYẾT ĐỊNH HỆ THỐNG ĐỊNH LƯỢNG - {r.symbol} (sàn {r.exchange or 'không rõ'}) ===",
        f"Ngày phân tích: {r.as_of_date}. Giá dùng để tính toán (đóng cửa phiên {r.last_bar_date}): {_p(price)} VND.",
        f"Toàn bộ chỉ báo bên dưới tính trên NẾN ĐÓNG CỬA ngày {r.last_bar_date}, không phải giá đang khớp.",
    ]
    if r.ceiling is not None:
        lines.append(
            f"Biên độ phiên kế tiếp (giá tham chiếu {_p(price)}): trần {_p(r.ceiling)}, sàn {_p(r.floor)} "
            f"(±{vn_rules.band_pct(r.exchange):g}% sàn {r.exchange})."
        )
    if live_quote_line:
        lines.append(live_quote_line)
    if adjustment_note:
        lines.append(adjustment_note)
    if held is not None:
        avg = f", giá vốn {_p(held.average_price)}" if held.average_price else ""
        pnl = ""
        if held.average_price:
            pnl = f", lãi/lỗ tạm tính {rfmt.fmt_signed_pct((price - held.average_price) / held.average_price * 100)}"
        lines.append(f"Vị thế đang nắm giữ: {rfmt.fmt_price(held.quantity)} cp{avg}{pnl} (do hệ thống tính).")

    lines += ["", f"Action hệ thống: {r.action} ({ACTION_LABEL_VI.get(r.action, r.action)}) - ĐÃ CHỐT, không agent nào được đổi."]
    lines.append(
        f"Confidence {rfmt.fmt_number(r.confidence)} | Setup {r.setup_type} | Regime VNINDEX {r.market_regime} | "
        f"Rủi ro {r.risk_level} | Chất lượng dữ liệu {r.data_quality}"
    )
    if r.reasons:
        lines.append("Lý do hệ thống: " + "; ".join(r.reasons[:6]))
    if r.invalidation_reason:
        lines.append(f"Điều kiện vô hiệu / theo dõi tiếp: {r.invalidation_reason}")

    lines += ["", "--- Kế hoạch định lượng (gắn với action hệ thống) ---"]
    if r.entry_low is not None:
        lines.append(
            f"Vùng mua {rfmt.format_level(price, r.entry_low)} - {rfmt.format_level(price, r.entry_high)} | "
            f"Stop {rfmt.format_level(price, r.stop_price)} | T1 {rfmt.format_level(price, r.target_price)}"
            + (f" | T2 {rfmt.format_level(price, r.target2)}" if r.target2 else "")
        )
        lines.append(
            f"R:R ~{rfmt.fmt_number(r.rr_ratio)} (tính tại giá mua cao nhất của vùng) | Tỷ trọng tham khảo "
            f"{rfmt.fmt_number(r.position_size_pct, 1)}% NAV (rủi ro tối đa 1% NAV/lệnh)"
            + (f" ≈ {rfmt.fmt_price(r.suggested_shares)} cổ phiếu (làm tròn lô 100)" if r.suggested_shares else "")
        )
        lines.append(
            "Quy tắc: chốt 1/2 vị thế tại T1, dời stop về hoà vốn cho phần còn lại. Hàng mua về T+2,5 "
            "nên không thể cắt lỗ trước phiên thứ 3."
        )
        for s in r.scenarios:
            lines.append(f"- {s['name'].upper()}: nếu {s['trigger']} -> {s['action']}")
    elif r.action == "SELL" and r.stop_price is not None:
        lines.append(
            f"Tín hiệu SELL (chỉ áp dụng cho vị thế đang giữ - không bán khống): vô hiệu nếu đóng cửa vượt "
            f"{rfmt.format_level(price, r.stop_price)}"
            + (f"; hỗ trợ tham chiếu {rfmt.format_level(price, r.target_price)} (không phải mức chờ để bán)" if r.target_price else "")
        )
    elif r.action == "HOLD" and r.stop_price is not None and r.target_price is not None:
        lines.append(
            f"Đang giữ: tham khảo chốt lời {rfmt.format_level(price, r.target_price)}, cắt lỗ nếu đóng cửa dưới "
            f"{rfmt.format_level(price, r.stop_price)}, R:R ~{rfmt.fmt_number(r.rr_ratio)}"
        )
    else:
        lines.append(
            f"Hệ thống KHÔNG đưa vùng giá, stop, target, R:R hay tỷ trọng cho action {r.action}. "
            "Không agent nào được tự đặt ra các con số này."
        )

    lines += ["", "--- Bức tranh kỹ thuật ---"]
    lines.append(
        f"Trend ~3 tháng {rfmt.fmt_signed_pct(stats.trend_3m)}, momentum {rfmt.fmt_number(stats.momentum)}, "
        f"volume trend {rfmt.fmt_signed_pct(stats.volume_trend)}, biến động {rfmt.fmt_pct(stats.volatility)}, "
        f"RSI14 {rfmt.fmt_number(stats.rsi14, 1) if stats.rsi14 is not None else 'chưa đủ dữ liệu'}, "
        f"sức mạnh tương đối vs VNINDEX (65 phiên) {rfmt.fmt_signed_pct(relative_strength)}"
    )
    if enhanced is not None:
        lines.append(feat.build_indicator_summary(enhanced, r.symbol))
        lines.append(rfmt.macd_strength_line(enhanced.macd.macd_line, enhanced.macd.histogram, price))
        lines.append(rfmt.adx_direction_line(
            enhanced.adx.adx, enhanced.adx.di_plus, enhanced.adx.di_minus, enhanced.adx.trending,
            available=enhanced.adx.available,
        ))
        lines.append(
            f"Giá so với đường trung bình: SMA20 {rfmt.format_level(price, enhanced.sma20)}, "
            f"SMA50 {rfmt.format_level(price, enhanced.sma50)}"
            + (f", SMA200 {rfmt.format_level(price, enhanced.sma200)}" if enhanced.sma200 is not None else "")
        )
    if support_resistance and support_resistance.support:
        lines.append(
            f"Biên 30 phiên: hỗ trợ {rfmt.format_level(price, support_resistance.support)}, "
            f"kháng cự {rfmt.format_level(price, support_resistance.resistance)}"
        )
    if key_levels and (key_levels.supports or key_levels.resistances):
        parts = []
        if key_levels.supports:
            parts.append("Hỗ trợ: " + ", ".join(rfmt.format_level(price, lv.price, lv.touches) for lv in key_levels.supports[:3]))
        if key_levels.resistances:
            parts.append("Kháng cự: " + ", ".join(rfmt.format_level(price, lv.price, lv.touches) for lv in key_levels.resistances[:3]))
        lines.append("Vùng giá quan trọng (swing pivot 60 phiên): " + " | ".join(parts))
        lines.append(rfmt.nearest_levels_line(
            price,
            [(lv.price, lv.touches) for lv in key_levels.supports],
            [(lv.price, lv.touches) for lv in key_levels.resistances],
            atr_pct=enhanced.atr_pct if enhanced else None,
        ))
    if ma_alignment and ma_alignment.alignment != "unknown":
        lines.append(
            f"MA alignment: {ma_alignment.alignment} (MA5 {_p(ma_alignment.ma5)}, MA10 {_p(ma_alignment.ma10)}, "
            f"MA20 {_p(ma_alignment.ma20)})"
        )
    if liquidity:
        lines.append(
            f"Thanh khoản: KL phiên gần nhất {rfmt.fmt_price(liquidity.current_volume)} cp so với TB20 "
            f"{rfmt.fmt_price(liquidity.avg_volume_20)} ({rfmt.fmt_number(liquidity.liquidity_ratio_pct, 1)}% TB20)"
        )
        if liquidity.is_thin:
            lines.append("⚠️ CẢNH BÁO THANH KHOẢN: khối lượng trung bình quá thấp - khó vào/ra khối lượng lớn.")

    lines += ["", "--- Dòng tiền ---"]
    lines.extend(flow_lines(stock_flows, market_flows))

    lines += ["", "--- Ngành & tin tức ---"]
    if sector_perf is not None:
        vs = (
            f", so với VNINDEX {rfmt.fmt_signed_pct(sector_perf.vs_vnindex_1m)}"
            if sector_perf.vs_vnindex_1m is not None else ", chưa có dữ liệu VNINDEX để so"
        )
        lines.append(
            f"Ngành {sector_perf.label} ({sector_perf.sample} mã): 1 tháng {rfmt.fmt_signed_pct(sector_perf.trend_1m)}, "
            f"3 tháng {rfmt.fmt_signed_pct(sector_perf.trend_3m)}{vs} - trạng thái {sector_perf.momentum}"
        )
    else:
        lines.append("Ngành: chưa đủ dữ liệu ngành cho mã này.")
    if news:
        lines.append(f"Tin {NEWS_LOOKBACK_DAYS} ngày trước ngày phân tích (✅ đúng mã, ⚠️ tin chung):")
        for item in news[:5]:
            mark = "✅" if rfmt.is_news_relevant(item.title, r.symbol, item.confirmed) else "⚠️"
            day = item.published.astimezone(vn_rules.VN_TZ).strftime("%d/%m/%Y") if item.published else "không rõ ngày"
            lines.append(f"{mark} {item.title} ({item.source}, {day})")
    else:
        lines.append(f"Tin: không tìm thấy tin trong {NEWS_LOOKBACK_DAYS} ngày trước ngày phân tích.")
    return "\n".join(lines)
