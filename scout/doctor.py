from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import ENV_PATH, ROOT, Settings, load_env_file, settings_from_env


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return value[0] + "…"
    return value[:4] + "…" + value[-4:]


def _http_json(url: str, headers: dict[str, str], timeout: int = 12) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:200]
    except Exception as exc:
        return 0, str(exc)


def checks(settings: Settings) -> list[tuple[bool, str, str]]:
    load_env_file()
    rows: list[tuple[bool, str, str]] = []

    py_ok = sys.version_info >= (3, 11)
    rows.append((py_ok, "python", ".".join(map(str, sys.version_info[:3]))))

    try:
        import openai  # noqa: F401

        rows.append((True, "openai", "installed"))
    except ImportError:
        rows.append((False, "openai", "run ./setup.sh"))

    try:
        import polymarket  # noqa: F401

        rows.append((True, "polymarket-client", "installed"))
    except ImportError:
        rows.append((False, "polymarket-client", "pip install polymarket-client"))

    rows.append((ENV_PATH.exists(), ".env", str(ENV_PATH) if ENV_PATH.exists() else "missing — ./setup.sh copies it"))

    key = os.getenv("XAI_API_KEY", "")
    rows.append((bool(key), "XAI_API_KEY", mask(key) if key else "paste into .env from https://console.x.ai"))

    status, payload = _http_json(
        settings.gamma_markets + "?closed=false&limit=1",
        {"User-Agent": settings.user_agent, "Accept": "application/json"},
    )
    gamma_ok = status == 200 and isinstance(payload, dict) and "markets" in payload
    rows.append((gamma_ok, "polymarket gamma", f"HTTP {status}"))

    if key:
        status, payload = _http_json(
            settings.xai_base_url.rstrip("/") + "/models",
            {"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        names = []
        if isinstance(payload, dict):
            names = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
        grok_ok = status == 200 and any(n and "grok" in str(n) for n in names)
        rows.append((grok_ok, "xAI API", f"HTTP {status} grok-4.6={'grok-4.6' in names}"))
    else:
        rows.append((False, "xAI API", "skipped — no key"))

    relayer_ok = bool(settings.relayer_api_key and settings.relayer_api_key_address)
    rows.append(
        (
            relayer_ok if settings.live else True,
            "relayer key",
            f"key={mask(settings.relayer_api_key)} addr={mask(settings.relayer_api_key_address)}"
            if relayer_ok
            else "missing RELAYER_API_KEY + RELAYER_API_KEY_ADDRESS",
        )
    )

    if settings.live:
        live_ok = bool(settings.private_key and settings.funder and relayer_ok)
        rows.append(
            (
                live_ok,
                "LIVE keys",
                f"key={mask(settings.private_key)} funder={mask(settings.funder)} sig={settings.signature_type}",
            )
        )
        if live_ok:
            try:
                from .execute import probe_live

                info = probe_live(settings)
                cash = info["collateral_pUSD"]
                rows.append(
                    (
                        True,
                        "live CLOB",
                        f"wallet={mask(info['wallet'])} type={info['wallet_type']} "
                        f"pUSD={cash:.2f} signer_ok={info['signer_matches_relayer']}",
                    )
                )
            except Exception as exc:
                rows.append((False, "live CLOB", f"{type(exc).__name__}: {exc}"[:180]))
    else:
        rows.append((True, "LIVE", "off (paper)"))

    return rows


def next_step(rows: list[tuple[bool, str, str]]) -> str:
    by_name = {name: ok for ok, name, _ in rows}
    if not by_name.get(".env", False):
        return "run ./setup.sh"
    if not by_name.get("XAI_API_KEY", False) or not by_name.get("xAI API", False):
        return "put $20–50 of credit on https://console.x.ai, paste XAI_API_KEY into .env, re-run: python -m scout doctor"
    if not by_name.get("polymarket gamma", False):
        return "Gamma is down or blocked. retry. if you are on a VPN, switch exit."
    if by_name.get("LIVE keys") is False:
        return "LIVE=1 but keys missing. fill POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER + Relayer API key, or set LIVE=0"
    if by_name.get("live CLOB") is False:
        return "Relayer/CLOB auth failed. re-run: .venv/bin/python -m scout doctor"
    if by_name.get("LIVE keys"):
        return "live auth is green: .venv/bin/python -m scout once"
    return "paper is ready: .venv/bin/python -m scout loop"


def print_report(rows: list[tuple[bool, str, str]]) -> int:
    print("Scout doctor")
    print("root:", ROOT)
    failed = 0
    for ok, name, detail in rows:
        mark = "ok " if ok else "NO "
        if not ok:
            failed += 1
        print(f"  {mark} {name:18} {detail}")
    print()
    print("next:", next_step(rows))
    return 1 if failed else 0
