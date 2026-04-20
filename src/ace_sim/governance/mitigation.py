from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..cognition.llm_router import OpenAIChatAdapter
from ..config.llm_config import load_llm_config


class SemanticScorer(Protocol):
    def score(
        self,
        *,
        proposal_text: str,
        proposer: str,
        open_proposal_texts: list[str],
    ) -> tuple[float, str]:
        ...


@dataclass
class ExistingProposalState:
    proposal_id: str
    proposer: str
    text: str
    status: str
    semantic_score: float | None = None
    priority: str = "NORMAL"


@dataclass
class MitigationProposalData:
    proposal_id: str
    proposer: str
    proposal_text: str
    current_tick: int


@dataclass
class MitigationDecision:
    allow: bool = True
    reject_reason: str | None = None
    semantic_score: float | None = None
    semantic_source: str = "unknown"
    priority: str = "NORMAL"
    evict_proposal_id: str | None = None
    tags: list[str] = field(default_factory=list)


class BaseGovernanceFilter:
    def process(
        self,
        *,
        proposal: MitigationProposalData,
        open_proposals: list[ExistingProposalState],
        decision: MitigationDecision,
        module: "GovernanceMitigationModule",
        max_open_proposals: int,
    ) -> MitigationDecision:
        raise NotImplementedError


class RuleBasedSemanticScorer:
    _SPAM_HINTS = (
        "logo",
        "meme",
        "slogan",
        "contest",
        "banner",
        "branding",
        "symbolic",
        "无意义",
        "口号",
        "吉祥物",
    )
    _SUBSTANCE_HINTS = (
        "mint",
        "minting",
        "swap fee",
        "daily mint cap",
        "inbox size",
        "ticks per day",
        "depeg",
        "patch",
        "fix",
        "emergency",
        "铸造",
        "费率",
        "脱锚",
        "修复",
        "补丁",
        "紧急",
        "治理",
    )

    def score(
        self,
        *,
        proposal_text: str,
        proposer: str,
        open_proposal_texts: list[str],
    ) -> tuple[float, str]:
        text = str(proposal_text).strip().lower()
        if not text:
            return 0.0, "rules"

        score = 0.5
        if len(text) < 40:
            score -= 0.2
        if len(text) > 120:
            score += 0.1
        if any(token in text for token in self._SPAM_HINTS):
            score -= 0.35
        if any(token in text for token in self._SUBSTANCE_HINTS):
            score += 0.25

        normalized = _normalize_text(text)
        for existing in open_proposal_texts:
            if _normalize_text(existing) == normalized:
                score -= 0.3
                break

        score = max(0.0, min(1.0, score))
        return score, "rules"


class FastLLMHybridScorer:
    def __init__(
        self,
        *,
        enable_llm: bool = True,
        timeout: float = 4.0,
        fallback: SemanticScorer | None = None,
    ) -> None:
        self.enable_llm = bool(enable_llm)
        self.timeout = float(timeout)
        self.fallback = fallback or RuleBasedSemanticScorer()
        self._adapter: OpenAIChatAdapter | None = None
        self._model: str | None = None
        self._ready: bool = False
        self._bootstrap_once()

    def _bootstrap_once(self) -> None:
        if not self.enable_llm:
            return
        try:
            cfg = load_llm_config()
            key = cfg.openai.resolved_api_key()
            if not key:
                return
            route = cfg.roles.get("retail")
            model = route.model if route is not None else "gpt-4o-mini"
            self._adapter = OpenAIChatAdapter(
                api_key=key,
                base_url=cfg.openai.base_url,
                organization=cfg.openai.organization,
                project=cfg.openai.project,
            )
            self._model = model
            self._ready = True
        except Exception:  # noqa: BLE001
            self._adapter = None
            self._model = None
            self._ready = False

    def score(
        self,
        *,
        proposal_text: str,
        proposer: str,
        open_proposal_texts: list[str],
    ) -> tuple[float, str]:
        if not self._ready or self._adapter is None or self._model is None:
            return self.fallback.score(
                proposal_text=proposal_text,
                proposer=proposer,
                open_proposal_texts=open_proposal_texts,
            )

        prompt = (
            "You are scoring governance proposal quality.\n"
            "Return only a number in [0,1].\n"
            "0 means placeholder/spam; 1 means concrete, impactful parameter/security patch.\n"
            f"Proposer: {proposer}\n"
            f"Proposal: {proposal_text}\n"
        )
        try:
            raw = self._adapter.generate(
                model=self._model,
                prompt=prompt,
                timeout=self.timeout,
                schema=None,
            )
            if isinstance(raw, dict):
                text = json.dumps(raw, ensure_ascii=False)
            else:
                text = str(raw)
            value = _extract_float(text)
            value = max(0.0, min(1.0, value))
            return value, "llm"
        except Exception:  # noqa: BLE001
            return self.fallback.score(
                proposal_text=proposal_text,
                proposer=proposer,
                open_proposal_texts=open_proposal_texts,
            )


