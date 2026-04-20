from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..action_registry.actions import (
    ActionValidationError,
    action_principal_amount,
    action_principal_token,
    is_economic_action,
    is_semantic_action,
    normalize_action_type,
    validate_action_schema,
)


class InsufficientBalanceError(Exception):
    """Raised when balance is insufficient for principal + gas precheck."""


class UnauthorizedActionError(Exception):
    """Raised when an agent role tries to submit a forbidden action."""


ROLE_ACTION_MATRIX: dict[str, set[str]] = {
    "retail": {"SWAP", "UST_TO_LUNA", "LUNA_TO_UST", "SPEAK", "VOTE", "PROPOSE"},
    "whale": {"SWAP", "UST_TO_LUNA", "LUNA_TO_UST", "SPEAK", "VOTE", "PROPOSE"},
    "project": {"SWAP", "UST_TO_LUNA", "LUNA_TO_UST", "SPEAK", "VOTE", "PROPOSE"},
}


class SecretaryAuditor:
    """Guardrail layer for action schema, balances, and slippage packaging."""

    def precheck_transaction(self, tx: Any, engine: Any, current_tick: int) -> None:
        if tx.gas_price < 0:
            raise ActionValidationError("gas_price must be >= 0")

        normalized_action = normalize_action_type(tx.action_type)
        normalized_params = validate_action_schema(normalized_action, tx.params)
        tx.action_type = normalized_action
        tx.params = normalized_params

        principal_token = action_principal_token(tx.action_type, tx.params)
        principal_amount = action_principal_amount(tx.action_type, tx.params)
        effective_gas = Decimal(
            str(getattr(tx, "effective_gas_price", getattr(tx, "gas_price", "0")))
        )
        required = principal_amount + effective_gas
        balance = engine.get_account_balance(tx.agent_id, principal_token)
        if balance < required:
            raise InsufficientBalanceError(
                f"insufficient {principal_token}: balance={balance}, required={required}"
            )

        if tx.action_type == "SWAP" and tx.resolved_min_amount_out is None:
            raise ActionValidationError("SWAP requires resolved_min_amount_out")

    def resolve_swap_guardrail(self, tx: Any, engine: Any) -> None:
        if normalize_action_type(tx.action_type) != "SWAP":
            return

        params = validate_action_schema("SWAP", tx.params)
        estimate = engine.estimate_amount_out(
            pool_name=params["pool_name"],
            token_in=params["token_in"],
            amount=params["amount"],
        )
        slippage_tolerance = Decimal(params["slippage_tolerance"])
        min_amount_out = Decimal(estimate["amount_out"]) * (
            Decimal("1") - slippage_tolerance
        )
        if min_amount_out <= 0:
            raise ActionValidationError("resolved min_amount_out must be > 0")

        tx.params = params
        tx.estimated_amount_out = Decimal(estimate["amount_out"])
        tx.resolved_min_amount_out = min_amount_out

    def validate_agent_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ActionValidationError("agent output must be a dict")

        required_fields = {"thought", "speak", "action"}
        missing = required_fields - set(payload.keys())
        if missing:
            raise ActionValidationError(f"agent output missing fields: {sorted(missing)}")

        thought = payload.get("thought")
        if not isinstance(thought, str) or not thought.strip():
            raise ActionValidationError("thought must be a non-empty string")

        normalized: dict[str, Any] = {
            "thought": thought.strip(),
            "speak": None,
            "action": None,
        }

        speak_payload = payload.get("speak")
        if speak_payload is not None:
            if not isinstance(speak_payload, dict):
                raise ActionValidationError("speak must be a dict or null")
            normalized["speak"] = validate_action_schema("SPEAK", speak_payload)

        action_payload = payload.get("action")
        if action_payload is not None:
            if not isinstance(action_payload, dict):
                raise ActionValidationError("action must be a dict or null")
            if "action_type" not in action_payload or "params" not in action_payload:
                raise ActionValidationError("action requires action_type and params")
            action_type = normalize_action_type(str(action_payload["action_type"]))
            params = validate_action_schema(action_type, action_payload["params"])
            item: dict[str, Any] = {
                "action_type": action_type,
                "params": params,
            }
            if is_economic_action(action_type):
                if "gas_price" not in action_payload:
                    raise ActionValidationError("economic action requires gas_price")
                gas_price = Decimal(str(action_payload["gas_price"]))
                if gas_price < 0:
                    raise ActionValidationError("gas_price must be >= 0")
                item["gas_price"] = gas_price
            normalized["action"] = item
        return normalized

    def assert_role_permission(self, agent_role: str, action_type: str) -> None:
        role = str(agent_role).strip().lower()
        if role not in ROLE_ACTION_MATRIX:
            raise UnauthorizedActionError(f"unknown agent role: {agent_role}")
        normalized_action = normalize_action_type(action_type)
        if normalized_action not in ROLE_ACTION_MATRIX[role]:
            raise UnauthorizedActionError(
                f"role {role} cannot execute action {normalized_action}"
            )

    def audit_semantic_action(self, action_type: str, params: dict[str, Any]) -> dict[str, Any]:
        normalized_action = normalize_action_type(action_type)
        if not is_semantic_action(normalized_action):
            raise ActionValidationError("audit_semantic_action expects semantic action")
        return validate_action_schema(normalized_action, params)


__all__ = [
    "SecretaryAuditor",
    "InsufficientBalanceError",
    "UnauthorizedActionError",
]
