"""Entry point for Botkeep (or any always-on host): web page + Telegram bot.

    python main_botkeep.py

Reads its settings from the environment or a .env file beside it (see
deploy/botkeep/.env.example). Serves the web page on SERVER_PORT (Botkeep sets
it) or PORT, polls Telegram when TELEGRAM_TOKEN is set, and analyses the
watchlist each weekday at DAILY_REPORT_TIME.
"""

import logging
import sys

from vnbot import settings as settings_module

SETTINGS = settings_module.load()   # before tradingagents: fixes its data paths

import uvicorn  # noqa: E402

from vnbot.jobs import JobQueue, prune_cache  # noqa: E402
from vnbot.service import DailyScheduler, Watchlist  # noqa: E402
from vnbot.telegram_bot import TelegramBot  # noqa: E402
from vnbot.web import create_app  # noqa: E402


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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
    logging.info("Removed %d stale cache files", prune_cache(SETTINGS))

    jobs = JobQueue(SETTINGS)
    watchlist = Watchlist(SETTINGS)
    bot = TelegramBot(SETTINGS, jobs, watchlist)
    jobs.start()
    bot.start()
    DailyScheduler(SETTINGS, jobs, watchlist).start()

    app = create_app(SETTINGS, jobs, watchlist)
    uvicorn.run(app, host="0.0.0.0", port=SETTINGS.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
