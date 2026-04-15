from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


@dataclass
class RouterConfig:
    max_concurrent: int = 5
    bucket_capacity: int = 10
    bucket_refill_rate_per_sec: float = 5.0
    max_retries: int = 3
    base_backoff_seconds: float = 0.25
    jitter_seconds: float = 0.15
    default_timeout: float = 20.0


@dataclass
class OpenAIProviderConfig:
    api_key: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    organization: str | None = None
    project: str | None = None

    def resolved_api_key(self) -> str | None:
        direct = (self.api_key or "").strip()
        if direct:
            return direct
        env_name = (self.api_key_env or "").strip()
        if env_name:
            value = (os.getenv(env_name, "") or "").strip()
            if value:
                return value
        return None


@dataclass
class RoleRouteConfig:
    backend: str
    model: str


@dataclass
class LLMConfig:
    router: RouterConfig = field(default_factory=RouterConfig)
    openai: OpenAIProviderConfig = field(default_factory=OpenAIProviderConfig)
    roles: dict[str, RoleRouteConfig] = field(default_factory=dict)
    source_path: Path | None = None


DEFAULT_ROLE_ROUTES: dict[str, RoleRouteConfig] = {
    "whale": RoleRouteConfig(backend="openai", model="gpt-4o"),
    "retail": RoleRouteConfig(backend="openai", model="gpt-4o-mini"),
    "project": RoleRouteConfig(backend="openai", model="gpt-4o-mini"),
}


def resolve_llm_config_path(config_path: str | Path | None = None) -> Path:
    if config_path is not None:
        return Path(config_path).resolve()

    env_path = (os.getenv("ACE_LLM_CONFIG_PATH", "") or "").strip()
    if env_path:
        return Path(env_path).resolve()

    repo_root = Path(__file__).resolve().parents[3]
    local_override = (repo_root / "config" / "llm_config.local.toml").resolve()
    if local_override.exists():
        return local_override
    return (repo_root / "config" / "llm_config.toml").resolve()


def load_llm_config(config_path: str | Path | None = None) -> LLMConfig:
    path = resolve_llm_config_path(config_path)
    if not path.exists():
        return LLMConfig(roles=dict(DEFAULT_ROLE_ROUTES), source_path=path)

    text = path.read_text(encoding="utf-8-sig")
    raw = tomllib.loads(text)

    router_raw = _ensure_dict(raw.get("router"))

    providers_raw = _ensure_dict(raw.get("providers"))
    openai_raw = _ensure_dict(providers_raw.get("openai"))

    roles_raw = _ensure_dict(raw.get("roles"))

    cfg = LLMConfig(
        router=RouterConfig(
            max_concurrent=_as_int(router_raw.get("max_concurrent"), 5),
            bucket_capacity=_as_int(router_raw.get("bucket_capacity"), 10),
            bucket_refill_rate_per_sec=_as_float(
                router_raw.get("bucket_refill_rate_per_sec"), 5.0
            ),
            max_retries=_as_int(router_raw.get("max_retries"), 3),
            base_backoff_seconds=_as_float(
                router_raw.get("base_backoff_seconds"), 0.25
            ),
            jitter_seconds=_as_float(router_raw.get("jitter_seconds"), 0.15),
            default_timeout=_as_float(router_raw.get("default_timeout"), 20.0),
        ),
        openai=OpenAIProviderConfig(
            api_key=_as_optional_str(openai_raw.get("api_key")),
            api_key_env=_as_str(openai_raw.get("api_key_env"), "OPENAI_API_KEY"),
            base_url=_as_optional_str(openai_raw.get("base_url")),
            organization=_as_optional_str(openai_raw.get("organization")),
            project=_as_optional_str(openai_raw.get("project")),
        ),
        roles=dict(DEFAULT_ROLE_ROUTES),
        source_path=path,
    )

    for role_name, role_payload in roles_raw.items():
        role_key = str(role_name).strip().lower()
        payload = _ensure_dict(role_payload)
        backend = _as_optional_str(payload.get("backend"))
        model = _as_optional_str(payload.get("model"))
        if not role_key or not backend or not model:
            continue
        cfg.roles[role_key] = RoleRouteConfig(backend=backend, model=model)

    return cfg


def resolve_role_route(
    role: str,
    *,
    default_backend: str,
    default_model: str,
    config_path: str | Path | None = None,
) -> tuple[str, str]:
    cfg = load_llm_config(config_path=config_path)
    role_key = str(role).strip().lower()
    route = cfg.roles.get(role_key)
    if route is None:
        return default_backend, default_model
    return route.backend, route.model


def _ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return int(default)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return float(default)


def _as_str(value: Any, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "LLMConfig",
    "RouterConfig",
    "OpenAIProviderConfig",
    "RoleRouteConfig",
    "DEFAULT_ROLE_ROUTES",
    "resolve_llm_config_path",
    "load_llm_config",
    "resolve_role_route",
]
