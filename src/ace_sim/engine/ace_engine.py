from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

# High precision is mandatory for LUNA-style hyperinflation scenarios.
getcontext().prec = max(getcontext().prec, 80)

TOKEN_SET = {"UST", "LUNA", "USDC"}

DECIMAL_ABS_TOL = Decimal("1e-24")
DECIMAL_REL_TOL = Decimal("1e-18")
FLOAT_ABS_TOL = 1e-15
FLOAT_REL_TOL = 1e-12


class InsufficientFundsError(Exception):
    """Raised when an account tries to spend more than its token balance."""


class InvariantViolationError(Exception):
    """Raised when asset conservation or consistency checks fail."""


class SlippageExceededError(Exception):
    """Raised when actual amount_out is lower than caller's min_amount_out."""


class Account(BaseModel):
    """Ledger account with balances for UST, LUNA and USDC."""

    model_config = ConfigDict(validate_assignment=True)

    address: str
    UST: Decimal = Field(default=Decimal("0"))
    LUNA: Decimal = Field(default=Decimal("0"))
    USDC: Decimal = Field(default=Decimal("0"))

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("address must be non-empty")
        return clean

    @field_validator("UST", "LUNA", "USDC", mode="before")
    @classmethod
    def to_decimal(cls, value: Any) -> Decimal:
        return to_decimal(value)

    @field_validator("UST", "LUNA", "USDC")
    @classmethod
    def non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("balances must be non-negative")
        return value


@dataclass
class AMM_Pool:
    """Constant-product pool x*y=k with configurable input-side fee."""

    token_x: str
    token_y: str
    reserve_x: Decimal
    reserve_y: Decimal

    def clone(self) -> "AMM_Pool":
        return AMM_Pool(
            token_x=self.token_x,
            token_y=self.token_y,
            reserve_x=Decimal(self.reserve_x),
            reserve_y=Decimal(self.reserve_y),
        )

    def validate(self) -> None:
        if self.token_x not in TOKEN_SET or self.token_y not in TOKEN_SET:
            raise ValueError("pool tokens must be among UST/LUNA/USDC")
        if self.token_x == self.token_y:
            raise ValueError("pool tokens must be different")
        if self.reserve_x <= 0 or self.reserve_y <= 0:
            raise ValueError("pool reserves must be strictly positive")

    def get_price(self, base_token: str, quote_token: str) -> Decimal:
        self.validate()
        if base_token == self.token_x and quote_token == self.token_y:
            return self.reserve_y / self.reserve_x
        if base_token == self.token_y and quote_token == self.token_x:
            return self.reserve_x / self.reserve_y
        raise ValueError("requested price pair does not belong to this pool")

    def swap(
        self,
        token_in: str,
        amount_in: Decimal,
        fee_rate: Decimal,
    ) -> dict[str, Decimal | str]:
        """
        Execute a pool swap and return token_out, amount_out, execution_price and slippage.
        Slippage is positive when execution price is worse than pre-trade mid price.
        """
        self.validate()
        token_in = token_in.upper()
        amount_in = to_decimal(amount_in)
        fee_rate = to_decimal(fee_rate)

        if amount_in <= 0:
            raise ValueError("swap amount must be positive")
        if fee_rate < 0 or fee_rate >= 1:
            raise ValueError("swap fee must be in [0, 1)")

        k_before = self.reserve_x * self.reserve_y
        amount_in_effective = amount_in * (Decimal("1") - fee_rate)

        if token_in == self.token_x:
            token_out = self.token_y
            mid_price = self.reserve_y / self.reserve_x
            amount_out = (self.reserve_y * amount_in_effective) / (
                self.reserve_x + amount_in_effective
            )
            if amount_out <= 0 or amount_out >= self.reserve_y:
                raise ValueError("invalid output amount for swap")

            self.reserve_x += amount_in
            self.reserve_y -= amount_out
            execution_price = amount_out / amount_in

        elif token_in == self.token_y:
            token_out = self.token_x
            mid_price = self.reserve_x / self.reserve_y
            amount_out = (self.reserve_x * amount_in_effective) / (
                self.reserve_y + amount_in_effective
            )
            if amount_out <= 0 or amount_out >= self.reserve_x:
                raise ValueError("invalid output amount for swap")

            self.reserve_y += amount_in
            self.reserve_x -= amount_out
            execution_price = amount_out / amount_in

        else:
            raise ValueError("token_in is not part of target pool")

        if mid_price <= 0:
            raise ValueError("invalid mid price")

        slippage = (mid_price - execution_price) / mid_price

        # Pool invariant in fee=0 case should remain nearly identical.
        if fee_rate == 0:
            k_after = self.reserve_x * self.reserve_y
            if not dual_isclose(k_before, k_after):
                raise InvariantViolationError(
                    f"pool invariant drifted in zero-fee mode: {k_before} != {k_after}"
                )

        return {
            "token_out": token_out,
            "amount_out": amount_out,
            "execution_price": execution_price,
            "slippage": slippage,
        }


