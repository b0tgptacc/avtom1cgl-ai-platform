import importlib.util
import json
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("stand", Path(__file__).parents[1] / "stand.py")
stand = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(stand)


def test_hermes_profile_has_only_required_document_tools():
    config = stand.hermes_config("agent-key")

    assert "api_server: [terminal, file, code_execution, skills, todo]" in config
    assert "session_search" not in config
    assert "delegation" not in config
    assert "TASK PROFILE" in stand.INSTRUCTIONS


def test_openclaw_profile_uses_coding_tool_baseline():
    config = json.loads(stand.openclaw_config("agent-key", "gateway-token"))

    assert config["tools"]["profile"] == "coding"
    assert config["tools"]["exec"]["timeoutSeconds"] == 3600


def test_agent_compose_exposes_only_router_quality_tool_bridge():
    users = {"ivanov@corp.local": {"id": "0123456789abcdef01234567", "slug": "ivanov", "dept": "legal",
                                    "agent_key": "ak-ivanov", "engines": {"hermes": {"key": "hk"}, "openclaw": {"key": "ok"}}}}
    env = {"AGENTS_ROUTER_KEY": "rk", "LITELLM_MASTER_KEY": "master", "LIBRECHAT_JWT_REFRESH_SECRET": "jwt"}

    rendered = stand.compose(users, env, ("hermes:test", "openclaw:test"))

    assert rendered.count("QUALITY_TOOLS_BASE: \"http://agents-router:8080/quality/v1\"") == 2
    assert rendered.count("QUALITY_TOOLS_API_KEY: \"ak-ivanov\"") == 2
    assert "quality-tools:8000" not in rendered
