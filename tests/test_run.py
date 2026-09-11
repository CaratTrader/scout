from argparse import Namespace

from scout.config import Settings
import scout.run as run_module


def test_loop_without_live_flag_forces_paper(monkeypatch):
    seen = []
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setattr(
        run_module,
        "settings_from_env",
        lambda: Settings(live=run_module.os.environ.get("LIVE") == "1"),
    )
    monkeypatch.setattr(
        run_module,
        "run_loop",
        lambda settings, **kwargs: seen.append(settings.live),
    )
    assert run_module.cmd_loop(Namespace(live=False, no_grok=True)) == 0
    assert seen == [False]


def test_once_live_flag_is_explicit_gate(monkeypatch):
    seen = []
    monkeypatch.setenv("LIVE", "0")
    monkeypatch.setattr(
        run_module,
        "settings_from_env",
        lambda: Settings(
            live=run_module.os.environ.get("LIVE") == "1",
            private_key="key",
            funder="wallet",
            relayer_api_key="relay",
            relayer_api_key_address="address",
        ),
    )
    monkeypatch.setattr(
        run_module,
        "run_loop",
        lambda settings, **kwargs: seen.append(settings.live),
    )
    assert run_module.cmd_once(Namespace(live=True, no_grok=True)) == 0
    assert seen == [True]
