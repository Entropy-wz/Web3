from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ActionValidationError(Exception):
    """Raised when an action payload violates the registry schema."""


ECONOMIC_ACTIONS = {"SWAP", "UST_TO_LUNA", "LUNA_TO_UST"}
SEMANTIC_ACTIONS = {"SPEAK", "VOTE"}

ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "SWAP": {
        "type": "object",
        "required": ["pool_name", "token_in", "amount", "slippage_tolerance"],
        "properties": {
            "pool_name": {"type": "string"},
            "token_in": {"type": "string", "enum": ["UST", "LUNA", "USDC"]},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "slippage_tolerance": {"type": "number", "minimum": 0, "exclusiveMaximum": 1},
        },
    },
    "UST_TO_LUNA": {
        "type": "object",
        "required": ["amount_ust"],
        "properties": {
            "amount_ust": {"type": "number", "exclusiveMinimum": 0},
        },
    },
    "LUNA_TO_UST": {
        "type": "object",
        "required": ["amount_luna"],
        "properties": {
            "amount_luna": {"type": "number", "exclusiveMinimum": 0},
        },
    },
    "SPEAK": {
        "type": "object",
        "required": ["target", "message"],
        "properties": {
            "target": {"type": "string"},
            "message": {"type": "string"},
            "mode": {"type": "string", "enum": ["new", "relay", "reply"]},
            "parent_event_id": {"type": "string"},
        },
    },
    "VOTE": {
        "type": "object",
        "required": ["proposal_id", "decision"],
        "properties": {
            "proposal_id": {"type": "string"},
            "decision": {"type": "string"},
        },
    },
}


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ActionValidationError(f"invalid decimal value: {value}") from exc


class SwapParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_name: str
    token_in: str
    amount: Decimal
    slippage_tolerance: Decimal

    @field_validator("pool_name")
    @classmethod
    def pool_name_valid(cls, value: str) -> str:
        if value not in {"Pool_A", "Pool_B"}:
            raise ValueError("pool_name must be Pool_A or Pool_B")
        return value

    @field_validator("token_in")
    @classmethod
    def token_in_valid(cls, value: str) -> str:
        token = value.upper()
        if token not in {"UST", "LUNA", "USDC"}:
            raise ValueError("token_in must be UST/LUNA/USDC")
        return token

    @field_validator("amount", "slippage_tolerance", mode="before")
    @classmethod
    def decimalize(cls, value: Any) -> Decimal:
        return to_decimal(value)

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be > 0")
        return value

    @field_validator("slippage_tolerance")
    @classmethod
    def slippage_range(cls, value: Decimal) -> Decimal:
        if value < 0 or value >= 1:
            raise ValueError("slippage_tolerance must be in [0,1)")
        return value


class USTToLUNAParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_ust: Decimal

    @field_validator("amount_ust", mode="before")
    @classmethod
    def decimalize(cls, value: Any) -> Decimal:
        return to_decimal(value)

    @field_validator("amount_ust")
    @classmethod
    def amount_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount_ust must be > 0")
        return value


class LUNAToUSTParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_luna: Decimal

    @field_validator("amount_luna", mode="before")
    @classmethod
    def decimalize(cls, value: Any) -> Decimal:
        return to_decimal(value)

    @field_validator("amount_luna")
    @classmethod
    def amount_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount_luna must be > 0")
        return value


class SpeakParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    message: str
    mode: str = "new"
    parent_event_id: str | None = None

    @field_validator("target", "message")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("target/message must be non-empty")
        return value.strip()

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"new", "relay", "reply"}:
            raise ValueError("mode must be one of: new/relay/reply")
        return normalized

    @field_validator("parent_event_id")
    @classmethod
    def parent_event_id_non_empty_if_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("parent_event_id cannot be empty")
        return cleaned

    @model_validator(mode="after")
    def enforce_parent_for_relay_reply(self) -> "SpeakParams":
        if self.mode in {"relay", "reply"} and not self.parent_event_id:
            raise ValueError("parent_event_id is required when mode is relay/reply")
        return self


class VoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str
    decision: str

    @field_validator("proposal_id", "decision")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("proposal_id/decision must be non-empty")
        return value.strip()


def normalize_action_type(action_type: str) -> str:
    normalized = action_type.strip().upper()
    if normalized not in ACTION_SCHEMAS:
        raise ActionValidationError(f"unknown action_type: {action_type}")
    return normalized


def validate_action_schema(action_type: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_action_type(action_type)
    if not isinstance(params, dict):
        raise ActionValidationError("params must be a dict")

    try:
        if normalized == "SWAP":
            validated = SwapParams(**params).model_dump()
        elif normalized == "UST_TO_LUNA":
            validated = USTToLUNAParams(**params).model_dump()
        elif normalized == "LUNA_TO_UST":
            validated = LUNAToUSTParams(**params).model_dump()
        elif normalized == "SPEAK":
            validated = SpeakParams(**params).model_dump()
        else:
            validated = VoteParams(**params).model_dump()
        return validated
    except Exception as exc:  # noqa: BLE001
        raise ActionValidationError(str(exc)) from exc


def is_economic_action(action_type: str) -> bool:
    return normalize_action_type(action_type) in ECONOMIC_ACTIONS


def is_semantic_action(action_type: str) -> bool:
    return normalize_action_type(action_type) in SEMANTIC_ACTIONS


def action_principal_token(action_type: str, params: dict[str, Any]) -> str:
    normalized = normalize_action_type(action_type)
    if normalized == "SWAP":
        return str(params["token_in"]).upper()
    if normalized == "UST_TO_LUNA":
        return "UST"
    if normalized == "LUNA_TO_UST":
        return "LUNA"
    raise ActionValidationError(f"{normalized} does not have economic principal token")


def action_principal_amount(action_type: str, params: dict[str, Any]) -> Decimal:
    normalized = normalize_action_type(action_type)
    if normalized == "SWAP":
        return to_decimal(params["amount"])
    if normalized == "UST_TO_LUNA":
        return to_decimal(params["amount_ust"])
    if normalized == "LUNA_TO_UST":
        return to_decimal(params["amount_luna"])
    raise ActionValidationError(f"{normalized} does not have economic principal amount")


__all__ = [
    "ACTION_SCHEMAS",
    "ActionValidationError",
    "ECONOMIC_ACTIONS",
    "SEMANTIC_ACTIONS",
    "validate_action_schema",
    "is_economic_action",
    "is_semantic_action",
    "action_principal_token",
    "action_principal_amount",
]
