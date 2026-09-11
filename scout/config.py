from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LEDGER_PATH = DATA_DIR / "ledger.json"
LIVE_LEDGER_PATH = DATA_DIR / "ledger_live.json"
SCAN_PATH = DATA_DIR / "last_scan.json"
JOURNAL_PATH = DATA_DIR / "journal.jsonl"
GROK_CACHE_PATH = DATA_DIR / "grok_cache.json"
GROK_API_LOCK = DATA_DIR / "grok_api_lock.json"
ENV_PATH = ROOT / ".env"


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class Settings:
    starting_bankroll: float = 50.0
    grok_min_edge: float = 0.07
    completeness_min_edge: float = 0.03
    min_confidence: float = 0.52
    max_fraction: float = 0.40
    kelly_mult: float = 0.60
    max_stake_usd: float = 25.0
    max_daily_loss: float = 22.0
    min_entry_price: float = 0.03
    max_entry_price: float = 0.97
    grok_max_volume_24h: float = 50_000_000.0
    grok_max_days: float = 1.5
    crypto_min_edge: float = 0.03
    taker_fee_rate: float = 0.07
    min_liquidity: float = 5_000.0
    min_volume_24h: float = 500.0
    max_spread: float = 0.10
    max_positions: int = 3
    max_crypto_positions: int = 2
    min_crypto_stake: float = 2.0
    max_crypto_stake: float = 20.0
    crypto_bankroll_frac: float = 0.40
    crypto_kelly_mult: float = 0.50
    crypto_model_weight: float = 0.75
    crypto_max_slip: float = 0.02
    crypto_signal_ttl_seconds: float = 3.0
    crypto_min_seconds_to_expiry: float = 30.0
    crypto_max_price_improvement: float = 0.10
    max_grok_stake: float = 4.0
    grok_bankroll_frac: float = 0.05
    max_session_drawdown: float = 12.0
    campaign_target_usd: float = 100.0
    profit_lock_fraction: float = 0.50
    halt_bankroll: float = 18.0
    max_consecutive_crypto_losses: int = 2
    max_drawdown: float = 0.55
    min_trade: float = 1.0
    scan_limit: int = 400
    grok_top: int = 4
    grok_model: str = "grok-4.6"
    loop_seconds: int = 20
    grok_every_seconds: int = 180
    stop_loss_pct: float = 0.35
    take_profit_price: float = 0.95
    fast_dump_pct: float = 0.10
    fast_dump_seconds: int = 90
    grok_max_hold_seconds: int = 2400
    grok_min_sources: int = 2
    live: bool = False
    user_agent: str = "scout-agent/0.2"
    gamma_markets: str = "https://gamma-api.polymarket.com/markets/keyset"
    xai_base_url: str = "https://api.x.ai/v1"
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    signature_type: int = 3
    private_key: str = ""
    funder: str = ""
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_passphrase: str = ""
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""
    dip_threshold: float = 0.08
    dip_window_ms: int = 3000
    pair_sum_target: float = 0.99
    tape_seconds: float = 12.0
    tape_interval: float = 0.35
    min_book_shares: float = 5.0


