from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..engine.ace_engine import ACE_Engine, InsufficientFundsError
from .compiler_agent import CompilerAgent, CompilerValidationError
from .mitigation import (
    ExistingProposalState,
    GovernanceMitigationModule,
    MitigationDecision,
    MitigationProposalData,
)


class GovernanceError(Exception):
    """Base governance exception."""


class ProposalLimitError(GovernanceError):
    """Raised when proposal concurrency guardrails are hit."""


class ProposalNotFoundError(GovernanceError):
    """Raised when a proposal id does not exist."""


class ProposalStateError(GovernanceError):
    """Raised when a proposal is not in a votable/settleable state."""


class ProposalMitigationError(GovernanceError):
    """Raised when proposal is rejected by mitigation filters."""


@dataclass
class VoteRecord:
    voter: str
    decision: str
    weight: Decimal
    voted_tick: int


@dataclass
class GovernanceProposal:
    proposal_id: str
    proposer: str
    text: str
    created_tick: int
    voting_end_tick: int
    status: str
    proposal_fee_luna: Decimal
    total_luna_snapshot: Decimal
    snapshot_weights: dict[str, Decimal] = field(default_factory=dict)
    votes: dict[str, VoteRecord] = field(default_factory=dict)
    settled_tick: int | None = None
    compiler_patch: list[dict[str, Any]] = field(default_factory=list)
    compiler_error: str | None = None
    quorum_ratio: Decimal = Decimal("0")
    approve_weight: Decimal = Decimal("0")
    reject_weight: Decimal = Decimal("0")
    abstain_weight: Decimal = Decimal("0")
    governance_concentration: Decimal = Decimal("0")


@dataclass
class GovernanceSettlement:
    proposal_id: str
    status: str
    passed: bool
    settled_tick: int
    quorum_ratio: Decimal
    approve_weight: Decimal
    reject_weight: Decimal
    abstain_weight: Decimal
    participating_weight: Decimal
    total_luna_snapshot: Decimal
    governance_concentration: Decimal
    queued_update_ids: list[str] = field(default_factory=list)
    compiler_error: str | None = None


@dataclass
class GovernanceUpdate:
    update_id: str
    proposal_id: str
    scope: str
    parameter: str
    new_value: Any
    reason: str
    activate_tick: int
    status: str = "pending"
    applied_tick: int | None = None
    apply_error: str | None = None


@dataclass
class GovernanceApplyResult:
    update_id: str
    proposal_id: str
    scope: str
    parameter: str
    status: str
    applied_tick: int
    previous_value: Any | None = None
    new_value: Any | None = None
    error: str | None = None


