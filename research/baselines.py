"""Baseline runs on TRAIN, VALID and TRAIN+VALID -> baselines.json and baselines.md.

    python -m research baselines [--no-quant] [--out research/out]

Never touches TEST (the panel is loaded locked).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from research import metrics as M
from research.backtest import run
from research.config import OUT_DIR, SPLITS
from research.data import load_panel
from research.factor_book import DefensiveBook
from research.strategies import EqualWeightRebalance, RepoMomentum

KEYS = ["total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "calmar", "turnover_per_year",
        "buys", "pct_time_invested", "avg_exposure_pct", "exposure_adj_cagr_pct"]


def vnindex_bh(panel, start, end) -> dict:
    s = panel.vnindex.loc[start:end]
    nav = s / s.iloc[0] * 1e9
    m = M.compute(nav, 1e9, pd.Series(1.0, index=nav.index), None)
    m["yearly"] = M.yearly(nav, 1e9, None, None).reset_index().to_dict("records")
    return m


def pack(res) -> dict:
    return {"strategy": res.name, "universe": res.universe_kind, "start": res.start, "end": res.end,
            "metrics": res.metrics, "vnindex": res.benchmark,
            "yearly": res.yearly.reset_index().to_dict("records"), "warnings": res.warnings}


def build_parser(ap: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    ap = ap or argparse.ArgumentParser(prog="python -m research baselines")
    ap.add_argument("--no-quant", action="store_true", help="skip the Quant Engine run (slow)")
    ap.add_argument("--out", default=str(OUT_DIR), help=f"output directory (default {OUT_DIR})")
    return ap


def main(argv=None, args=None) -> int:
    args = args or build_parser().parse_args(argv)
    out_dir = Path(args.out)
    try:
        panel = load_panel()
    except FileNotFoundError as exc:          # no data downloaded yet: say so, as `run` does
        print(str(exc), file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {"generated": pd.Timestamp.now().isoformat(timespec="seconds"), "data": panel.info, "runs": []}
    for split, (start, end) in SPLITS.items():
        out["runs"].append({"split": split, "strategy": "vnindex_buyhold", "universe": "-", "start": start, "end": end,
                            "metrics": vnindex_bh(panel, start, end)})
        jobs = [
            (RepoMomentum(), "vn30_today", {}),
            (RepoMomentum(), "liq_top30", {}),
            (EqualWeightRebalance(), "liq_top30", {}),
            (DefensiveBook(), "liq_top30", {}),
        ]
        if split == "TRAIN+VALID":
            jobs += [(RepoMomentum(name="repo_momentum_slip2x"), "liq_top30", {"slippage_mult": 2.0}),
                     (RepoMomentum(name="repo_momentum_adv5pct"), "liq_top30", {"max_adv_pct": 0.05}),
                     (RepoMomentum(), "liq_top50", {}),
                     (EqualWeightRebalance(), "vn30_today", {})]
        for strat, kind, kw in jobs:
            t = time.time()
            res = run(strat, start, end, universe_kind=kind, panel=panel, **kw)
            rec = pack(res)
            rec["split"] = split
            rec["options"] = kw
            rec["seconds"] = round(time.time() - t, 1)
            out["runs"].append(rec)
            print(split, res.summary(), f"({rec['seconds']}s)", flush=True)
    if not args.no_quant:
        from research.quant_engine import QuantEngine, build_dataset

        start, end = SPLITS["VALID"]
        t = time.time()
        try:
            data = build_dataset(panel.vn30_today, start, end)
            built = time.time() - t
            res = run(QuantEngine(data), start, end, universe_kind="vn30_today", panel=panel)
            rec = pack(res)
            rec.update(split="VALID", options={"flows": False}, seconds=round(time.time() - t, 1),
                       note=f"simulator.build_dataset on today's VN30, money flows NOT replayed, DEFAULT params; "
                            f"dataset built in {built:.0f}s; skipped symbols: {data.skipped}")
            out["runs"].append(rec)
            print("VALID", res.summary(), flush=True)
        except Exception as exc:  # noqa: BLE001
            out["runs"].append({"split": "VALID", "strategy": "quant_engine", "error": repr(exc)})
            print("quant engine failed:", exc)
    (out_dir / "baselines.json").write_text(json.dumps(out, indent=1, default=str))
    (out_dir / "baselines.md").write_text(to_markdown(out))
    print(f"wrote {out_dir / 'baselines.json'} and {out_dir / 'baselines.md'}")
    return 0


def _fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def to_markdown(out: dict) -> str:
    lines = ["# Baselines (harness, repo ledger: fees 0.15%/0.15%+0.1% tax, 0.1% slippage/side, T+2, lots, bands)",
             "", f"Generated {out['generated']}. Data through {out['data'].get('data_end')} "
             f"(panel truncated at {out['data'].get('truncated_at', '-')}), {out['data'].get('n_symbols')} stocks. "
             "Capital 1e9 VND. VNINDEX buy&hold has no costs (price index, no dividends).", ""]
    for split in SPLITS:
        lines += [f"## {split}", "",
                  "| strategy | universe | options | total % | CAGR % | MDD % | Sharpe | Calmar | turnover/yr | buys | time inv % | avg exp % | exp-adj CAGR % | VNINDEX % |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in out["runs"]:
            if r.get("split") != split or "metrics" not in r:
                continue
            m = r["metrics"]
            opts = ",".join(f"{k}={v}" for k, v in (r.get("options") or {}).items()) or "-"
            bench = r.get("vnindex", {}).get("total_return_pct") if r.get("vnindex") else m.get("total_return_pct")
            lines.append("| " + " | ".join([
                r["strategy"], r.get("universe", "-"), opts, _fmt(m.get("total_return_pct")), _fmt(m.get("cagr_pct")),
                _fmt(m.get("max_drawdown_pct")), _fmt(m.get("sharpe"), 2), _fmt(m.get("calmar"), 2),
                _fmt(m.get("turnover_per_year"), 2), _fmt(m.get("buys")), _fmt(m.get("pct_time_invested")),
                _fmt(m.get("avg_exposure_pct")), _fmt(m.get("exposure_adj_cagr_pct")), _fmt(bench)]) + " |")
        lines.append("")
    # Yearly table for TRAIN+VALID
    lines += ["## Calendar-year returns % (TRAIN+VALID runs; VNINDEX last column)", ""]
    runs = [r for r in out["runs"] if r.get("split") == "TRAIN+VALID" and r.get("yearly")]
    if runs:
        years = [y["year"] for y in runs[0]["yearly"]]
        head = [f"{r['strategy']}/{r.get('universe')}" + ("*" if r.get("options") else "") for r in runs]
        lines.append("| year | " + " | ".join(head) + " | VNINDEX |")
        lines.append("|---" * (len(head) + 2) + "|")
        for i, y in enumerate(years):
            row = [str(y)] + [_fmt(r["yearly"][i]["return_pct"]) for r in runs]
            row.append(_fmt(runs[0]["yearly"][i].get("vnindex_pct")))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append("`*` = with options (slippage_mult / max_adv_pct), see tables above.")
    lines += ["", "## Reading these numbers", "",
              "- `vn30_today` rows use TODAY's VN30 members back to 2013: survivorship + look-ahead. "
              "`ew_monthly/vn30_today` (+534%) is effectively buy&hold of the ~15 of today's VN30 already "
              "listed in 2013 (the ledger never resizes, so later listings get no cash): it measures the bias, "
              "not a strategy. Compare it with `ew_monthly/liq_top30` (+143%).",
              "- Point-in-time (`liq_top30`) the repo momentum (DEFAULT params) made +101% over 2013-2023 "
              "vs VNINDEX +170% (price index, no dividends), with a -49% max drawdown; it lost to the index in "
              "7 of 11 years. 2x slippage costs it ~1.3%/yr CAGR (turnover ~7x/yr).",
              "- `factor` = the bot's default book (\"Phòng thủ\", `tradingagents.trading.factor`) on its own "
              "cap50 universe (the `universe` column shows it), driven by the live module.",
              "- `ew_monthly` = equal weight over the universe, reconstituted at each month end; held names are "
              "not resized (ledger: one entry, full exit).",
              "- `quant_engine` (VALID): with money flows not replayed the engine almost never says BUY "
              "(2 BUY out of 4,488 stock-days on FPT/HPG/MWG/VCB/SSI/TCB; the rest NO_TRADE/WATCH), so it sat "
              "~98% in cash and lost 4.7% on costs/stops. Not representative of the live engine with flows.",
              ]
    notes = [r for r in out["runs"] if r.get("note") or r.get("error") or r.get("warnings")]
    if notes:
        lines += ["", "## Notes", ""]
        for r in notes:
            if r.get("note"):
                lines.append(f"- {r['strategy']} ({r['split']}): {r['note']}")
            if r.get("error"):
                lines.append(f"- {r['strategy']} ({r['split']}): ERROR {r['error']}")
            for w in r.get("warnings") or []:
                lines.append(f"- {r['strategy']}/{r.get('universe')} ({r['split']}): {w}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
