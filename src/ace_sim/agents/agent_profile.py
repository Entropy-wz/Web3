from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math

from ..config.llm_config import resolve_role_route


@dataclass
class AttentionPolicy:
    price_change_threshold: Decimal = Decimal("0.01")
    risk_wake_threshold: Decimal = Decimal("0.70")
    force_wake_interval: int = 12
    memory_top_k: int = 6


@dataclass
class AgentProfile:
    agent_id: str
    role: str
    llm_backend: str
    llm_model: str
    risk_threshold: Decimal
    persona_type: str = "default"
    hidden_goals: list[str] = field(default_factory=list)
    strategy_prompt: str = ""
    social_policy: str = ""
    governance_policy: str = ""
    attention_policy: AttentionPolicy = field(default_factory=AttentionPolicy)


def default_agent_profile(agent_id: str, role: str) -> AgentProfile:
    role_norm = role.strip().lower()
    if role_norm == "whale":
        backend, model = resolve_role_route(
            role="whale",
            default_backend="openai",
            default_model="gpt-4o",
        )
        return AgentProfile(
            agent_id=agent_id,
            role="whale",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.65"),
            persona_type="whale_opportunist",
            hidden_goals=[
                "maximize pnl while minimizing visible slippage",
                "front-run weak liquidity windows",
            ],
            strategy_prompt=(
                "You are an opportunistic whale. Attack thin liquidity and capture "
                "risk-free windows before others react."
            ),
            social_policy="Use strategic FUD when it improves execution quality.",
            governance_policy=(
                "Track LUNA voting power concentration and defend profitable "
                "exit/arbitrage paths."
            ),
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.008"),
                risk_wake_threshold=Decimal("0.60"),
                force_wake_interval=8,
                memory_top_k=8,
            ),
        )
    if role_norm == "project":
        backend, model = resolve_role_route(
            role="project",
            default_backend="openai",
            default_model="gpt-4o-mini",
        )
        return AgentProfile(
            agent_id=agent_id,
            role="project",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.55"),
            persona_type="project_defender",
            hidden_goals=[
                "reduce panic spread",
                "defend peg confidence narrative",
            ],
            strategy_prompt=(
                "Act as protocol defender: balance peg support costs against "
                "system-level survivability."
            ),
            social_policy="Publicly reassure even under stress to slow panic contagion.",
            governance_policy=(
                "Use governance to tune minting/swap parameters for crisis containment."
            ),
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.009"),
                risk_wake_threshold=Decimal("0.55"),
                force_wake_interval=10,
                memory_top_k=7,
            ),
        )
    backend, model = resolve_role_route(
        role="retail",
        default_backend="openai",
        default_model="gpt-4o-mini",
    )
    return AgentProfile(
        agent_id=agent_id,
        role="retail",
        llm_backend=backend,
        llm_model=model,
        risk_threshold=Decimal("0.75"),
        persona_type="retail_follower",
        hidden_goals=[
            "avoid large drawdowns",
            "follow strong social signals quickly",
        ],
        strategy_prompt=(
            "You are a retail participant with bounded rationality. React fast to "
            "clear social and price cues."
        ),
        social_policy="Mirror strong narratives from whales/project accounts.",
        governance_policy=(
            "Coordinate with other retailers to propose friction controls when panic rises."
        ),
        attention_policy=AttentionPolicy(
            price_change_threshold=Decimal("0.01"),
            risk_wake_threshold=Decimal("0.75"),
            force_wake_interval=15,
            memory_top_k=5,
        ),
    )


@dataclass
class AgentBootstrap:
    agent_id: str
    role: str
    community_id: str
    initial_ust: Decimal
    initial_luna: Decimal
    initial_usdc: Decimal
    profile: AgentProfile

    def initial_balances(self) -> dict[str, Decimal]:
        return {
            "UST": Decimal(self.initial_ust),
            "LUNA": Decimal(self.initial_luna),
            "USDC": Decimal(self.initial_usdc),
        }