class SemanticQualityFilter(BaseGovernanceFilter):
    def __init__(self, *, threshold: float = 0.3) -> None:
        self.threshold = float(threshold)

    def process(
        self,
        *,
        proposal: MitigationProposalData,
        open_proposals: list[ExistingProposalState],
        decision: MitigationDecision,
        module: "GovernanceMitigationModule",
        max_open_proposals: int,
    ) -> MitigationDecision:
        open_texts = [item.text for item in open_proposals if item.status == "open"]
        score, source = module.semantic_scorer.score(
            proposal_text=proposal.proposal_text,
            proposer=proposal.proposer,
            open_proposal_texts=open_texts,
        )
        decision.semantic_score = score
        decision.semantic_source = source
        if score < self.threshold:
            decision.allow = False
            decision.reject_reason = (
                f"Spam/Placeholder proposal rejected by mitigation: score={score:.3f} < {self.threshold:.3f}"
            )
            decision.tags.append("semantic_reject")
        return decision


class PriorityOverrideFilter(BaseGovernanceFilter):
    _EMERGENCY_PAT = re.compile(
        r"(fix|patch|emergency|hotfix|修复|补丁|紧急)",
        flags=re.IGNORECASE,
    )

    def process(
        self,
        *,
        proposal: MitigationProposalData,
        open_proposals: list[ExistingProposalState],
        decision: MitigationDecision,
        module: "GovernanceMitigationModule",
        max_open_proposals: int,
    ) -> MitigationDecision:
        if proposal.proposer == "project_0" and self._EMERGENCY_PAT.search(
            proposal.proposal_text
        ):
            decision.priority = "HIGH"
            decision.tags.append("priority_high")
        return decision


class PreemptiveSlotFilter(BaseGovernanceFilter):
    def process(
        self,
        *,
        proposal: MitigationProposalData,
        open_proposals: list[ExistingProposalState],
        decision: MitigationDecision,
        module: "GovernanceMitigationModule",
        max_open_proposals: int,
    ) -> MitigationDecision:
        if not decision.allow:
            return decision
        if decision.priority != "HIGH":
            return decision

        current_open = [item for item in open_proposals if item.status == "open"]
        if len(current_open) < int(max_open_proposals):
            return decision

        candidates = [item for item in current_open if item.priority != "HIGH"]
        if not candidates:
            decision.allow = False
            decision.reject_reason = "open proposal limit reached and no non-high proposal can be evicted"
            decision.tags.append("preemptive_failed")
            return decision

        evict_target = min(
            candidates,
            key=lambda item: module.resolve_existing_score(item),
        )
        decision.evict_proposal_id = evict_target.proposal_id
        decision.tags.append("preemptive_eviction")
        return decision


