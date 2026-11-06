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
