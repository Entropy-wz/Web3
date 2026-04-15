from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DECIMAL_ABS_TOL = Decimal("1e-18")
DECIMAL_REL_TOL = Decimal("1e-18")


@dataclass
class LedgerRow:
    row_id: int
    action_type: str
    actor: str | None
    success: bool
    error_message: str | None
    created_at: str
    snapshot: dict[str, Any]


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value}") from exc


def parse_rows(db_path: Path) -> list[LedgerRow]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT id, action_type, actor, success, error_message, created_at, snapshot_json
        FROM ledger
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()

    parsed: list[LedgerRow] = []
    for row in rows:
        parsed.append(
            LedgerRow(
                row_id=int(row[0]),
                action_type=str(row[1]),
                actor=row[2],
                success=bool(row[3]),
                error_message=row[4],
                created_at=str(row[5]),
                snapshot=json.loads(row[6]) if row[6] else {},
            )
        )
    return parsed


def get_oracle_price(snapshot: dict[str, Any]) -> Decimal:
    return to_decimal(snapshot["oracle_price_usdc_per_luna"])


def get_accounts(snapshot: dict[str, Any]) -> dict[str, dict[str, str]]:
    return snapshot.get("accounts", {})


def get_account_balances(snapshot: dict[str, Any], actor: str | None) -> dict[str, Decimal]:
    if not actor:
        return {"UST": Decimal("0"), "LUNA": Decimal("0"), "USDC": Decimal("0")}
    accounts = get_accounts(snapshot)
    if actor not in accounts:
        return {"UST": Decimal("0"), "LUNA": Decimal("0"), "USDC": Decimal("0")}

    data = accounts[actor]
    return {
        "UST": to_decimal(data.get("UST", "0")),
        "LUNA": to_decimal(data.get("LUNA", "0")),
        "USDC": to_decimal(data.get("USDC", "0")),
    }


def account_value_usdc(snapshot: dict[str, Any], actor: str | None) -> Decimal:
    b = get_account_balances(snapshot, actor)
    p = get_oracle_price(snapshot)
    return b["UST"] + b["USDC"] + (b["LUNA"] * p)


def format_num(x: Decimal, places: int = 6) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{x.quantize(q):,}"


def plus_num(x: Decimal, places: int = 6) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{format_num(x, places=places)}"


def summarize_invariant(snapshot: dict[str, Any]) -> tuple[bool, dict[str, Decimal]]:
    accounts = snapshot["accounts"]
    pools = snapshot["pools"]
    fee_vault = snapshot.get("fee_vault", {})
    counters = {k: to_decimal(v) for k, v in snapshot["counters"].items()}
    genesis = {k: to_decimal(v) for k, v in snapshot["genesis_totals"].items()}

    acc_ust = sum(to_decimal(v["UST"]) for v in accounts.values())
    acc_luna = sum(to_decimal(v["LUNA"]) for v in accounts.values())
    acc_usdc = sum(to_decimal(v["USDC"]) for v in accounts.values())

    total_ust = acc_ust + to_decimal(pools["Pool_A"]["reserve_x"])
    total_luna = acc_luna + to_decimal(pools["Pool_B"]["reserve_x"])
    total_usdc = (
        acc_usdc
        + to_decimal(pools["Pool_A"]["reserve_y"])
        + to_decimal(pools["Pool_B"]["reserve_y"])
    )
    total_ust += to_decimal(fee_vault.get("UST", "0"))
    total_luna += to_decimal(fee_vault.get("LUNA", "0"))
    total_usdc += to_decimal(fee_vault.get("USDC", "0"))

    expected_ust = genesis["UST"] + counters["total_ust_minted"] - counters["total_ust_burned_for_luna"]
    expected_luna = genesis["LUNA"] + counters["total_luna_minted"] - counters["total_luna_burned_for_ust"]
    expected_usdc = genesis["USDC"]

    ok = (
        close_enough(total_ust, expected_ust)
        and close_enough(total_luna, expected_luna)
        and close_enough(total_usdc, expected_usdc)
    )
    return ok, {
        "total_ust": total_ust,
        "expected_ust": expected_ust,
        "total_luna": total_luna,
        "expected_luna": expected_luna,
        "total_usdc": total_usdc,
        "expected_usdc": expected_usdc,
    }


def close_enough(left: Decimal, right: Decimal) -> bool:
    diff = abs(left - right)
    scale = max(abs(left), abs(right), Decimal("1"))
    threshold = max(DECIMAL_ABS_TOL, DECIMAL_REL_TOL * scale)
    return diff <= threshold


