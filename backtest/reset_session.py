"""Start a fresh risk session on the paper ledger (same semantics as the
'deposit detected' rebase in agent._apply_risk_state). History is kept."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout.ledger import utc_now  # noqa: E402

path = Path(__file__).resolve().parent.parent / "data" / "ledger.json"
led = json.loads(path.read_text())
cash = float(led["cash"])
print(f"before: halted={led['halted']} reason={led['halt_reason']!r} cash={cash}")
led["halted"] = False
led["halt_reason"] = ""
led["risk_baseline_cash"] = cash
led["realized_peak_cash"] = cash
led["locked_equity_floor"] = cash
led["session_started_at"] = utc_now()
led["crypto_loss_streak"] = 0
path.write_text(json.dumps(led, indent=2) + "\n")
print(f"after: fresh session, baseline={cash}, started={led['session_started_at']}")