def build_luna_crash_bootstrap(
    *,
    retail_count: int = 21,
) -> list[AgentBootstrap]:
    if retail_count < 21 or retail_count > 27:
        raise ValueError("retail_count must be in [21, 27] for the 24-30 cohort setup")

    panic_count, follower_count, lunatic_count = _split_retail_442(retail_count)

    bootstrap: list[AgentBootstrap] = [
        _build_project_bootstrap(),
        _build_whale_bootstrap("whale_0", community_id="c1", whale_type="opportunist_a"),
        _build_whale_bootstrap("whale_1", community_id="c1", whale_type="opportunist_b"),
    ]

    for idx in range(panic_count):
        bootstrap.append(
            _build_retail_bootstrap(
                agent_id=f"retail_panic_{idx:02d}",
                community_id="c0",
                subtype="panic",
            )
        )
    for idx in range(follower_count):
        bootstrap.append(
            _build_retail_bootstrap(
                agent_id=f"retail_follower_{idx:02d}",
                community_id="c0",
                subtype="follower",
            )
        )
    for idx in range(lunatic_count):
        bootstrap.append(
            _build_retail_bootstrap(
                agent_id=f"retail_lunatic_{idx:02d}",
                community_id="c2",
                subtype="lunatic",
            )
        )
    return bootstrap


def default_black_swan_tick0_actions() -> list[dict[str, object]]:
    return [
        {
            "agent_id": "whale_0",
            "kind": "transaction",
            "action_type": "SWAP",
            "params": {
                "pool_name": "Pool_A",
                "token_in": "UST",
                "amount": "30000000",
                "slippage_tolerance": "0.50",
            },
            "gas_price": "999",
        },
        {
            "agent_id": "whale_0",
            "kind": "event",
            "action_type": "SPEAK",
            "params": {
                "target": "forum",
                "message": "UST is a Ponzi, it's over. Getting out now.",
                "mode": "new",
            },
        },
    ]


def _split_retail_442(retail_count: int) -> tuple[int, int, int]:
    panic = int(math.floor(retail_count * 0.4))
    follower = int(math.floor(retail_count * 0.4))
    lunatic = retail_count - panic - follower
    return panic, follower, lunatic


def _build_project_bootstrap() -> AgentBootstrap:
    backend, model = "openai", "gpt-4o"
    profile = AgentProfile(
        agent_id="project_0",
        role="project",
        llm_backend=backend,
        llm_model=model,
        risk_threshold=Decimal("0.58"),
        persona_type="project_defender",
        hidden_goals=[
            "maintain UST peg near 1.00 with treasury support",
            "if peg rescue fails, prioritize preventing protocol-wide LUNA collapse",
            "shape expectations to slow retail panic cascade",
        ],
        strategy_prompt=(
            "Defend the system: estimate reserve burn for peg defense; if rescue is "
            "unwinnable, pivot to preserving protocol survivability."
        ),
        social_policy=(
            "Always publish reassuring communication. Even when internal state is "
            "stressed, external tone stays confident."
        ),
        governance_policy=(
            "In early stress, support raising daily_mint_cap to absorb sell pressure; "
            "in late-stage death spiral, consider stop-minting to cap hyperinflation."
        ),
        attention_policy=AttentionPolicy(
            price_change_threshold=Decimal("0.0075"),
            risk_wake_threshold=Decimal("0.50"),
            force_wake_interval=3,
            memory_top_k=10,
        ),
    )
    return AgentBootstrap(
        agent_id="project_0",
        role="project",
        community_id="c2",
        initial_ust=Decimal("10000000"),
        initial_luna=Decimal("10000000"),
        initial_usdc=Decimal("100000000"),
        profile=profile,
    )


def _build_whale_bootstrap(
    agent_id: str,
    *,
    community_id: str,
    whale_type: str,
) -> AgentBootstrap:
    backend, model = "openai", "gpt-4o"
    if whale_type == "opportunist_b":
        goals = [
            "extract arbitrage whenever spread exceeds 2%",
            "cycle between UST<->LUNA and LUNA/USDC exits while liquidity permits",
            "retain optionality to accumulate governance influence via cheap LUNA",
        ]
        strategy_prompt = (
            "You are a rational arbitrage whale. If spread >= 2%, aggressively run "
            "mint-burn + spot exits."
        )
        initial_ust = Decimal("20000000")
        initial_usdc = Decimal("50000000")
    else:
        goals = [
            "maximize extraction before systemic collapse",
            "trigger panic at thin-liquidity moments for better exits",
            "exit quickly once contagion starts accelerating",
        ]
        strategy_prompt = (
            "You are a predatory whale. Strike when Pool_A depth is vulnerable and "
            "amplify reflexive panic."
        )
        initial_ust = Decimal("50000000")
        initial_usdc = Decimal("20000000")

    profile = AgentProfile(
        agent_id=agent_id,
        role="whale",
        llm_backend=backend,
        llm_model=model,
        risk_threshold=Decimal("0.72"),
        persona_type=f"whale_{whale_type}",
        hidden_goals=goals,
        strategy_prompt=strategy_prompt,
        social_policy=(
            "Release selective FUD before large sells, e.g. hints that system liquidity "
            "is unsafe."
        ),
        governance_policy=(
            "Track LUNA voting power. If holdings exceed 10% of total supply, behave as "
            "rule-maker. Oppose stop-minting proposals that block arbitrage exits."
        ),
        attention_policy=AttentionPolicy(
            price_change_threshold=Decimal("0.006"),
            risk_wake_threshold=Decimal("0.58"),
            force_wake_interval=2,
            memory_top_k=10,
        ),
    )
    return AgentBootstrap(
        agent_id=agent_id,
        role="whale",
        community_id=community_id,
        initial_ust=initial_ust,
        initial_luna=Decimal("0"),
        initial_usdc=initial_usdc,
        profile=profile,
    )


