# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Changes that need action when upgrading are listed under "Upgrading from" in their release.

## [Unreleased]

### Added

- **Web pages redesigned** ("champagne on obsidian": ivory or obsidian with a champagne-gold accent, Playfair Display over Be Vietnam Pro, light and dark). The home page opens on the day's numbers (AI requests used out of the budget, the market session, today's intraday alerts, the 15:30 scan) and can ask the AI critic for one code (`/critic`, one request). A new **AI & cảnh báo** page (`/ai`) shows the budget per provider, today's intraday alerts, the critic's recent verdicts with their shadow results and how often its objections were right, and which notification channels are on. Chart series colours were re-checked with the dataviz palette validator for both themes.
- **Intraday alerts, no AI** (`vnbot/intraday.py`, `INTRADAY_ALERTS=1`): every `INTRADAY_INTERVAL_MIN` (15) minutes of continuous trading the last matched price of the real book's holdings (signal mode) and the Quant Engine paper book is checked against stop (with the shares T+2 lets you sell), T1, T2 and the floor; the engine's limit buy orders for today against their zone and planned stop; VNINDEX against `INTRADAY_VNI_DROP_PCT` (2) % and twice that. Each alert goes out once a day, across restarts. The books still trade on the closed daily bar.
- **Zoom Team Chat beside Telegram** (`vnbot/zoom.py`, ported from the OKX bot): broadcasts also go to an Incoming Webhook and/or a chatbot JID (`ZOOM_TO_JID`); the OKX bot's chatbot app can be reused for these notifications. Commands typed in Zoom reach `/webhook/zoom` (HMAC-checked with `ZOOM_SECRET_TOKEN`, senders in `ZOOM_ALLOWED_USERS`) and run like Telegram ones; that needs an app of this bot's own, since a Zoom app has one bot endpoint.
- **AI budget, so the bot no longer knocks over 9Router and OpenRouter.** The provider chain (`llm_clients/fallback_client.py`) counts requests per Vietnam day: `LLM_MAX_CALLS_PER_DAY` in all (default 60; past it, calls raise `LLMBudgetExceeded` until tomorrow) and `<PROVIDER>_MAX_CALLS_PER_DAY` per provider (OpenRouter defaults to 45, under its free tier's 50 a day; a provider past its cap is skipped). Counts are kept in `data/llm_usage.json` across restarts. A full analysis (`/phantich`, about 25 requests) is refused up front when fewer are left, after the engine's decision is sent. At most `LLM_MAX_CONCURRENT` (default 2) requests are in flight. `<PROVIDER>_MIN_INTERVAL_SEC` spaces the requests to one provider (for a gateway in front of a single free account). `/trangthai` and the web page show the day's counts.
- **AI critic, one request per candidate, the OKX bot's way** (`vnbot/critic.py`). The daily run screens VN100 plus the watchlist with the Quant Engine first (no AI; `SCAN_UNIVERSE=vn100|vn30|watchlist`, the three paper books keep their own universe) and one message counts the actions and lists the watchlist; only the `DAILY_AI_MAX` (default 5) strongest codes whose action is in `DAILY_AI_ACTIONS` (default `BUY,SELL`) go to the AI, within the day's budget. With `DAILY_AI_MODE=critic` (default) each gets one request asking the AI to find reasons to reject the engine's decision; it answers agree/reject, a confidence, a reason and the main risks. The verdict is advice: it changes neither the action nor any price, and the trading books do not follow it. `full` queues the multi-agent analysis as before (about 25 requests a code), `off` asks nothing. Before, every watchlist code (up to 30) got the full analysis each weekday.
  - Every verdict is kept (`data/critic_log.json`) and settled on the sessions that followed: a BUY from the decision day's close to the first of stop (-1R, first within a session) or target (+R:R), else the close after 20 sessions; a SELL on the price 10 sessions later. The daily message and `/phanbien` say how the trades the AI agreed with and rejected turned out, and how often its objections were right. The last five settled cases go into the next prompt as lessons, at no extra request.
  - `/phanbien MÃ [NGÀY]` on Telegram: the engine's decision and the critic's verdict, one request.
- **Defensive trend book** (`trading/factor.py`), now the strategy the real-account signals follow by default (`SIGNAL_STRATEGY=factor`). Ten stocks among the 50 largest market caps of the 100 most liquid HOSE stocks, ranked on low 1-year volatility plus nearness to the 52-week high, rebuilt at each month end with a hold buffer (a held stock stays while it ranks in the top 15); invested only while VNINDEX closes above its 200-session average, checked on the last session of each week. Orders at the next open through the same ledger; no stops or targets. It runs on its own paper book (`paper_factor.json`) beside the Quant Engine and the momentum rotation, which stay as comparison books.
  - Chosen on 2026-10-15 by an independent evaluation of 14 candidates (data to 2026-10-14, a point-in-time universe of 568 HOSE/ex-HOSE stocks, 162 of which left HOSE (delisted, or moved to HNX/UPCoM), the repo's ledger with fees, tax, slippage, T+2, lots, ticks and limit locks), with the selection rule frozen before the 2024-2026 TEST period was run once. CAGR / max drawdown after costs: TRAIN 2013-2020 18.1% / -25.9%, VALID 2021-2023 13.9% / -12.0%, TEST 01/2024-10/2026 8.1% / -20.8%, 2013-10/2026 15.6% / -25.9% (VNINDEX price index 10.7% / -45.3%; the old momentum rule on a point-in-time VN30 proxy 4.7% / -45.2%). Its edge is drawdown, not return: it trailed VNINDEX (15.9%) and the VN30-proxy momentum rule (11.8%) on TEST. Seven delisted stocks without VCI share counts can never enter the cap ranking (a small optimistic bias).
  - The live rules make the same decisions as the evaluated research code: replayed through the research harness on the real 2013-2026 data the fills are identical, order for order, and `tests/test_factor.py` replays a synthetic market through a line-by-line port of the harness, the module and `TradingBot.catch_up` session by session. One deliberate difference: equal scores go to the lower volatility (the research code left ties to pandas' unstable sort); with it the replay gives TRAIN 17.7%, VALID 13.9%, TEST 8.4%, 2013-2026 15.3% / -26.4%.
  - The point-in-time universe is built from the VCI listing, price-history (matched value, unadjusted close, shares outstanding; `money_flows.load_price_history`) and daily bars, only on the sessions that rebuild the basket, and cached per day in `trading/factor_universe.json`. If it cannot be built, the session fails and is retried like a session with a missing bar; the basket is never ranked on another universe. The build reads VCI without the shared 5-minute breaker (one failed request no longer fails every stock after it), asks a failed stock once more, and stops after 10 failures in a row (an outage).
  - The session after the latest one is guessed from the exchange calendar (`vn_rules.is_session_day`: weekends, fixed-date holidays and a dated table of Tết, Hùng Kings, days in lieu and bridge days for 2026-2030), so a week or month that ends before Tết is seen on its real last session. A closure the table does not know makes the week's check or the month's rebuild run on the next session.
  - Parameters `fb_universe`, `fb_top_k`, `fb_buffer`, `fb_market_ma`, `fb_rebalance_months` in `quant/params.py` (`FACTOR_FIXED`). The nightly calibration does not touch them: the evaluation tested only this point and its single neighbours (K 5/15, buffer 1/2, SMA 150/250, ...; TEST 4.6-14.9% a year, K=15 4.9%), never a recalibrating variant, and the nightly dataset of today's names cannot rebuild the point-in-time universe. Values set by hand are clamped to the tested range.
- The daily report, `/danhmuc`, `/lenh`, `/khop` and the `/bot` page (NAV chart, book card, current basket, thresholds) show the three books. Each book's return is dated from its own start, and the chart indexes every line to 100 on the first session all books share.
- `simulator.build_dataset(..., inputs=False)` builds bars only; `WARMUP_BARS` is 260 (the defensive book's 252-session score).
- **Research harness in the repo** (`research/`, outside the Botkeep bundle): the point-in-time backtester the 2026-10-15 evaluation used, driving the repo's real ledger. `python -m research download` fetches and incrementally updates the data (2011 to today for every stock that was ever on HOSE, delisted ones included, with VCI price-history: matched value, unadjusted close, shares; ~315 MB, never committed: `research/data/` or `TRADING_VN_RESEARCH_DATA`), `python -m research run <strategy>` backtests `factor` (the live `trading/factor.py` driven session by session as the bot runs it), `momentum`, `ew`, `quant` or `vnindex` on fixed TRAIN 2013-2020 / VALID 2021-2023 / TEST 2024- splits, with TEST locked unless `--unlock-test`, and `python -m research baselines` writes the baseline tables. `python -m research run factor --split VALID` reproduces the published 13.86% / -12.0%. Files are written whole (temp file, then rename), Ctrl-C stops the download cleanly, and the data end moves only when a full download finishes (a `--symbols` run never leaves the other stocks looking suspended). Guide in `research/README.md`, the evaluation's results in `research/RESULTS.md` (both in Vietnamese); tests in `tests/test_research_harness.py` (real-data checks are `integration`).

### Changed

- When every provider is resting (after a 429, an outage or a refused key) a call now fails at once instead of trying them all anyway; `LLM_TRY_RESTING=1` restores the old behaviour. The OpenAI SDK no longer retries inside a call (`max_retries=0`): the chain is the fallback, and a retry on a 429 only adds load.
- **The momentum rotation's reported numbers were biased.** Its +73-91% (2022-2026) was measured on today's VN30, which holds stocks that were on UPCoM when it bought them (MCH, BSR). On point-in-time universes the same rule made 0.1-11.8% a year over 2024-2026, with drawdowns of 33-45% (up to 50% in the other periods). It keeps running on its paper book for comparison.

### Upgrading from 0.7.1

- `SIGNAL_STRATEGY` now defaults to `factor`. To keep the real-account signals on the momentum rotation, set `SIGNAL_STRATEGY=momentum` (the Botkeep `.env.example` used to set it explicitly). When the real account holds stocks, the first report after a change of strategy says so: its orders move the real account to the new strategy's basket, which can mean selling everything (on 2026-10-14 the defensive book is in cash). Existing `paper.json` and `paper_momentum.json` books load and continue unchanged; the defensive book starts fresh at the next session. On 2026-10-14 VNINDEX (1,704.9) was below its SMA200 (1,797.9), so the new book starts in cash and stays there until a week closes above it.

## [0.7.1] — 2026-10-14

### Added

- **`/chay` and a "Chạy phiên ngay" button on `/bot`** run the bot on the last closed VNINDEX session in the background, for when the server restarted past `DAILY_REPORT_TIME`. A session already run is not run twice: the daily job and `/chay` return the saved report (`last_report.json`).
- **Light and dark theme** for every web page and the HTML reports, following the device setting, with a sun/moon switch in the header that the browser remembers. Peach-tea, orange and lemongrass palette; Be Vietnam Pro text.
- **NAV chart on `/bot`**: both paper books and VNINDEX indexed to 100, with direct end labels and a hover readout.

### Changed

- **Output Guard runs early as well.** The Research Manager's plan and the Trader's plan are checked right after they are written, so the risk team and the Portfolio Manager no longer read a recommendation that contradicts the Quant Engine. The final check is unchanged and keeps the notes of the early ones.
- The web pages are redesigned for phones and desktops: sticky header with navigation, action badges, KPI tiles, scrollable tables, a progress stepper on the analysis page.

## [0.7.0] — 2026-10-14 — Vietnamese market edition

The framework analyses Vietnamese stocks (HOSE, HNX, UPCoM) only, on Vietnamese data, and a deterministic quantitative engine decides the action that the agents explain.

### Added

- **Vietnamese data sources.** Daily prices from DNSE with VCI as fallback (VND, adjusted for corporate actions); statements with their publication dates, TTM ratios, corporate events and company disclosures from VCI; broker research from CafeF; news from Google News in Vietnamese. No key needed.
- **Quant Engine.** A graph step beside the analysts, ported from the lananh-main policy layer, decides BUY / HOLD / WATCH / SELL / NO_TRADE with the entry zone, stop, targets, risk/reward and position size, from price, VNINDEX regime, sector and news. Every later agent reads it as fixed.
- **Output Guard.** A last graph step checks the Research Manager, Trader and Portfolio Manager texts in code: a contradicting action is replaced by the system summary, a price that is in no data is flagged, the final rating is always the engine's, and one standard disclaimer ends the decision.
- **Market rules** in `tradingagents/market/vn_rules.py`: price bands, tick sizes, board lots, T+2 settlement, sessions and round-trip costs. The trade plan rounds to the tick, sizes in lots of 100 when cash is given, and the backtest scores BUY net of costs.
- Tools `get_corporate_events` and `get_market_context` (indices, VNINDEX regime, sector rotation).
- **Money flows in the Quant Engine.** Foreign and proprietary (tự doanh) net trading per stock and foreign room (VCI), market-wide foreign/proprietary flow (CafeF), ETF creations/redemptions (VCI) and HOSE breadth (VNDirect), cached per day and read point-in-time. Market-wide foreign selling and a VNINDEX high on weakening breadth each lower the regime a notch; stock flows move confidence in the direction of the signal; a full foreign room voids foreign buying. With `flow_weight` 0 and the switches off, decisions are the original ones.
- **Tunable thresholds** (`quant/params.py`): the policy's gates and the flow thresholds, defaults equal to the original constants, the set in force read from `quant_params.json`.
- **Trading bot** (`tradingagents/trading/`, `vnbot/trader.py`). A paper account trades the engine on VN30 plus the watchlist every session under Vietnamese rules (limit buys at the top of the entry zone, lots of 100, T+2, fees and sell tax, stop / half at T1 with a break-even stop / rest at T2 / time stop, no fills on limit-locked sessions). `TRADE_MODE=signal` adds suggested orders for a real book that its owner updates with `/khop` and `/von`. No order is ever sent to a broker. Telegram `/danhmuc`, `/lenh`, `/hieuchinh`; web page `/bot`.
- **Momentum rotation** (`trading/momentum.py`), a second strategy on its own paper book beside the Quant Engine's: the top 5 VN30 stocks by ~3-month return, equal weight, re-ranked every ~21 sessions, all cash while VNINDEX is under its 100-session average; orders at the next open through the same ledger. Backtest 01/2022-10/2026 on today's VN30: +91% with a 21% max drawdown (VNINDEX +13.8%), positive in both the falling and the rising half. The daily report compares the two books; `SIGNAL_STRATEGY` picks which one the real signals follow (default momentum).
- **Quant Engine strategy variants as parameters**, off by default: distribution days with a minimum drop and O'Neil's 5% expiry, the first resistance that pays the minimum R:R, breakouts with a projected target, a share sold at T1 and a trailing stop instead of T2. Tested 2022-2024 then confirmed on 2024-2026: they did not hold up, so the defaults are unchanged; the calibration may still move them.
- **Nightly self-calibration** (champion/challenger): one tunable moved one step at most per night, accepted only when it beats the thresholds in force on the training sessions and again on the 60 most recent sessions it was not chosen on, with enough trades and no deeper drawdown; the previous set and the defaults are always challengers. History in `calibration_history.jsonl`.
- **Botkeep hardening.** The web page stays locked until `WEB_PASSWORD` is set, refuses forms posted from another site and cannot be framed; a request for a ticker and date already waiting or running joins that analysis; people can keep at most `MAX_PENDING` (default 10) requests waiting; the daily run skips weekday holidays (no VNINDEX session) and retries when the data cannot be read; Telegram flood control (429) is waited out; `DAILY_REPORT_TIME` accepts `9:05`.

### Changed

- The rating scale is the engine's five actions; the Research Manager states its stance on the action, the Trader writes the execution notes, the Portfolio Manager writes the thesis and risks.
- Reports are in Vietnamese by default; the benchmark is VNINDEX; tickers accept `FPT`, `FPT.VN`, `HOSE:FPT`.
- The Quant Engine pairs a stock's closes with VNINDEX by trading date (not by array position) for relative strength, so a suspended or newly listed stock is compared over the same sessions.
- A close-to-close move inside the exchange's band (UPCoM ±15%, HNX ±10%) is no longer taken for an unadjusted ex-rights gap unless it is a drop matching a bonus/split ratio, and a rise never locks the stock; penny prices keep a one-tick band.
- **CafeF ticker news** (`Events_RelatedNews_New.aspx`, plain HTML, paged back to the window) joins VCI disclosures, CafeF research and Google News.
- **Output Guard** catches plain calls ("Mua vào", "Khuyến nghị: Mua", "giải ngân", "tăng/giảm tỷ trọng", "Overweight", "bán ra", "cắt lỗ ngay"); ignores conditional, rule and market-side uses of BÁN/MUA ("nếu thủng stop thì BÁN", "T+2 mới được BÁN", "khối ngoại bán ròng", "lực BÁN"); counts a negation only next to the word; reads a price at the end of a sentence; leaves volumes, EPS and values in tỷ/triệu out of the price check; removes only the model's own "task done" line.
- WATCH / NO_TRADE trader proposals carry no stop, target or R:R; a SELL names its levels as invalidation and support. The HTML report colours the five actions.
- Packaging: `pip install ".[bot]"` installs the always-on app (vnbot is part of the package); `docker compose --profile bot up -d bot` runs it; `data/` is ignored by git.
- The Output Guard no longer flags index points ("VNINDEX 1.720 điểm") as prices outside the data.
- The README is rewritten in Vietnamese around this edition, with a full Botkeep guide in `deploy/botkeep/README.md`.

### Removed

- Yahoo Finance, Alpha Vantage, SEC EDGAR, FRED, Polymarket, Reddit, StockTwits and Jev screening, the crypto pipeline, and the `yfinance` and `pytz` dependencies.

### Upgrading from 0.6.0

- `data_vendors` values are now `dnse`, `vci`, `local`, `vn_news`, `google_news`; old vendor names are refused.
- `PortfolioRating` is an alias of `SystemAction`; `TraderAction` is gone. Memory-log entries written with the old five-tier ratings still parse only when their label is Buy, Hold or Sell.

## [0.6.0] — 2026-10-03

HTML reports, a provider per model tier, past decisions settled for every ticker while the analysts work, and company news read while Yahoo's news feed is down.

### Added

- **HTML report.** A saved report also includes `complete_report.html`, the report as one page with its sections listed beside the text, that reads well in a browser, on a phone and in print. Answering the save question at the prompt also asks about the page and can open it in your browser; `--no-html` or `save_reports(..., html=False)` skips it. The page is self-contained: it loads nothing and runs nothing. (#1419)
- **A provider per model tier.** The quick and deep models can run on different providers, for example the managers on Claude while the analysts run on OpenAI: set `deep_think_provider` or `quick_think_provider` (`TRADINGAGENTS_DEEP_THINK_PROVIDER`, `TRADINGAGENTS_QUICK_THINK_PROVIDER`) with the tier's model. The report header names each tier's provider. (#1440)
- **Past decisions settle alongside the analysts.** While a run's analysts work, every logged decision whose holding period has passed is settled, for any ticker, not only the one being analyzed. If settling fails, the run goes on and the report header says so. (#1428, #1445)
- **Crypto on Reddit.** For a crypto ticker the Sentiment Analyst reads r/CryptoCurrency and r/CryptoMarkets, plus r/Bitcoin, r/ethereum or r/solana for those coins, instead of the stock subreddits. (#1461)
- `settle_all_pending()` settles every due decision without running an analysis, for a scheduled job; it and `settle_pending()` return the decisions settled and any that could not be. (#1445)
- The report header names the run's rating.
- Euro-area macro series by name: `ecb_deposit_rate`, `ecb_main_refi_rate`, `euro_hicp`, `euro_core_hicp`, `euro_real_gdp`, `germany_10y`, `france_10y` and `eur_usd`. (#1465)

### Models

- A Gemini call times out after 600 seconds, as OpenAI and Anthropic calls do, so a stalled call no longer hangs the run. (#1417)

### Upgrading from 0.5.2

- markdown-it-py 4.0 or later is installed.
- A saved report also writes `complete_report.html`; pass `--no-html`, or `html=False` to `save_reports()`, to keep only the Markdown.
- A run no longer settles its ticker's decisions before it starts; the Memory Log step settles every ticker's during the run.
- `settle_pending()` returns a `Settlement` with `settled` and `failed` (was `None`).
- Checkpoints from 0.5.2 are not resumed.

### Fixed

- Company news is read from Yahoo search while Yahoo's news feed returns nothing. When Yahoo has no article about the symbol, its news is reported unavailable, not absent, and a configured next news vendor is tried. (#1467, #1469)
- A rating quoted from someone else, such as "Consensus rating: Buy", no longer replaces the Portfolio Manager's own. (#1466)
- A Yahoo news article with a null field no longer loses the ticker's news. (#1458)
- A start date a model writes with stray characters is sent back to it to correct, instead of failing the news or price request. (#1476)
- A request Alpha Vantage rejects is reported as unavailable, not served as data; the API key is kept out of every Alpha Vantage error. (#1442)
- Yahoo fundamentals print each money figure in its own currency: London prices in GBp, market cap and per-share figures in GBP, and an ADR's income figures in its reporting currency, such as TWD for TSMC. (#1456)
- Jev screening uses the endpoint `TYPESAFE_BASE_URL` names, so it can run through OpenRouter. (#1416)
- Runs sharing a memory log settle each decision once, and an interrupted settlement keeps the outcomes it finished.
- The report header names the sentiment analyst `sentiment`, as the CLI does.
- `requirements.txt` says the dependencies are declared in `pyproject.toml`.

### Contributors

[@amitbhansali](https://github.com/amitbhansali), [@AyushJain-601](https://github.com/AyushJain-601), [@BichengWang](https://github.com/BichengWang), [@byte7m](https://github.com/byte7m), [@Dangerboi](https://github.com/Dangerboi), [@davidalmeida90](https://github.com/davidalmeida90), [@Jackzigen](https://github.com/Jackzigen), [@jhill-cmd](https://github.com/jhill-cmd), [@Recnelis0](https://github.com/Recnelis0), [@seaaxe](https://github.com/seaaxe), [@vaegev](https://github.com/vaegev).

## [0.5.2] — 2026-09-29

Parallel analysts, unattended CLI runs, reports that record what produced them,
and past runs served only what was known on their date.

### Added

- **Parallel analysts.** The selected analysts run at the same time, so the analyst phase takes about as long as the slowest one; the CLI shows each analyst's progress and time as it finishes, and the debate starts when all reports are in. (#1255, #433)
- **Unattended CLI runs.** Flags answer the per-run questions, and `TRADINGAGENTS_*` variables the rest, so a scheduled job or script asks nothing: `tradingagents --ticker NVDA --date 2026-09-26 --analysts market,news --save --no-show`. Without a terminal, a missing answer stops the run before it starts and names what to set. (#1127, #1133)
- **Reports record what produced them.** Each report opens with the analysis date, version, provider, models, analysts, rounds and data vendors; the state log keeps them as `run_settings`. (#752, #1197)
- **Docker data folder.** `TRADINGAGENTS_DATA_DIR` keeps results, reports and the memory log in a host folder. (#865)
- **A cap on an analyst's tool calls.** After `max_tool_rounds` rounds (default 20, `TRADINGAGENTS_MAX_TOOL_ROUNDS`) the analyst writes its report from what it has. (#1420)
- `tradingagents backtest` shows each cell as it starts and the `--run-id` to continue; `--analysts` accepts `sentiment`.
- Benchmarks for Taiwan, Korea, Singapore and the main European exchanges. (#1392)
- Python 3.14 support; the Docker image runs Python 3.13.

### Models

- `deepseek-flash` is treated as a thinking model, so structured output works. (#1388)

### Upgrading from 0.5.1

- Python 3.11 or later is required; pandas 3 is installed.
- Minimum versions: yfinance 1.7.0, langchain-core 1.6.5, langchain-openai 1.6.6, langchain-anthropic 1.7.4, langchain-google-genai 4.4.0, langgraph 1.2.12, langgraph-checkpoint-sqlite 3.1.1, langchain-aws 1.7.9 (`bedrock` extra).
- Statements default to SEC EDGAR, then Yahoo (`fundamental_data = "sec_edgar,yfinance"`).
- A past-dated run gets no Yahoo or Alpha Vantage statements or insider trades, which carry no filing date, and only the company's current name, marked as such.
- The memory log is the `tradingagents.memory` package (was `tradingagents.decision_log`); settlement and reflection moved there from `tradingagents.graph`, and `Reflector` is no longer exported from `tradingagents.graph`.
- `process_signal` is removed; the rating is `propagate()`'s second value or `final_state["final_rating"]`.
- A rating is read only from its `Rating:` label; a free-text decision without one is `REVIEW`.
- Data-layer functions take `as_of_date=` (was `curr_date=`); `build_instrument_context` and `resolve_instrument_context` take `trade_date=`.
- `VendorRateLimitError` is `VendorUnavailableError`.
- Data tools take the instrument from the run's state; tool calls no longer carry `symbol` or `ticker`.
- State log: `trader_investment_plan` (was `trader_investment_decision`), `judge_decision` removed, `final_rating` and `run_settings` added.
- Checkpoints from 0.5.1, or saved under different settings, are not resumed.

### Fixed

- Data tools serve the run's instrument, even when the model passes another symbol.
- A failed vendor request is reported as unavailable, not as a missing or delisted symbol; one vendor's "no data" no longer speaks for a chain in which another failed. (#1386)
- The rating is the Portfolio Manager's own, not one its thesis quotes (#1383); its line keeps an English label in other output languages (#1435).
- Backtest cells given twice run once (#1415), and settlement compares a decision with its benchmark over the same dates.
- A snapshot for a symbol with no prices no longer ends the run.
- Concurrent runs no longer drop memory log entries.
- SEC EDGAR quarterly figures filed only year to date are served, labelled with their span.
- Yahoo dividend yield and debt-to-equity print as percentages. (#1414)
- The FRED change line names the dates it spans. (#1397)
- `stream_run` serves the graph's own config to its tools.
- The test suite stays off the network and out of the user's files, and passes in any order; integration tests run with `-m integration`. (#1395)

### Contributors

[@1Wizzy](https://github.com/1Wizzy), [@BichengWang](https://github.com/BichengWang), [@Chaoqi31](https://github.com/Chaoqi31), [@davidalmeida90](https://github.com/davidalmeida90), [@djconnexion77](https://github.com/djconnexion77), [@fuzing](https://github.com/fuzing), [@hailampy123](https://github.com/hailampy123), [@hesam-shams](https://github.com/hesam-shams), [@HEYALT](https://github.com/HEYALT), [@Jackzigen](https://github.com/Jackzigen), [@kagura-agent](https://github.com/kagura-agent), [@loulanyue](https://github.com/loulanyue), [@mannubaveja007](https://github.com/mannubaveja007), [@olivergpt](https://github.com/olivergpt), [@rita112025-cpu](https://github.com/rita112025-cpu), [@shuyan-code](https://github.com/shuyan-code), [@SingTheCode](https://github.com/SingTheCode), [@sjq597](https://github.com/sjq597), [@xiaodu55](https://github.com/xiaodu55), [@Youholdme](https://github.com/Youholdme).

## [0.5.1] — 2026-09-24

A package layout organised by what each module holds, social posts screened by
TypeSafe's Jev when a key is set, GPT-6 Sol and Luna as the default models, and
fixes to run isolation, SEC EDGAR statements and historical runs.

### Upgrading from 0.5.0

Some modules moved, and the old import paths are gone. Update imports as follows:

- `tradingagents.dataflows.interface` is `tradingagents.dataflows.router`, and `dataflows.symbol_utils` is `dataflows.symbols`. `dataflows.utils` is gone: `get_current_date` is in `dataflows.date_window`, `safe_ticker_component` in `dataflows.symbols`, and `get_scrubbed` and `vendor_reachable` in `dataflows.net`.
- Vendor modules live under `tradingagents.dataflows.vendors`: `yahoo` (`ohlcv`, `market`, `fundamentals`, `news`, `snapshot`, from the former `stockstats_utils`, `y_finance`, `yfinance_news` and `market_data_validator`), `alpha_vantage` (a package, from the `alpha_vantage_*` modules), and `sec_edgar`, `fred`, `polymarket`, `reddit`, `stocktwits`.
- `tradingagents.agents.utils` is gone: the agent tools are in `agents.tools`, and `agent_utils`, `agent_states`, `rating` and `structured` are `agents.context`, `agents.state`, `agents.rating` and `agents.structured`.
- The decision log is `tradingagents.decision_log` (was `agents.utils.memory`), and `cli.utils` is `cli.prompts`.
- `backtest.summarize` takes a `run_backtest` result or the path of a decision log, in place of a `TradingMemoryLog`.
- Removed: `SignalProcessor` (the rating is parsed by `process_signal`), the `create_social_media_analyst` alias (use `create_sentiment_analyst`), `symbol_utils.is_yahoo_safe`, the unused `project_dir` config key, and the graph attributes `curr_state`, `ticker` and `log_states_dict`, which held the previous run's state.

### Added

- **Jev post screening.** With `TYPESAFE_API_KEY` set, TypeSafe's Jev reads each StockTwits and Reddit post the Sentiment Analyst fetches: posts that are not about the company are dropped, and each source opens with a count of the rest by stance. Without the key nothing changes. (#1376)
- B3 tickers (`.SA`) are benchmarked against the Ibovespa. (#1366)

### Models

- GPT-6 Sol and GPT-6 Luna are the default deep and quick models, and Claude Opus 5.5 replaces Opus 5 in the picker. Opus 5 and GPT-5.4 Mini remain valid model IDs.

### Fixed

- Several graphs in one process each read their own data vendors. (#1369)
- SEC EDGAR cash flow statements find capital expenditure for filers that moved it to purchases of productive assets (NVIDIA since fiscal 2022, Amazon since 2016), whose recent periods read as empty. (#1370)
- SEC EDGAR annual statements list fiscal years only.
- A historical run is no longer told today's date, or a date after the run, in coverage notices and the instrument context.
- The Fundamentals Analyst can call the insider transactions tool.
- An unreachable Yahoo on insider transactions is reported as unavailable, not as a symbol without data.
- A graph reused across runs, as in a backtest, no longer keeps every run's full state.
- A checkpointed CLI run says whether it resumed or started fresh.
- A blank path variable (`TRADINGAGENTS_RESULTS_DIR` and the like) keeps the default path.
- StockTwits messages reach the prompt as plain text rather than HTML-escaped.
- The test suite is independent of the developer's `.env`, time zone and network. (#1368, #1372)

### Contributors

Thanks to everyone who reported these or sent a fix:

[@codify88](https://github.com/codify88), [@davidalmeida90](https://github.com/davidalmeida90), [@duongylinh](https://github.com/duongylinh), [@jccl2](https://github.com/jccl2), [@yuina368](https://github.com/yuina368).

## [0.5.0] — 2026-09-18

Point-in-time integrity across every dated path, decisions that are recorded as
they were made, backtesting over a grid of tickers and dates, the caller's
portfolio as run input, and SEC EDGAR fundamentals served as filed.

### Highlights

- **Fundamentals as filed.** SEC EDGAR serves US company statements as they stood on the run's date: a period that has ended but has not been filed is not served, and a figure restated later still reads as first reported. Keyless, opt-in via the vendor chain.
- **Backtesting.** `run_backtest` runs the pipeline over a ticker and date grid into its own decision log, and `summarize` scores the settled cells; `tradingagents backtest` does the same from the CLI.
- **Portfolio context.** `propagate(..., portfolio=...)` and `--portfolio` let the trader, risk and portfolio agents size against real holdings. A run without one is never treated as a flat book.
- **Decisions are recorded as made.** An unreadable decision is flagged for review everywhere instead of becoming a tradeable Hold, and a rating argued against is no longer read as the call.

### Point-in-time and honest attribution

- Dated tools take the run's date from graph state, so an omitted or later date cannot reach a vendor. (#1331, #1319, #1118)
- Insider filings and prediction-market odds are bounded by the run date; insider rows state that a trade becomes public when its Form 4 is filed.
- A feed that never observed a window reports it as unavailable rather than as an absence, across news, Reddit and StockTwits.
- The resolved company identity says when it describes today rather than the run date.
- The verification snapshot quotes the prices the vendor reported, never a gap-filled value.
- A vendor failure is a vendor failure: yfinance raises instead of returning its errors as text, an outage is not reported as a company with no data, and a chain where every vendor is unavailable says so instead of ending the run.
- The macro vintage pin is clamped to the vendor's own clock, so a run dated today cannot ask for a vintage it does not have.
- A historical run is not served a present-day company profile by either fundamentals vendor. (#1300)

### Decisions and evaluation

- The labelled rating decides, whatever separates it, and prose naming several ratings is reviewed rather than guessed.
- Decision prompts state the shape of their answer, so a provider without structured output still returns a readable decision.
- A report that was not produced says so, instead of appearing as an empty section.
- Backtest scoring reads the direction each rating claimed: a Sell that fell is a hit, and Hold reports no hit rate.
- The outcome window is configurable (`holding_period_days`), and reflection states the window it judges.
- A settled decision is not logged twice, and a failed reflection no longer stops the next run. (#645)
- The trader states entry and stop levels as prices, so a percentage no longer fails the whole proposal. (#1288)

### CLI

- `tradingagents backtest`, with `--run-id` to continue an interrupted sweep. (#1234)
- The previous run's selections come back as prompt defaults. (#1236, #920)
- A run with no readable rating says so; the live view no longer scrolls; messages that read like Python values are shown. (#649, #784)
- The state log keeps non-ASCII readable. (#1081)

### Data sources

- SEC EDGAR fundamentals vendor (US filers, keyless).
- Hong Kong and Shanghai tickers resolve to the symbols Yahoo serves. (#1342, #957, #1260)
- Reddit is fetched as one combined request per run. (#1286)
- One OHLCV cache file per symbol. (#1330)

### Models

- Current lineups for every provider: GPT-6 Astra and the GPT-5.6 family, Gemini 3.8 Flash, Claude Opus 5 and Fable 5.1, Grok 4.6, DeepSeek Flash, Qwen 3.8, GLM-5.3, MiniMax M3, Kimi K3 and the current Mistral snapshots.
- Every provider accepts a model ID the picker does not list.
- GLM traffic goes to the platform its key belongs to, and Ollama structured output no longer sends a tool_choice it rejects. (#1062)

### Changed

- The memory log records `REVIEW` for a decision with no readable rating, where it previously recorded `Hold`.
- Optional fields the model did not provide are named as such rather than omitted.
- Removed dependencies nothing imports: backtrader, redis, setuptools, langchain-experimental, parsel, tqdm. (#1353, #1070)

### Contributors

Thanks to everyone who reported these or sent a fix:

[@akashkpfreelancer](https://github.com/akashkpfreelancer), [@angziii](https://github.com/angziii), [@anupamme](https://github.com/anupamme), [@AyushKar2005](https://github.com/AyushKar2005), [@bulkypanda](https://github.com/bulkypanda), [@CadeYu](https://github.com/CadeYu), [@chiang21fcb](https://github.com/chiang21fcb), [@dajiaohuang](https://github.com/dajiaohuang), [@dewrama](https://github.com/dewrama), [@DogInfantry](https://github.com/DogInfantry), [@emitov](https://github.com/emitov), [@farukerdem34](https://github.com/farukerdem34), [@flydragon2018](https://github.com/flydragon2018), [@fusshell](https://github.com/fusshell), [@Ganesh1729-ui](https://github.com/Ganesh1729-ui), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@hamzabudeir](https://github.com/hamzabudeir), [@ihsieh31](https://github.com/ihsieh31), [@jaylew20250206](https://github.com/jaylew20250206), [@kaushik-yadav](https://github.com/kaushik-yadav), [@kbnnf](https://github.com/kbnnf), [@kevinkda](https://github.com/kevinkda), [@LudwigJMarx](https://github.com/LudwigJMarx), [@lx7720](https://github.com/lx7720), [@malandrindev](https://github.com/malandrindev), [@mhd325ic-hash](https://github.com/mhd325ic-hash), [@minhdn90](https://github.com/minhdn90), [@miznan](https://github.com/miznan), [@mmssix](https://github.com/mmssix), [@mrbob-git](https://github.com/mrbob-git), [@newnewself](https://github.com/newnewself), [@prithvirajrh](https://github.com/prithvirajrh), [@PyriteResearch](https://github.com/PyriteResearch), [@Rajatendu1](https://github.com/Rajatendu1), [@Recnelis0](https://github.com/Recnelis0), [@Rodvask](https://github.com/Rodvask), [@samhoooo](https://github.com/samhoooo), [@sheiun-xu](https://github.com/sheiun-xu), [@shivsin25](https://github.com/shivsin25), [@SmileShaun](https://github.com/SmileShaun), [@SonnyRajagopalan](https://github.com/SonnyRajagopalan), [@taro0915](https://github.com/taro0915), [@wupengbo125](https://github.com/wupengbo125), [@wxggzz](https://github.com/wxggzz), [@Yixiang-Wu](https://github.com/Yixiang-Wu), [@ZahirBodrike](https://github.com/ZahirBodrike), [@ZHUYAWEI](https://github.com/ZHUYAWEI), [@zkwang616](https://github.com/zkwang616).

## [0.4.0] — 2026-08-31

Look-ahead and point-in-time fixes across the data and memory layers, clearer
decision signals, working CLI checkpoint resume, and the GPT-5.6 / GLM-5.3 models.

### Fixed

- **FRED macro look-ahead.** Historical macro requests were served from today's
  data vintage, leaking later revisions into a backtest; both the observations
  and metadata requests now pin the vintage to the as-of date. (#1275)
- **Social sentiment look-ahead.** StockTwits and Reddit were fetched with no
  date, so a historical run showed today's chatter as if it were from the as-of
  date; the social path is now trimmed to the analysis window, via one shared
  UTC half-open window rule (`dataflows/date_window`) used by news too. (#1220)
- **Memory point-in-time guard.** `get_past_context` returned every resolved
  lesson regardless of the run date; each resolved entry now records the date
  its outcome became known, and a historical run only sees lessons resolved by
  the trade date. (#1251)
- **Premature reflection.** A decision was settled on a partial return if a rerun
  happened before its holding window fully traded; resolution now waits for the
  full window. (#1169)
- **Latest OHLCV bar dropped.** The newest bar with a NaN close was silently
  dropped before the date cutoff, making the previous trading day look like the
  latest; dates are normalized per element (DST- and non-US-market safe) and a
  missing latest close raises rather than falling back. (#1201)
- **Debate opening fabrication.** The first speaker in each debate round rebutted
  an empty opponent response, fabricating the other side; all five debators now
  open with their own case when no opponent has spoken. (#1176)
- **Silent Hold.** An unparseable Portfolio Manager rating (including a fullwidth
  colon) was coerced to a tradeable Hold; it now surfaces a `REVIEW` sentinel,
  with `parse_rating` keeping its silent default for compatibility callers. (#1170)
- **`--checkpoint` was a no-op on the CLI.** Checkpoint setup lived only in
  `propagate()`; the CLI streamed the checkpointer-less graph. The lifecycle is
  now shared, and a resume feeds `None` so LangGraph continues the interrupted
  run instead of duplicating messages. (#1249)
- **DeepSeek via OpenRouter.** `deepseek/<id>` fell through to default
  capabilities and had object-form `tool_choice` forced on it; the official
  namespace is stripped so it reuses the native DeepSeek quirks. (#1199)
- **Trader price grounding.** The Trader saw only the digested plan; it now also
  receives the technical market report so entry/stop levels anchor to real price
  structure. (#1167)

### Added

- **Configurable output-token cap.** `max_tokens` / `TRADINGAGENTS_MAX_TOKENS`,
  forwarded to every provider (Gemini as `max_output_tokens`), so a model that
  emits unbounded reasoning can be bounded instead of hanging. (#1204)
- **Latest models.** Added the GPT-5.6 family (`gpt-5.6` / `gpt-5.6-terra` /
  `gpt-5.6-luna`) and GLM-5.3 (`glm-5.3`, `glm-5.3-flash`). The default models
  are now `gpt-5.6` (deep) and `gpt-5.6-luna` (quick).

### Contributors

Thanks to everyone who reported these or sent a fix:

[@PyriteResearch](https://github.com/PyriteResearch), [@yiran1268](https://github.com/yiran1268), [@fabiolenine](https://github.com/fabiolenine), [@lx7720](https://github.com/lx7720), [@taro0915](https://github.com/taro0915), [@Jaswanth-Sriram-Veturi](https://github.com/Jaswanth-Sriram-Veturi), [@ariesy](https://github.com/ariesy), [@liangzj1999](https://github.com/liangzj1999), [@zkwang616](https://github.com/zkwang616), [@aniketshukla1](https://github.com/aniketshukla1), [@loulanyue](https://github.com/loulanyue), [@hudsonwa](https://github.com/hudsonwa), [@daleselaji-dev](https://github.com/daleselaji-dev), [@wolfoswald777-crypto](https://github.com/wolfoswald777-crypto).

## [0.3.1] — 2026-07-05

Correctness and stability patch: data look-ahead, graph-router crash-safety,
checkpoint identity, crypto sentiment sources, and configurable resilience.

### Fixed

- **Alpha Vantage look-ahead filter now runs.** The fundamentals payload is a
  JSON string, so the dict-only guard skipped filtering and future-dated reports
  leaked into historical runs; parse before filtering. (#1115, @zachthebird)
- **News analyst prompt matches the tool.** The prompt advertised
  `get_news(query, ...)` but the tool takes a ticker; aligned to stop
  hallucinated free-text query calls. (#1116, @shcheuk)
- **Shared debate/risk routers can't crash mid-run.** Both routers return more
  targets than any one edge mapped; every edge now shares the complete path map,
  so a fall-through under prompt/i18n/refactor drift stays routable.
  (#1088, @Fr3ya, @sa7an7, @Sushanth012)
- **Checkpoint resume respects graph shape.** The thread id folds in selected
  analysts, debate/risk depth, and asset mode, so a resume under different
  choices no longer continues the wrong graph. (#1089, @bossjoker1, @Ghraven)
- **Crypto sentiment sources resolve.** StockTwits lists crypto as `<BASE>.X`
  (Yahoo's `BTC-USD` 404s) and Reddit needs the base symbol to match; the social
  path now maps crypto correctly for both. (#1113, @suremadoreai)

### Added

- **Configurable LLM retry budget.** `llm_max_retries` /
  `TRADINGAGENTS_LLM_MAX_RETRIES` is forwarded to every provider, so a transient
  429 burst no longer aborts a run. (#1091, @yanggaome)
- **Bedrock API-key auth.** `AWS_BEARER_TOKEN_BEDROCK` authenticates Amazon
  Bedrock without AWS access keys and takes precedence over an ambient
  `AWS_PROFILE`. (#1103, @praxstack)
- **Latest Claude models.** Added Claude Sonnet 5 (`claude-sonnet-5`) and
  Fable 5 (`claude-fable-5`); effort control now covers the Claude 5 line.

## [0.3.0] — 2026-06-22

Stabilization and extensibility release: a CI gate, a unified verified
data-access contract, a provider and data-vendor registry, and a maintenance
sweep that hardened config precedence, the model catalog, data resilience, and
structured output.

### Added

- **CI gate.** GitHub Actions runs the pytest suite across Python 3.10-3.13,
  strict `ruff`, and a clean-install smoke that imports the package and CLI to
  catch undeclared dependencies. (#994, #197)
- **Provider registry.** OpenAI-compatible providers register as a single spec,
  and a generic `openai_compatible` endpoint covers vLLM, LM Studio, and relays.
  Adds NVIDIA NIM, Kimi, Groq, Mistral, and a native Amazon Bedrock client.
- **Macro and prediction-market vendors.** FRED macro indicators and Polymarket
  event probabilities, surfaced to the news and macro analysts.
- **Programmatic report output.** `TradingAgentsGraph.save_reports()` writes the
  same report tree the CLI produces, for headless and API runs. (#1037)
- **Env-configurable reasoning depth** via `TRADINGAGENTS_OPENAI_REASONING_EFFORT`,
  `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, and `TRADINGAGENTS_ANTHROPIC_EFFORT`,
  each gated to the models that accept it.

### Changed

- **Verified data-access contract.** Symbol normalization on every vendor path
  (identity, returns, CLI, news); the configured vendor list is the exact
  resolution chain with no silent fallback to unselected vendors; a typed
  `VendorError` taxonomy; look-ahead-safe news windows; stale-OHLCV rejection;
  inclusive yfinance date ranges.
- **Config precedence.** An explicit `TRADINGAGENTS_*` value or CLI flag now wins
  over interactive defaults for debate and risk round counts,
  `--checkpoint / --no-checkpoint`, and the Docker provider profile; invalid
  boolean env values fail loudly. (#975, #976, #977)
- **Current-generation model catalog.** Refreshed provider lineups; retired
  `gpt-4.1`, Claude Sonnet 4.5, and the Gemini 2.5 line.
- **Optional vendors degrade** instead of aborting a run: a failed macro or
  prediction-market lookup returns a no-data sentinel.
- **Analyst prompts lead with the current date** so tool-call date ranges anchor
  to the run date rather than the model's training cutoff. (#836)

### Fixed

- **Instrument identity.** Deterministic ticker-to-company resolution prevents
  wrong-company hallucination, and a verified market-data snapshot grounds price
  and indicator claims. (#814, #830)
- **Social and market data sources.** Reddit RSS-first with 429 backoff,
  StockTwits transport hardening, and Alpha Vantage timeout plus
  key-versus-rate-limit handling.
- **Structured output.** Local OpenAI-compatible servers no longer reject
  object-form `tool_choice`; a thinking model that returns no parsed result falls
  back to free text; null-ish strings in optional price fields coerce to `None`.
  (#1038, #1051, #1057)

### Removed

- The no-op `analyst_concurrency_limit` config knob; parallel analyst execution
  is planned for a later release. (#979)
- The unused committed `uv.lock`. (#1030)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

[@CadeYu](https://github.com/CadeYu), [@Zavianx](https://github.com/Zavianx), [@weijianz-opc](https://github.com/weijianz-opc), [@naltun](https://github.com/naltun), [@brahmasky](https://github.com/brahmasky), [@nik2208](https://github.com/nik2208), [@thieucong98](https://github.com/thieucong98), [@Derekko-web](https://github.com/Derekko-web), [@LukiPrince](https://github.com/LukiPrince), [@Eddieargenal](https://github.com/Eddieargenal), [@Ghraven](https://github.com/Ghraven), [@ms32035](https://github.com/ms32035), [@yting27](https://github.com/yting27), [@nyxst4ck](https://github.com/nyxst4ck), [@KenCheung-AIxFinance](https://github.com/KenCheung-AIxFinance), [@yangyusheng2n](https://github.com/yangyusheng2n), [@fareloj](https://github.com/fareloj), [@haosenwang1018](https://github.com/haosenwang1018), [@octo-patch](https://github.com/octo-patch), [@seifenk](https://github.com/seifenk), [@CaoYuhaoCarl](https://github.com/CaoYuhaoCarl), [@mihailnica10](https://github.com/mihailnica10), [@Dado-hash](https://github.com/Dado-hash), [@Handsomemikezzz](https://github.com/Handsomemikezzz), [@ydhawesome](https://github.com/ydhawesome), [@macd2](https://github.com/macd2), [@AyushKar2005](https://github.com/AyushKar2005), [@wildhuman](https://github.com/wildhuman), [@robert23kim](https://github.com/robert23kim), [@bngness](https://github.com/bngness), [@tedix-rodrigo](https://github.com/tedix-rodrigo), [@malaccan](https://github.com/malaccan), [@rfalken78](https://github.com/rfalken78), [@dengli1971-droid](https://github.com/dengli1971-droid), [@proofconcept39](https://github.com/proofconcept39), [@prasta1](https://github.com/prasta1), [@liximin](https://github.com/liximin), [@jeffhuen](https://github.com/jeffhuen), [@mazar](https://github.com/mazar), [@soyangelromero](https://github.com/soyangelromero), [@CNQQC](https://github.com/CNQQC), [@dovetaill](https://github.com/dovetaill), [@fperdigon](https://github.com/fperdigon), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@RSXLX](https://github.com/RSXLX).

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
