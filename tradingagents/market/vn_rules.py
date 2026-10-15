"""Trading rules of the Vietnamese stock market, in one place.

Sources: HOSE/HNX trading regulations as applied on the KRX trading system
(live since 05/2025). Re-check these against the exchanges' current rules when
they change; every agent, the policy layer and the backtest read them here.

- Price band (biên độ) around the reference price: HOSE ±7%, HNX ±10%,
  UPCoM ±15%; a first listing day or a return from a 25+ session suspension
  trades in ±20% (HOSE), ±30% (HNX) or ±40% (UPCoM).
- Tick size (bước giá): HOSE 10đ below 10.000đ, 50đ below 50.000đ, else 100đ;
  HNX and UPCoM 100đ at every price.
- Board lot (lô chẵn) is 100 shares; odd lots (1-99) trade on a separate board.
- Settlement is T+2: shares bought on T reach the account in the afternoon of
  T+2, so the earliest sale is the T+2 afternoon session ("T+2,5": continuous
  afternoon or ATC), never T+2's ATO or morning. Counting the buy session as
  the first, the earliest exit is in the third session (``SETTLEMENT_BARS``),
  and only in its afternoon; open-to-close exits start one session later.
- No naked short selling of listed shares: a SELL only exits a holding.
- Sessions (HOSE): ATO 9:00-9:15, continuous 9:15-11:30, lunch 11:30-13:00,
  continuous 13:00-14:30, ATC 14:30-14:45, put-through until 15:00.
"""

from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Index codes as the price vendors name them. HNX and UPCoM composite indices
# are "HNX" / "UPCOM" on DNSE; the aliases users type map onto these.
INDEX_SYMBOLS = frozenset({"VNINDEX", "VN30", "HNX", "HNX30", "UPCOM"})
INDEX_ALIASES = {
    "VNINDEX": "VNINDEX", "VN-INDEX": "VNINDEX", "VNI": "VNINDEX", "^VNINDEX": "VNINDEX",
    "VN30": "VN30", "VN30INDEX": "VN30", "^VN30": "VN30",
    "HNX": "HNX", "HNXINDEX": "HNX", "HNX-INDEX": "HNX", "^HNX": "HNX",
    "HNX30": "HNX30", "HNX30INDEX": "HNX30",
    "UPCOM": "UPCOM", "UPCOMINDEX": "UPCOM", "UPCOM-INDEX": "UPCOM", "UPINDEX": "UPCOM",
}
BENCHMARK = "VNINDEX"

EXCHANGES = ("HOSE", "HNX", "UPCOM")
_EXCHANGE_ALIASES = {"HOSE": "HOSE", "HSX": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM", "UPX": "UPCOM"}

PRICE_BAND_PCT = {"HOSE": 7.0, "HNX": 10.0, "UPCOM": 15.0}
FIRST_DAY_BAND_PCT = {"HOSE": 20.0, "HNX": 30.0, "UPCOM": 40.0}

LOT_SIZE = 100
# T+2: shares bought on T arrive in the afternoon of the second session after T.
SETTLEMENT_SESSIONS = 2
# Daily bars from the buy bar to the first bar with sellable shares, both
# counted (T, T+1, T+2): the shortest window a trade can be realised in.
SETTLEMENT_BARS = SETTLEMENT_SESSIONS + 1

# Typical retail costs, as a percent of traded value.
BUY_FEE_PCT = 0.15
SELL_FEE_PCT = 0.15
SELL_TAX_PCT = 0.10  # personal income tax on the sale value
SLIPPAGE_PCT = 0.10  # per fill, against the trader (see ``slipped``)

# Weekdays the exchanges are closed. Every year: the fixed-date public holidays.
FIXED_HOLIDAYS = ("01-01", "04-30", "05-01", "09-02")
# Plus, by date, the lunar ones (Tết, Hùng Kings), the days off in lieu and the
# bridge days. 2026 is the closures VNINDEX really had (and 24/11, Vietnamese
# Culture Day, a holiday from 2026); 2027-2030 follow the Labour Code calendar
# (Tết from the 29th/30th of the last lunar month to the 4th/5th of the first,
# days off in lieu of a holiday on a weekend). The government adds bridge days
# year by year: add them here when HOSE announces its calendar.
MARKET_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-27", "2026-04-30", "2026-05-01", "2026-08-31", "2026-09-01", "2026-09-02", "2026-11-24",
    "2027-01-01", "2027-02-04", "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10",
    "2027-04-16", "2027-04-30", "2027-05-03", "2027-09-02", "2027-09-03", "2027-11-24",
    "2028-01-03", "2028-01-24", "2028-01-25", "2028-01-26", "2028-01-27", "2028-01-28",
    "2028-04-04", "2028-05-01", "2028-05-02", "2028-09-01", "2028-09-04", "2028-11-24",
    "2029-01-01", "2029-02-12", "2029-02-13", "2029-02-14", "2029-02-15", "2029-02-16",
    "2029-04-23", "2029-04-30", "2029-05-01", "2029-09-03", "2029-09-04", "2029-11-26",
    "2030-01-01", "2030-01-31", "2030-02-01", "2030-02-04", "2030-02-05", "2030-02-06",
    "2030-04-12", "2030-04-30", "2030-05-01", "2030-09-02", "2030-09-03", "2030-11-25",
})