def _build_retail_bootstrap(
    *,
    agent_id: str,
    community_id: str,
    subtype: str,
) -> AgentBootstrap:
    backend, model = "openai", "gpt-4o-mini"

    subtype_norm = subtype.strip().lower()
    if subtype_norm == "panic":
        profile = AgentProfile(
            agent_id=agent_id,
            role="retail",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.32"),
            persona_type="retail_panic_prone",
            hidden_goals=[
                "preserve principal at all cost",
                "exit immediately on depeg or rumor acceleration",
            ],
            strategy_prompt=(
                "Extremely risk-averse: if UST drops below 0.99 or rumor count spikes, "
                "sell quickly with high slippage tolerance."
            ),
            social_policy="Ask for safety confirmations; spread fear when unsure.",
            governance_policy=(
                "Support proposals that reduce inflation speed and increase market friction."
            ),
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.004"),
                risk_wake_threshold=Decimal("0.25"),
                force_wake_interval=1,
                memory_top_k=4,
            ),
        )
        return AgentBootstrap(
            agent_id=agent_id,
            role="retail",
            community_id=community_id,
            initial_ust=Decimal("10000"),
            initial_luna=Decimal("0"),
            initial_usdc=Decimal("0"),
            profile=profile,
        )

    if subtype_norm == "lunatic":
        profile = AgentProfile(
            agent_id=agent_id,
            role="retail",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.90"),
            persona_type="retail_lunatic",
            hidden_goals=[
                "defend LUNA narrative against FUD",
                "buy dips repeatedly unless catastrophic threshold is crossed",
            ],
            strategy_prompt=(
                "Community believer: buy-the-dip until either price < 0.5 anchor or "
                "portfolio drawdown exceeds 90%."
            ),
            social_policy="Counter FUD and encourage collective holding behavior.",
            governance_policy=(
                "Coordinate retailers to propose higher swap_fee or lower daily_mint_cap."
            ),
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.02"),
                risk_wake_threshold=Decimal("0.85"),
                force_wake_interval=4,
                memory_top_k=6,
            ),
        )
        return AgentBootstrap(
            agent_id=agent_id,
            role="retail",
            community_id=community_id,
            initial_ust=Decimal("5000"),
            initial_luna=Decimal("3000"),
            initial_usdc=Decimal("2000"),
            profile=profile,
        )

    profile = AgentProfile(
        agent_id=agent_id,
        role="retail",
        llm_backend=backend,
        llm_model=model,
        risk_threshold=Decimal("0.62"),
        persona_type="retail_follower",
        hidden_goals=[
            "follow strong social signals from whales/project",
            "avoid being the last holder in a bank-run scenario",
        ],
        strategy_prompt=(
            "Herd follower: sell when whale-collapse narrative dominates; buy only when "
            "project reassurance remains credible."
        ),
        social_policy="Echo dominant narratives from high-visibility accounts.",
        governance_policy=(
            "Join coalition proposals that curb arbitrage extraction costs."
        ),
        attention_policy=AttentionPolicy(
            price_change_threshold=Decimal("0.01"),
            risk_wake_threshold=Decimal("0.55"),
            force_wake_interval=2,
            memory_top_k=5,
        ),
    )
    return AgentBootstrap(
        agent_id=agent_id,
        role="retail",
        community_id=community_id,
        initial_ust=Decimal("8000"),
        initial_luna=Decimal("1000"),
        initial_usdc=Decimal("1000"),
        profile=profile,
    )


__all__ = [
    "AgentProfile",
    "AttentionPolicy",
    "AgentBootstrap",
    "default_agent_profile",
    "build_luna_crash_bootstrap",
    "default_black_swan_tick0_actions",
]
