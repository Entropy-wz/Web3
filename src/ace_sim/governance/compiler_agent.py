from __future__ import annotations

import re
from decimal import Decimal
from typing import Any


class CompilerValidationError(Exception):
    """Raised when compiled DSL patch violates whitelist or value constraints."""


class CompilerAgent:
    """Safe NLP-to-DSL compiler with strict whitelist validation.

    Notes:
    - This compiler never executes arbitrary code.
    - It only emits validated parameter patches.
    - LLM integration can be plugged in later via llm_callable.
    """

    def __init__(self, llm_callable: Any | None = None) -> None:
        self.llm_callable = llm_callable

    def compile_proposal(self, proposal_text: str) -> list[dict[str, Any]]:
        text = str(proposal_text).strip()
        if not text:
            raise CompilerValidationError("proposal text must be non-empty")

        if self.llm_callable is not None:
            try:
                raw = self.llm_callable(text)
                patches = self._coerce_llm_output(raw)
                return [self.validate_patch(item) for item in patches]
            except Exception:
                # Fall back to deterministic rule compiler.
                pass

        patches = self._rule_compile(text)
        return [self.validate_patch(item) for item in patches]

    def validate_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise CompilerValidationError("patch must be a dict")

        required = {"scope", "parameter", "new_value", "reason"}
        missing = required - set(patch.keys())
        if missing:
            raise CompilerValidationError(f"patch missing fields: {sorted(missing)}")

        scope = str(patch["scope"]).strip().lower()
        parameter = str(patch["parameter"]).strip()
        reason = str(patch["reason"]).strip() or "governance update"
        raw_value = patch["new_value"]

        if scope == "engine":
            if parameter not in {"minting_allowed", "swap_fee", "daily_mint_cap"}:
                raise CompilerValidationError(
                    f"engine parameter not allowed: {parameter}"
                )
            if parameter == "minting_allowed":
                value = self._to_bool(raw_value)
            elif parameter == "swap_fee":
                value = Decimal(str(raw_value))
                if value < 0 or value >= 1:
                    raise CompilerValidationError("swap_fee must be in [0,1)")
            else:
                if raw_value is None or str(raw_value).strip().lower() == "none":
                    value = None
                else:
                    value = Decimal(str(raw_value))
                    if value < 0:
                        raise CompilerValidationError(
                            "daily_mint_cap must be >= 0 or None"
                        )

        elif scope == "orchestrator":
            if parameter not in {"ticks_per_day", "max_inbox_size"}:
                raise CompilerValidationError(
                    f"orchestrator parameter not allowed: {parameter}"
                )
            value = int(str(raw_value))
            if value <= 0:
                raise CompilerValidationError(f"{parameter} must be > 0")

        else:
            raise CompilerValidationError(f"scope not allowed: {scope}")

        return {
            "scope": scope,
            "parameter": parameter,
            "new_value": value,
            "reason": reason,
        }

    def _coerce_llm_output(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            if not all(isinstance(item, dict) for item in raw):
                raise CompilerValidationError("llm output list must contain dict patches")
            return raw
        raise CompilerValidationError("llm output must be dict or list[dict]")

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on", "enable", "enabled"}:
            return True
        if text in {"false", "0", "no", "off", "disable", "disabled"}:
            return False
        raise CompilerValidationError("minting_allowed must be boolean-like")

    def _rule_compile(self, text: str) -> list[dict[str, Any]]:
        lower = text.lower()
        patches: list[dict[str, Any]] = []

        disable_mint_tokens = [
            "disable mint",
            "disable minting",
            "stop mint",
            "关闭铸造",
            "停止铸造",
            "禁用铸造",
        ]
        enable_mint_tokens = [
            "enable mint",
            "enable minting",
            "reopen mint",
            "恢复铸造",
            "开启铸造",
            "启用铸造",
        ]

        if any(token in lower for token in disable_mint_tokens):
            patches.append(
                {
                    "scope": "engine",
                    "parameter": "minting_allowed",
                    "new_value": False,
                    "reason": "proposal requested to disable minting",
                }
            )
        if any(token in lower for token in enable_mint_tokens):
            patches.append(
                {
                    "scope": "engine",
                    "parameter": "minting_allowed",
                    "new_value": True,
                    "reason": "proposal requested to enable minting",
                }
            )

        fee_match = re.search(
            r"(?:swap[\s_-]*fee|手续费|费率)[^0-9]*([0-9]*\.?[0-9]+)",
            lower,
            flags=re.IGNORECASE,
        )
        if fee_match:
            patches.append(
                {
                    "scope": "engine",
                    "parameter": "swap_fee",
                    "new_value": fee_match.group(1),
                    "reason": "proposal requested swap fee update",
                }
            )

        cap_match = re.search(
            r"(?:daily[\s_-]*mint[\s_-]*cap|mint[\s_-]*cap|铸造上限)[^0-9]*([0-9]+(?:\.[0-9]+)?)",
            lower,
            flags=re.IGNORECASE,
        )
        if cap_match:
            patches.append(
                {
                    "scope": "engine",
                    "parameter": "daily_mint_cap",
                    "new_value": cap_match.group(1),
                    "reason": "proposal requested daily mint cap update",
                }
            )

        tpd_match = re.search(
            r"(?:ticks?[\s_-]*per[\s_-]*day|每.?天\s*tick)[^0-9]*([0-9]+)",
            lower,
            flags=re.IGNORECASE,
        )
        if tpd_match:
            patches.append(
                {
                    "scope": "orchestrator",
                    "parameter": "ticks_per_day",
                    "new_value": tpd_match.group(1),
                    "reason": "proposal requested ticks_per_day update",
                }
            )

        inbox_match = re.search(
            r"(?:max[\s_-]*inbox[\s_-]*size|inbox[\s_-]*size|收件箱上限|认知带宽)[^0-9]*([0-9]+)",
            lower,
            flags=re.IGNORECASE,
        )
        if inbox_match:
            patches.append(
                {
                    "scope": "orchestrator",
                    "parameter": "max_inbox_size",
                    "new_value": inbox_match.group(1),
                    "reason": "proposal requested max inbox size update",
                }
            )

        if not patches:
            raise CompilerValidationError(
                "proposal text cannot be compiled into whitelist patches"
            )

        dedup: dict[tuple[str, str], dict[str, Any]] = {}
        for item in patches:
            dedup[(item["scope"], item["parameter"])] = item
        return list(dedup.values())


__all__ = [
    "CompilerAgent",
    "CompilerValidationError",
]
