"""A provider chain as one chat model: 9Router first, then Groq, OpenRouter, Grok.

Port of lananh-main's provider chain (``ai/orchestrator.py``,
``ai/provider_state.py``) for the agents of this framework, which expect a
single LangChain chat model they can ``invoke``, ``bind_tools`` and ask for
``with_structured_output``. Each call tries the providers in order:

- a provider that fails to answer (connection, timeout, server error, a 4xx
  such as a request too large for its context) is skipped for this call;
- one that throttles (429) rests for its ``Retry-After`` or
  ``LLM_QUOTA_COOLDOWN_SEC`` (default 300 s), one that refuses the key for an
  hour, and 9Router once down rests ``ROUTER9_RETRY_SEC`` (default 900 s), so
  the next calls go straight to a working provider;
- when every provider is resting, the call fails at once instead of hitting
  providers that just throttled or went down again (``LLM_TRY_RESTING=1``
  brings back the old "try them all anyway");
- at most ``LLM_MAX_CONCURRENT`` requests are in flight at once (default 2:
  the parallel analysts no longer fire four requests together);
- a day (Vietnam time) has a budget: ``LLM_MAX_CALLS_PER_DAY`` requests in all
  (default 300) and ``<PROVIDER>_MAX_CALLS_PER_DAY`` per provider (OpenRouter
  defaults to 45, under its free tier's 50 a day; 0 = no cap). A provider past
  its cap is skipped until tomorrow; past the total, calls fail with
  ``LLMBudgetExceeded``. Counts survive a restart when ``LLM_USAGE_PATH`` is set.

An answer the provider did give but that does not parse (structured output) is
not a provider failure: it is raised to the agent, which falls back to free
text, as it does on any single provider.

Configuration (environment):

    PROVIDER_ORDER       router9,groq,openrouter,xai (default; unset keys are skipped)
    ROUTER9_BASE_URL     9Router endpoint, OpenAI-compatible (…/v1)
    ROUTER9_API_KEY      9Router key
    ROUTER9_MODEL        default model on 9Router (the tier model overrides it)
    GROQ_API_KEY         GROQ_MODEL (default openai/gpt-oss-20b)
    OPENROUTER_API_KEY   OPENROUTER_MODEL (default a ":free" model)
    XAI_API_KEY          XAI_MODEL (default grok-4-fast) - Grok, optional
    LLM_CALL_TIMEOUT_SEC per-call timeout (default 120)
    LLM_MAX_CONCURRENT   requests in flight at once (default 2)
    LLM_MAX_CALLS_PER_DAY  requests per day, all providers (default 300; 0 = no cap)
    ROUTER9_MAX_CALLS_PER_DAY, GROQ_..., OPENROUTER_... (default 45), XAI_...
    LLM_USAGE_PATH       JSON file keeping the day's counts across restarts
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from .base_client import BaseLLMClient, normalize_content

logger = logging.getLogger(__name__)

DEFAULT_ORDER = ("router9", "groq", "openrouter", "xai")


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    label: str
    base_url_env: str | None
    default_base_url: str | None
    key_env: str
    model_env: str
    default_model: str


ENDPOINTS = {
    "router9": ProviderEndpoint("router9", "9Router", "ROUTER9_BASE_URL", None,
                                "ROUTER9_API_KEY", "ROUTER9_MODEL", "gpt-4o-mini"),
    "groq": ProviderEndpoint("groq", "Groq", "GROQ_BASE_URL", "https://api.groq.com/openai/v1",
                             "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-20b"),
    "openrouter": ProviderEndpoint("openrouter", "OpenRouter", "OPENROUTER_BASE_URL",
                                   "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                                   "OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
    "xai": ProviderEndpoint("xai", "Grok (xAI)", "XAI_BASE_URL", "https://api.x.ai/v1",
                            "XAI_API_KEY", "XAI_MODEL", "grok-4-fast"),
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


class LLMBudgetExceeded(RuntimeError):
    """The day's request budget is spent: no provider is called until tomorrow."""


class AllProvidersResting(RuntimeError):
    """Every provider is resting after a recent failure; the call is not sent."""


# ─── Provider health, shared by every model of the process ───────────────────

_resting_until: dict[str, float] = {}
_last_error: dict[str, str] = {}
_lock = threading.Lock()


def _rest(name: str, seconds: float, reason: str) -> None:
    with _lock:
        _resting_until[name] = time.time() + seconds
        _last_error[name] = reason
    logger.warning("LLM provider %s rests %.0fs: %s", name, seconds, reason)


def _recovered(name: str) -> None:
    with _lock:
        was = _resting_until.pop(name, None)
        _last_error.pop(name, None)
    if was:
        logger.info("LLM provider %s answers again", name)


def is_resting(name: str) -> bool:
    with _lock:
        return time.time() < _resting_until.get(name, 0.0)


