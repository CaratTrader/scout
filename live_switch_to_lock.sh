#!/bin/zsh
# Switch the LIVE job to the lock profile (docs/com.tradeinc.scout.live.lock.plist) and restart it.
# Run this yourself; it touches real-money trading. Reverse with:
#   cp docs/com.tradeinc.scout.live.plist ~/Library/LaunchAgents/com.tradeinc.scout.live.plist && ./live_switch_to_lock.sh --restart-only
set -euo pipefail
cd "$(dirname "$0")"
PLIST=~/Library/LaunchAgents/com.tradeinc.scout.live.plist
if [[ "${1:-}" != "--restart-only" ]]; then
  cp docs/com.tradeinc.scout.live.lock.plist "$PLIST"
  plutil -lint "$PLIST"
fi
launchctl bootout gui/501/com.tradeinc.scout.live 2>/dev/null || true
sleep 2
launchctl bootstrap gui/501 "$PLIST"
sleep 3
PID=$(launchctl list | awk '$3=="com.tradeinc.scout.live"{print $1}')
echo "live job pid: ${PID:-none}"
if [[ -n "${PID:-}" && "$PID" != "-" ]]; then
  ps -p "$PID" -wwE -o command= | tr ' ' '\n' | grep -E '^(CRYPTO_LAG_MODEL|TWAP_LOCK_MIN_Z|TWAP_LOCK_ASK_CAP|MAX_CRYPTO_STAKE|LOOP_SECONDS|CRYPTO_ONLY)=' || true
fi
echo "watch:  tail -f data/live.log"