def settings_from_env() -> Settings:
    load_env_file()
    base = Settings()
    return Settings(
        starting_bankroll=_f("STARTING_BANKROLL", base.starting_bankroll),
        grok_min_edge=_f("GROK_MIN_EDGE", base.grok_min_edge),
        completeness_min_edge=_f("COMPLETENESS_MIN_EDGE", base.completeness_min_edge),
        min_confidence=_f("MIN_CONFIDENCE", base.min_confidence),
        max_fraction=_f("MAX_FRACTION", base.max_fraction),
        kelly_mult=_f("KELLY_MULT", base.kelly_mult),
        max_stake_usd=_f("MAX_STAKE_USD", base.max_stake_usd),
        max_daily_loss=_f("MAX_DAILY_LOSS", base.max_daily_loss),
        min_entry_price=_f("MIN_ENTRY_PRICE", base.min_entry_price),
        max_entry_price=_f("MAX_ENTRY_PRICE", base.max_entry_price),
        grok_max_volume_24h=_f("GROK_MAX_VOLUME_24H", base.grok_max_volume_24h),
        grok_max_days=_f("GROK_MAX_DAYS", base.grok_max_days),
        crypto_min_edge=_f("CRYPTO_MIN_EDGE", base.crypto_min_edge),
        taker_fee_rate=_f("TAKER_FEE_RATE", base.taker_fee_rate),
        min_liquidity=_f("MIN_LIQUIDITY", base.min_liquidity),
        min_volume_24h=_f("MIN_VOLUME_24H", base.min_volume_24h),
        max_spread=_f("MAX_SPREAD", base.max_spread),
        max_positions=_i("MAX_POSITIONS", base.max_positions),
        max_crypto_positions=_i("MAX_CRYPTO_POSITIONS", base.max_crypto_positions),
        min_crypto_stake=_f("MIN_CRYPTO_STAKE", base.min_crypto_stake),
        max_crypto_stake=_f("MAX_CRYPTO_STAKE", base.max_crypto_stake),
        crypto_bankroll_frac=_f("CRYPTO_BANKROLL_FRAC", base.crypto_bankroll_frac),
        crypto_kelly_mult=_f("CRYPTO_KELLY_MULT", base.crypto_kelly_mult),
        crypto_model_weight=_f("CRYPTO_MODEL_WEIGHT", base.crypto_model_weight),
        crypto_max_slip=_f("CRYPTO_MAX_SLIP", base.crypto_max_slip),
        crypto_signal_ttl_seconds=_f(
            "CRYPTO_SIGNAL_TTL_SECONDS", base.crypto_signal_ttl_seconds
        ),
        crypto_min_seconds_to_expiry=_f(
            "CRYPTO_MIN_SECONDS_TO_EXPIRY", base.crypto_min_seconds_to_expiry
        ),
        crypto_max_price_improvement=_f(
            "CRYPTO_MAX_PRICE_IMPROVEMENT", base.crypto_max_price_improvement
        ),
        max_grok_stake=_f("MAX_GROK_STAKE", base.max_grok_stake),
        grok_bankroll_frac=_f("GROK_BANKROLL_FRAC", base.grok_bankroll_frac),
        max_session_drawdown=_f("MAX_SESSION_DRAWDOWN", base.max_session_drawdown),
        campaign_target_usd=_f("CAMPAIGN_TARGET_USD", base.campaign_target_usd),
        profit_lock_fraction=_f("PROFIT_LOCK_FRACTION", base.profit_lock_fraction),
        halt_bankroll=_f("HALT_BANKROLL", base.halt_bankroll),
        max_consecutive_crypto_losses=_i(
            "MAX_CONSECUTIVE_CRYPTO_LOSSES", base.max_consecutive_crypto_losses
        ),
        max_drawdown=_f("MAX_DRAWDOWN", base.max_drawdown),
        min_trade=_f("MIN_TRADE", base.min_trade),
        scan_limit=_i("SCAN_LIMIT", base.scan_limit),
        grok_top=_i("GROK_TOP", base.grok_top),
        grok_model=os.getenv("GROK_MODEL", base.grok_model),
        loop_seconds=_i("LOOP_SECONDS", base.loop_seconds),
        grok_every_seconds=_i("GROK_EVERY_SECONDS", base.grok_every_seconds),
        stop_loss_pct=_f("STOP_LOSS_PCT", base.stop_loss_pct),
        take_profit_price=_f("TAKE_PROFIT_PRICE", base.take_profit_price),
        fast_dump_pct=_f("FAST_DUMP_PCT", base.fast_dump_pct),
        fast_dump_seconds=_i("FAST_DUMP_SECONDS", base.fast_dump_seconds),
        grok_max_hold_seconds=_i("GROK_MAX_HOLD_SECONDS", base.grok_max_hold_seconds),
        grok_min_sources=_i("GROK_MIN_SOURCES", base.grok_min_sources),
        live=_flag("LIVE", False),
        user_agent=os.getenv("SCOUT_UA", base.user_agent),
        gamma_markets=os.getenv("GAMMA_MARKETS", base.gamma_markets),
        xai_base_url=os.getenv("XAI_BASE_URL", base.xai_base_url),
        clob_host=os.getenv("CLOB_HOST", base.clob_host),
        chain_id=_i("CHAIN_ID", base.chain_id),
        signature_type=_i("POLYMARKET_SIGNATURE_TYPE", base.signature_type),
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        funder=os.getenv("POLYMARKET_FUNDER", os.getenv("POLYMARKET_FUNDER_ADDRESS", "")),
        clob_api_key=os.getenv("CLOB_API_KEY", ""),
        clob_secret=os.getenv("CLOB_SECRET", os.getenv("CLOB_API_SECRET", "")),
        clob_passphrase=os.getenv("CLOB_PASSPHRASE", os.getenv("CLOB_PASS_PHRASE", "")),
        relayer_api_key=os.getenv(
            "RELAYER_API_KEY", os.getenv("POLYMARKET_RELAYER_API_KEY", "")
        ),
        relayer_api_key_address=os.getenv(
            "RELAYER_API_KEY_ADDRESS", os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "")
        ),
        dip_threshold=_f("DIP_THRESHOLD", base.dip_threshold),
        dip_window_ms=_i("DIP_WINDOW_MS", base.dip_window_ms),
        pair_sum_target=_f("PAIR_SUM_TARGET", base.pair_sum_target),
        tape_seconds=_f("TAPE_SECONDS", base.tape_seconds),
        tape_interval=_f("TAPE_INTERVAL", base.tape_interval),
        min_book_shares=_f("MIN_BOOK_SHARES", base.min_book_shares),
    )


def hex_private_key(raw: str) -> str:
    key = raw.strip()
    if not key:
        return ""
    return key if key.startswith("0x") else "0x" + key
