"""Command line of the research harness: ``python -m research <command>``.

    python -m research download [--workers 4] [--refresh] [--symbols FPT,VNM]
    python -m research baselines [--no-quant] [--out research/out]
    python -m research list
    python -m research run factor --split VALID
    python -m research run momentum --split TRAIN --universe liq_top50 --slippage-mult 2
    python -m research run ew --start 2016-01-01 --end 2019-12-31 --out research/out/ew_2016
    python -m research run factor --split TEST --unlock-test      # evaluator only, once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from research import config

SPLIT_CHOICES = ("TRAIN", "VALID", "TRAIN+VALID", "TEST", "ALL")

METRIC_ROWS = [
    ("total_return_pct", "Total return %"),
    ("cagr_pct", "CAGR %"),
    ("max_drawdown_pct", "Max drawdown %"),
    ("sharpe", "Sharpe (daily, rf=0)"),
    ("vol_ann_pct", "Volatility %/yr"),
    ("calmar", "Calmar"),
    ("avg_exposure_pct", "Avg exposure %"),
    ("pct_time_invested", "Time invested %"),
    ("exposure_adj_cagr_pct", "CAGR / exposure %"),
    ("turnover_per_year", "Turnover /yr (one-way)"),
    ("buys", "Buys"),
    ("sells", "Sells"),
    ("trades_per_year", "Buys /yr"),
    ("win_rate_pct", "Win rate % (sells)"),
    ("fees_tax_paid_pct_capital", "Fees+tax % of capital"),
]


def split_dates(split: str) -> tuple[str, str]:
    split = split.upper()
    if split == "TEST":
        return config.TEST
    if split == "ALL":
        return config.ALL
    return config.SPLITS[split]


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def metrics_table(res) -> pd.DataFrame:
    rows = []
    for key, label in METRIC_ROWS:
        if key not in res.metrics:
            continue
        rows.append({"metric": label, res.name: _fmt(res.metrics.get(key)),
                     "VNINDEX": _fmt(res.benchmark.get(key)) if key in res.benchmark else "-"})
    return pd.DataFrame(rows).set_index("metric")


def report(res, *, capital: float, options: dict) -> str:
    opts = ", ".join(f"{k}={v}" for k, v in options.items() if v is not None)
    lines = [f"{res.name} [{res.universe_kind}] {res.start}..{res.end} ({res.metrics['sessions']} sessions, "
             f"capital {capital:,.0f} VND{', ' + opts if opts else ''})", "",
             metrics_table(res).to_string(), "", "Calendar-year returns (%):", res.yearly.to_string()]
    if res.warnings:
        lines += ["", "Warnings:"] + [f"- {w}" for w in res.warnings]
    lines += ["", res.summary()]
    return "\n".join(lines)


def write_outputs(res, out: Path, options: dict) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / "summary.json", out / "nav.csv", out / "fills.csv", out / "yearly.csv"]
    summary = {"strategy": res.name, "universe": res.universe_kind, "start": res.start, "end": res.end,
               "options": options, "metrics": res.metrics, "vnindex": res.benchmark,
               "yearly": res.yearly.reset_index().to_dict("records"), "warnings": res.warnings}
    paths[0].write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    res.daily.to_csv(paths[1], index_label="date")
    res.fills.to_csv(paths[2], index=False)
    res.yearly.to_csv(paths[3])
    return paths


def cmd_list(_args) -> int:
    from research.strategies import STRATEGIES

    width = max(len(k) for k in STRATEGIES)
    for name, text in STRATEGIES.items():
        print(f"{name:<{width}}  {text}")
    return 0


def cmd_run(args) -> int:
    from research.backtest import TestPeriodLocked, run, run_index
    from research.data import load_panel
    from research.strategies import make

    start, end = split_dates(args.split)
    start, end = args.start or start, args.end or end
    unlock = bool(args.unlock_test)
    if pd.Timestamp(end) > pd.Timestamp(config.LOCK_AFTER) and not unlock:
        print(f"TEST is locked: the period ends {end}, after {config.LOCK_AFTER}. Tune on TRAIN, check on VALID; "
              "run TEST once at the end with --unlock-test.", file=sys.stderr)
        return 2
    options = {"split": None if (args.start or args.end) else args.split, "universe": args.universe,
               "slippage_mult": args.slippage_mult, "max_adv_pct": args.max_adv_pct,
               "unlock_test": unlock or None}
    t0 = time.time()
    try:
        panel = load_panel(unlock_test=unlock)
        if args.strategy.lower() == "vnindex":
            options.update(universe=None, slippage_mult=None, max_adv_pct=None)
            res = run_index(start, end, capital=args.capital, unlock_test=unlock, panel=panel)
        else:
            strategy = make(args.strategy, start=start, end=end, unlock_test=unlock, panel=panel,
                            next_day=args.next_day)
            if args.strategy.lower() == "factor":
                options["next_day"] = args.next_day
            if getattr(strategy, "universe_label", None):
                options["universe"] = None          # the strategy's own universe (shown in the header)
            res = run(strategy, start, end, capital=args.capital, universe_kind=args.universe,
                      slippage_mult=args.slippage_mult, max_adv_pct=args.max_adv_pct, unlock_test=unlock, panel=panel)
    except TestPeriodLocked as exc:
        print(f"TEST is locked: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(report(res, capital=args.capital, options={k: v for k, v in options.items() if k != "split"}))
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        for path in write_outputs(res, Path(args.out), options):
            print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m research", description="Research harness for TradingAgents VN "
                                 f"(data: {config.DATA_DIR}; set {config.DATA_ENV} to move it).")
    sub = ap.add_subparsers(dest="command", required=True)

    from research import baselines, download

    download.build_parser(sub.add_parser("download", help="fetch / update the data set (incremental, resumable)"))
    baselines.build_parser(sub.add_parser("baselines", help="baseline runs on TRAIN, VALID, TRAIN+VALID"))
    sub.add_parser("list", help="the strategies `run` knows")

    r = sub.add_parser("run", help="backtest one strategy and print its metrics and yearly returns")
    r.add_argument("strategy", help="factor | momentum | ew | quant | vnindex | buyhold:SYM (see `list`)")
    r.add_argument("--split", default="VALID", type=str.upper, choices=SPLIT_CHOICES,
                   help="fixed period (default VALID); TEST and ALL need --unlock-test")
    r.add_argument("--start", help="first day (YYYY-MM-DD), overrides --split")
    r.add_argument("--end", help="last day (YYYY-MM-DD), overrides --split")
    r.add_argument("--unlock-test", action="store_true", help="allow sessions after 2023-12-31 (evaluator only)")
    r.add_argument("--capital", type=float, default=1e9, help="starting cash in VND (default 1e9)")
    r.add_argument("--slippage-mult", type=float, default=1.0, help="scale the ledger's 0.1%% slippage (stress test)")
    r.add_argument("--max-adv-pct", type=float, default=None,
                   help="cap each buy at this fraction of the 20-session average volume (e.g. 0.05)")
    r.add_argument("--universe", default="liq_top30",
                   help="liq_topN or vn30_today (default liq_top30; factor uses its own cap universe)")
    r.add_argument("--next-day", default="calendar", choices=("calendar", "guess"),
                   help="factor only: the next session as known (calendar) or guessed like the bot's latest session")
    r.add_argument("--out", help="directory for summary.json, nav.csv, fills.csv, yearly.csv")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        from research import download

        return download.main(args=args)
    if args.command == "baselines":
        from research import baselines

        return baselines.main(args=args)
    if args.command == "list":
        return cmd_list(args)
    return cmd_run(args)
