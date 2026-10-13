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


def _estimated_nav(portfolio, price: float) -> float | None:
    if portfolio is None or getattr(portfolio, "cash", None) is None:
        return None
    nav = float(portfolio.cash)
    for position in portfolio.positions:
        if position.average_price:
            nav += float(position.quantity) * float(position.average_price)
    return nav if nav > 0 else None


def analyze(symbol: str, as_of_date: str, *, portfolio=None) -> QuantResult:
    """Run the full deterministic pipeline for ``symbol`` at ``as_of_date``.

    Raises NoMarketDataError when the stock itself has no usable prices; every
    other input (VNINDEX, news, sector, exchange) degrades gracefully.
    """
    code = normalize_symbol(symbol)
    if vn_rules.is_index(code):
        raise NoMarketDataError(symbol, code, "an index has no trading decision; analyse a stock")
    frame = _series(code, as_of_date)
    closes, highs, lows, volumes, dates = _lists(frame)
    try:
        vni = _series(vn_rules.BENCHMARK, as_of_date)
        vni_closes, vni_highs, vni_lows, vni_volumes, _ = _lists(vni)
    except VendorError as exc:
        logger.warning("VNINDEX unavailable for %s: %s", as_of_date, exc)
        vni_closes = vni_highs = vni_lows = vni_volumes = []
    exchange = exchange_of(code)
    held = portfolio.position_in(code) if portfolio is not None else None
    is_holding = bool(held and held.quantity > 0)

    audit = price_adjust.audit_series(code, frame.attrs.get("source", ""), closes, dates)
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
    news_impact, news = _news_impact(code, as_of_date)
    stats = feat.calc_signal_stats(closes, volumes, price)
    relative_strength = feat.calc_relative_strength(closes, vni_closes, lookback=65) if vni_closes else 0.0
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

    decision = policy.evaluate_policy(policy.PolicyInputs(
        price=price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment,
        support_resistance=support_resistance, liquidity=liquidity, session=session,
        relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact,
        quality=quality, vnindex_multi_tf=vni_multi_tf, vnindex_adx=vni_adx,
        vnindex_distribution_days=vni_distribution, key_levels=key_levels, exchange=exchange,
        is_holding=is_holding, unadjusted_gap=window_gap is not None,
    ))

    ceiling, floor = vn_rules.ceiling_floor(price, exchange) if exchange else (None, None)
    plan = decision.trade_plan
    shares = None
    nav = _estimated_nav(portfolio, price)
    if plan is not None and nav:
        shares = vn_rules.shares_for(nav, plan.position_size_pct, plan.entry_high)
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
        adjustment_note=audit.note, held=held,
        live_quote_line=_live_quote_line(code, as_of_date, price),
    )
    return result


# ─── Rendering ───────────────────────────────────────────────────────────────


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
