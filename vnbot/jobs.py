"""The analysis queue: one analysis at a time, results kept on disk.

Each job first runs the quantitative engine (seconds, no LLM) so the asker
gets the system decision at once, then the full multi-agent graph (minutes),
whose report is saved as Markdown and HTML under ``data/results/reports``.
Listeners (the Telegram bot) are told when each stage lands.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vnbot.settings import Settings, graph_config

logger = logging.getLogger(__name__)

MAX_JOBS_KEPT = 200
# A full analysis costs this many AI requests, give or take (four analysts with
# their tool rounds, the debate, the trader, three risk views, the manager).
CALLS_PER_ANALYSIS = 25
ACTIVE = ("queued", "quick", "running")


class QueueFull(ValueError):
    """Too many requests waiting; the message is shown to the asker."""


@dataclass
class Job:
    id: str
    ticker: str
    date: str
    source: str                    # "web" | "telegram" | "daily"
    chat_id: int | None = None
    status: str = "queued"         # queued | quick | running | done | failed
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    action: str = ""
    quick_summary: str = ""
    final_decision: str = ""
    guard_notes: str = ""
    report_dir: str = ""
    error: str = ""
    # Everyone who asked for this ticker and date while it was waiting or
    # running: the same analysis serves them all instead of running twice.
    chat_ids: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.chat_id is not None and self.chat_id not in self.chat_ids:
            self.chat_ids.append(self.chat_id)
        if self.source and self.source not in self.sources:
            self.sources.append(self.source)

    @property
    def html_path(self) -> Path | None:
        path = Path(self.report_dir) / "complete_report.html" if self.report_dir else None
        return path if path and path.exists() else None


Listener = Callable[[Job, str], None]   # (job, event) with event "quick" | "done" | "failed"


class JobQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._graph = None
        self._path = settings.data_dir / "jobs.json"
        self._load()
        self._worker = threading.Thread(target=self._run, name="analysis-worker", daemon=True)

    # ── lifecycle ──
    def start(self) -> None:
        self._worker.start()

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    # ── public API ──
    def submit(self, ticker: str, date: str, source: str, chat_id: int | None = None) -> Job:
        """Queue an analysis, or join the one already waiting/running for this ticker and date.

        Requests from people (web, Telegram) are refused with QueueFull once
        ``max_pending`` of them wait; the daily watchlist run is not capped
        (the watchlist itself is at most 30 codes).
        """
        with self._lock:
            for existing in self._jobs.values():
                if existing.ticker == ticker and existing.date == date and existing.status in ACTIVE:
                    if chat_id is not None and chat_id not in existing.chat_ids:
                        existing.chat_ids.append(chat_id)
                    if source not in existing.sources:
                        existing.sources.append(source)
                    self._save()
                    return existing
            if source != "daily":
                waiting = sum(1 for j in self._jobs.values() if j.status == "queued" and j.source != "daily")
                if waiting >= self.settings.max_pending:
                    raise QueueFull(
                        f"Hàng đợi đang có {waiting} lượt chờ (tối đa {self.settings.max_pending}). "
                        "Thử lại sau ít phút."
                    )
            job = Job(id=uuid.uuid4().hex[:10], ticker=ticker, date=date, source=source, chat_id=chat_id)
            self._jobs[job.id] = job
            self._trim()
            self._save()
        self._queue.put(job.id)
        return job

    def is_active(self) -> bool:
        with self._lock:
            return any(j.status in ACTIVE for j in self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 30) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]

    def position(self, job_id: str) -> int:
        """How many jobs run before this one (0 = running or next)."""
        with self._lock:
            waiting = [j for j in self._jobs.values() if j.status == "queued"]
        waiting.sort(key=lambda j: j.created)
        ids = [j.id for j in waiting]
        return ids.index(job_id) if job_id in ids else 0

    # ── worker ──
    def _graph_instance(self):
        if self._graph is None:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            self._graph = TradingAgentsGraph(config=graph_config(self.settings))
        return self._graph

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
                logger.exception("Job %s (%s) failed", job.id, job.ticker)
                job.status, job.error, job.finished = "failed", str(exc)[:500], time.time()
                self._persist()
                self._notify(job, "failed")

    def _process(self, job: Job) -> None:
        from tradingagents.quant import engine

        job.status, job.started = "quick", time.time()
        self._persist()
        quick = engine.analyze(job.ticker, job.date)
        job.action, job.quick_summary = quick.action, quick.deterministic_summary
        self._persist()
        self._notify(job, "quick")

        from tradingagents.llm_clients.fallback_client import budget_left

        left = budget_left()
        if left is not None and left < CALLS_PER_ANALYSIS:
            # Refused before the first request: a half-run analysis would spend the rest for nothing.
            raise RuntimeError(f"Hạn mức AI hôm nay còn {left} lượt, không đủ cho phân tích đầy đủ "
                               f"(khoảng {CALLS_PER_ANALYSIS} lượt). Dùng /phanbien (1 lượt) hoặc thử lại ngày mai.")
        job.status = "running"
        self._persist()
        graph = self._graph_instance()
        state, rating = graph.propagate(job.ticker, job.date)
        report_dir = self.settings.data_dir / "results" / "reports" / f"{job.ticker}_{job.date}_{job.id}"
        graph.save_reports(state, job.ticker, save_path=report_dir)
        job.action = rating
        job.final_decision = state.get("final_trade_decision", "")
        job.guard_notes = state.get("guard_notes", "")
        job.report_dir = str(report_dir)
        job.status, job.finished = "done", time.time()
        self._persist()
        self._prune_reports()
        self._notify(job, "done")

    def _notify(self, job: Job, event: str) -> None:
        for listener in self._listeners:
            try:
                listener(job, event)
            except Exception:  # noqa: BLE001 - a listener failure is not the job's
                logger.exception("Listener failed for job %s (%s)", job.id, event)

    # ── storage ──
    def _persist(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        data = [asdict(j) for j in self._jobs.values()]
        write_text_atomic(self._path, json.dumps(data, ensure_ascii=False))

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("jobs.json unreadable; starting with no history")
            return
        for row in rows:
            job = Job(**{k: v for k, v in row.items() if k in Job.__dataclass_fields__})
            if job.status in ACTIVE:
                # The process stopped mid-job: say so rather than show it running forever.
                job.status, job.error = "failed", "máy chủ khởi động lại khi đang phân tích"
            self._jobs[job.id] = job

    def _trim(self) -> None:
        if len(self._jobs) <= MAX_JOBS_KEPT:
            return
        for job in sorted(self._jobs.values(), key=lambda j: j.created)[: len(self._jobs) - MAX_JOBS_KEPT]:
            if job.status in ("done", "failed"):
                self._jobs.pop(job.id, None)

    def _prune_reports(self) -> None:
        """Keep the newest ``keep_reports`` report folders (2 GB of disk is shared)."""
        root = self.settings.data_dir / "results" / "reports"
        if not root.exists():
            return
        folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in folders[self.settings.keep_reports:]:
            shutil.rmtree(old, ignore_errors=True)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` all at once: a unique temporary file in the same
    folder, flushed to disk, then renamed over. A crash leaves the old file or the new
    one, never half of one, and two writers never share a temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prune_cache(settings: Settings) -> int:
    """Delete cached downloads older than ``cache_max_age_days``; returns how many."""
    root = settings.data_dir / "cache"
    if not root.exists():
        return 0
    cutoff = time.time() - settings.cache_max_age_days * 86400
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
