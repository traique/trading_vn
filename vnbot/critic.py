"""AI phản biện: one AI request per candidate, the way the OKX bot asks its AI.

The quantitative engine picks the action and every price; the critic is asked
once to find reasons to reject it and answers in JSON (agree or reject, a
confidence, a reason). Its verdict is advice: it never changes the engine's
action or levels, and the trading books do not follow it.

Every verdict is kept and later settled on the bars that followed ("shadow"
results, as the OKX bot tracks the signals its AI turned down), so the owner
sees whether the AI's objections were right before trusting them:

- BUY: entered at the decision day's close; the first of stop (-1R, checked
  first within a session) or target (+R:R) on the following sessions, else the
  close after ``HOLD_SESSIONS`` sessions, in R.
- SELL: the price change over ``SELL_SESSIONS`` sessions; the call was right
  when the price fell.

The last settled cases go back into the prompt as lessons, at no extra request.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

from vnbot.jobs import write_text_atomic

logger = logging.getLogger(__name__)

HOLD_SESSIONS = 20
SELL_SESSIONS = 10
MAX_KEPT = 500
LESSONS_IN_PROMPT = 5
_LOCK = threading.Lock()      # the daily run and Telegram share one log file

SYSTEM = (
    "Bạn là người phản biện độc lập cho một hệ thống giao dịch cổ phiếu Việt Nam (HOSE/HNX/UPCoM, T+2, "
    "biên độ giá). Hệ thống định lượng đã ra quyết định dưới đây bằng code. Nhiệm vụ của bạn: tìm lý do để "
    "TỪ CHỐI quyết định đó (rủi ro thị trường chung, dòng tiền, tin tức, kỹ thuật, thanh khoản, R:R). "
    "Chỉ đồng ý khi không tìm được rủi ro đáng kể. Bạn không được đổi hành động hay các mức giá.\n"
    "Trả lời DUY NHẤT một object JSON, không thêm chữ nào khác:\n"
    '{"verdict": "AGREE" hoặc "REJECT", "confidence": số 0-100 (độ chắc của bạn), '
    '"reason": "1-2 câu tiếng Việt", "risks": ["rủi ro chính", "..."]}'
)


def build_prompt(result, lessons: list[str] | None = None) -> str:
    """The engine's decision as a short text, plus the lessons of past verdicts."""
    parts = [result.deterministic_summary or f"{result.symbol}: {result.action}"]
    extra = list((result.reasons or [])[4:])
    if extra:
        parts.append("Lý do khác: " + "; ".join(extra))
    if getattr(result, "scenarios", None):
        parts.append("Kịch bản: " + "; ".join(f"{s.get('name')}: {s.get('trigger')} -> {s.get('action')}"
                                              for s in result.scenarios[:3]))
    if lessons:
        parts.append("Kết quả các lần phản biện trước (để bạn tự hiệu chỉnh):\n" + "\n".join(lessons))
    return "\n".join(parts)


def parse(text: str) -> dict | None:
    """The verdict in an answer, or None when it has none."""
    data, decoder, text = None, json.JSONDecoder(), text or ""
    for start in (m.start() for m in re.finditer(r"\{", text)):   # the first object with a verdict
        try:
            candidate, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            data = candidate
            break
    if data is None:
        return None
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in ("AGREE", "REJECT"):
        return None
    try:
        confidence = max(0, min(100, int(float(data.get("confidence", 0)))))
    except (TypeError, ValueError):
        confidence = 0
    risks = data.get("risks") or []
    if not isinstance(risks, list):
        risks = [str(risks)]
    return {"verdict": verdict, "confidence": confidence, "reason": str(data.get("reason", "")).strip()[:500],
            "risks": [str(r)[:200] for r in risks[:4]]}


def default_llm(settings):
    from tradingagents.llm_clients.factory import create_llm_client

    return create_llm_client("router9_chain", settings.quick_model).get_llm()