def is_session_day(day: str) -> bool:
    """Whether the exchanges are expected to open on ``day`` (YYYY-MM-DD): a weekday that
    is not a known holiday. A closure the table does not know (an outage, a bridge day
    not announced yet) is not seen."""
    d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    return d.weekday() < 5 and day[5:10] not in FIXED_HOLIDAYS and day[:10] not in MARKET_HOLIDAYS


ATO_START, ATO_END = time(9, 0), time(9, 15)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
ATC_START, ATC_END = time(14, 30), time(14, 45)
PUT_THROUGH_END = time(15, 0)


def normalize_exchange(exchange: str | None) -> str | None:
    """HOSE / HNX / UPCOM, or None when unknown."""
    if not exchange:
        return None
    return _EXCHANGE_ALIASES.get(exchange.strip().upper().replace(" ", ""))


def is_index(symbol: str) -> bool:
    return symbol.strip().upper() in INDEX_ALIASES


def tick_size(price: float, exchange: str | None = None) -> int:
    """Order tick size in VND. Unknown exchange uses 100đ, valid on every board."""
    if normalize_exchange(exchange) == "HOSE":
        if price < 10_000:
            return 10
        if price < 50_000:
            return 50
        return 100
    return 100


def round_to_tick(price: float, exchange: str | None = None) -> float:
    """Nearest valid order price."""
    tick = tick_size(price, exchange)
    return round(price / tick) * tick


def floor_to_tick(price: float, exchange: str | None = None) -> float:
    """The highest valid order price at or below ``price``."""
    tick = tick_size(price, exchange)
    return float(math.floor(price / tick + 1e-9) * tick)


def ceil_to_tick(price: float, exchange: str | None = None) -> float:
    """The lowest valid order price at or above ``price``."""
    tick = tick_size(price, exchange)
    return float(math.ceil(price / tick - 1e-9) * tick)


def slipped(price: float, side: str, exchange: str | None = None) -> float:
    """``price`` moved ``SLIPPAGE_PCT`` against the trader and onto the tick grid
    in the same direction: a BUY pays more, a SELL receives less.

    An unknown exchange uses the HOSE grid (the finest; most of the basket is
    HOSE), so a cheap stock is not charged a 100đ tick it would not pay there.
    """
    ex = normalize_exchange(exchange) or "HOSE"
    if side.upper() == "BUY":
        return ceil_to_tick(price * (1 + SLIPPAGE_PCT / 100), ex)
    return floor_to_tick(price * (1 - SLIPPAGE_PCT / 100), ex)