@dataclass
class EngineState:
    accounts: dict[str, Account]
    pools: dict[str, AMM_Pool]
    fee_vault: dict[str, Decimal]
    counters: dict[str, Decimal]
    total_luna_supply: Decimal
    genesis_totals: dict[str, Decimal]
    mint_window_day: int
    mint_window_used_ust: Decimal


class ACE_Engine:
    """Application-layer chain engine (ACE) for Web3 economic simulations."""

    def __init__(
        self,
        db_path: str | Path = "ace_engine.sqlite3",
        pool_a_reserves: tuple[Any, Any] = ("1000000", "1000000"),
        pool_b_reserves: tuple[Any, Any] = ("1000000", "1000000"),
        engine_config: dict[str, Any] | None = None,
    ) -> None:
        self.accounts: dict[str, Account] = {}

        self.pools: dict[str, AMM_Pool] = {
            "Pool_A": AMM_Pool(
                token_x="UST",
                token_y="USDC",
                reserve_x=to_decimal(pool_a_reserves[0]),
                reserve_y=to_decimal(pool_a_reserves[1]),
            ),
            "Pool_B": AMM_Pool(
                token_x="LUNA",
                token_y="USDC",
                reserve_x=to_decimal(pool_b_reserves[0]),
                reserve_y=to_decimal(pool_b_reserves[1]),
            ),
        }
        self.pools["Pool_A"].validate()
        self.pools["Pool_B"].validate()

        base_config = {
            "minting_allowed": True,
            "swap_fee": Decimal("0.0"),
            "daily_mint_cap": Decimal("1000000"),
        }
        if engine_config:
            base_config.update(engine_config)
        self.engine_config: dict[str, Any] = {
            "minting_allowed": bool(base_config["minting_allowed"]),
            "swap_fee": to_decimal(base_config["swap_fee"]),
            "daily_mint_cap": self._normalize_daily_mint_cap(
                base_config["daily_mint_cap"]
            ),
        }
        self._validate_engine_config()

        self.counters: dict[str, Decimal] = {
            "total_ust_burned_for_luna": Decimal("0"),
            "total_luna_minted": Decimal("0"),
            "total_luna_burned_for_ust": Decimal("0"),
            "total_ust_minted": Decimal("0"),
        }

        self.current_tick: int = 0
        self.ticks_per_day: int = 100

        self.fee_vault: dict[str, Decimal] = {
            "UST": Decimal("0"),
            "LUNA": Decimal("0"),
            "USDC": Decimal("0"),
        }
        self.total_luna_supply: Decimal = self.pools["Pool_B"].reserve_x
        self.genesis_totals: dict[str, Decimal] = {
            "UST": self.pools["Pool_A"].reserve_x,
            "LUNA": self.pools["Pool_B"].reserve_x,
            "USDC": self.pools["Pool_A"].reserve_y + self.pools["Pool_B"].reserve_y,
        }
        self.mint_window_day: int = self._current_sim_day()
        self.mint_window_used_ust: Decimal = Decimal("0")

        self._db_path = Path(db_path).resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_ledger_table()

        self.check_global_invariants()

    def create_account(
        self,
        address: str,
        ust: Any = Decimal("0"),
        luna: Any = Decimal("0"),
        usdc: Any = Decimal("0"),
    ) -> Account:
        params = {
            "address": address,
            "UST": to_decimal(ust),
            "LUNA": to_decimal(luna),
            "USDC": to_decimal(usdc),
        }

        def mutator(state: EngineState) -> Account:
            if address in state.accounts:
                raise ValueError(f"account already exists: {address}")
            account = Account(
                address=address,
                UST=params["UST"],
                LUNA=params["LUNA"],
                USDC=params["USDC"],
            )
            state.accounts[address] = account

            state.genesis_totals["UST"] += account.UST
            state.genesis_totals["LUNA"] += account.LUNA
            state.genesis_totals["USDC"] += account.USDC
            state.total_luna_supply += account.LUNA
            return account

        account = self._atomic_action("CREATE_ACCOUNT", address, params, mutator)
        return account

    def swap(
        self,
        address: str,
        pool_name: str,
        token_in: str,
        amount: Any,
        min_amount_out: Any | None = None,
    ) -> dict[str, Decimal | str]:
        token_in = token_in.upper()
        amount_dec = to_decimal(amount)
        min_amount_out_dec = (
            None if min_amount_out is None else to_decimal(min_amount_out)
        )
        if min_amount_out_dec is not None and min_amount_out_dec <= 0:
            raise ValueError("min_amount_out must be positive when provided")
        params = {
            "pool": pool_name,
            "token_in": token_in,
            "amount": amount_dec,
            "min_amount_out": min_amount_out_dec,
            "swap_fee": self.engine_config["swap_fee"],
        }

        def mutator(state: EngineState) -> dict[str, Decimal | str]:
            account = self._require_account(state, address)
            pool = self._require_pool(state, pool_name)

            self._withdraw(account, token_in, amount_dec)
            swap_result = pool.swap(token_in, amount_dec, self.engine_config["swap_fee"])
            token_out = str(swap_result["token_out"])
            amount_out = to_decimal(swap_result["amount_out"])
            if min_amount_out_dec is not None and amount_out < min_amount_out_dec:
                raise SlippageExceededError(
                    f"amount_out={amount_out} < min_amount_out={min_amount_out_dec}"
                )
            self._deposit(account, token_out, amount_out)

            return {
                "pool": pool_name,
                "token_in": token_in,
                "amount_in": amount_dec,
                "token_out": token_out,
                "amount_out": amount_out,
                "execution_price": to_decimal(swap_result["execution_price"]),
                "slippage": to_decimal(swap_result["slippage"]),
            }

        return self._atomic_action("SWAP", address, params, mutator)

    def ust_to_luna(self, address: str, amount_ust: Any) -> dict[str, Decimal | str]:
        amount_ust_dec = to_decimal(amount_ust)
        params = {
            "amount_ust": amount_ust_dec,
            "oracle_source": "Pool_B(LUNA/USDC)",
        }

        def mutator(state: EngineState) -> dict[str, Decimal | str]:
            self._ensure_minting_enabled()
            account = self._require_account(state, address)
            self._withdraw(account, "UST", amount_ust_dec)
            self._enforce_daily_mint_cap(state, amount_ust_dec)

            price = self._oracle_price_from_state(state)
            luna_minted = amount_ust_dec / price
            if luna_minted <= 0:
                raise ValueError("luna minted must be positive")

            self._deposit(account, "LUNA", luna_minted)
            state.counters["total_ust_burned_for_luna"] += amount_ust_dec
            state.counters["total_luna_minted"] += luna_minted
            state.total_luna_supply += luna_minted

            return {
                "action": "UST_TO_LUNA",
                "amount_ust_burned": amount_ust_dec,
                "oracle_price_usdc_per_luna": price,
                "luna_minted": luna_minted,
            }

        return self._atomic_action("UST_TO_LUNA", address, params, mutator)

    def luna_to_ust(self, address: str, amount_luna: Any) -> dict[str, Decimal | str]:
        amount_luna_dec = to_decimal(amount_luna)
        params = {
            "amount_luna": amount_luna_dec,
            "oracle_source": "Pool_B(LUNA/USDC)",
        }

        def mutator(state: EngineState) -> dict[str, Decimal | str]:
            self._ensure_minting_enabled()
            account = self._require_account(state, address)
            self._withdraw(account, "LUNA", amount_luna_dec)

            price = self._oracle_price_from_state(state)
            ust_minted = amount_luna_dec * price
            if ust_minted <= 0:
                raise ValueError("ust minted must be positive")

            self._deposit(account, "UST", ust_minted)
            state.counters["total_luna_burned_for_ust"] += amount_luna_dec
            state.counters["total_ust_minted"] += ust_minted
            state.total_luna_supply -= amount_luna_dec

            if state.total_luna_supply < 0:
                raise InvariantViolationError("total_luna_supply cannot become negative")

            return {
                "action": "LUNA_TO_UST",
                "amount_luna_burned": amount_luna_dec,
                "oracle_price_usdc_per_luna": price,
                "ust_minted": ust_minted,
            }

        return self._atomic_action("LUNA_TO_UST", address, params, mutator)

    def get_oracle_price(self) -> Decimal:
        return self._oracle_price_from_state(self._clone_state())

    def set_simulation_clock(self, current_tick: int, ticks_per_day: int) -> None:
        if current_tick < 0:
            raise ValueError("current_tick must be >= 0")
        if ticks_per_day <= 0:
            raise ValueError("ticks_per_day must be > 0")
        self.current_tick = int(current_tick)
        self.ticks_per_day = int(ticks_per_day)

    def get_simulation_clock(self) -> dict[str, int]:
        return {
            "current_tick": int(self.current_tick),
            "ticks_per_day": int(self.ticks_per_day),
            "current_day": int(self._current_sim_day()),
        }

    def get_account_balance(self, address: str, token: str) -> Decimal:
        account = self.accounts.get(address)
        if account is None:
            raise ValueError(f"unknown account: {address}")
        token_upper = token.upper()
        if token_upper not in TOKEN_SET:
            raise ValueError(f"unsupported token: {token_upper}")
        return Decimal(getattr(account, token_upper))

    def estimate_amount_out(
        self,
        pool_name: str,
        token_in: str,
        amount: Any,
    ) -> dict[str, Decimal | str]:
        amount_dec = to_decimal(amount)
        if amount_dec <= 0:
            raise ValueError("amount must be positive")
        state = self._clone_state()
        pool = self._require_pool(state, pool_name)
        pool_clone = pool.clone()
        swap_result = pool_clone.swap(
            token_in=token_in,
            amount_in=amount_dec,
            fee_rate=self.engine_config["swap_fee"],
        )
        return {
            "pool": pool_name,
            "token_in": token_in.upper(),
            "amount_in": amount_dec,
            "token_out": str(swap_result["token_out"]),
            "amount_out": to_decimal(swap_result["amount_out"]),
            "execution_price": to_decimal(swap_result["execution_price"]),
            "slippage": to_decimal(swap_result["slippage"]),
        }

    def charge_fee(
        self,
        address: str,
        token: str,
        amount: Any,
        reason: str = "",
    ) -> dict[str, Decimal | str]:
        token_upper = token.upper()
        amount_dec = to_decimal(amount)
        if amount_dec <= 0:
            raise ValueError("fee amount must be positive")
        params = {
            "token": token_upper,
            "amount": amount_dec,
            "reason": reason,
        }

        def mutator(state: EngineState) -> dict[str, Decimal | str]:
            account = self._require_account(state, address)
            self._withdraw(account, token_upper, amount_dec)
            state.fee_vault[token_upper] += amount_dec
            return {
                "token": token_upper,
                "amount": amount_dec,
                "reason": reason,
            }

        return self._atomic_action("CHARGE_FEE", address, params, mutator)

    def get_engine_config(self) -> dict[str, Any]:
        return {
            "minting_allowed": bool(self.engine_config["minting_allowed"]),
            "swap_fee": Decimal(self.engine_config["swap_fee"]),
            "daily_mint_cap": (
                None
                if self.engine_config["daily_mint_cap"] is None
                else Decimal(self.engine_config["daily_mint_cap"])
            ),
        }

    def update_engine_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("updates must be a non-empty dict")

        allowed = {"minting_allowed", "swap_fee", "daily_mint_cap"}
        unknown = set(updates.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")

        previous = self.get_engine_config()
        try:
            next_config = self.get_engine_config()
            if "minting_allowed" in updates:
                next_config["minting_allowed"] = bool(updates["minting_allowed"])
            if "swap_fee" in updates:
                next_config["swap_fee"] = to_decimal(updates["swap_fee"])
            if "daily_mint_cap" in updates:
                next_config["daily_mint_cap"] = self._normalize_daily_mint_cap(
                    updates["daily_mint_cap"]
                )

            self.engine_config = next_config
            self._validate_engine_config()

            snapshot = self._build_snapshot(self._clone_state())
            self._write_ledger(
                action_type="UPDATE_ENGINE_CONFIG",
                actor="SYSTEM",
                params={"before": previous, "updates": updates, "after": next_config},
                success=True,
                error_message=None,
                snapshot=snapshot,
            )
            return self.get_engine_config()
        except Exception as exc:
            self.engine_config = previous
            snapshot = self._build_snapshot(self._clone_state())
            self._write_ledger(
                action_type="UPDATE_ENGINE_CONFIG",
                actor="SYSTEM",
                params={"before": previous, "updates": updates},
                success=False,
                error_message=str(exc),
                snapshot=snapshot,
            )
            raise

    def check_global_invariants(self) -> dict[str, Decimal]:
        state = self._clone_state()
        return self._check_global_invariants_state(state)

    def get_token_totals(self) -> dict[str, Decimal]:
        return self._compute_totals(self._clone_state())

    def get_ledger_count(self) -> int:
        cursor = self._conn.execute("SELECT COUNT(1) FROM ledger")
        row = cursor.fetchone()
        return int(row[0])

    def get_ledger_success_failure(self) -> dict[str, int]:
        cursor = self._conn.execute(
            "SELECT success, COUNT(1) FROM ledger GROUP BY success ORDER BY success"
        )
        stats = {"success": 0, "failure": 0}
        for success, count in cursor.fetchall():
            if int(success) == 1:
                stats["success"] = int(count)
            else:
                stats["failure"] = int(count)
        return stats

    def get_state_snapshot(self) -> dict[str, Any]:
        return self._build_snapshot(self._clone_state())

    def get_db_path(self) -> Path:
        return Path(self._db_path)

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None

    # --------------------------
    # Internal helpers
    # --------------------------

    def _validate_engine_config(self) -> None:
        fee = self.engine_config["swap_fee"]
        if fee < 0 or fee >= 1:
            raise ValueError("engine_config.swap_fee must be in [0, 1)")

        cap = self.engine_config["daily_mint_cap"]
        if cap is not None and cap < 0:
            raise ValueError("engine_config.daily_mint_cap must be >= 0 or None")

    def _init_ledger_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                actor TEXT,
                params_json TEXT NOT NULL,
                success INTEGER NOT NULL,
                error_message TEXT,
                snapshot_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _clone_state(self) -> EngineState:
        return EngineState(
            accounts={
                address: account.model_copy(deep=True)
                for address, account in self.accounts.items()
            },
            pools={name: pool.clone() for name, pool in self.pools.items()},
            fee_vault={k: Decimal(v) for k, v in self.fee_vault.items()},
            counters={k: Decimal(v) for k, v in self.counters.items()},
            total_luna_supply=Decimal(self.total_luna_supply),
            genesis_totals={k: Decimal(v) for k, v in self.genesis_totals.items()},
            mint_window_day=int(self.mint_window_day),
            mint_window_used_ust=Decimal(self.mint_window_used_ust),
        )

    def _commit_state(self, state: EngineState) -> None:
        self.accounts = state.accounts
        self.pools = state.pools
        self.fee_vault = state.fee_vault
        self.counters = state.counters
        self.total_luna_supply = state.total_luna_supply
        self.genesis_totals = state.genesis_totals
        self.mint_window_day = state.mint_window_day
        self.mint_window_used_ust = state.mint_window_used_ust

    def _atomic_action(
        self,
        action_type: str,
        actor: str,
        params: dict[str, Any],
        mutator: Callable[[EngineState], Any],
    ) -> Any:
        state = self._clone_state()
        try:
            result = mutator(state)
            self._check_global_invariants_state(state)
            self._commit_state(state)
            snapshot = self._build_snapshot(self._clone_state())
            self._write_ledger(
                action_type=action_type,
                actor=actor,
                params=params,
                success=True,
                error_message=None,
                snapshot=snapshot,
            )
            return result
        except Exception as exc:
            snapshot = self._build_snapshot(self._clone_state())
            self._write_ledger(
                action_type=action_type,
                actor=actor,
                params=params,
                success=False,
                error_message=str(exc),
                snapshot=snapshot,
            )
            raise

    def _write_ledger(
        self,
        action_type: str,
        actor: str,
        params: dict[str, Any],
        success: bool,
        error_message: str | None,
        snapshot: dict[str, Any],
    ) -> None:
        created_at = datetime.now(tz=timezone.utc).isoformat()
        params_json = json.dumps(to_jsonable(params), ensure_ascii=False)
        snapshot_json = json.dumps(to_jsonable(snapshot), ensure_ascii=False)

        self._conn.execute(
            """
            INSERT INTO ledger (
                action_type,
                actor,
                params_json,
                success,
                error_message,
                snapshot_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_type,
                actor,
                params_json,
                1 if success else 0,
                error_message,
                snapshot_json,
                created_at,
            ),
        )
        self._conn.commit()

    def _require_account(self, state: EngineState, address: str) -> Account:
        account = state.accounts.get(address)
        if account is None:
            raise ValueError(f"unknown account: {address}")
        return account

    def _require_pool(self, state: EngineState, pool_name: str) -> AMM_Pool:
        pool = state.pools.get(pool_name)
        if pool is None:
            raise ValueError(f"unknown pool: {pool_name}")
        return pool

    def _ensure_minting_enabled(self) -> None:
        if not self.engine_config["minting_allowed"]:
            raise PermissionError("minting is disabled by engine_config")

    def _current_sim_day(self) -> int:
        return self.current_tick // self.ticks_per_day

    def _normalize_daily_mint_cap(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        cap = to_decimal(value)
        if cap < 0:
            raise ValueError("daily_mint_cap must be >= 0 or None")
        return cap

    def _enforce_daily_mint_cap(self, state: EngineState, amount_ust: Decimal) -> None:
        cap = self.engine_config["daily_mint_cap"]
        if cap is None:
            return

        current_day = self._current_sim_day()
        if state.mint_window_day != current_day:
            state.mint_window_day = current_day
            state.mint_window_used_ust = Decimal("0")

        remaining = cap - state.mint_window_used_ust
        if amount_ust > remaining:
            raise PermissionError(
                "daily mint cap exceeded: "
                f"cap={cap}, used={state.mint_window_used_ust}, requested={amount_ust}"
            )

        state.mint_window_used_ust += amount_ust

    def _withdraw(self, account: Account, token: str, amount: Decimal) -> None:
        token = token.upper()
        if token not in TOKEN_SET:
            raise ValueError(f"unsupported token: {token}")
        amount = to_decimal(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")

        balance = to_decimal(getattr(account, token))
        if balance < amount:
            raise InsufficientFundsError(
                f"insufficient {token}: balance={balance}, required={amount}"
            )
        setattr(account, token, balance - amount)

    def _deposit(self, account: Account, token: str, amount: Decimal) -> None:
        token = token.upper()
        if token not in TOKEN_SET:
            raise ValueError(f"unsupported token: {token}")
        amount = to_decimal(amount)
        if amount < 0:
            raise ValueError("amount cannot be negative")

        balance = to_decimal(getattr(account, token))
        setattr(account, token, balance + amount)

    def _oracle_price_from_state(self, state: EngineState) -> Decimal:
        pool_b = self._require_pool(state, "Pool_B")
        price = pool_b.get_price(base_token="LUNA", quote_token="USDC")
        if price <= 0:
            raise ValueError("oracle price must be > 0")
        return price

    def _compute_totals(self, state: EngineState) -> dict[str, Decimal]:
        totals = {
            "UST": Decimal("0"),
            "LUNA": Decimal("0"),
            "USDC": Decimal("0"),
        }
        for account in state.accounts.values():
            totals["UST"] += account.UST
            totals["LUNA"] += account.LUNA
            totals["USDC"] += account.USDC

        for pool in state.pools.values():
            totals[pool.token_x] += pool.reserve_x
            totals[pool.token_y] += pool.reserve_y

        for token, amount in state.fee_vault.items():
            totals[token] += amount

        return totals

    def _check_global_invariants_state(self, state: EngineState) -> dict[str, Decimal]:
        for account in state.accounts.values():
            if account.UST < 0 or account.LUNA < 0 or account.USDC < 0:
                raise InvariantViolationError("account balance cannot be negative")

        for pool in state.pools.values():
            if pool.reserve_x <= 0 or pool.reserve_y <= 0:
                raise InvariantViolationError("pool reserves must stay positive")

        for token, amount in state.fee_vault.items():
            if token not in TOKEN_SET:
                raise InvariantViolationError(f"unsupported fee vault token: {token}")
            if amount < 0:
                raise InvariantViolationError("fee vault balance cannot be negative")

        for key, value in state.counters.items():
            if value < 0:
                raise InvariantViolationError(f"counter {key} cannot be negative")

        if state.mint_window_used_ust < 0:
            raise InvariantViolationError("mint_window_used_ust cannot be negative")

        cap = self.engine_config["daily_mint_cap"]
        if cap is not None and state.mint_window_day == self._current_sim_day():
            if state.mint_window_used_ust > cap and not dual_isclose(
                state.mint_window_used_ust, cap
            ):
                raise InvariantViolationError(
                    "mint_window_used_ust cannot exceed daily_mint_cap"
                )

        totals = self._compute_totals(state)

        expected_ust = (
            state.genesis_totals["UST"]
            + state.counters["total_ust_minted"]
            - state.counters["total_ust_burned_for_luna"]
        )
        expected_luna = (
            state.genesis_totals["LUNA"]
            + state.counters["total_luna_minted"]
            - state.counters["total_luna_burned_for_ust"]
        )
        expected_usdc = state.genesis_totals["USDC"]

        self._assert_dual_close(totals["UST"], expected_ust, "UST conservation")
        self._assert_dual_close(totals["LUNA"], expected_luna, "LUNA conservation")
        self._assert_dual_close(totals["USDC"], expected_usdc, "USDC conservation")
        self._assert_dual_close(
            totals["LUNA"],
            state.total_luna_supply,
            "LUNA total supply consistency",
        )
        return totals

    def _assert_dual_close(self, left: Decimal, right: Decimal, label: str) -> None:
        if not dual_isclose(left, right):
            raise InvariantViolationError(f"{label} failed: left={left}, right={right}")

    def _build_snapshot(self, state: EngineState) -> dict[str, Any]:
        accounts_snapshot: dict[str, dict[str, str]] = {}
        for address in sorted(state.accounts.keys()):
            account = state.accounts[address]
            accounts_snapshot[address] = {
                "UST": str(account.UST),
                "LUNA": str(account.LUNA),
                "USDC": str(account.USDC),
            }

        pools_snapshot: dict[str, dict[str, str]] = {}
        for name in sorted(state.pools.keys()):
            pool = state.pools[name]
            pools_snapshot[name] = {
                "token_x": pool.token_x,
                "token_y": pool.token_y,
                "reserve_x": str(pool.reserve_x),
                "reserve_y": str(pool.reserve_y),
            }

        return {
            "accounts": accounts_snapshot,
            "pools": pools_snapshot,
            "fee_vault": {k: str(v) for k, v in state.fee_vault.items()},
            "counters": {k: str(v) for k, v in state.counters.items()},
            "total_luna_supply": str(state.total_luna_supply),
            "genesis_totals": {k: str(v) for k, v in state.genesis_totals.items()},
            "engine_config": {
                "minting_allowed": bool(self.engine_config["minting_allowed"]),
                "swap_fee": str(self.engine_config["swap_fee"]),
                "daily_mint_cap": (
                    None
                    if self.engine_config["daily_mint_cap"] is None
                    else str(self.engine_config["daily_mint_cap"])
                ),
            },
            "mint_window": {
                "day_index": state.mint_window_day,
                "used_ust": str(state.mint_window_used_ust),
            },
            "simulation_clock": {
                "current_tick": self.current_tick,
                "ticks_per_day": self.ticks_per_day,
                "current_day": self._current_sim_day(),
            },
            "oracle_price_usdc_per_luna": str(self._oracle_price_from_state(state)),
        }


def dual_isclose(left: Decimal, right: Decimal) -> bool:
    left = to_decimal(left)
    right = to_decimal(right)
    diff = abs(left - right)

    decimal_scale = max(abs(left), abs(right), Decimal("1"))
    decimal_threshold = max(DECIMAL_ABS_TOL, DECIMAL_REL_TOL * decimal_scale)
    decimal_ok = diff <= decimal_threshold

    # Required by spec: include math.isclose as a second guard.
    float_ok = math.isclose(
        float(left),
        float(right),
        rel_tol=FLOAT_REL_TOL,
        abs_tol=FLOAT_ABS_TOL,
    )
    return decimal_ok and float_ok


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid numeric amount")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"cannot convert to Decimal: {value}") from exc


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Account):
        return {
            "address": value.address,
            "UST": str(value.UST),
            "LUNA": str(value.LUNA),
            "USDC": str(value.USDC),
        }
    if isinstance(value, AMM_Pool):
        return {
            "token_x": value.token_x,
            "token_y": value.token_y,
            "reserve_x": str(value.reserve_x),
            "reserve_y": str(value.reserve_y),
        }
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    return value


__all__ = [
    "ACE_Engine",
    "Account",
    "AMM_Pool",
    "InsufficientFundsError",
    "SlippageExceededError",
    "InvariantViolationError",
]
