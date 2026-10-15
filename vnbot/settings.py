"""Settings of the Botkeep app, read once from the environment.

Imported before ``tradingagents`` so the framework's paths land under the app's
data directory (the only writable place a small host guarantees) and its
config uses the provider chain, Vietnamese output and the VN data vendors.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# A session's daily bar is final from 15:00 (end of the put-through window); the
# providers publish it within minutes. The trading bot and the calibration never
# run before this, so they only ever see final bars (see DailyScheduler).
MIN_TRADE_TIME = "15:05"
DEFAULT_TRADE_TIME = "15:30"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _hhmm(name: str, default: str) -> str:
    """HH:MM with two-digit hours ("9:00" -> "09:00"), so times compare as text."""
    raw = (os.environ.get(name) or "").strip()
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{hour:02d}:{minute:02d}"
    except ValueError:
        pass
    return default


def _trade_time(report_time: str) -> str:
    """When the trading bot settles the day: TRADE_TIME, else the report time when it is
    after the close, else 15:30. Never before MIN_TRADE_TIME: before the open today's
    bar does not exist yet and during the session it is still moving, so a run then
    would mistake every day for a holiday or every stock for suspended."""
    raw = (os.environ.get("TRADE_TIME") or "").strip()
    if raw:
        value = _hhmm("TRADE_TIME", "")
        if not value:
            logger.warning("TRADE_TIME=%r is not HH:MM; using %s", raw, DEFAULT_TRADE_TIME)
            value = DEFAULT_TRADE_TIME
    else:
        value = report_time if report_time >= MIN_TRADE_TIME else DEFAULT_TRADE_TIME
    if value < MIN_TRADE_TIME:
        logger.warning("TRADE_TIME %s is before the day's bar is final; the bot trades at %s instead",
                       value, MIN_TRADE_TIME)
        value = MIN_TRADE_TIME
    return value


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "co", "có")


def _choice(name: str, options: tuple[str, ...], default: str) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in options else default


def _ids(name: str) -> frozenset[int]:
    out = set()
    for part in (os.environ.get(name) or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int
    web_password: str
    public_url: str
    telegram_token: str
    telegram_allowed_ids: frozenset[int]
    telegram_report_chat_ids: frozenset[int]
    watchlist_default: tuple[str, ...]
    daily_report_time: str          # HH:MM, Vietnam time, weekdays: the watchlist analyses
    cache_max_age_days: int
    quick_model: str
    deep_model: str
    debate_rounds: int
    risk_rounds: int
    max_tool_rounds: int
    keep_reports: int
    max_pending: int = 10           # web/Telegram requests allowed to wait at once
    trade_mode: str = "paper"       # paper | signal | off
    paper_capital: float = 1_000_000_000.0
    universe_vn30: bool = True      # trade VN30 (plus the watchlist)
    calibrate: bool = True          # nightly self-calibration of the thresholds
    signal_strategy: str = "momentum"   # which paper strategy the real signals follow
    trade_time: str = DEFAULT_TRADE_TIME  # HH:MM, Vietnam time, >= MIN_TRADE_TIME: bot + calibration
    extra: dict = field(default_factory=dict)


def load() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR") or "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # The framework's own paths, unless the operator set them.
    os.environ.setdefault("TRADINGAGENTS_RESULTS_DIR", str(data_dir / "results"))
    os.environ.setdefault("TRADINGAGENTS_CACHE_DIR", str(data_dir / "cache"))
    os.environ.setdefault("TRADINGAGENTS_MEMORY_LOG_PATH", str(data_dir / "memory" / "trading_memory.md"))
    os.environ.setdefault("TRADINGAGENTS_OUTPUT_LANGUAGE", "Vietnamese")
    allowed = _ids("TELEGRAM_ALLOWED_IDS") or _ids("ALLOWED_USER_ID")
    report_time = _hhmm("DAILY_REPORT_TIME", "15:30")
    watch = tuple(
        s.strip().upper() for s in (os.environ.get("WATCHLIST") or "").split(",") if s.strip()
    )
    return Settings(
        data_dir=data_dir,
        port=_int("SERVER_PORT", _int("PORT", 8080)),
        web_password=os.environ.get("WEB_PASSWORD", ""),
        public_url=(os.environ.get("PUBLIC_URL") or "").rstrip("/"),
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        telegram_allowed_ids=allowed,
        telegram_report_chat_ids=_ids("TELEGRAM_REPORT_CHAT_IDS") or allowed,
        watchlist_default=watch,
        daily_report_time=report_time,
        cache_max_age_days=_int("CACHE_MAX_AGE_DAYS", 3),
        quick_model=os.environ.get("QUICK_MODEL") or os.environ.get("ROUTER9_MODEL") or "default",
        deep_model=os.environ.get("DEEP_MODEL") or os.environ.get("ROUTER9_MODEL") or "default",
        debate_rounds=_int("MAX_DEBATE_ROUNDS", 1),
        risk_rounds=_int("MAX_RISK_ROUNDS", 1),
        max_tool_rounds=_int("MAX_TOOL_ROUNDS", 8),
        keep_reports=_int("KEEP_REPORTS", 60),
        max_pending=max(1, _int("MAX_PENDING", 10)),
        trade_mode=_choice("TRADE_MODE", ("paper", "signal", "off"), "paper"),
        paper_capital=float(max(_int("PAPER_CAPITAL", 1_000_000_000), 10_000_000)),
        universe_vn30=_flag("UNIVERSE_VN30", True),
        calibrate=_flag("AUTO_CALIBRATE", True),
        signal_strategy=_choice("SIGNAL_STRATEGY", ("momentum", "engine"), "momentum"),
        trade_time=_trade_time(report_time),
    )


def graph_config(settings: Settings) -> dict:
    """The framework config for this host: the provider chain on both tiers."""
    from tradingagents.default_config import build_default_config

    config = build_default_config()
    config.update(
        llm_provider="router9_chain",
        quick_think_provider=None,
        deep_think_provider=None,
        quick_think_llm=settings.quick_model,
        deep_think_llm=settings.deep_model,
        backend_url=None,
        output_language=os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE", "Vietnamese"),
        max_debate_rounds=settings.debate_rounds,
        max_risk_discuss_rounds=settings.risk_rounds,
        max_tool_rounds=settings.max_tool_rounds,
        checkpoint_enabled=False,
    )
    return config