def ceiling_floor(reference_price: float, exchange: str | None, *, first_day: bool = False) -> tuple[float, float]:
    """(ceiling, floor) prices for a session with this reference price.

    The ceiling rounds down and the floor rounds up to the tick, so both stay
    inside the band as the exchanges compute them.
    """
    ex = normalize_exchange(exchange) or "HOSE"
    band = (FIRST_DAY_BAND_PCT if first_day else PRICE_BAND_PCT)[ex] / 100
    raw_ceiling = reference_price * (1 + band)
    raw_floor = reference_price * (1 - band)
    ceiling = math.floor(raw_ceiling / tick_size(raw_ceiling, ex)) * tick_size(raw_ceiling, ex)
    floor = math.ceil(raw_floor / tick_size(raw_floor, ex)) * tick_size(raw_floor, ex)
    # A band narrower than one tick (very low prices) still moves one tick each way.
    if ceiling <= reference_price:
        ceiling = reference_price + tick_size(reference_price, ex)
    if floor >= reference_price and reference_price - tick_size(reference_price, ex) > 0:
        floor = reference_price - tick_size(reference_price, ex)
    return float(ceiling), float(floor)


def band_pct(exchange: str | None) -> float:
    """The daily band of an exchange in percent (HOSE when unknown)."""
    return PRICE_BAND_PCT[normalize_exchange(exchange) or "HOSE"]


def round_lot(shares: float) -> int:
    """Shares rounded down to a board lot of 100."""
    return max(0, int(shares // LOT_SIZE) * LOT_SIZE)


def shares_for(nav: float, position_pct: float, price: float) -> int:
    """Board-lot shares a position of ``position_pct`` % of ``nav`` buys at ``price``."""
    if nav <= 0 or position_pct <= 0 or price <= 0:
        return 0
    return round_lot(nav * position_pct / 100 / price)


def round_trip_cost_pct() -> float:
    """Fees, tax and slippage of one buy and one sell, in percent."""
    return BUY_FEE_PCT + SELL_FEE_PCT + SELL_TAX_PCT + 2 * SLIPPAGE_PCT


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def session_phase(at: datetime | None = None) -> str:
    """Which part of the trading day ``at`` falls in.

    One of: closed, pre_open, ato, continuous, lunch, atc, put_through, after_close.
    Holidays are not modelled; the price feed simply has no bar on those days.
    """
    at = (at or now_vn()).astimezone(VN_TZ)
    if at.weekday() >= 5:
        return "closed"
    t = at.time()
    if t < ATO_START:
        return "pre_open"
    if t < ATO_END:
        return "ato"
    if t < MORNING_END:
        return "continuous"
    if t < AFTERNOON_START:
        return "lunch"
    if t < ATC_START:
        return "continuous"
    if t < ATC_END:
        return "atc"
    if t < PUT_THROUGH_END:
        return "put_through"
    return "after_close"


def is_market_hours(at: datetime | None = None) -> bool:
    """Whether today's daily bar may still change (9:00-15:00 on a weekday)."""
    return session_phase(at) in {"ato", "continuous", "lunch", "atc", "put_through"}


def rules_summary(exchange: str | None = None) -> str:
    """The rules an agent must respect, as a short Vietnamese block for prompts."""
    ex = normalize_exchange(exchange)
    band = f"±{band_pct(ex):g}% (sàn {ex})" if ex else "HOSE ±7%, HNX ±10%, UPCoM ±15%"
    return (
        "Luật thị trường chứng khoán Việt Nam cần tôn trọng:\n"
        f"- Biên độ giá trong phiên: {band} quanh giá tham chiếu; giá trần/sàn không thể vượt.\n"
        "- Bước giá HOSE: 10đ (<10.000đ), 50đ (<50.000đ), 100đ (từ 50.000đ); HNX/UPCoM: 100đ.\n"
        "- Lô chẵn 100 cổ phiếu.\n"
        "- Thanh toán T+2: cổ phiếu mua phiên T về tài khoản chiều T+2, sớm nhất bán được ở "
        "phiên chiều T+2 (T+2,5: liên tục chiều hoặc ATC), không bán được ở ATO/phiên sáng T+2 - "
        "một lệnh mua đuổi sai không thể thoát ngay.\n"
        "- Không bán khống cổ phiếu: BÁN chỉ có nghĩa là thoát vị thế đang nắm giữ.\n"
        "- Phiên: ATO 9:00-9:15, liên tục 9:15-11:30 và 13:00-14:30, ATC 14:30-14:45.\n"
        "- Đơn vị giá: VND (đồng), số viết kiểu Việt Nam (70.500 là bảy mươi nghìn năm trăm đồng)."
    )
