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
   ``MIN_TRAIN_GAIN`` with at least ``MIN_TRAIN_TRADES`` buys; the best
   ``TOP_FOR_HOLDOUT`` such go on.
4. Walk-forward: the training part is cut into ``WALK_FORWARD_FOLDS``
   disjoint, consecutive windows; a challenger must score at least as well as
   the champion on EVERY one of them (a gain that comes from one lucky
   stretch is not a gain). Skipped when a window would be shorter than
   ``MIN_FOLD_SESSIONS`` (too short to mean anything).
5. Those left are traded on the holdout, which none of them was picked on.
   A challenger must have traded there at least ``MIN_HOLDOUT_TRADES`` times
   (one that simply stops trading while the champion loses proves nothing).
   The best on the holdout must beat the champion there by
   ``MIN_HOLDOUT_GAIN`` times the number of challengers that reached the
   holdout (a Bonferroni-like penalty: the more are tried, the likelier one
   wins by luck), with a max drawdown no more than ``MAX_DRAWDOWN_WORSE``
   points deeper. When the champion itself traded fewer than
   ``MIN_HOLDOUT_TRADES`` times there (it drifted too strict, or a risk-off
   stretch), there is no score to beat by a margin: a challenger that did
   trade there and is no worse wins, and so does the reset or the way back
   when no worse. A quiet champion must not veto its own replacement, or a
   do-nothing set would keep the bot in cash for good.
6. Rate limit: after a change, the strategy is not changed again for
   ``MIN_SESSIONS_BETWEEN_CHANGES`` sessions (read from the history). The
   holdout of one night overlaps the next night's by all but one session, so
   nightly retries would keep testing the same data until something passes.
   The rule holds for every challenger, the way back and the reset included.

No challenger may draw down more than ``MAX_DRAWDOWN_ABS`` (25%, the risk
the owner accepts) on either part. So at most one small change per strategy
every ``MIN_SESSIONS_BETWEEN_CHANGES`` sessions, only on evidence from data it
was not chosen on, and a bad change is undone the same way it was made: the
previous champion is always a challenger. Every night's result is appended to
``calibration_history.jsonl`` (one JSON object per line, timestamped in
Vietnam time; an unreadable line is skipped, never fatal).

