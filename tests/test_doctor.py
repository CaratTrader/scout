from types import SimpleNamespace

from scout.doctor import mask, next_step
from scout.run import _want_grok


def test_mask_does_not_leak():
    assert mask("") == "(empty)"
    assert mask("abcdefghijklmnop") == "abcd…mnop"
    assert "efghijkl" not in mask("abcdefghijklmnop")


def test_next_step_key_then_loop():
    missing_env = [(False, ".env", "")]
    assert next_step(missing_env) == "run ./setup.sh"
    ready = [
        (True, ".env", ""),
        (True, "XAI_API_KEY", ""),
        (True, "xAI API", ""),
        (True, "polymarket gamma", ""),
        (True, "LIVE", ""),
    ]
    assert "scout loop" in next_step(ready)


def test_live_grok_is_on_unless_no_grok():
    assert _want_grok(SimpleNamespace(no_grok=False, grok=False), live=True) is True
    assert _want_grok(SimpleNamespace(no_grok=False, grok=True), live=True) is True
    assert _want_grok(SimpleNamespace(no_grok=True, grok=True), live=True) is False
    assert _want_grok(SimpleNamespace(no_grok=False, grok=False), live=False) is True


def test_next_step_live_auth_failed():
    rows = [
        (True, ".env", ""),
        (True, "XAI_API_KEY", ""),
        (True, "xAI API", ""),
        (True, "polymarket gamma", ""),
        (True, "LIVE keys", ""),
        (False, "live CLOB", "400"),
    ]
    assert "auth failed" in next_step(rows)
