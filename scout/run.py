from __future__ import annotations

import argparse
import json
import os

from .agent import cycle, print_cycle, run_loop
from .config import JOURNAL_PATH, SCAN_PATH, settings_from_env
from .doctor import checks, print_report
from .ledger import load_ledger, mark_to_market
from .performance import summarize_performance


def _want_grok(args: argparse.Namespace, *, live: bool) -> bool:
    if getattr(args, "no_grok", False):
        return False
    return True


def cmd_scan(args: argparse.Namespace) -> int:
    os.environ["LIVE"] = "0"
    settings = settings_from_env()
    report = cycle(settings, use_grok=_want_grok(args, live=False))
    print_cycle(report)
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    os.environ["LIVE"] = "1" if args.live else "0"
    settings = settings_from_env()
    if args.live and not settings.live:
        raise SystemExit("pass --live and set LIVE=1 in .env")
    use_grok = _want_grok(args, live=settings.live)
    if settings.live:
        if not settings.private_key or not settings.funder:
            raise SystemExit("LIVE needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env")
        if not settings.relayer_api_key or not settings.relayer_api_key_address:
            raise SystemExit("LIVE needs RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS in .env")
        print("LIVE MODE. real USDC orders. Ctrl-C to stop.")
        print(
            "xAI/Grok:",
            "ON (--grok)"
            if use_grok
            else "OFF (--no-grok)",
        )
    else:
        print("paper mode. loop every", settings.loop_seconds, "s")
    run_loop(settings, once=False, use_grok=use_grok)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    os.environ["LIVE"] = "1" if args.live else "0"
    settings = settings_from_env()
    if settings.live and (not settings.private_key or not settings.funder):
        raise SystemExit("LIVE needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env")
    if settings.live and (not settings.relayer_api_key or not settings.relayer_api_key_address):
        raise SystemExit("LIVE needs RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS in .env")
    use_grok = _want_grok(args, live=settings.live)
    if settings.live:
        print(
            "xAI/Grok:",
            "ON (--grok)"
            if use_grok
            else "OFF (--no-grok)",
        )
    run_loop(settings, once=True, use_grok=use_grok)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = settings_from_env()
    return print_report(checks(settings))


def cmd_status(_: argparse.Namespace) -> int:
    settings = settings_from_env()
    ledger = load_ledger(settings)
    by_id = {}
    if SCAN_PATH.exists():
        last = json.loads(SCAN_PATH.read_text())
        for row in last.get("candidates", []) + last.get("universe_sample", []):
            by_id[row["id"]] = row
    equity = mark_to_market(ledger, by_id)
    print(json.dumps({**ledger, "equity_mark": equity}, indent=2))
    return 0


def cmd_performance(_: argparse.Namespace) -> int:
    settings = settings_from_env()
    ledger = load_ledger(settings)
    events = []
    if JOURNAL_PATH.exists():
        for line in JOURNAL_PATH.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(json.dumps(summarize_performance(ledger, events), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Grok 4.6 Polymarket agent. Paper unless LIVE=1.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="One cycle, always paper")
    p_scan.add_argument("--no-grok", action="store_true")
    p_scan.add_argument("--grok", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_once = sub.add_parser("once", help="One cycle, execute fills (paper unless --live)")
    p_once.add_argument("--no-grok", action="store_true")
    p_once.add_argument("--grok", action="store_true", help="No-op; Grok is on unless --no-grok")
    p_once.add_argument("--live", action="store_true")
    p_once.set_defaults(func=cmd_once)

    p_loop = sub.add_parser("loop", help="Tight loop. Paper unless --live")
    p_loop.add_argument("--no-grok", action="store_true")
    p_loop.add_argument("--grok", action="store_true", help="No-op; Grok is on unless --no-grok")
    p_loop.add_argument("--live", action="store_true")
    p_loop.set_defaults(func=cmd_loop)

    p_status = sub.add_parser("status", help="Show ledger")
    p_status.set_defaults(func=cmd_status)

    p_perf = sub.add_parser("performance", help="Realized strategy, fee, latency, and calibration report")
    p_perf.set_defaults(func=cmd_performance)

    p_doc = sub.add_parser("doctor", help="5-minute infra check")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)
