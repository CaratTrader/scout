#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null; then
  echo "Need python3.11+ on PATH"
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created .env — the only thing you must paste is XAI_API_KEY"
fi

echo
.venv/bin/python -m scout doctor
echo
echo "5-minute paper:"
echo "  1. https://console.x.ai  → API key + \$20 credit"
echo "  2. paste it as XAI_API_KEY in $(pwd)/.env"
echo "  3. .venv/bin/python -m scout doctor"
echo "  4. .venv/bin/python -m scout loop"
echo
echo "Do not turn LIVE on in this step. Bets are a second pass — see QUICKSTART.md"