def print_timeline(rows: list[LedgerRow]) -> None:
    print("=" * 88)
    print("步骤追踪（每一步前后变化）")
    print("=" * 88)

    prev_snapshot: dict[str, Any] | None = None
    for row in rows:
        curr = row.snapshot
        actor = row.actor or "-"
        p_after = get_oracle_price(curr)
        p_before = get_oracle_price(prev_snapshot) if prev_snapshot else p_after

        value_after = account_value_usdc(curr, row.actor)
        value_before = account_value_usdc(prev_snapshot, row.actor) if prev_snapshot else Decimal("0")

        b_after = get_account_balances(curr, row.actor)
        b_before = get_account_balances(prev_snapshot, row.actor) if prev_snapshot else {
            "UST": Decimal("0"),
            "LUNA": Decimal("0"),
            "USDC": Decimal("0"),
        }

        d_ust = b_after["UST"] - b_before["UST"]
        d_luna = b_after["LUNA"] - b_before["LUNA"]
        d_usdc = b_after["USDC"] - b_before["USDC"]

        status = "成功" if row.success else "失败"
        print(f"[{row.row_id:>3}] {row.action_type:<14} 参与者={actor:<8} 结果={status} 时间={row.created_at}")
        if not row.success and row.error_message:
            print(f"      失败原因: {row.error_message}")
        print(
            "      价格(USDC/LUNA): "
            f"{format_num(p_before, 8)} -> {format_num(p_after, 8)} "
            f"({plus_num(p_after - p_before, 8)})"
        )
        print(
            "      账户变化: "
            f"UST {plus_num(d_ust)} | LUNA {plus_num(d_luna)} | USDC {plus_num(d_usdc)}"
        )
        print(
            "      账户估值(按当下价格折算): "
            f"{format_num(value_before)} -> {format_num(value_after)} "
            f"({plus_num(value_after - value_before)})"
        )

        prev_snapshot = curr


def print_winner_board(last_snapshot: dict[str, Any], first_seen_snapshot_by_actor: dict[str, dict[str, Any]]) -> None:
    print("\n" + "=" * 88)
    print("最终谁赚谁亏")
    print("=" * 88)

    board: list[tuple[str, Decimal, Decimal, Decimal]] = []
    accounts = get_accounts(last_snapshot)
    for actor in sorted(accounts.keys()):
        base_snap = first_seen_snapshot_by_actor[actor]
        base_val = account_value_usdc(base_snap, actor)
        final_val = account_value_usdc(last_snapshot, actor)
        pnl = final_val - base_val
        board.append((actor, base_val, final_val, pnl))

    board.sort(key=lambda x: x[3], reverse=True)
    for actor, base_val, final_val, pnl in board:
        print(
            f"{actor:<12} 起点估值={format_num(base_val)} 终点估值={format_num(final_val)} "
            f"变化={plus_num(pnl)}"
        )


def print_global_summary(last_snapshot: dict[str, Any], rows: list[LedgerRow]) -> None:
    print("\n" + "=" * 88)
    print("全局总览")
    print("=" * 88)

    success = sum(1 for r in rows if r.success)
    fail = len(rows) - success
    print(f"流水总数: {len(rows)} | 成功: {success} | 失败: {fail}")

    p = get_oracle_price(last_snapshot)
    print(f"最新价格(USDC/LUNA): {format_num(p, 8)}")

    ok, metric = summarize_invariant(last_snapshot)
    print(f"总账闭合检查: {'通过' if ok else '未通过'}")
    print(
        "UST 总量: "
        f"{format_num(metric['total_ust'])} (应为 {format_num(metric['expected_ust'])})"
    )
    print(
        "LUNA 总量: "
        f"{format_num(metric['total_luna'])} (应为 {format_num(metric['expected_luna'])})"
    )
    print(
        "USDC 总量: "
        f"{format_num(metric['total_usdc'])} (应为 {format_num(metric['expected_usdc'])})"
    )


def build_first_seen_snapshot_by_actor(rows: list[LedgerRow]) -> dict[str, dict[str, Any]]:
    first_seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        snap = row.snapshot
        for actor in get_accounts(snap).keys():
            if actor not in first_seen:
                first_seen[actor] = snap
    return first_seen


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-readable ACE ledger report")
    default_db = Path(__file__).resolve().parents[2] / "data" / "sqlite" / "ace_demo.sqlite3"
    parser.add_argument(
        "--db",
        default=str(default_db),
        help="path to sqlite database",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")

    rows = parse_rows(db_path)
    if not rows:
        print("数据库里还没有流水记录。")
        return

    first_seen = build_first_seen_snapshot_by_actor(rows)
    last_snapshot = rows[-1].snapshot

    print_timeline(rows)
    print_winner_board(last_snapshot, first_seen)
    print_global_summary(last_snapshot, rows)


if __name__ == "__main__":
    main()