Score = return % minus half the max drawdown % (``simulator.score``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from tradingagents.market import vn_rules
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
# Walk-forward check on the training part: disjoint windows, each at least this long.
WALK_FORWARD_FOLDS = 3
MIN_FOLD_SESSIONS = 40
# At most one accepted change per strategy in this many sessions.
MIN_SESSIONS_BETWEEN_CHANGES = 20
HISTORY_FILE = "calibration_history.jsonl"

STRATEGIES = {"engine": "Quant Engine", "momentum": "Động lượng"}
# Challengers that are known sets, not untested moves: the way back and the reset.
WAY_BACK, RESET = "quay lại bộ trước", "về mặc định"
KNOWN_SETS = (WAY_BACK, RESET)


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
    folds: list[sim.Score] | None = None     # walk-forward windows of the training part


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
    applied: bool = False      # the accepted challenger was put in force (not a dry run)

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
                    "holdout": asdict(t.holdout) if t.holdout else None,
                    "folds": [asdict(f) for f in t.folds] if t.folds else None}
        return {"as_of": self.as_of, "at": vn_rules.now_vn().isoformat(timespec="seconds"),
                "symbols": self.symbols, "sessions": self.sessions, "holdout_from": self.holdout_from,
                "champion": trial(self.champion), "accepted": trial(self.accepted),
                "applied": bool(self.accepted is not None and self.applied),
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


def _read_history() -> list[dict]:
    """Every readable night of the history, oldest first. A line that is not a JSON
    object (a write cut by a crash, a hand edit) is skipped, never fatal."""
    path = history_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            logger.warning("Calibration history: skipped an unreadable line in %s", path)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _changes(strategy: str, as_of: str | None = None) -> list[dict]:
    """The nights that put a change of this strategy in force (dry runs left out),
    oldest first; with ``as_of``, only those on or before it."""
    out = []
    for row in _read_history():
        if not (isinstance(row.get("accepted"), dict) and isinstance(row.get("champion"), dict)):
            continue
        if row.get("strategy", "engine") != strategy or not row.get("applied", True):
            continue
        if as_of is not None and str(row.get("as_of", "")) > as_of:
            continue
        out.append(row)
    return out


def _previous_champion(current: Params, strategy: str = "engine") -> Params | None:
    """The thresholds in force before this strategy's last accepted change, if any
    (only that strategy's fields are taken back)."""
    keys = _keys(strategy)
    for row in reversed(_changes(strategy)):
        try:
            old = qparams.coerce(row["champion"]["params"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        previous = replace(current, **{k: getattr(old, k) for k in keys})
        return previous if previous != current else None
    return None


def _sessions_since_change(strategy: str, as_of: str, dates: list[str]) -> tuple[str, int] | None:
    """(date, sessions since) of this strategy's last change on or before ``as_of``;
    None when there is none, or it is older than the dataset (long enough ago)."""
    changes = _changes(strategy, as_of)
    if not changes:
        return None
    last = str(changes[-1].get("as_of", ""))
    if not dates or last < dates[0]:
        return None
    return last, sum(1 for d in dates if last < d <= as_of)


def _folds(train_dates: list[str]) -> list[tuple[str, str]]:
    """``WALK_FORWARD_FOLDS`` disjoint consecutive (start, end) windows of the
    training part, or none when one would be shorter than ``MIN_FOLD_SESSIONS``."""
    n = len(train_dates)
    size = n // WALK_FORWARD_FOLDS if WALK_FORWARD_FOLDS > 0 else 0
    if size < MIN_FOLD_SESSIONS:
        return []
    bounds = [i * size for i in range(WALK_FORWARD_FOLDS)] + [n]
    return [(train_dates[bounds[i]], train_dates[bounds[i + 1] - 1]) for i in range(WALK_FORWARD_FOLDS)]


def holdout_margin(tested: int) -> float:
    """What the best of ``tested`` challengers must win by on the holdout."""
    return MIN_HOLDOUT_GAIN * max(tested, 1)


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

    def on_window(p, start, end):
        return sim.score(run(data, p, start=start, end=end))

    champ = Trial("hiện tại", champion, on_train(champion), on_holdout(champion))
    report = Report(as_of=as_of, symbols=len(data.symbols), sessions=len(data.dates),
                    holdout_from=holdout_from, champion=champ, strategy=strategy)

    recent = _sessions_since_change(strategy, as_of, data.dates)
    if recent is not None and recent[1] < MIN_SESSIONS_BETWEEN_CHANGES:
        report.note = (f"Vừa đổi ngày {recent[0]} ({recent[1]} phiên trước); chờ đủ "
                       f"{MIN_SESSIONS_BETWEEN_CHANGES} phiên mới xét đổi tiếp.")
        _append_history(report)
        return report

    challengers = neighbours(champion, keys)
    previous = _previous_champion(champion, strategy)
    if previous is not None:
        challengers.append(Trial(WAY_BACK, previous))
    reset = replace(champion, **{k: getattr(qparams.DEFAULT, k) for k in keys})
    if reset != champion:
        challengers.append(Trial(RESET, reset))
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

    folds = _folds([d for d in data.dates if d <= train_end])
    if promising and folds:
        champ.folds = [on_window(champion, start, end) for start, end in folds]
        for trial in promising:
            trial.folds = [on_window(trial.params, start, end) for start, end in folds]
        steady = [t for t in promising
                  if all(f.value >= c.value for f, c in zip(t.folds, champ.folds, strict=True))]
        if not steady:
            report.note = (f"Bộ thắng trên dữ liệu huấn luyện thua ở ít nhất một trong "
                           f"{len(folds)} giai đoạn huấn luyện - lợi thế không ổn định.")
        promising = steady

    for trial in promising:
        trial.holdout = on_holdout(trial.params)
    margin = holdout_margin(len(promising))

    def within_drawdown(t: Trial) -> bool:
        return (t.holdout.max_drawdown_pct <= champ.holdout.max_drawdown_pct + MAX_DRAWDOWN_WORSE
                and t.holdout.max_drawdown_pct <= MAX_DRAWDOWN_ABS)

    champ_quiet = champ.holdout.trades < MIN_HOLDOUT_TRADES
    if champ_quiet:
        # A champion that (almost) stopped trading cannot be beaten "by a margin" on
        # the holdout, and must not veto its own replacement forever (a set drifted too
        # strict would keep the bot in cash). So: a challenger that does trade there and
        # is no worse, or the reset / way back (known sets) when no worse, may replace it.
        qualified = [
            t for t in promising
            if (t.holdout.trades >= MIN_HOLDOUT_TRADES or t.name in KNOWN_SETS)
            and t.holdout.value >= champ.holdout.value and within_drawdown(t)
        ]
    else:
        qualified = [
            t for t in promising
            if t.holdout.trades >= MIN_HOLDOUT_TRADES
            and t.holdout.value >= champ.holdout.value + margin and within_drawdown(t)
        ]
    if qualified:
        report.accepted = max(qualified, key=lambda t: (t.holdout.trades >= MIN_HOLDOUT_TRADES, t.holdout.value))
    elif promising and champ_quiet:
        report.note = (f"Bộ hiện tại chỉ có {champ.holdout.trades} lệnh mua trên dữ liệu kiểm tra và không bộ "
                       "thắng nào giao dịch đủ ở đó - chưa đủ bằng chứng để đổi.")
    elif promising:
        report.note = "Bộ thắng trên dữ liệu huấn luyện không giữ được lợi thế trên dữ liệu kiểm tra."

    if report.accepted and apply:
        qparams.save(report.accepted.params, note=f"{as_of} {strategy}: {report.accepted.name}")
        report.applied = True
        logger.info("Calibration %s: %s", as_of, report.accepted.name)
    _append_history(report)
    return report


def _append_history(report: Report) -> None:
    """Append one line; a line cut short by an earlier crash is closed first so it
    cannot swallow this one, and the line is on disk before this returns."""
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(report.to_dict(), ensure_ascii=False) + "\n"
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                line = "\n" + line
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def last_reports(limit: int = 10) -> list[dict]:
    rows = _read_history()
    return rows[-limit:] if limit > 0 else []
