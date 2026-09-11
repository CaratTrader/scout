#!/bin/sh
# One real cycle with the live caps (same values as docs/com.tradeinc.scout.live.plist).
cd "$(dirname "$0")" || exit 1
export MIN_CRYPTO_STAKE=1 MAX_CRYPTO_STAKE=2 MAX_CRYPTO_POSITIONS=1 MAX_POSITIONS=1 CRYPTO_MIN_EDGE=0.05 \
       CRYPTO_MAX_SLIP=0.01 CRYPTO_SIGNAL_TTL_SECONDS=3 MAX_SESSION_DRAWDOWN=6 LESSONS_SOURCE=data/ledger.json CRYPTO_BASIS_MIN_BPS=0.15 HALT_BANKROLL=40 CAMPAIGN_TARGET_USD=100
echo "== doctor (the xAI line is expected to say NO; it is not used)"
.venv/bin/python -X utf8 -m scout doctor
echo; echo "== one LIVE cycle (max \$2 stake)"
exec .venv/bin/python -X utf8 -u -m scout once --live --no-grok