class GovernanceModule:
    """Governance workflow with LUNA-only voting weight snapshots."""

    def __init__(
        self,
        db_path: str | Path,
        compiler_agent: CompilerAgent | None = None,
        *,
        proposal_fee_luna: Any = "1000",
        max_open_proposals: int = 3,
        max_open_per_agent: int = 1,
        voting_window_ticks: int = 20,
        quorum_ratio: Any = "0.3",
        mitigation_strategy: GovernanceMitigationModule | None = None,
    ) -> None:
        self.compiler_agent = compiler_agent or CompilerAgent()
        self.mitigation_strategy = mitigation_strategy
        self.logger = logging.getLogger("ace_sim.governance")

        self.proposal_fee_luna = Decimal(str(proposal_fee_luna))
        self.max_open_proposals = int(max_open_proposals)
        self.max_open_per_agent = int(max_open_per_agent)
        self.voting_window_ticks = int(voting_window_ticks)
        self.quorum_ratio = Decimal(str(quorum_ratio))

        if self.proposal_fee_luna <= 0:
            raise ValueError("proposal_fee_luna must be > 0")
        if self.max_open_proposals <= 0:
            raise ValueError("max_open_proposals must be > 0")
        if self.max_open_per_agent <= 0:
            raise ValueError("max_open_per_agent must be > 0")
        if self.voting_window_ticks <= 0:
            raise ValueError("voting_window_ticks must be > 0")
        if self.quorum_ratio < 0 or self.quorum_ratio > 1:
            raise ValueError("quorum_ratio must be in [0,1]")

        self._proposals: dict[str, GovernanceProposal] = {}
        self._pending_updates: list[GovernanceUpdate] = []
        self.parameter_version: int = 0

        self._db_path = Path(db_path).resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_tables()

    def close(self) -> None:
        if self.mitigation_strategy is not None:
            self.mitigation_strategy.close()
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None

    def submit_proposal(
        self,
        *,
        proposer: str,
        proposal_text: str,
        current_tick: int,
        engine: ACE_Engine,
    ) -> str:
        proposer = str(proposer).strip()
        proposal_text = str(proposal_text).strip()
        if not proposer:
            raise GovernanceError("proposer cannot be empty")
        if not proposal_text:
            raise GovernanceError("proposal text cannot be empty")

        proposal_id = str(uuid4())
        open_props = [p for p in self._proposals.values() if p.status == "open"]
        mitigation_decision: MitigationDecision | None = None
        if self.mitigation_strategy is not None:
            mitigation_decision = self.mitigation_strategy.pre_check_proposal(
                proposal_data=MitigationProposalData(
                    proposal_id=proposal_id,
                    proposer=proposer,
                    proposal_text=proposal_text,
                    current_tick=int(current_tick),
                ),
                open_proposals=[
                    ExistingProposalState(
                        proposal_id=item.proposal_id,
                        proposer=item.proposer,
                        text=item.text,
                        status=item.status,
                    )
                    for item in open_props
                ],
                max_open_proposals=self.max_open_proposals,
            )
            if not mitigation_decision.allow:
                raise ProposalMitigationError(
                    mitigation_decision.reject_reason or "proposal rejected by mitigation"
                )
            if mitigation_decision.evict_proposal_id is not None:
                self._evict_open_proposal_by_mitigation(
                    proposal_id=mitigation_decision.evict_proposal_id,
                    by_proposer=proposer,
                    current_tick=int(current_tick),
                )
                open_props = [p for p in self._proposals.values() if p.status == "open"]

        if len(open_props) >= self.max_open_proposals:
            raise ProposalLimitError(
                f"open proposal limit reached: {self.max_open_proposals}"
            )
        proposer_open = [p for p in open_props if p.proposer == proposer]
        if len(proposer_open) >= self.max_open_per_agent:
            raise ProposalLimitError(
                f"proposer open limit reached: {self.max_open_per_agent}"
            )

        try:
            luna_balance = engine.get_account_balance(proposer, "LUNA")
        except Exception as exc:  # noqa: BLE001
            raise GovernanceError(f"unknown proposer account: {proposer}") from exc

        if luna_balance < self.proposal_fee_luna:
            raise InsufficientFundsError(
                f"insufficient LUNA for proposal fee: balance={luna_balance}, "
                f"required={self.proposal_fee_luna}"
            )

        engine.charge_fee(
            address=proposer,
            token="LUNA",
            amount=self.proposal_fee_luna,
            reason=f"proposal_fee:{proposal_id}",
        )

        snapshot_weights = {
            address: Decimal(account.LUNA)
            for address, account in engine.accounts.items()
        }
        total_luna_snapshot = sum(snapshot_weights.values(), Decimal("0"))

        proposal = GovernanceProposal(
            proposal_id=proposal_id,
            proposer=proposer,
            text=proposal_text,
            created_tick=int(current_tick),
            voting_end_tick=int(current_tick) + self.voting_window_ticks,
            status="open",
            proposal_fee_luna=self.proposal_fee_luna,
            total_luna_snapshot=total_luna_snapshot,
            snapshot_weights=snapshot_weights,
        )
        self._proposals[proposal_id] = proposal
        self._write_proposal(proposal)
        if self.mitigation_strategy is not None and mitigation_decision is not None:
            self.mitigation_strategy.on_proposal_accepted(
                proposal_id=proposal_id,
                proposer=proposer,
                proposal_text=proposal_text,
                decision=mitigation_decision,
            )
        return proposal_id

    def submit_vote(
        self,
        *,
        voter: str,
        proposal_id: str,
        decision: str,
        current_tick: int,
    ) -> str:
        voter = str(voter).strip()
        proposal_id = str(proposal_id).strip()
        decision_norm = str(decision).strip().lower()

        if not voter:
            raise GovernanceError("voter cannot be empty")
        if decision_norm not in {"approve", "reject", "abstain"}:
            raise GovernanceError("decision must be approve/reject/abstain")

        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(f"proposal not found: {proposal_id}")
        if proposal.status != "open":
            raise ProposalStateError(
                f"proposal is not open, status={proposal.status}"
            )
        if int(current_tick) > proposal.voting_end_tick:
            raise ProposalStateError("proposal voting window already ended")

        weight = Decimal(proposal.snapshot_weights.get(voter, Decimal("0")))
        vote = VoteRecord(
            voter=voter,
            decision=decision_norm,
            weight=weight,
            voted_tick=int(current_tick),
        )
        proposal.votes[voter] = vote
        self._write_vote(proposal_id=proposal_id, vote=vote)
        return f"{proposal_id}:{voter}"

    def settle_due(
        self,
        *,
        current_tick: int,
    ) -> list[GovernanceSettlement]:
        settlements: list[GovernanceSettlement] = []

        due = [
            proposal
            for proposal in self._proposals.values()
            if proposal.status == "open" and int(current_tick) >= proposal.voting_end_tick
        ]

        for proposal in sorted(due, key=lambda p: (p.voting_end_tick, p.proposal_id)):
            approve_weight = sum(
                (vote.weight for vote in proposal.votes.values() if vote.decision == "approve"),
                Decimal("0"),
            )
            reject_weight = sum(
                (vote.weight for vote in proposal.votes.values() if vote.decision == "reject"),
                Decimal("0"),
            )
            abstain_weight = sum(
                (vote.weight for vote in proposal.votes.values() if vote.decision == "abstain"),
                Decimal("0"),
            )
            participating = approve_weight + reject_weight + abstain_weight

            if proposal.total_luna_snapshot > 0:
                quorum_ratio = participating / proposal.total_luna_snapshot
            else:
                quorum_ratio = Decimal("0")

            proposal.approve_weight = approve_weight
            proposal.reject_weight = reject_weight
            proposal.abstain_weight = abstain_weight
            proposal.quorum_ratio = quorum_ratio
            proposal.settled_tick = int(current_tick)

            top3_approve = sorted(
                (
                    vote.weight
                    for vote in proposal.votes.values()
                    if vote.decision == "approve"
                ),
                reverse=True,
            )[:3]
            top3_sum = sum(top3_approve, Decimal("0"))
            governance_concentration = (
                Decimal("0")
                if participating <= 0
                else top3_sum / participating
            )
            proposal.governance_concentration = governance_concentration

            passed = quorum_ratio >= self.quorum_ratio and approve_weight > reject_weight
            queued_update_ids: list[str] = []
            compiler_error: str | None = None

            if passed:
                try:
                    patches = self.compiler_agent.compile_proposal(proposal.text)
                    proposal.compiler_patch = patches
                    proposal.status = "passed_pending_apply"
                    for patch in patches:
                        update_id = str(uuid4())
                        update_item = GovernanceUpdate(
                            update_id=update_id,
                            proposal_id=proposal.proposal_id,
                            scope=str(patch["scope"]),
                            parameter=str(patch["parameter"]),
                            new_value=patch["new_value"],
                            reason=str(patch["reason"]),
                            activate_tick=int(current_tick) + 1,
                        )
                        self._pending_updates.append(update_item)
                        queued_update_ids.append(update_id)
                        self._write_pending_update(update_item)
                except (CompilerValidationError, ValueError) as exc:
                    compiler_error = str(exc)
                    proposal.compiler_error = compiler_error
                    proposal.status = "passed_compile_failed"
            else:
                proposal.status = "rejected"

            settlement = GovernanceSettlement(
                proposal_id=proposal.proposal_id,
                status=proposal.status,
                passed=passed and proposal.status == "passed_pending_apply",
                settled_tick=int(current_tick),
                quorum_ratio=quorum_ratio,
                approve_weight=approve_weight,
                reject_weight=reject_weight,
                abstain_weight=abstain_weight,
                participating_weight=participating,
                total_luna_snapshot=proposal.total_luna_snapshot,
                governance_concentration=governance_concentration,
                queued_update_ids=queued_update_ids,
                compiler_error=compiler_error,
            )
            settlements.append(settlement)
            self._update_settlement(proposal, settlement)

        return settlements

    def apply_due_updates(
        self,
        *,
        current_tick: int,
        engine: ACE_Engine,
        orchestrator: Any,
    ) -> list[GovernanceApplyResult]:
        results: list[GovernanceApplyResult] = []

        for update in self._pending_updates:
            if update.status != "pending":
                continue
            if int(update.activate_tick) > int(current_tick):
                continue

            previous_value = self._read_current_value(
                scope=update.scope,
                parameter=update.parameter,
                engine=engine,
                orchestrator=orchestrator,
            )
            try:
                if update.scope == "engine":
                    engine.update_engine_config({update.parameter: update.new_value})
                elif update.scope == "orchestrator":
                    if update.parameter == "ticks_per_day":
                        orchestrator.set_ticks_per_day(int(update.new_value))
                    elif update.parameter == "max_inbox_size":
                        orchestrator.set_default_max_inbox_size(int(update.new_value))
                    else:
                        raise GovernanceError(
                            f"unsupported orchestrator parameter: {update.parameter}"
                        )
                else:
                    raise GovernanceError(f"unsupported update scope: {update.scope}")

                update.status = "applied"
                update.applied_tick = int(current_tick)
                self.parameter_version += 1
                result = GovernanceApplyResult(
                    update_id=update.update_id,
                    proposal_id=update.proposal_id,
                    scope=update.scope,
                    parameter=update.parameter,
                    status="applied",
                    applied_tick=int(current_tick),
                    previous_value=previous_value,
                    new_value=update.new_value,
                    error=None,
                )
            except Exception as exc:  # noqa: BLE001
                update.status = "failed"
                update.applied_tick = int(current_tick)
                update.apply_error = str(exc)
                result = GovernanceApplyResult(
                    update_id=update.update_id,
                    proposal_id=update.proposal_id,
                    scope=update.scope,
                    parameter=update.parameter,
                    status="failed",
                    applied_tick=int(current_tick),
                    previous_value=previous_value,
                    new_value=update.new_value,
                    error=str(exc),
                )

            self._update_pending_update(update)
            results.append(result)

        changed_proposals = {item.proposal_id for item in self._pending_updates}
        for proposal_id in changed_proposals:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.status != "passed_pending_apply":
                continue
            related = [item for item in self._pending_updates if item.proposal_id == proposal_id]
            if not related:
                continue
            if any(item.status == "pending" for item in related):
                continue
            if any(item.status == "failed" for item in related):
                proposal.status = "apply_failed"
            else:
                proposal.status = "applied"
            self._update_proposal_status(
                proposal_id=proposal.proposal_id,
                status=proposal.status,
                settled_tick=proposal.settled_tick,
            )

        return results

    def _read_current_value(
        self,
        *,
        scope: str,
        parameter: str,
        engine: ACE_Engine,
        orchestrator: Any,
    ) -> Any | None:
        try:
            if scope == "engine":
                return engine.get_engine_config().get(parameter)
            if scope == "orchestrator":
                if parameter == "ticks_per_day":
                    return int(orchestrator.ticks_per_day)
                if parameter == "max_inbox_size":
                    return int(orchestrator.default_max_inbox_size)
            return None
        except Exception:  # noqa: BLE001
            return None

    def _evict_open_proposal_by_mitigation(
        self,
        *,
        proposal_id: str,
        by_proposer: str,
        current_tick: int,
    ) -> None:
        target = self._proposals.get(str(proposal_id))
        if target is None or target.status != "open":
            return
        target.status = "REJECTED_BY_MITIGATION"
        target.settled_tick = int(current_tick)
        self._update_proposal_status(
            proposal_id=target.proposal_id,
            status=target.status,
            settled_tick=target.settled_tick,
        )
        if self.mitigation_strategy is not None:
            self.mitigation_strategy.on_proposal_evicted(
                proposal_id=target.proposal_id,
                current_tick=int(current_tick),
            )
        self.logger.info(
            "[PROPOSAL-EVICTION] evicted=%s by=%s tick=%d",
            target.proposal_id,
            by_proposer,
            int(current_tick),
        )

    def get_state(self) -> dict[str, Any]:
        open_count = sum(1 for p in self._proposals.values() if p.status == "open")
        pending_updates = [
            {
                "update_id": item.update_id,
                "proposal_id": item.proposal_id,
                "scope": item.scope,
                "parameter": item.parameter,
                "new_value": _jsonable(item.new_value),
                "activate_tick": item.activate_tick,
                "status": item.status,
            }
            for item in self._pending_updates
            if item.status == "pending"
        ]

        proposals = []
        for proposal in sorted(
            self._proposals.values(),
            key=lambda p: (p.created_tick, p.proposal_id),
        ):
            proposals.append(
                {
                    "proposal_id": proposal.proposal_id,
                    "proposer": proposal.proposer,
                    "status": proposal.status,
                    "created_tick": proposal.created_tick,
                    "voting_end_tick": proposal.voting_end_tick,
                    "total_luna_snapshot": str(proposal.total_luna_snapshot),
                    "votes": len(proposal.votes),
                    "quorum_ratio": str(proposal.quorum_ratio),
                    "approve_weight": str(proposal.approve_weight),
                    "reject_weight": str(proposal.reject_weight),
                    "abstain_weight": str(proposal.abstain_weight),
                    "governance_concentration": str(proposal.governance_concentration),
                    "settled_tick": proposal.settled_tick,
                }
            )

        return {
            "parameter_version": int(self.parameter_version),
            "open_proposals": int(open_count),
            "proposal_fee_luna": str(self.proposal_fee_luna),
            "max_open_proposals": int(self.max_open_proposals),
            "max_open_per_agent": int(self.max_open_per_agent),
            "voting_window_ticks": int(self.voting_window_ticks),
            "quorum_ratio": str(self.quorum_ratio),
            "pending_updates": pending_updates,
            "proposals": proposals,
        }

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL UNIQUE,
                proposer TEXT NOT NULL,
                proposal_text TEXT NOT NULL,
                created_tick INTEGER NOT NULL,
                voting_end_tick INTEGER NOT NULL,
                status TEXT NOT NULL,
                proposal_fee_luna TEXT NOT NULL,
                total_luna_snapshot TEXT NOT NULL,
                snapshot_weights_json TEXT NOT NULL,
                compiler_patch_json TEXT,
                compiler_error TEXT,
                quorum_ratio TEXT,
                approve_weight TEXT,
                reject_weight TEXT,
                abstain_weight TEXT,
                governance_concentration TEXT,
                settled_tick INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                voter TEXT NOT NULL,
                decision TEXT NOT NULL,
                weight TEXT NOT NULL,
                voted_tick INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(proposal_id, voter)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_pending_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id TEXT NOT NULL UNIQUE,
                proposal_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                parameter TEXT NOT NULL,
                new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                activate_tick INTEGER NOT NULL,
                status TEXT NOT NULL,
                applied_tick INTEGER,
                apply_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _write_proposal(self, proposal: GovernanceProposal) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO governance_proposals (
                proposal_id,
                proposer,
                proposal_text,
                created_tick,
                voting_end_tick,
                status,
                proposal_fee_luna,
                total_luna_snapshot,
                snapshot_weights_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.proposal_id,
                proposal.proposer,
                proposal.text,
                int(proposal.created_tick),
                int(proposal.voting_end_tick),
                proposal.status,
                str(proposal.proposal_fee_luna),
                str(proposal.total_luna_snapshot),
                json.dumps(
                    {
                        k: str(v)
                        for k, v in sorted(proposal.snapshot_weights.items())
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        self._conn.commit()

    def _write_vote(self, proposal_id: str, vote: VoteRecord) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO governance_votes (
                proposal_id,
                voter,
                decision,
                weight,
                voted_tick,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id, voter)
            DO UPDATE SET
                decision = excluded.decision,
                weight = excluded.weight,
                voted_tick = excluded.voted_tick,
                updated_at = excluded.updated_at
            """,
            (
                proposal_id,
                vote.voter,
                vote.decision,
                str(vote.weight),
                int(vote.voted_tick),
                now,
            ),
        )
        self._conn.commit()

    def _write_pending_update(self, update: GovernanceUpdate) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO governance_pending_updates (
                update_id,
                proposal_id,
                scope,
                parameter,
                new_value_json,
                reason,
                activate_tick,
                status,
                applied_tick,
                apply_error,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.update_id,
                update.proposal_id,
                update.scope,
                update.parameter,
                json.dumps(_jsonable(update.new_value), ensure_ascii=False),
                update.reason,
                int(update.activate_tick),
                update.status,
                update.applied_tick,
                update.apply_error,
                now,
                now,
            ),
        )
        self._conn.commit()

    def _update_pending_update(self, update: GovernanceUpdate) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE governance_pending_updates
            SET status = ?, applied_tick = ?, apply_error = ?, updated_at = ?
            WHERE update_id = ?
            """,
            (
                update.status,
                update.applied_tick,
                update.apply_error,
                now,
                update.update_id,
            ),
        )
        self._conn.commit()

    def _update_settlement(
        self,
        proposal: GovernanceProposal,
        settlement: GovernanceSettlement,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE governance_proposals
            SET status = ?,
                compiler_patch_json = ?,
                compiler_error = ?,
                quorum_ratio = ?,
                approve_weight = ?,
                reject_weight = ?,
                abstain_weight = ?,
                governance_concentration = ?,
                settled_tick = ?,
                updated_at = ?
            WHERE proposal_id = ?
            """,
            (
                proposal.status,
                (
                    json.dumps([_jsonable(item) for item in proposal.compiler_patch], ensure_ascii=False)
                    if proposal.compiler_patch
                    else None
                ),
                settlement.compiler_error,
                str(settlement.quorum_ratio),
                str(settlement.approve_weight),
                str(settlement.reject_weight),
                str(settlement.abstain_weight),
                str(settlement.governance_concentration),
                int(settlement.settled_tick),
                now,
                proposal.proposal_id,
            ),
        )
        self._conn.commit()

    def _update_proposal_status(
        self,
        *,
        proposal_id: str,
        status: str,
        settled_tick: int | None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE governance_proposals
            SET status = ?, settled_tick = ?, updated_at = ?
            WHERE proposal_id = ?
            """,
            (
                status,
                settled_tick,
                now,
                proposal_id,
            ),
        )
        self._conn.commit()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


__all__ = [
    "GovernanceModule",
    "GovernanceSettlement",
    "GovernanceUpdate",
    "GovernanceApplyResult",
    "GovernanceError",
    "ProposalLimitError",
    "ProposalMitigationError",
    "ProposalNotFoundError",
    "ProposalStateError",
]
