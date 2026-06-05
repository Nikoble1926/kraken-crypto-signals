"""
x402 pay-per-call Crypto Signals API (Base / EVM)
"""

import os
import sys
import time
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync

load_dotenv()

EVM_ADDRESS = os.getenv("EVM_ADDRESS")
NETWORK = os.getenv("NETWORK", "eip155:84532")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PRICE = os.getenv("PRICE", "$0.01")
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")

if not EVM_ADDRESS:
    raise ValueError("Missing EVM_ADDRESS in .env (your Base receiving address)")

if BOT_ROOT not in sys.path:
    sys.path.insert(0, BOT_ROOT)

from core.data_engine import DataEngine            # noqa: E402
from core.strategy_engine import StrategyEngine    # noqa: E402

_DEFAULT_PAIR = "BTC/USD"
_DEFAULT_TIMEFRAME = "1m"
_CACHE_TTL_SECONDS = 8
_SCORE_NORMALIZER = 100.0
_DISCLAIMER = (
    "Educational signal from a multi-timeframe Kraken strategy. "
    "Not financial advice. Markets can move against any signal; "
    "use independent risk management."
)

_engine = DataEngine()
_strategy = StrategyEngine()
_lock = threading.Lock()
_cache = {}


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _compute(pair):
    mtf = _engine.get_multi_timeframe_candles(pair)
    result = _strategy.analyze_multi_timeframe(mtf)
    signal = str(result.get("signal", "HOLD")).upper()
    score = float(result.get("score", 0) or 0)
    confidence = _clamp(score / _SCORE_NORMALIZER) if result.get("valid") else 0.0
    return {
        "pair": pair,
        "signal": signal,
        "confidence": round(confidence, 3),
        "timeframe": _DEFAULT_TIMEFRAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": _DISCLAIMER,
    }


def generate_signal(pair=_DEFAULT_PAIR):
    pair = (pair or _DEFAULT_PAIR).upper().replace("-", "/")
    now = time.time()
    cached = _cache.get(pair)
    if cached and cached[0] > now:
        return cached[1]
    with _lock:
        cached = _cache.get(pair)
        if cached and cached[0] > now:
            return cached[1]
        try:
            payload = _compute(pair)
        except Exception as e:
            payload = {
                "pair": pair,
                "signal": "HOLD",
                "confidence": 0.0,
                "timeframe": _DEFAULT_TIMEFRAME,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "disclaimer": _DISCLAIMER,
                "error": "{}: {}".format(type(e).__name__, e),
            }
        _cache[pair] = (now + _CACHE_TTL_SECONDS, payload)
        return payload


app = Flask(__name__)

facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
server = x402ResourceServerSync(facilitator)
server.register(NETWORK, ExactEvmServerScheme())

routes = {
    "GET /signals": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price=PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Crypto trading signal (USDC pay-per-call)",
    ),
}
payment_middleware(app, routes=routes, server=server)


@app.route("/signals")
def signals():
    pair = request.args.get("pair", _DEFAULT_PAIR)
    return jsonify(generate_signal(pair))


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name": "Kraken Crypto Signals",
        "description": "Pay-per-call multi-timeframe crypto trading signals (BUY/SELL/HOLD).",
        "endpoints": [
            {"path": "/signals", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)"}}
        ],
        "disclaimer": _DISCLAIMER,
    })


@app.route("/")
def home():
    return """<!doctype html><html><head><meta charset="utf-8">
<title>Kraken Crypto Signals — x402 API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,Arial,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.55;color:#111;background:#fafafa}code{background:#eee;padding:2px 6px;border-radius:4px}.box{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 20px;margin:16px 0}a{color:#1f6feb}</style>
</head><body>
<h1>Kraken Crypto Signals</h1>
<p>Pay-per-call crypto trading signals (<b>BUY / SELL / HOLD</b> + confidence) from a live multi-timeframe Kraken strategy. Built for AI agents via the <b>x402</b> protocol.</p>
<div class="box"><b>Agent endpoint (paid):</b><br>
<code>GET https://signals.nsgoods.org/signals?pair=BTC/USD</code><br><br>
Price: <b>$0.01 USDC</b> per call &middot; Network: <b>Base</b> (eip155:8453)<br>
Pay automatically with any x402 client — no account, no API key.</div>
<p><b>Free:</b> <a href="/.well-known/x402">manifest</a> &middot; <a href="/health">health</a></p>
<p style="color:#666;font-size:14px">For automated/agent use. Not financial advice.</p>
</body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4021, debug=False)