class GovernanceMitigationModule:
    def __init__(
        self,
        *,
        base_db_path: str | Path,
        mode: str = "semantic",
        semantic_scorer: SemanticScorer | None = None,
        quality_threshold: float = 0.3,
        enable_llm_scoring: bool = True,
        llm_timeout: float = 4.0,
    ) -> None:
        self.mode = str(mode).strip().lower() or "semantic"
        self.logger = logging.getLogger("ace_sim.governance")
        self.quality_threshold = float(quality_threshold)
        if not (0.0 <= self.quality_threshold <= 1.0):
            raise ValueError("quality_threshold must be within [0,1]")

        self._db_path = _derive_mitigation_db_path(base_db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_tables()

        self._proposal_meta: dict[str, dict[str, Any]] = self._load_existing_meta()
        self.semantic_scorer: SemanticScorer = semantic_scorer or FastLLMHybridScorer(
            enable_llm=enable_llm_scoring,
            timeout=llm_timeout,
            fallback=RuleBasedSemanticScorer(),
        )
        self.filters: list[BaseGovernanceFilter] = self._build_filters()

        self.logger.info(
            "[MITIGATION-A-ACTIVATE] mode=%s db=%s threshold=%.2f",
            self.mode,
            str(self._db_path),
            self.quality_threshold,
        )

    @classmethod
    def from_mode(
        cls,
        *,
        base_db_path: str | Path,
        mode: str,
        semantic_scorer: SemanticScorer | None = None,
        enable_llm_scoring: bool = True,
        llm_timeout: float = 4.0,
    ) -> "GovernanceMitigationModule":
        return cls(
            base_db_path=base_db_path,
            mode=mode,
            semantic_scorer=semantic_scorer,
            enable_llm_scoring=enable_llm_scoring,
            llm_timeout=llm_timeout,
        )

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None

    def pre_check_proposal(
        self,
        *,
        proposal_data: MitigationProposalData,
        open_proposals: list[ExistingProposalState],
        max_open_proposals: int,
    ) -> MitigationDecision:
        decision = MitigationDecision()
        hydrated = [self._hydrate_existing(item) for item in open_proposals]

        for filt in self.filters:
            decision = filt.process(
                proposal=proposal_data,
                open_proposals=hydrated,
                decision=decision,
                module=self,
                max_open_proposals=max_open_proposals,
            )
            if not decision.allow:
                break

        self._write_judgment(proposal=proposal_data, decision=decision)
        return decision

    def on_proposal_accepted(
        self,
        *,
        proposal_id: str,
        proposer: str,
        proposal_text: str,
        decision: MitigationDecision,
    ) -> None:
        semantic_score = (
            float(decision.semantic_score)
            if decision.semantic_score is not None
            else None
        )
        row = {
            "proposal_id": proposal_id,
            "proposer": proposer,
            "proposal_text": proposal_text,
            "semantic_score": semantic_score,
            "priority": str(decision.priority or "NORMAL"),
            "semantic_source": str(decision.semantic_source or "unknown"),
        }
        self._proposal_meta[proposal_id] = row
        self._upsert_meta(row)

    def on_proposal_evicted(self, *, proposal_id: str, current_tick: int) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE mitigation_proposal_meta
            SET status = ?, updated_at = ?, updated_tick = ?
            WHERE proposal_id = ?
            """,
            ("REJECTED_BY_MITIGATION", now, int(current_tick), proposal_id),
        )
        self._conn.commit()
        if proposal_id in self._proposal_meta:
            self._proposal_meta[proposal_id]["status"] = "REJECTED_BY_MITIGATION"

    def resolve_existing_score(self, proposal: ExistingProposalState) -> float:
        if proposal.semantic_score is not None:
            return float(proposal.semantic_score)
        meta = self._proposal_meta.get(proposal.proposal_id)
        if meta is not None and meta.get("semantic_score") is not None:
            return float(meta["semantic_score"])
        score, _ = RuleBasedSemanticScorer().score(
            proposal_text=proposal.text,
            proposer=proposal.proposer,
            open_proposal_texts=[],
        )
        return float(score)

    def _hydrate_existing(self, proposal: ExistingProposalState) -> ExistingProposalState:
        if proposal.semantic_score is not None and proposal.priority:
            return proposal
        meta = self._proposal_meta.get(proposal.proposal_id, {})
        semantic_score = proposal.semantic_score
        if semantic_score is None and meta.get("semantic_score") is not None:
            semantic_score = float(meta["semantic_score"])
        priority = proposal.priority or "NORMAL"
        if (not priority or priority == "NORMAL") and meta.get("priority"):
            priority = str(meta["priority"])
        return ExistingProposalState(
            proposal_id=proposal.proposal_id,
            proposer=proposal.proposer,
            text=proposal.text,
            status=proposal.status,
            semantic_score=semantic_score,
            priority=priority,
        )

    def _build_filters(self) -> list[BaseGovernanceFilter]:
        if self.mode == "none":
            return []
        if self.mode == "priority":
            return [PriorityOverrideFilter(), PreemptiveSlotFilter()]
        if self.mode in {"semantic", "full"}:
            return [
                SemanticQualityFilter(threshold=self.quality_threshold),
                PriorityOverrideFilter(),
                PreemptiveSlotFilter(),
            ]
        raise ValueError(f"unsupported mitigation mode: {self.mode}")

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mitigation_judgments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                proposer TEXT NOT NULL,
                proposal_text TEXT NOT NULL,
                semantic_score REAL,
                semantic_source TEXT,
                priority TEXT NOT NULL,
                allow INTEGER NOT NULL,
                reject_reason TEXT,
                evict_proposal_id TEXT,
                tags_json TEXT,
                created_tick INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mitigation_proposal_meta (
                proposal_id TEXT PRIMARY KEY,
                proposer TEXT NOT NULL,
                proposal_text TEXT NOT NULL,
                semantic_score REAL,
                semantic_source TEXT,
                priority TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                updated_tick INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _load_existing_meta(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT proposal_id, proposer, proposal_text, semantic_score, semantic_source, priority, status
            FROM mitigation_proposal_meta
            """
        ).fetchall()
        payload: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload[str(row[0])] = {
                "proposal_id": str(row[0]),
                "proposer": str(row[1]),
                "proposal_text": str(row[2]),
                "semantic_score": float(row[3]) if row[3] is not None else None,
                "semantic_source": str(row[4]) if row[4] is not None else "unknown",
                "priority": str(row[5]) if row[5] is not None else "NORMAL",
                "status": str(row[6]) if row[6] is not None else "open",
            }
        return payload

    def _write_judgment(
        self,
        *,
        proposal: MitigationProposalData,
        decision: MitigationDecision,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO mitigation_judgments (
                proposal_id,
                proposer,
                proposal_text,
                semantic_score,
                semantic_source,
                priority,
                allow,
                reject_reason,
                evict_proposal_id,
                tags_json,
                created_tick,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.proposal_id,
                proposal.proposer,
                proposal.proposal_text,
                float(decision.semantic_score) if decision.semantic_score is not None else None,
                decision.semantic_source,
                decision.priority,
                1 if decision.allow else 0,
                decision.reject_reason,
                decision.evict_proposal_id,
                json.dumps(decision.tags, ensure_ascii=False),
                int(proposal.current_tick),
                now,
            ),
        )
        self._conn.commit()

    def _upsert_meta(self, row: dict[str, Any]) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO mitigation_proposal_meta (
                proposal_id,
                proposer,
                proposal_text,
                semantic_score,
                semantic_source,
                priority,
                status,
                updated_tick,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id)
            DO UPDATE SET
                proposer=excluded.proposer,
                proposal_text=excluded.proposal_text,
                semantic_score=excluded.semantic_score,
                semantic_source=excluded.semantic_source,
                priority=excluded.priority,
                status=excluded.status,
                updated_tick=excluded.updated_tick,
                updated_at=excluded.updated_at
            """,
            (
                row["proposal_id"],
                row["proposer"],
                row["proposal_text"],
                row["semantic_score"],
                row["semantic_source"],
                row["priority"],
                "open",
                None,
                now,
                now,
            ),
        )
        self._conn.commit()


def _derive_mitigation_db_path(base_db_path: str | Path) -> Path:
    base = Path(base_db_path).resolve()
    suffix = "".join(base.suffixes)
    stem = base.name[: -len(suffix)] if suffix else base.name
    file_name = f"{stem}.mitigation.sqlite3"
    return base.with_name(file_name)


def _extract_float(text: str) -> float:
    match = re.search(r"([0-9]*\.?[0-9]+)", str(text))
    if match is None:
        raise ValueError("cannot parse score from response")
    return float(match.group(1))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


__all__ = [
    "BaseGovernanceFilter",
    "ExistingProposalState",
    "FastLLMHybridScorer",
    "GovernanceMitigationModule",
    "MitigationDecision",
    "MitigationProposalData",
    "RuleBasedSemanticScorer",
]
