from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..agents.agent_profile import AgentProfile
from ..config.llm_config import load_llm_config

LOGGER = logging.getLogger(__name__)


class LLMBackendAdapter(Protocol):
    def generate(
        self,
        *,
        model: str,
        prompt: str,
        timeout: float,
        schema: dict[str, Any] | None = None,
    ) -> Any: ...


@dataclass
class RouteResult:
    decision: dict[str, Any]
    backend_used: str
    model_used: str
    used_fallback: bool
    error: str | None = None


class TokenBucket:
    def __init__(self, refill_rate_per_sec: float, capacity: int) -> None:
        if refill_rate_per_sec <= 0:
            raise ValueError("refill_rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.refill_rate_per_sec = float(refill_rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        if tokens <= 0:
            return True
        deadline = None if timeout is None else time.monotonic() + float(timeout)

        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._updated_at)
        if elapsed == 0:
            return
        refill = elapsed * self.refill_rate_per_sec
        self._tokens = min(self.capacity, self._tokens + refill)
        self._updated_at = now


class OpenAIChatAdapter:
    """Best-effort OpenAI adapter. Raises if SDK/key is unavailable."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        organization: str | None = None,
        project: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.organization = organization
        self.project = project

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        timeout: float,
        schema: dict[str, Any] | None = None,
    ) -> Any:
        del schema
        try:
            from openai import OpenAI
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("openai sdk not available") from exc

        client_kwargs: dict[str, Any] = {}
        if self.api_key:
            client_kwargs["api_key"] = self.api_key
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        if self.organization:
            client_kwargs["organization"] = self.organization
        if self.project:
            client_kwargs["project"] = self.project

        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            timeout=timeout,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Web3 simulation agent. "
                        "Return strict JSON only with keys: thought, speak, action."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("empty response from OpenAI")
        return content


class LocalRuleAdapter:
    """Cheap deterministic local adapter for offline fallback."""

    def generate(
        self,
        *,
        model: str,
        prompt: str,
        timeout: float,
        schema: dict[str, Any] | None = None,
    ) -> Any:
        del model, timeout, schema
        lower = prompt.lower()
        panic = any(keyword in lower for keyword in ["depeg", "panic", "bank run", "liquidity", "selloff"])
        if panic:
            return {
                "thought": "Market stress is elevated; prioritize risk control.",
                "speak": {
                    "target": "forum",
                    "message": "Liquidity stress rising. Reduce leverage.",
                    "mode": "new",
                },
                "action": None,
            }
        return {
            "thought": "No strong trigger detected; keep monitoring.",
            "speak": None,
            "action": None,
        }


class LLMRouter:
    """Heterogeneous model router with semaphore, token-bucket and retries."""

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        bucket_capacity: int | None = None,
        bucket_refill_rate_per_sec: float | None = None,
        max_retries: int | None = None,
        base_backoff_seconds: float | None = None,
        jitter_seconds: float | None = None,
        fallback_adapter: LLMBackendAdapter | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        config = load_llm_config(config_path)
        router_cfg = config.router
        openai_cfg = config.openai

        max_concurrent = (
            router_cfg.max_concurrent if max_concurrent is None else max_concurrent
        )
        bucket_capacity = (
            router_cfg.bucket_capacity if bucket_capacity is None else bucket_capacity
        )
        bucket_refill_rate_per_sec = (
            router_cfg.bucket_refill_rate_per_sec
            if bucket_refill_rate_per_sec is None
            else bucket_refill_rate_per_sec
        )
        max_retries = router_cfg.max_retries if max_retries is None else max_retries
        base_backoff_seconds = (
            router_cfg.base_backoff_seconds
            if base_backoff_seconds is None
            else base_backoff_seconds
        )
        jitter_seconds = (
            router_cfg.jitter_seconds if jitter_seconds is None else jitter_seconds
        )

        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be > 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self.max_retries = int(max_retries)
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.jitter_seconds = float(jitter_seconds)
        self.enable_format_repair = _env_bool(
            "ACE_LLM_AUTO_REPAIR_FORMAT",
            default=True,
        )
        self.enable_format_retry_once = _env_bool(
            "ACE_LLM_FORMAT_RETRY_ONCE",
            default=True,
        )

        self._semaphore = threading.Semaphore(max_concurrent)
        self._bucket = TokenBucket(
            refill_rate_per_sec=bucket_refill_rate_per_sec,
            capacity=bucket_capacity,
        )
        self._random = random.Random(42)

        self._adapters: dict[str, LLMBackendAdapter] = {
            "openai": OpenAIChatAdapter(
                api_key=openai_cfg.resolved_api_key(),
                base_url=openai_cfg.base_url,
                organization=openai_cfg.organization,
                project=openai_cfg.project,
            ),
            "local": LocalRuleAdapter(),
            "rule": LocalRuleAdapter(),
        }
        self._fallback_adapter = fallback_adapter or LocalRuleAdapter()

    def register_adapter(self, backend: str, adapter: LLMBackendAdapter) -> None:
        key = str(backend).strip().lower()
        if not key:
            raise ValueError("backend must be non-empty")
        self._adapters[key] = adapter

    def route(
        self,
        profile: AgentProfile,
        prompt: str,
        schema: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> RouteResult:
        agent_id = str(profile.agent_id).strip() or "unknown"
        backend = str(profile.llm_backend).strip().lower() or "rule"
        model = str(profile.llm_model).strip() or "rule-model"

        adapter = self._adapters.get(backend)
        if adapter is None:
            LOGGER.warning(
                "[LLM-WARN] unknown backend=%s model=%s agent=%s, fallback enabled",
                backend,
                model,
                agent_id,
            )
            return self._fallback(profile=profile, error=f"unknown backend: {backend}")

        error_message: str | None = None
        format_retry_used = False
        for attempt in range(self.max_retries + 1):
            raw_preview: str | None = None
            raw: Any = None
            try:
                raw = self._execute_with_controls(
                    adapter=adapter,
                    model=model,
                    prompt=prompt,
                    timeout=timeout,
                    schema=schema,
                )
                raw_preview = self._preview_raw(raw)
                decision = self._coerce_decision(raw)
                return RouteResult(
                    decision=decision,
                    backend_used=backend,
                    model_used=model,
                    used_fallback=False,
                    error=None,
                )
            except Exception as exc:  # noqa: BLE001
                if self.enable_format_repair:
                    repaired = self._try_repair_decision(raw)
                    if repaired is not None:
                        LOGGER.warning(
                            "[LLM-WARN] malformed output repaired backend=%s model=%s agent=%s reason=%s",
                            backend,
                            model,
                            agent_id,
                            str(exc),
                        )
                        return RouteResult(
                            decision=repaired,
                            backend_used=backend,
                            model_used=model,
                            used_fallback=False,
                            error=f"format_repaired: {str(exc)}",
                        )

                if (
                    self.enable_format_retry_once
                    and not format_retry_used
                    and self._is_format_error(exc)
                    and raw_preview is not None
                ):
                    format_retry_used = True
                    raw_retry: Any = None
                    retry_preview: str | None = None
                    try:
                        repair_prompt = self._build_format_retry_prompt(
                            original_prompt=prompt,
                            raw_preview=raw_preview,
                            error_message=str(exc),
                        )
                        raw_retry = self._execute_with_controls(
                            adapter=adapter,
                            model=model,
                            prompt=repair_prompt,
                            timeout=timeout,
                            schema=schema,
                        )
                        retry_preview = self._preview_raw(raw_retry)
                        decision_retry = self._coerce_decision(raw_retry)
                        LOGGER.warning(
                            "[LLM-WARN] malformed output recovered by retry backend=%s model=%s agent=%s",
                            backend,
                            model,
                            agent_id,
                        )
                        return RouteResult(
                            decision=decision_retry,
                            backend_used=backend,
                            model_used=model,
                            used_fallback=False,
                            error="format_retry_recovered",
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        if self.enable_format_repair:
                            repaired_retry = self._try_repair_decision(raw_retry)
                            if repaired_retry is not None:
                                LOGGER.warning(
                                    "[LLM-WARN] malformed output repaired after retry backend=%s model=%s agent=%s reason=%s",
                                    backend,
                                    model,
                                    agent_id,
                                    str(retry_exc),
                                )
                                return RouteResult(
                                    decision=repaired_retry,
                                    backend_used=backend,
                                    model_used=model,
                                    used_fallback=False,
                                    error=f"format_retry_then_repair: {str(retry_exc)}",
                                )
                        exc = retry_exc
                        raw_preview = retry_preview

                error_message = str(exc)
                retryable = self._is_retryable_error(exc)
                attempt_no = attempt + 1
                max_attempts = self.max_retries + 1
                if attempt >= self.max_retries or not retryable:
                    LOGGER.warning(
                        "[LLM-WARN] request failed backend=%s model=%s agent=%s attempt=%d/%d retryable=%s error=%s",
                        backend,
                        model,
                        agent_id,
                        attempt_no,
                        max_attempts,
                        retryable,
                        error_message,
                    )
                    if (
                        raw_preview is not None
                        and os.getenv("ACE_LOG_RAW_LLM_ON_PARSE_ERROR", "0").strip().lower()
                        in {"1", "true", "yes"}
                    ):
                        LOGGER.warning(
                            "[LLM-WARN] raw_response_preview backend=%s model=%s agent=%s preview=%s",
                            backend,
                            model,
                            agent_id,
                            raw_preview,
                        )
                    break
                delay = self._sleep_backoff(attempt)
                LOGGER.warning(
                    "[LLM-WARN] transient error backend=%s model=%s agent=%s attempt=%d/%d backoff=%.3fs error=%s",
                    backend,
                    model,
                    agent_id,
                    attempt_no,
                    max_attempts,
                    delay,
                    error_message,
                )

        return self._fallback(profile=profile, error=error_message)

    def _execute_with_controls(
        self,
        *,
        adapter: LLMBackendAdapter,
        model: str,
        prompt: str,
        timeout: float,
        schema: dict[str, Any] | None,
    ) -> Any:
        acquired = self._semaphore.acquire(timeout=max(0.1, timeout))
        if not acquired:
            raise TimeoutError("semaphore acquire timed out")
        try:
            if not self._bucket.acquire(timeout=timeout):
                raise TimeoutError("token bucket acquire timed out")
            return adapter.generate(
                model=model,
                prompt=prompt,
                timeout=timeout,
                schema=schema,
            )
        finally:
            self._semaphore.release()

    def _fallback(self, profile: AgentProfile, error: str | None) -> RouteResult:
        if error:
            LOGGER.warning(
                "[LLM-WARN] fallback activated role=%s model=%s agent=%s reason=%s",
                profile.role,
                profile.llm_model,
                profile.agent_id,
                error,
            )
        raw = self._fallback_adapter.generate(
            model=f"fallback:{profile.llm_model}",
            prompt="fallback",
            timeout=1.0,
            schema=None,
        )
        decision = self._coerce_decision(raw)
        if error:
            decision["thought"] = f"{decision['thought']} (fallback triggered: {error})"
        return RouteResult(
            decision=decision,
            backend_used="fallback",
            model_used="rule",
            used_fallback=True,
            error=error,
        )

    def _coerce_decision(self, raw: Any) -> dict[str, Any]:
        parsed: Any = raw
        if isinstance(raw, str):
            parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("model output must be dict/json")

        thought = parsed.get("thought")
        speak = parsed.get("speak")
        action = parsed.get("action")

        if not isinstance(thought, str) or not thought.strip():
            raise ValueError("thought must be non-empty string")
        if speak is not None and not isinstance(speak, dict):
            raise ValueError("speak must be object or null")
        if action is not None and not isinstance(action, dict):
            raise ValueError("action must be object or null")

        return {
            "thought": thought.strip(),
            "speak": speak,
            "action": action,
        }

    def _try_repair_decision(self, raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        parsed: Any = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(parsed, dict):
            return None

        thought_val = parsed.get("thought")
        if isinstance(thought_val, str) and thought_val.strip():
            thought = thought_val.strip()
        else:
            thought = "No valid thought provided; keep conservative posture."

        speak_val = parsed.get("speak")
        if isinstance(speak_val, dict):
            speak = speak_val
        elif isinstance(speak_val, str):
            msg = speak_val.strip()
            speak = (
                {"target": "forum", "message": msg, "mode": "new"}
                if msg
                else None
            )
        else:
            speak = None

        action_val = parsed.get("action")
        if isinstance(action_val, dict) and "action_type" in action_val:
            action = action_val
        else:
            action = None

        try:
            return self._coerce_decision(
                {
                    "thought": thought,
                    "speak": speak,
                    "action": action,
                }
            )
        except Exception:  # noqa: BLE001
            return None

    def _preview_raw(self, raw: Any, max_len: int = 800) -> str:
        if isinstance(raw, str):
            text = raw
        else:
            try:
                text = json.dumps(raw, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                text = str(raw)
        condensed = " ".join(text.split())
        if len(condensed) > max_len:
            return condensed[: max_len - 3] + "..."
        return condensed

    def _build_format_retry_prompt(
        self,
        *,
        original_prompt: str,
        raw_preview: str,
        error_message: str,
    ) -> str:
        return "\n".join(
            [
                "The previous output violated required JSON shape.",
                f"Validation error: {error_message}",
                "Fix only structure while preserving intent.",
                "Return strict JSON object with keys: thought, speak, action.",
                "Rules:",
                "- thought: non-empty string",
                "- speak: object or null",
                "- action: object or null",
                "If speak is plain text, wrap as:",
                '{"target":"forum","message":"...","mode":"new"}',
                "If action is uncertain, set action to null.",
                f"Previous output: {raw_preview}",
                f"Original task context: {original_prompt}",
            ]
        )

    def _is_format_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        tokens = [
            "speak must be object or null",
            "action must be object or null",
            "thought must be non-empty string",
            "model output must be dict/json",
        ]
        return any(token in text for token in tokens)

    def _is_retryable_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        retry_tokens = ["429", "rate limit", "timeout", "timed out", "5xx", "503", "502", "connection"]
        return any(token in text for token in retry_tokens)

    def _sleep_backoff(self, attempt: int) -> float:
        base = self.base_backoff_seconds * (2**attempt)
        jitter = self._random.random() * self.jitter_seconds
        delay = base + jitter
        time.sleep(delay)
        return delay


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


__all__ = [
    "LLMRouter",
    "RouteResult",
    "LLMBackendAdapter",
    "TokenBucket",
    "OpenAIChatAdapter",
    "LocalRuleAdapter",
]