def ask(result, llm, lessons: list[str] | None = None) -> dict:
    """One request. Raises the provider chain's error when no provider answers."""
    from langchain_core.messages import HumanMessage, SystemMessage

    reply = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=build_prompt(result, lessons))])
    content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content, ensure_ascii=False)
    verdict = parse(content)
    if verdict is None:
        raise ValueError(f"AI trả lời không đúng định dạng: {content[:160]!r}")
    return verdict


def verdict_line(code: str, result, verdict: dict | None, error: str = "") -> str:
    head = f"{code} {result.action} ({result.confidence * 100:.0f}%)"
    if verdict is None:
        return f"⚠️ {head} → AI không trả lời: {error[:160]}"
    icon, word = ("✅", "ĐỒNG Ý") if verdict["verdict"] == "AGREE" else ("🛑", "PHẢN ĐỐI")
    risks = f"\n   Rủi ro: {'; '.join(verdict['risks'])}" if verdict["risks"] else ""
    return f"{icon} {head} → AI {word} {verdict['confidence']}%: {verdict['reason']}{risks}"


class CriticLog:
    """Verdicts on disk (``critic_log.json``) and their shadow results."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "critic_log.json"
        self._lock = _LOCK

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _write(self, rows: list[dict]) -> None:
        write_text_atomic(self.path, json.dumps(rows[-MAX_KEPT:], ensure_ascii=False))

    def add(self, day: str, result, verdict: dict) -> None:
        row = {"day": day, "code": result.symbol, "action": result.action, "price": result.price,
               "stop": result.stop_price, "target": result.target_price,
               "verdict": verdict["verdict"], "confidence": verdict["confidence"], "reason": verdict["reason"],
               "outcome": None}
        with self._lock:
            rows = [r for r in self._read() if not (r["day"] == day and r["code"] == row["code"])]
            rows.append(row)
            self._write(rows)

    def rows(self) -> list[dict]:
        with self._lock:
            return self._read()

    def settle_now(self) -> None:
        """Settle on fresh bars; a failure only delays the shadow results."""
        try:
            self.settle(bars_after)
        except Exception:  # noqa: BLE001 - the verdicts still go out
            logger.exception("Critic settle failed")

    def review(self, day: str, result, llm) -> str:
        """Ask the critic about one decision, keep its verdict, and say it as text."""
        try:
            verdict = ask(result, llm, self.lessons())
        except Exception as exc:  # noqa: BLE001 - budget spent, providers resting, a bad answer
            logger.warning("Critic %s: %s", result.symbol, exc)
            return verdict_line(result.symbol, result, None, str(exc))
        self.add(day, result, verdict)
        return verdict_line(result.symbol, result, verdict)

    def settle(self, bars_after: Callable[[str, str], list[dict]]) -> int:
        """Settle what the following bars now decide; ``bars_after(code, day)`` gives the
        sessions after ``day`` as dicts with High, Low, Close. Returns how many settled."""
        with self._lock:
            rows = self._read()
            settled = 0
            for row in rows:
                if row.get("outcome") is not None:
                    continue
                try:
                    bars = bars_after(row["code"], row["day"])
                except Exception as exc:  # noqa: BLE001 - one code's data must not stop the rest
                    logger.warning("Critic settle %s %s: %s", row["code"], row["day"], exc)
                    continue
                outcome = settle_one(row, bars)
                if outcome is not None:
                    row["outcome"] = outcome
                    settled += 1
            if settled:
                self._write(rows)
            return settled

    def lessons(self, n: int = LESSONS_IN_PROMPT) -> list[str]:
        done = [r for r in self.rows() if r.get("outcome") is not None][-n:]
        return [lesson(r) for r in done]

    def stats(self) -> dict:
        """For BUY verdicts: how the trades the AI agreed with and rejected turned out."""
        out = {}
        done = [r for r in self.rows() if r.get("outcome") is not None and r["action"] == "BUY"]
        for verdict in ("AGREE", "REJECT"):
            rs = [r["outcome"]["r"] for r in done if r["verdict"] == verdict]
            out[verdict] = {"n": len(rs), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                            "win": round(100 * sum(1 for x in rs if x > 0) / len(rs)) if rs else None}
        rejected = out["REJECT"]["n"]
        right = sum(1 for r in done if r["verdict"] == "REJECT" and r["outcome"]["r"] <= 0)
        out["veto_right_pct"] = round(100 * right / rejected) if rejected else None
        out["pending"] = sum(1 for r in self.rows() if r.get("outcome") is None)
        return out

    def stats_text(self) -> str:
        s = self.stats()
        a, r = s["AGREE"], s["REJECT"]
        if not a["n"] and not r["n"]:
            return f"AI phản biện: chưa có kết quả (đang chờ {s['pending']} lần)."
        def row(name, x):
            return f"{name}: {x['n']} lệnh, TB {x['avg_r']:+.2f}R, thắng {x['win']}%" if x["n"] else f"{name}: 0 lệnh"
        lines = ["AI phản biện, kết quả bóng của các tín hiệu MUA:", row("AI đồng ý", a), row("AI phản đối", r)]
        if s["veto_right_pct"] is not None:
            lines.append(f"AI phản đối đúng {s['veto_right_pct']}% (lệnh bị phản đối kết thúc lỗ hoặc hoà)")
        lines.append(f"Đang chờ kết quả: {s['pending']}")
        return "\n".join(lines)


def settle_one(row: dict, bars: list[dict]) -> dict | None:
    """The shadow result of one verdict, or None while the bars do not decide it yet."""
    price = float(row["price"])
    if row["action"] == "SELL":
        if len(bars) < SELL_SESSIONS:
            return None
        change = float(bars[SELL_SESSIONS - 1]["Close"]) / price - 1
        return {"r": round(-change * 100, 2), "label": "giá giảm" if change < 0 else "giá tăng",
                "pct": round(change * 100, 2)}
    stop, target = row.get("stop"), row.get("target")
    if not stop or not target or stop >= price:
        if len(bars) < HOLD_SESSIONS:
            return None
        change = float(bars[HOLD_SESSIONS - 1]["Close"]) / price - 1
        return {"r": round(change * 100, 2), "label": "không có stop", "pct": round(change * 100, 2)}
    risk = price - float(stop)
    for i, bar in enumerate(bars[:HOLD_SESSIONS]):
        if float(bar["Low"]) <= stop:
            return {"r": -1.0, "label": "chạm stop", "sessions": i + 1}
        if float(bar["High"]) >= target:
            return {"r": round((float(target) - price) / risk, 2), "label": "chạm mục tiêu", "sessions": i + 1}
    if len(bars) < HOLD_SESSIONS:
        return None
    close = float(bars[HOLD_SESSIONS - 1]["Close"])
    return {"r": round((close - price) / risk, 2), "label": "hết thời gian", "sessions": HOLD_SESSIONS}


def lesson(row: dict) -> str:
    out = row["outcome"]
    word = "đồng ý" if row["verdict"] == "AGREE" else "phản đối"
    right = (out["r"] > 0) == (row["verdict"] == "AGREE")
    result = f"{out['label']}, {out['r']:+.2f}R" if row["action"] == "BUY" else f"{out['label']} {out['pct']:+.1f}%"
    return (f"- {row['day']} {row['code']} {row['action']}: AI {word} {row['confidence']}% → {result} "
            f"(AI {'đúng' if right else 'sai'})")


def bars_after(code: str, day: str) -> list[dict]:
    """The daily bars after ``day`` up to today (Vietnam time)."""
    from tradingagents.dataflows.ohlcv import load_ohlcv
    from tradingagents.market import vn_rules

    frame = load_ohlcv(code, vn_rules.now_vn().strftime("%Y-%m-%d"))
    later = frame[frame["Date"].dt.strftime("%Y-%m-%d") > day]
    return later[["High", "Low", "Close"]].to_dict("records")
