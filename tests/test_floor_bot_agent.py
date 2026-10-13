from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

BOT_PATH = Path(__file__).resolve().parents[1] / "floor-bot" / "bot.py"


def load_bot():
    spec = importlib.util.spec_from_file_location("floor_bot_module", BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture_function(name: str):
    """Compile one top-level function from the capture script the bot runs in its subprocess."""
    tree = ast.parse(BOT_PATH.read_text(encoding="utf-8"))
    script = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and f"def {name}(" in node.value
    )
    function = next(node for node in ast.parse(script).body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace: dict = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(BOT_PATH), "exec"), namespace)  # noqa: S102
    return namespace[name]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("GET /http-bind?room=x&token=abc.def&jwt=eyJ123", "GET /http-bind?room=x&token=[redacted]&jwt=[redacted]"),
        ('{"jwt":"eyJabc","room":"x"}', '{"jwt":"[redacted]","room":"x"}'),
        ("{'access_token': 'tok'}", "{'access_token': '[redacted]'}"),
        ("token: s3cret next", "token: [redacted] next"),
        ("password = hunter2;", "password = [redacted];"),
        ("tokenizer=fine jwtx=fine", "tokenizer=fine jwtx=fine"),
    ],
)
def test_log_redaction_covers_query_json_and_prose_forms(line, expected):
    bot = load_bot()
    assert bot.SECRET_VALUE.sub(r"\1[redacted]", line) == expected


@pytest.mark.parametrize(
    ("requested", "final", "allowed"),
    [
        ("https://meet.example.org/TrackA", "https://meet.example.org/TrackA", True),
        ("https://meet.example.org/TrackA", "https://MEET.example.org/tracka/", True),
        ("http://meet.example.org:8080/TrackA", "https://meet.example.org:8443/TrackA", True),
        ("https://meet.example.org/TrackA", "https://meet.example.org/TrackB", False),
        ("https://meet.example.org/TrackA", "https://other.example.org/TrackA", False),
        ("https://meet.example.org/TrackA", "https://meet.example.org:8443/TrackA", False),
        ("https://meet.example.org/TrackA", "http://meet.example.org/TrackA", False),
    ],
)
def test_redirect_rule_keeps_the_requested_room_and_credentials_safe(requested, final, allowed):
    same_meeting = capture_function("same_meeting")
    assert same_meeting(requested, final) is allowed