def provider_status() -> list[dict]:
    """Each configured provider in order, with whether it rests and why (for a status page)."""
    now = time.time()
    rows = []
    for name in configured_order():
        with _lock:
            until = _resting_until.get(name, 0.0)
            reason = _last_error.get(name, "")
        rows.append({
            "name": name,
            "label": ENDPOINTS[name].label,
            "model": os.environ.get(ENDPOINTS[name].model_env) or ENDPOINTS[name].default_model,
            "resting_seconds": max(0, int(until - now)),
            "last_error": reason,
            "calls_today": usage_today()["by"].get(name, 0),
            "cap": provider_cap(name),
        })
    return rows


def reset_provider_state() -> None:
    with _lock:
        _resting_until.clear()
        _last_error.clear()
        _usage.clear()
    global _semaphore
    _semaphore = None


# ─── Daily budget and concurrency ───────────────────────────────────────────

_VN = timezone(timedelta(hours=7))
_usage: dict = {}                 # {"day": "YYYY-MM-DD", "total": n, "by": {provider: n}}
_semaphore: threading.BoundedSemaphore | None = None
_semaphore_lock = threading.Lock()
DEFAULT_PROVIDER_CAPS = {"openrouter": 45}


def _today() -> str:
    return datetime.now(_VN).strftime("%Y-%m-%d")


def _total_cap() -> int:
    return int(_env_float("LLM_MAX_CALLS_PER_DAY", 300))


def provider_cap(name: str) -> int:
    """Requests a day this provider may receive; 0 = no cap."""
    return int(_env_float(f"{name.upper()}_MAX_CALLS_PER_DAY", DEFAULT_PROVIDER_CAPS.get(name, 0)))


def _usage_today() -> dict:
    """The day's counts (caller holds ``_lock``), loaded from LLM_USAGE_PATH after a restart."""
    day = _today()
    if _usage.get("day") != day:
        loaded = {}
        path = os.environ.get("LLM_USAGE_PATH")
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError):
                loaded = {}
        if loaded.get("day") != day:
            loaded = {"day": day, "total": 0, "by": {}}
        _usage.clear()
        _usage.update(day=day, total=int(loaded.get("total", 0)), by=dict(loaded.get("by") or {}))
    return _usage


def _save_usage() -> None:
    path = os.environ.get("LLM_USAGE_PATH")
    if not path:
        return
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_usage, handle)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("LLM usage not saved: %s", exc)


def _over_cap(name: str) -> bool:
    cap = provider_cap(name)
    with _lock:
        return bool(cap) and _usage_today()["by"].get(name, 0) >= cap


def _spend(name: str) -> None:
    """Count one request to ``name``; LLMBudgetExceeded when the day's total is spent."""
    cap = _total_cap()
    with _lock:
        usage = _usage_today()
        if cap and usage["total"] >= cap:
            raise LLMBudgetExceeded(
                f"Đã dùng hết hạn mức {cap} lượt gọi AI hôm nay (LLM_MAX_CALLS_PER_DAY); "
                "mai bot tự chạy lại.")
        usage["total"] += 1
        usage["by"][name] = usage["by"].get(name, 0) + 1
        _save_usage()


def usage_today() -> dict:
    """The day's request counts and caps (for a status page)."""
    with _lock:
        usage = _usage_today()
        return {"day": usage["day"], "total": usage["total"], "cap": _total_cap(), "by": dict(usage["by"])}


def budget_left() -> int | None:
    """Requests left today in the total budget; None when it has no cap."""
    cap = _total_cap()
    if not cap:
        return None
    with _lock:
        return max(0, cap - _usage_today()["total"])


def _slot() -> threading.BoundedSemaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(max(1, int(_env_float("LLM_MAX_CONCURRENT", 2))))
        return _semaphore


def configured_order() -> list[str]:
    """Providers in PROVIDER_ORDER that have a key (and, for 9Router, an endpoint)."""
    raw = os.environ.get("PROVIDER_ORDER") or ",".join(DEFAULT_ORDER)
    order = []
    for name in (n.strip().lower() for n in raw.split(",")):
        if name == "grok":
            name = "xai"
        endpoint = ENDPOINTS.get(name)
        if endpoint is None or name in order or not os.environ.get(endpoint.key_env):
            continue
        if endpoint.default_base_url is None and not os.environ.get(endpoint.base_url_env or ""):
            continue
        order.append(name)
    return order


def _classify(exc: Exception) -> tuple[bool, float, str]:
    """(is it the provider's failure, how long to rest, a short reason)."""
    import httpx
    import openai

    status = getattr(exc, "status_code", None)
    if isinstance(exc, openai.RateLimitError) or status == 429:
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
        return True, retry_after or _env_float("LLM_QUOTA_COOLDOWN_SEC", 300), "rate limited (429)"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return True, 3600, f"key refused ({status})"
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, httpx.HTTPError, TimeoutError)):
        return True, 0, type(exc).__name__
    if isinstance(exc, openai.APIStatusError):
        return True, 0, f"HTTP {status}"
    if isinstance(exc, openai.APIError):
        return True, 0, type(exc).__name__
    return False, 0, ""


