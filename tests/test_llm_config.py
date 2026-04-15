from __future__ import annotations

from pathlib import Path

from ace_sim.agents.agent_profile import default_agent_profile
from ace_sim.cognition.llm_router import LLMRouter
from ace_sim.config.llm_config import load_llm_config, resolve_role_route


def test_load_llm_config_and_role_route_from_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "llm_config.toml"
    cfg_path.write_text(
        """
[router]
max_concurrent = 7
bucket_capacity = 21

[providers.openai]
api_key_env = "CUSTOM_OPENAI_KEY"
base_url = "https://proxy.example.com/v1"

[roles.whale]
backend = "openai"
model = "gpt-4.1"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("ACE_LLM_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("CUSTOM_OPENAI_KEY", "sk-test-123")

    cfg = load_llm_config()

    assert cfg.router.max_concurrent == 7
    assert cfg.router.bucket_capacity == 21
    assert cfg.openai.base_url == "https://proxy.example.com/v1"
    assert cfg.openai.resolved_api_key() == "sk-test-123"

    backend, model = resolve_role_route(
        "whale",
        default_backend="openai",
        default_model="gpt-4o",
    )
    assert backend == "openai"
    assert model == "gpt-4.1"


def test_default_agent_profile_picks_model_from_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "llm_config.toml"
    cfg_path.write_text(
        """
[roles.retail]
backend = "local"
model = "llama3-8b"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("ACE_LLM_CONFIG_PATH", str(cfg_path))

    profile = default_agent_profile("retail_1", "retail")
    assert profile.llm_backend == "local"
    assert profile.llm_model == "llama3-8b"


def test_llm_router_reads_openai_proxy_config(tmp_path):
    cfg_path = tmp_path / "llm_config.toml"
    cfg_path.write_text(
        """
[providers.openai]
api_key = "sk-inline"
base_url = "https://gateway.example.org/v1"
organization = "org-test"
project = "proj-test"
""".strip(),
        encoding="utf-8",
    )

    router = LLMRouter(config_path=cfg_path)
    adapter = router._adapters["openai"]

    assert adapter.api_key == "sk-inline"
    assert adapter.base_url == "https://gateway.example.org/v1"
    assert adapter.organization == "org-test"
    assert adapter.project == "proj-test"
