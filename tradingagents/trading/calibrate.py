"""Nightly self-calibration (champion / challenger) of each strategy's thresholds.

Run once per strategy: ``engine`` moves the Quant Engine's tunables
(``params.TUNABLE``), ``momentum`` the rotation's (``params.MOMENTUM_TUNABLE``);
each night may change at most one field of each.

Each night, on the basket's last ``TRAIN_SESSIONS + HOLDOUT_SESSIONS`` sessions:

1. The thresholds in force (the champion) are traded on the training part
   and, separately, on the holdout part (the most recent sessions).
2. Challengers are the champion with ONE tunable moved one step either way,
   plus the previous champion (a way back) and the defaults (a reset).
3. A challenger must beat the champion on the training part by
   ``MIN_TRAIN_GAIN`` with at least ``MIN_TRAIN_TRADES`` buys; the best three
   such are then traded on the holdout, which none of them was picked on.
4. The first that also does at least as well as the champion on the holdout
   - better by ``MIN_HOLDOUT_GAIN`` when it traded there at least
   ``MIN_HOLDOUT_TRADES`` times, no worse when the holdout gave too few trades
   to judge (a quiet, risk-off stretch) - with a max drawdown no more than
   ``MAX_DRAWDOWN_WORSE`` points deeper, replaces it.

No challenger may draw down more than ``MAX_DRAWDOWN_ABS`` (25%, the risk
the owner accepts) on either part. So at most one small change per night, only
on evidence from data it was not chosen on, and a bad change is undone the same way it was made: the previous
champion is always a challenger. Every night's result is appended to
``calibration_history.jsonl``.

Score = return % minus half the max drawdown % (``simulator.score``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from tradingagents.quant import params as qparams
from tradingagents.quant.params import MOMENTUM_TUNABLE, TUNABLE, Params
from tradingagents.trading import simulator as sim

logger = logging.getLogger(__name__)

TRAIN_SESSIONS = 400
HOLDOUT_SESSIONS = 60
MIN_TRAIN_GAIN = 0.5
MIN_HOLDOUT_GAIN = 0.25
MIN_TRAIN_TRADES = 6
MIN_HOLDOUT_TRADES = 2
MAX_DRAWDOWN_WORSE = 2.0
TOP_FOR_HOLDOUT = 3
# The drawdown the owner accepts: no challenger deeper than this on either part.
MAX_DRAWDOWN_ABS = 25.0
HISTORY_FILE = "calibration_history.jsonl"

STRATEGIES = {"engine": "Quant Engine", "momentum": "Động lượng"}


def _runner(strategy: str):
    if strategy == "momentum":
        from tradingagents.trading import momentum

        return momentum.run
    return sim.run


def _keys(strategy: str) -> dict:
    return MOMENTUM_TUNABLE if strategy == "momentum" else TUNABLE


@dataclass
class Trial:
    name: str
    params: Params
    train: sim.Score | None = None
    holdout: sim.Score | None = None


@dataclass
class Report:
    as_of: str
    symbols: int
    sessions: int
    holdout_from: str
    champion: Trial
    tried: list[Trial] = field(default_factory=list)
    accepted: Trial | None = None
    note: str = ""
    strategy: str = "engine"

    def summary(self) -> str:
        c = self.champion
        lines = [f"Hiệu chỉnh {STRATEGIES.get(self.strategy, self.strategy)} {self.as_of} "
                 f"({self.symbols} mã, {self.sessions} phiên, kiểm tra từ {self.holdout_from})",
                 f"Bộ hiện tại - huấn luyện: {c.train.line()}",
                 f"Bộ hiện tại - kiểm tra: {c.holdout.line()}"]
        if self.accepted:
            a = self.accepted
            lines += [f"✅ ĐỔI: {a.name}",
                      f"   huấn luyện: {a.train.line()}",
                      f"   kiểm tra: {a.holdout.line()}"]
        else:
            lines.append(f"Giữ nguyên. {self.note}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        def trial(t: Trial | None):
            if t is None:
                return None
            return {"name": t.name, "params": qparams.to_dict(t.params),
                    "train": asdict(t.train) if t.train else None,
                    "holdout": asdict(t.holdout) if t.holdout else None}
        return {"as_of": self.as_of, "at": datetime.now().isoformat(timespec="seconds"),
                "symbols": self.symbols, "sessions": self.sessions, "holdout_from": self.holdout_from,
                "champion": trial(self.champion), "accepted": trial(self.accepted),
                "tried": [trial(t) for t in self.tried], "note": self.note, "strategy": self.strategy}


def _step(value, low, high, step, direction):
    new = value + direction * step
    new = int(round(new)) if isinstance(value, int) else round(new, 4)
    return new if low <= new <= high else None


def neighbours(champion: Params, keys: dict | None = None) -> list[Trial]:
    """The champion with one tunable moved one step up or down, within its bounds."""
    out = []
    for name, (low, high, step) in (keys or TUNABLE).items():
        current = getattr(champion, name)
        for direction in (-1, 1):
            new = _step(current, low, high, step, direction)
            if new is not None and new != current:
                out.append(Trial(f"{name}: {current} → {new}", replace(champion, **{name: new})))
    return out


def history_path() -> Path:
    return qparams._path().with_name(HISTORY_FILE)


def _previous_champion(current: Params, strategy: str = "engine") -> Params | None:
    """The thresholds in force before this strategy's last accepted change, if any
    (only that strategy's fields are taken back)."""
    path = history_path()
    if not path.exists():
        return None
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    keys = _keys(strategy)
    for row in reversed(rows):
        if row.get("accepted") and row.get("champion") and row.get("strategy", "engine") == strategy:
            old = qparams.coerce(row["champion"]["params"])
            previous = replace(current, **{k: getattr(old, k) for k in keys})
            return previous if previous != current else None
    return None


def calibrate(symbols: list[str], as_of: str, *, apply: bool = True, data: sim.Dataset | None = None,
              champion: Params | None = None, strategy: str = "engine") -> Report:
    """One night's calibration of one strategy. ``apply`` puts an accepted challenger in force."""
    champion = champion or qparams.active()
    run, keys = _runner(strategy), _keys(strategy)
    data = data or sim.build_dataset(symbols, TRAIN_SESSIONS + HOLDOUT_SESSIONS, as_of)
    if len(data.dates) < HOLDOUT_SESSIONS + 20:
        raise ValueError(f"only {len(data.dates)} sessions of history; need {HOLDOUT_SESSIONS + 20}")
    holdout_from = data.dates[-HOLDOUT_SESSIONS]
    train_end = data.dates[-HOLDOUT_SESSIONS - 1]

    def on_train(p):
        return sim.score(run(data, p, end=train_end))

    def on_holdout(p):
        return sim.score(run(data, p, start=holdout_from))

    champ = Trial("hiện tại", champion, on_train(champion), on_holdout(champion))
    report = Report(as_of=as_of, symbols=len(data.symbols), sessions=len(data.dates),
                    holdout_from=holdout_from, champion=champ, strategy=strategy)

    challengers = neighbours(champion, keys)
    previous = _previous_champion(champion, strategy)
    if previous is not None:
        challengers.append(Trial("quay lại bộ trước", previous))
    reset = replace(champion, **{k: getattr(qparams.DEFAULT, k) for k in keys})
    if reset != champion:
        challengers.append(Trial("về mặc định", reset))
    for trial in challengers:
        trial.train = on_train(trial.params)
    report.tried = challengers

    promising = sorted(
        (t for t in challengers
         if t.train.value >= champ.train.value + MIN_TRAIN_GAIN and t.train.trades >= MIN_TRAIN_TRADES
         and t.train.max_drawdown_pct <= MAX_DRAWDOWN_ABS),
        key=lambda t: t.train.value, reverse=True,
    )[:TOP_FOR_HOLDOUT]
    if not promising:
        report.note = "Không bộ nào thắng rõ trên dữ liệu huấn luyện."
    for trial in promising:
        trial.holdout = on_holdout(trial.params)
        judged = trial.holdout.trades >= MIN_HOLDOUT_TRADES
        margin = MIN_HOLDOUT_GAIN if judged else 0.0
        if (
            trial.holdout.value >= champ.holdout.value + margin
            and trial.holdout.max_drawdown_pct <= champ.holdout.max_drawdown_pct + MAX_DRAWDOWN_WORSE
            and trial.holdout.max_drawdown_pct <= MAX_DRAWDOWN_ABS
        ):
            report.accepted = trial
            break
    if promising and report.accepted is None:
        report.note = "Bộ thắng trên dữ liệu huấn luyện không giữ được lợi thế trên dữ liệu kiểm tra."

    if report.accepted and apply:
        qparams.save(report.accepted.params, note=f"{as_of} {strategy}: {report.accepted.name}")
        logger.info("Calibration %s: %s", as_of, report.accepted.name)
    _append_history(report)
    return report


def _append_history(report: Report) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")


def last_reports(limit: int = 10) -> list[dict]:
    path = history_path()
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