def _router9_rest() -> float:
    return _env_float("ROUTER9_RETRY_SEC", 900)


def run_chain(chain: Sequence[tuple[str, Any]], call) -> Any:
    """``call(model)`` on each provider in turn until one answers.

    ``chain`` is ``[(name, model), ...]``. Resting providers and those past
    their daily cap are skipped; when that leaves none, the call fails without
    sending anything (``LLM_TRY_RESTING=1``: resting ones are tried last instead).
    """
    if not chain:
        raise RuntimeError(
            "No LLM provider configured: set ROUTER9_BASE_URL and ROUTER9_API_KEY, "
            "or GROQ_API_KEY / OPENROUTER_API_KEY / XAI_API_KEY."
        )
    usable = [item for item in chain if not _over_cap(item[0])]
    ready = [item for item in usable if not is_resting(item[0])]
    ordered = list(ready)
    if os.environ.get("LLM_TRY_RESTING", "").strip().lower() in ("1", "true", "yes", "on"):
        ordered += [item for item in usable if item not in ready]
    if not ordered:
        why = "; ".join(f"{ENDPOINTS[n].label}: " + ("hết hạn mức ngày" if _over_cap(n) else
                                                    f"đang nghỉ ({_last_error.get(n, '')})")
                        for n, _ in chain if n in ENDPOINTS)
        raise AllProvidersResting("Mọi nhà cung cấp AI đang nghỉ hoặc hết hạn mức, không gửi thêm: " + why)
    errors = []
    for name, model in ordered:
        _spend(name)
        try:
            with _slot():
                result = call(model)
        except Exception as exc:  # noqa: BLE001 - classified below
            is_provider_failure, rest, reason = _classify(exc)
            if not is_provider_failure:
                raise
            if name == "router9":
                rest = max(rest, _router9_rest())
            if rest:
                _rest(name, rest, reason)
            else:
                with _lock:
                    _last_error[name] = reason
            errors.append(f"{ENDPOINTS[name].label}: {reason}")
            logger.warning("LLM provider %s failed (%s); trying the next one", name, reason)
            continue
        _recovered(name)
        return result
    raise RuntimeError("Every LLM provider failed: " + "; ".join(errors))


# ─── The chat model the agents use ──────────────────────────────────────────


class FallbackChatModel(BaseChatModel):
    """One chat model over the provider chain; see the module docstring."""

    chain: list[tuple[str, Any]]
    tool_args: tuple | None = None   # (tools, kwargs) once bind_tools is called

    @property
    def _llm_type(self) -> str:
        return "provider-chain"

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs
    ) -> ChatResult:
        def call(model):
            runnable = model
            if self.tool_args is not None:
                tools, tool_kwargs = self.tool_args
                runnable = model.bind_tools(tools, **tool_kwargs)
            return normalize_content(runnable.invoke(messages, stop=stop, **kwargs))

        message = run_chain(self.chain, call)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tool_args": (list(tools), kwargs)})

    def with_structured_output(self, schema, **kwargs):
        chain = self.chain

        def invoke(prompt):
            return run_chain(chain, lambda model: model.with_structured_output(schema, **kwargs).invoke(prompt))

        return RunnableLambda(invoke)


def build_chain(model_override: str | None = None, **llm_kwargs) -> list[tuple[str, Any]]:
    """The configured providers as chat models, in order.

    ``model_override`` replaces the 9Router model (the tier model of the
    config); the other providers keep their own *_MODEL setting, since a model
    name of one gateway rarely exists on another.
    """
    from .openai_client import LocalCompatibleChatOpenAI

    timeout = _env_float("LLM_CALL_TIMEOUT_SEC", 120)
    chain = []
    for name in configured_order():
        endpoint = ENDPOINTS[name]
        model = os.environ.get(endpoint.model_env) or endpoint.default_model
        if name == "router9" and model_override:
            model = model_override
        base_url = (os.environ.get(endpoint.base_url_env or "") or endpoint.default_base_url or "").rstrip("/")
        kwargs = {k: v for k, v in llm_kwargs.items() if k in ("temperature", "max_tokens", "callbacks")}
        # No retry inside the SDK: a retry on a 429 is exactly what overloads a
        # gateway, and the chain is the real fallback.
        chain.append((name, LocalCompatibleChatOpenAI(
            model=model, base_url=base_url, api_key=os.environ[endpoint.key_env],
            timeout=timeout, max_retries=0, **kwargs,
        )))
    return chain


class FallbackClient(BaseLLMClient):
    """``llm_provider: "router9_chain"`` - the provider chain as one client."""

    provider = "router9_chain"

    def get_llm(self) -> Any:
        model = None if self.model in (None, "", "default") else self.model
        return FallbackChatModel(chain=build_chain(model, **self.kwargs))

    def validate_model(self) -> bool:
        return True
