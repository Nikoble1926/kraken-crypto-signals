#!/usr/bin/env python3
"""Verifiable track record (commit-reveal hash chain). Reads PUBLIC market data
only; does not touch the bot. Stores full snapshot privately, publishes only a
hash commitment to the public chain (served at /track-record)."""
import os
import sys
import json
import hashlib
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv("/root/signals_api/.env")
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")
if BOT_ROOT not in sys.path:
    sys.path.insert(0, BOT_ROOT)

from core.data_engine import DataEngine            # noqa: E402
from core.strategy_engine import StrategyEngine    # noqa: E402

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD",
         "DOGE/USD", "ADA/USD", "AVAX/USD", "LINK/USD"]
PRIV = "/root/signals_api/track_record_private.jsonl"
PUB = "/root/signals_api/track_record_public.jsonl"


def compute_signals():
    eng = DataEngine()
    strat = StrategyEngine()
    out = []
    for p in PAIRS:
        try:
            mtf = eng.get_multi_timeframe_candles(p)
            r = strat.analyze_multi_timeframe(mtf)
            sig = str(r.get("signal", "HOLD")).upper()
            score = float(r.get("score", 0) or 0)
            conf = round(max(0.0, min(1.0, score / 100.0)) if r.get("valid") else 0.0, 3)
            out.append({"pair": p, "signal": sig, "confidence": conf})
        except Exception as e:
            out.append({"pair": p, "error": type(e).__name__})
    return out


def _last_public_hash():
    if not os.path.exists(PUB):
        return "GENESIS", 0
    last, count = None, 0
    with open(PUB) as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
                count += 1
    if not last:
        return "GENESIS", 0
    return json.loads(last)["h"], count


def main():
    ts = datetime.now(timezone.utc).isoformat()
    prev, seq = _last_public_hash()
    nonce = secrets.token_hex(16)
    full = {"seq": seq, "ts": ts, "prev": prev, "nonce": nonce,
            "signals": compute_signals()}
    h = hashlib.sha256(
        json.dumps(full, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    full["h"] = h
    commitment = {"seq": seq, "ts": ts, "prev": prev, "h": h}
    with open(PRIV, "a") as f:
        f.write(json.dumps(full, sort_keys=True, separators=(",", ":")) + "\n")
    with open(PUB, "a") as f:
        f.write(json.dumps(commitment, separators=(",", ":")) + "\n")
    print("appended seq", seq, "ts", ts, "h", h[:16])


if __name__ == "__main__":
    main()
