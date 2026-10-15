"""Performance metrics from a daily NAV series (and the fills, when there are any)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

SESSIONS_PER_YEAR = 252


def _returns(nav: pd.Series, capital: float) -> pd.Series:
    return pd.concat([pd.Series([capital]), nav.reset_index(drop=True)]).pct_change().dropna().set_axis(nav.index)


def max_drawdown(nav: pd.Series, capital: float | None = None) -> float:
    vals = nav.to_numpy(dtype=float)
    if capital is not None:
        vals = np.concatenate([[capital], vals])
    peak = np.maximum.accumulate(vals)
    return float(((vals / peak) - 1).min() * 100) if len(vals) else 0.0


def compute(nav: pd.Series, capital: float, exposure: pd.Series | None = None, fills: pd.DataFrame | None = None) -> dict:
    """Metrics of a NAV path that starts from ``capital`` before its first session.

    - years: calendar days from the first to the last session / 365.25
      (at least one session's worth).
    - sharpe: daily returns, rf = 0, x sqrt(252).
    - turnover_per_year: one-way, (buys + sells) / 2 / average NAV / years.
    - pct_time_invested: share of sessions with exposure > 5% of NAV.
    - exposure_adj_cagr_pct: CAGR / average exposure (return per unit of capital at risk).
    """
    nav = nav.astype(float)
    r = _returns(nav, capital)
    days = (nav.index[-1] - nav.index[0]).days
    years = max(days / 365.25, 1 / SESSIONS_PER_YEAR)
    total = nav.iloc[-1] / capital - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    std = r.std(ddof=1)
    sharpe = float(r.mean() / std * math.sqrt(SESSIONS_PER_YEAR)) if std and std > 0 else 0.0
    mdd = max_drawdown(nav, capital)
    out = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "sessions": int(len(nav)),
        "years": round(years, 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "sharpe": round(sharpe, 3),
        "vol_ann_pct": round(float(std * math.sqrt(SESSIONS_PER_YEAR) * 100), 2) if std == std else None,
        "calmar": round(cagr * 100 / abs(mdd), 3) if mdd < 0 else None,
    }
    if exposure is not None:
        exp_mean = float(exposure.mean())
        out["avg_exposure_pct"] = round(exp_mean * 100, 1)
        out["pct_time_invested"] = round(float((exposure > 0.05).mean() * 100), 1)
        out["exposure_adj_cagr_pct"] = round(cagr * 100 / exp_mean, 2) if exp_mean > 0.01 else None
    if fills is not None:
        if len(fills):
            value = (fills["qty"] * fills["price"])
            buys = fills[fills["side"] == "BUY"]
            sells = fills[fills["side"] == "SELL"]
            out["turnover_per_year"] = round(float(value.sum() / 2 / nav.mean() / years), 2)
            out["buys"] = int(len(buys))
            out["sells"] = int(len(sells))
            out["trades_per_year"] = round(len(buys) / years, 1)
            pnl = sells["pnl"].dropna()
            out["win_rate_pct"] = round(float((pnl > 0).mean() * 100), 1) if len(pnl) else None
            out["fees_tax_paid_pct_capital"] = round(float(fills["cost"].sum() / capital * 100), 2)
        else:
            out.update(turnover_per_year=0.0, buys=0, sells=0, trades_per_year=0.0, win_rate_pct=None,
                       fees_tax_paid_pct_capital=0.0)
    return _plain(out)


def _plain(d: dict) -> dict:
    """numpy scalars -> Python numbers (JSON-friendly)."""
    return {k: (v.item() if isinstance(v, np.generic) else v) for k, v in d.items()}


def yearly(nav: pd.Series, capital: float, exposure: pd.Series | None = None, bench: pd.Series | None = None) -> pd.DataFrame:
    """One row per calendar year: return, max drawdown, Sharpe, exposure, VNINDEX return."""
    rows = []
    prev_nav, prev_bench = capital, None
    if bench is not None:
        prev_bench = float(bench.iloc[0])
    for year, chunk in nav.groupby(nav.index.year):
        r = _returns(chunk, prev_nav)
        std = r.std(ddof=1)
        row = {"year": int(year), "return_pct": round((chunk.iloc[-1] / prev_nav - 1) * 100, 2),
               "max_drawdown_pct": round(max_drawdown(chunk, prev_nav), 2),
               "sharpe": round(float(r.mean() / std * math.sqrt(SESSIONS_PER_YEAR)), 2) if std and std > 0 else None}
        if exposure is not None:
            row["avg_exposure_pct"] = round(float(exposure.loc[chunk.index].mean() * 100), 1)
        if bench is not None:
            b = bench.loc[chunk.index]
            row["vnindex_pct"] = round((b.iloc[-1] / prev_bench - 1) * 100, 2)
            row["excess_pct"] = round(row["return_pct"] - row["vnindex_pct"], 2)
            prev_bench = float(b.iloc[-1])
        rows.append(row)
        prev_nav = float(chunk.iloc[-1])
    return pd.DataFrame(rows).set_index("year")
