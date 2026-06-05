"""
x402 pay-per-call Crypto Signals API (Base / EVM)
--------------------------------------------------
Wraps the existing Kraken bot signal logic (StrategyEngine.analyze_multi_timeframe)
in an x402 paywall. An agent calls GET /signals -> 402 -> pays USDC on Base -> gets
the signal JSON.

DEPLOY: run this on the SAME server as the bot (BOT_ROOT=/root/bot) so it can import
DataEngine/StrategyEngine and reuse the venv + OHLCV cache. It runs as a SEPARATE
process and only reads PUBLIC market data -- it does not touch the running bot.

SECURITY: only your PUBLIC Base receiving address (EVM_ADDRESS) goes here.
Never put a private key / seed phrase in this file or on the server.

Based on the official Coinbase x402 Flask example (confirmed API, June 2026).
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

# ---- Config (from .env) ----------------------------------------------------
EVM_ADDRESS = os.getenv("EVM_ADDRESS")                 # your Base wallet (receives USDC)
NETWORK = os.getenv("NETWORK", "eip155:84532")         # 84532 = Base Sepolia (test); 8453 = mainnet
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PRICE = os.getenv("PRICE", "$0.01")                    # price per /signals call
TOP_PRICE = os.getenv("TOP_PRICE", "$0.10")            # price per /signals/top call (market scan)
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")          # path to the Kraken bot package

if not EVM_ADDRESS:
    raise ValueError("Missing EVM_ADDRESS in .env (your Base receiving address)")

# ---- Make the bot package importable ---------------------------------------
if BOT_ROOT not in sys.path:
    sys.path.insert(0, BOT_ROOT)

from core.data_engine import DataEngine            # noqa: E402
from core.strategy_engine import StrategyEngine    # noqa: E402

# ---- Signal adapter (real Kraken logic, cached per pair) -------------------
_DEFAULT_PAIR = "BTC/USD"
_DEFAULT_TIMEFRAME = "1m"          # strategy's primary tf; 5m/15m are confirmations
_CACHE_TTL_SECONDS = 8             # matches DataEngine's 1m candle cache
_SCORE_NORMALIZER = 100.0          # rough cap for score -> 0..1 confidence
_DISCLAIMER = (
    "Educational signal from a multi-timeframe Kraken strategy. "
    "Not financial advice. Markets can move against any signal; "
    "use independent risk management."
)

_engine = DataEngine()
_strategy = StrategyEngine()
_lock = threading.Lock()
_cache = {}   # pair -> (expires_at, payload)


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
    """Latest trading signal as a JSON-serializable dict, cached per pair."""
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


# ---- Premium: market scan (strongest signals across major pairs) -----------
# Curated set kept small so a scan stays fast and never hammers Kraken's API.
_TOP_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "ADA/USD",
    "AVAX/USD", "LINK/USD", "LTC/USD", "DOT/USD", "XLM/USD", "UNI/USD",
]
_TOP_CACHE_TTL_SECONDS = 45         # whole-scan cache; keeps /top fast + cheap to serve
_TOP_DEFAULT_LIMIT = 3              # how many strongest signals to return
_top_lock = threading.Lock()        # separate from _lock to avoid nested-lock deadlock
_top_cache = {}                     # "top" -> (expires_at, payload)


def generate_top(limit=_TOP_DEFAULT_LIMIT):
    """Scan major pairs and return the strongest BUY/SELL signals right now."""
    now = time.time()
    cached = _top_cache.get("top")
    if cached and cached[0] > now:
        return cached[1]
    with _top_lock:
        cached = _top_cache.get("top")
        if cached and cached[0] > now:
            return cached[1]
        scanned = []
        for pair in _TOP_PAIRS:
            try:
                # generate_signal uses its own (_lock) per-pair cache -> safe here
                scanned.append(generate_signal(pair))
            except Exception:
                continue
        actionable = [s for s in scanned if s.get("signal") in ("BUY", "SELL")]
        actionable.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        payload = {
            "scanned_pairs": len(scanned),
            "actionable": len(actionable),
            "top": actionable[:limit],
            "universe": _TOP_PAIRS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": _DISCLAIMER,
        }
        _top_cache["top"] = (now + _TOP_CACHE_TTL_SECONDS, payload)
        return payload


# ---- App + x402 middleware (sync variants, Flask is sync) ------------------
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
        description="Crypto trading signal for one pair (USDC pay-per-call)",
    ),
    "GET /signals/top": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price=TOP_PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Market scan: strongest BUY/SELL signals across major pairs (USDC pay-per-call)",
    ),
}
payment_middleware(app, routes=routes, server=server)


# ---- Routes ----------------------------------------------------------------
@app.route("/signals")
def signals():
    pair = request.args.get("pair", _DEFAULT_PAIR)
    return jsonify(generate_signal(pair))


@app.route("/signals/top")
def signals_top():
    return jsonify(generate_top())


_LANDING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Kraken Crypto Signals &mdash; x402 API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,Arial,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.55;color:#111;background:#fafafa}code{background:#eee;padding:2px 6px;border-radius:4px}.box{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 20px;margin:16px 0}a{color:#1f6feb}</style>
</head><body>
<h1>Kraken Crypto Signals</h1>
<p>Pay-per-call crypto trading signals (<b>BUY / SELL / HOLD</b> + confidence) from a live multi-timeframe Kraken strategy. Built for AI agents via the <b>x402</b> protocol on Base.</p>
<div class="box"><b>Agent endpoints (paid in USDC):</b><br>
<code>GET /signals?pair=BTC/USD</code> &mdash; one pair &middot; $0.01<br>
<code>GET /signals/top</code> &mdash; market scan, strongest signals &middot; $0.10</div>
<div class="box">Any active Kraken <code>*/USD</code> pair works via <code>?pair=</code> (ETH/USD, SOL/USD, XRP/USD, &hellip;).</div>
<p>Machine-readable manifest: <a href="/.well-known/x402">/.well-known/x402</a> &middot; Code: <a href="https://github.com/Nikoble1926/kraken-crypto-signals">GitHub</a></p>
<p style="color:#666;font-size:14px">Educational signals. Not financial advice.</p>
</body></html>"""


@app.route("/")
def home():
    return _LANDING_HTML


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name": "Kraken Crypto Signals",
        "description": "Pay-per-call multi-timeframe crypto trading signals (BUY/SELL/HOLD).",
        "pairs": (
            "All active Kraken */USD spot pairs (e.g. BTC/USD, ETH/USD, SOL/USD, "
            "XRP/USD, DOGE/USD). Pass any with ?pair=."
        ),
        "endpoints": [
            {"path": "/signals", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)"},
             "description": "Trading signal for one pair"},
            {"path": "/signals/top", "method": "GET", "price": TOP_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "description": "Market scan: strongest BUY/SELL signals across major pairs"},
        ],
        "disclaimer": _DISCLAIMER,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4021, debug=False)
