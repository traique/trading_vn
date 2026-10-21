"""Entry point for Botkeep (or any always-on host): web page + Telegram bot.

    python main_botkeep.py

Reads its settings from the environment or a .env file beside it (see
deploy/botkeep/.env.example). Serves the web page on SERVER_PORT (Botkeep sets
it) or PORT, polls Telegram when TELEGRAM_TOKEN is set, analyses the
watchlist each weekday at DAILY_REPORT_TIME and runs the trading bot after the
close at TRADE_TIME (never before 15:05, when the day's bar is final).
"""

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from vnbot import settings as settings_module

SETTINGS = settings_module.load()   # before tradingagents: fixes its data paths

import uvicorn  # noqa: E402

from vnbot.intraday import IntradayMonitor  # noqa: E402
from vnbot.jobs import JobQueue, prune_cache  # noqa: E402
from vnbot.service import DailyScheduler, Watchlist  # noqa: E402
from vnbot.telegram_bot import TelegramBot  # noqa: E402
from vnbot.trader import TradingBot  # noqa: E402
from vnbot.web import create_app  # noqa: E402
from vnbot.zoom import ZoomChannel  # noqa: E402

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _vn_time(seconds):
    """Log timestamps in Vietnam time, whatever the host's timezone (often UTC)."""
    return datetime.fromtimestamp(seconds, VN_TZ).timetuple()


def main() -> None:
    logging.Formatter.converter = staticmethod(_vn_time)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s+07 %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from tradingagents.llm_clients.fallback_client import configured_order

    order = configured_order()
    if not order:
        logging.warning("No AI provider configured: set ROUTER9_BASE_URL+ROUTER9_API_KEY or GROQ/OPENROUTER/XAI keys")
    else:
        logging.info("AI provider chain: %s", " -> ".join(order))
    if not SETTINGS.web_password:
        logging.warning("WEB_PASSWORD is not set: the web page stays locked until it is (Telegram still works)")
    if SETTINGS.telegram_token and not SETTINGS.telegram_allowed_ids:
        logging.warning("TELEGRAM_ALLOWED_IDS is empty: the bot only answers /id until you add your id")
    logging.info("Removed %d stale cache files", prune_cache(SETTINGS))

    jobs = JobQueue(SETTINGS)
    watchlist = Watchlist(SETTINGS)
    bot = TelegramBot(SETTINGS, jobs, watchlist)
    zoom = ZoomChannel()
    if zoom.enabled:
        bot.zoom = zoom
        zoom.handler = bot.zoom_command
        zoom.alert_owner = bot.broadcast
    logging.info("Zoom: notifications %s, commands %s", "on" if zoom.notify_on else "off",
                 "on (/webhook/zoom)" if zoom.commands_on else "off")
    trader = TradingBot(SETTINGS, watchlist, notify=bot.broadcast)
    bot.trader = trader
    jobs.start()
    bot.start()
    DailyScheduler(SETTINGS, jobs, watchlist, bot=trader, notify=bot.broadcast).start()
    if SETTINGS.intraday_alerts and SETTINGS.trade_mode != "off":
        IntradayMonitor(SETTINGS, trader, bot.broadcast).start()
        logging.info("Intraday alerts: every %d min during the session (no AI)", SETTINGS.intraday_interval_min)
    logging.info("Trading bot: mode %s, calibration %s, runs daily at %s (Vietnam time); watchlist report at %s",
                 SETTINGS.trade_mode, "on" if SETTINGS.calibrate else "off", SETTINGS.trade_time,
                 SETTINGS.daily_report_time)

    app = create_app(SETTINGS, jobs, watchlist, trader=trader, zoom=zoom)
    uvicorn.run(app, host="0.0.0.0", port=SETTINGS.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
