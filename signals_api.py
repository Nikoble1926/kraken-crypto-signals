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
import json
import base64
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync
from x402.extensions.bazaar import declare_discovery_extension, OutputConfig

from cdp.auth import GetAuthHeadersOptions, get_auth_headers

from eth_account import Account
from eth_account.messages import encode_defunct

load_dotenv()

# ---- Config (from .env) ----------------------------------------------------
EVM_ADDRESS = os.getenv("EVM_ADDRESS")                 # your Base wallet (receives USDC)
NETWORK = os.getenv("NETWORK", "eip155:84532")         # 84532 = Base Sepolia (test); 8453 = mainnet
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PRICE = os.getenv("PRICE", "$0.01")                    # price per /signals call
TOP_PRICE = os.getenv("TOP_PRICE", "$0.10")            # price per /signals/top call (market scan)
HISTORY_PRICE = os.getenv("HISTORY_PRICE", "$0.05")    # /history (mirrors extra_endpoints.py)
REGIME_PRICE = os.getenv("REGIME_PRICE", "$0.02")      # /regime/changed
BACKTEST_PRICE = os.getenv("BACKTEST_PRICE", "$0.25")  # /backtest
INDICATORS_PRICE = os.getenv("INDICATORS_PRICE", "$0.005")  # /indicators (high-freq TA)
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")          # path to the Kraken bot package
SIGNING_KEY = os.getenv("SIGNING_KEY")                 # dedicated FUND-LESS key that signs signals
CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID")           # CDP facilitator auth (Bazaar indexer requires CDP)
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")   # CDP facilitator auth

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
            # Data pipeline failure -> mark unavailable so the route returns
            # 503 (no charge on the standard x402 settle-after-success flow)
            # instead of selling a junk HOLD signal.
            unavailable = {
                "_unavailable": True,
                "pair": pair,
                "reason": "{}: {}".format(type(e).__name__, e),
            }
            _cache[pair] = (now + 2, unavailable)  # short cache during outage
            return unavailable
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
                s = generate_signal(pair)
            except Exception:
                continue
            if s.get("_unavailable"):
                continue
            scanned.append(s)
        if not scanned:
            # whole-market data outage -> let the route return 503 (no charge)
            return {"_unavailable": True, "reason": "market data unavailable"}
        actionable = [s for s in scanned if s.get("signal") in ("BUY", "SELL")]
        actionable.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        payload = {
            "scanned_pairs": len(scanned),
            "actionable": len(actionable),
            "top": actionable[:10],  # cache up to 10; route slices to ?limit
            "universe": _TOP_PAIRS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": _DISCLAIMER,
        }
        _top_cache["top"] = (now + _TOP_CACHE_TTL_SECONDS, payload)
        return payload


# ---- Signed signals --------------------------------------------------------
# Every signal response carries an ECDSA signature buyers can verify (recover)
# against our published signer address -> proves the signal is authentic and
# untampered. Uses a DEDICATED, FUND-LESS key (never the receiving wallet key).
_SIGNER_ADDRESS = Account.from_key(SIGNING_KEY).address if SIGNING_KEY else None


def _signed(payload):
    """Return payload + 'signed_by' (address) + 'signature' (over the JSON body
    BEFORE these two keys are added)."""
    if not SIGNING_KEY:
        return payload
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    raw = Account.sign_message(encode_defunct(text=body), private_key=SIGNING_KEY).signature
    sig = raw.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    out = dict(payload)
    out["signed_by"] = _SIGNER_ADDRESS
    out["signature"] = sig
    return out


# ---- App + x402 middleware (sync variants, Flask is sync) ------------------
app = Flask(__name__)

# ---- Facilitator wiring ----------------------------------------------------
# Active: CDP Facilitator. Bazaar indexing happens on the first successful
# settle through CDP (docs.cdp.coinbase.com/x402/bazaar). Auth is per-request
# JWT, generated by the cdp-sdk helper and adapted to the x402 SDK's
# AuthProvider protocol via CreateHeadersAuthProvider.
# Fallback - swap back if CDP fails:
#   facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
# (FACILITATOR_URL in .env defaults to https://facilitator.payai.network)

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_CDP_HOST = "api.cdp.coinbase.com"
_CDP_BASE_PATH = "/platform/v2/x402"


def _cdp_endpoint_headers(method: str, path: str) -> dict:
    return get_auth_headers(GetAuthHeadersOptions(
        api_key_id=CDP_API_KEY_ID,
        api_key_secret=CDP_API_KEY_SECRET,
        request_method=method,
        request_host=_CDP_HOST,
        request_path=path,
    ))


def cdp_create_headers() -> dict:
    """Return the per-x402-endpoint auth headers dict the SDK expects.
    Generates fresh JWTs per call so each request carries a valid Authorization.
    """
    return {
        "verify":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/verify"),
        "settle":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/settle"),
        "supported": _cdp_endpoint_headers("GET",  f"{_CDP_BASE_PATH}/supported"),
        "bazaar":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/bazaar"),
    }


if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(cdp_create_headers),
    ))
else:
    # CDP creds missing -> automatic fallback to the PayAI/.env-configured URL
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))

server = x402ResourceServerSync(facilitator)
server.register(NETWORK, ExactEvmServerScheme())


# ---- EXTENSION-RESPONSES logging (Bazaar indexing trace) -------------------
# Coinbase's CDP facilitator returns an EXTENSION-RESPONSES header on its
# settle response. We surface it to journald so we can see whether Bazaar
# metadata is being processed or rejected after the first real payment.
@app.after_request
def _log_extension_responses(resp):
    er = resp.headers.get("EXTENSION-RESPONSES") or resp.headers.get("extension-responses")
    if er:
        try:
            print(f"[x402-bazaar] EXTENSION-RESPONSES: {er[:800]}", flush=True)
        except Exception:
            pass
    return resp


# ---- Bazaar discovery extensions (agentic.market indexer) ------------------
# Top-level "extensions.bazaar" gets injected into the 402 PAYMENT-REQUIRED
# response so indexers (e.g. agentic.market) can list this service. Output
# example + schema mirror the actual signed JSON returned post-payment.

_SIG_EXAMPLE = {
    "pair": "BTC/USD",
    "signal": "HOLD",
    "confidence": 0.42,
    "timeframe": "1m",
    "generated_at": "2026-06-06T22:00:00+00:00",
    "disclaimer": _DISCLAIMER,
    "signed_by": "0x5e63d01d6A266BC17f577B80199a2a07B15053C7",
    "signature": "0x10fafe37c311ef9f70d1c5b119454a553c82c0bb9981b09560485c3dc8072fc47bebad8022f12b240e02c38cbe4243a1f10b63f06502356c263dea35571974411c",
}

_SIG_SCHEMA = {
    "type": "object",
    "properties": {
        "pair":         {"type": "string"},
        "signal":       {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence":   {"type": "number", "minimum": 0, "maximum": 1},
        "timeframe":    {"type": "string"},
        "generated_at": {"type": "string", "format": "date-time"},
        "disclaimer":   {"type": "string"},
        "signed_by":    {"type": "string"},
        "signature":    {"type": "string"},
    },
    "required": ["pair", "signal", "confidence", "timeframe", "generated_at"],
}

_TOP_EXAMPLE = {
    "scanned_pairs": 12,
    "actionable": 2,
    "top": [
        {"pair": "BTC/USD", "signal": "BUY", "confidence": 0.62,
         "timeframe": "1m", "generated_at": "2026-06-06T22:00:00+00:00"},
    ],
    "universe": ["BTC/USD", "ETH/USD", "SOL/USD", "..."],
    "generated_at": "2026-06-06T22:00:00+00:00",
    "disclaimer": "Educational signal. Not financial advice.",
    "signed_by": "0x5e63d01d6A266BC17f577B80199a2a07B15053C7",
    "signature": "0x...",
}

_TOP_SCHEMA = {
    "type": "object",
    "properties": {
        "scanned_pairs": {"type": "integer", "minimum": 0},
        "actionable":    {"type": "integer", "minimum": 0},
        "top": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair":         {"type": "string"},
                    "signal":       {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence":   {"type": "number"},
                    "timeframe":    {"type": "string"},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer":   {"type": "string"},
                },
            },
        },
        "universe":     {"type": "array", "items": {"type": "string"}},
        "generated_at": {"type": "string", "format": "date-time"},
        "disclaimer":   {"type": "string"},
        "signed_by":    {"type": "string"},
        "signature":    {"type": "string"},
    },
    "required": ["scanned_pairs", "actionable", "top", "universe", "generated_at"],
}

_BAZAAR_SIGNALS_EXT = declare_discovery_extension(
    input={"pair": "BTC/USD"},
    input_schema={
        "type": "object",
        "properties": {
            "pair": {
                "type": "string",
                "default": "BTC/USD",
                "description": "Any active Kraken */USD spot pair (e.g. BTC/USD, ETH/USD)",
            },
        },
        "required": [],
    },
    output=OutputConfig(example=_SIG_EXAMPLE, schema=_SIG_SCHEMA),
)
# declare_discovery_extension() leaves input.method blank because the middleware
# is supposed to enrich it from the route key. The schema validator runs first
# and rejects the missing method, so we set it inline.
_BAZAAR_SIGNALS_EXT["bazaar"]["info"]["input"]["method"] = "GET"

_BAZAAR_TOP_EXT = declare_discovery_extension(
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 3,
                "description": "How many top signals to return (1-10).",
            },
        },
        "required": [],
    },
    output=OutputConfig(example=_TOP_EXAMPLE, schema=_TOP_SCHEMA),
)
_BAZAAR_TOP_EXT["bazaar"]["info"]["input"]["method"] = "GET"


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
        description="Live market-state data for one crypto pair: BUY/SELL/HOLD state + confidence from a multi-timeframe engine. Machine-readable, ECDSA-signed. Input data for AI finance agents (USDC pay-per-call).",
        extensions=_BAZAAR_SIGNALS_EXT,
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
        description="Market scan data: strongest BUY/SELL states across major crypto pairs right now, with confidence. Machine-readable input for AI trading agents (USDC pay-per-call).",
        extensions=_BAZAAR_TOP_EXT,
    ),
}
# ---- Optional extra paid endpoints (additive; safe no-op if anything fails) ----
try:
    from extra_endpoints import register as _register_extra
    _register_extra(
        app, routes,
        EVM_ADDRESS=EVM_ADDRESS, NETWORK=NETWORK, signed=_signed,
        unavailable_body=(lambda reason=None: {
            "error": "signal_unavailable",
            "message": ("Live data is temporarily unavailable. You were not "
                        "charged; please retry shortly."),
            "detail": reason}),
        DISCLAIMER=_DISCLAIMER, RouteConfig=RouteConfig, PaymentOption=PaymentOption,
        declare_discovery_extension=declare_discovery_extension,
        OutputConfig=OutputConfig,
        engine=_engine, strategy=_strategy)
except Exception as _e:
    print("[extra_endpoints] skipped:", _e, flush=True)


payment_middleware(app, routes=routes, server=server)


# ---- Routes ----------------------------------------------------------------
def _unavailable_body(reason=None):
    return {
        "error": "signal_unavailable",
        "message": ("Live market data is temporarily unavailable. "
                    "You were not charged; please retry shortly."),
        "detail": reason,
    }


@app.route("/signals")
def signals():
    pair = request.args.get("pair", _DEFAULT_PAIR)
    payload = generate_signal(pair)
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    return jsonify(_signed(payload))


@app.route("/signals/top")
def signals_top():
    try:
        limit = int(request.args.get("limit", _TOP_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _TOP_DEFAULT_LIMIT
    limit = max(1, min(10, limit))
    payload = generate_top()
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    out = dict(payload)
    out["top"] = payload.get("top", [])[:limit]
    return jsonify(_signed(out))


@app.route("/signals/preview")
def signals_preview():
    """Free sample: BTC/USD direction only (no confidence). Lets agents/devs
    evaluate before paying for /signals (confidence + any pair) or /signals/top."""
    payload = generate_signal(_DEFAULT_PAIR)
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    return jsonify(_signed({
        "pair": payload.get("pair"),
        "signal": payload.get("signal"),
        "preview": True,
        "note": ("Free sample: BTC/USD direction only. Pay GET /signals for "
                 "confidence + any Kraken */USD pair, or GET /signals/top for a "
                 "market scan."),
        "timeframe": payload.get("timeframe"),
        "generated_at": payload.get("generated_at"),
        "disclaimer": payload.get("disclaimer"),
    }))


_LANDING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Kraken Crypto Signals - x402 API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="talentapp:project_verification" content="5ba32c6b051dd9be1a05c3de8e074080ba65dafda1659e3f6407ad97c2877f9a4d493422a60a1495e6a76cc4f9e0fd46abbafc2cba7d4e67cccf62bb0e1b7740">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Michroma&display=swap" rel="stylesheet">
<style>
:root{--ink:#cdd4dd;--dim:#69737f;--faint:#39414b;--hal:#e01b24;--line:rgba(160,175,190,.14)}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:var(--ink);font:15px/1.75 "Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;overflow-x:hidden}
#cosmos{position:fixed;inset:0;z-index:0;display:block}
.mich{font-family:"Michroma",sans-serif}
.micro{font-family:"Michroma",sans-serif;font-size:9.5px;letter-spacing:.42em;text-transform:uppercase;color:var(--dim)}
.num{color:var(--faint)}
#gamewrap{position:relative;z-index:2;height:min(68vh,600px);min-height:380px;background:#000;border-bottom:1px solid rgba(160,175,190,.18);cursor:crosshair}
#game{display:block;width:100%;height:100%}
#hud{position:absolute;inset:0;pointer-events:none;font-family:"Michroma",sans-serif;text-transform:uppercase}
#hud .tl{position:absolute;top:16px;left:20px;font-size:10px;letter-spacing:.3em;color:#8fa0b4}
#hud .tr{position:absolute;top:16px;right:20px;font-size:10px;letter-spacing:.3em;color:#8fa0b4;text-align:right}
#overlay{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;background:rgba(0,0,0,.55);text-align:center;padding:20px}
#overlay h2{font-family:"Michroma",sans-serif;font-weight:400;font-size:clamp(15px,3vw,22px);letter-spacing:.34em;color:#eef2f7;text-transform:uppercase}
#overlay p{color:var(--dim);font-size:12.5px;letter-spacing:.12em;max-width:52ch}
#overlay .go{font-family:"Michroma",sans-serif;font-size:11px;letter-spacing:.3em;color:#000;background:#e6ebf2;border:none;padding:12px 26px;cursor:pointer;text-transform:uppercase}
#overlay .go:hover{background:#fff}
#overlay.hidden{display:none}
main{position:relative;z-index:1;max-width:660px;margin:0 auto;padding:9vh 26px 12vh}
.hal{display:flex;align-items:center;gap:14px;margin-bottom:8vh}
.hal .eye{width:12px;height:12px;border-radius:50%;background:radial-gradient(circle at 38% 34%,#ffd9a0 0%,#ff5c33 22%,var(--hal) 45%,#5a0208 78%,#1a0102 100%);box-shadow:0 0 14px 2px rgba(224,27,36,.55),0 0 44px 8px rgba(224,27,36,.18);animation:breathe 4.2s ease-in-out infinite}
@keyframes breathe{0%,100%{box-shadow:0 0 14px 2px rgba(224,27,36,.55),0 0 44px 8px rgba(224,27,36,.18)}50%{box-shadow:0 0 8px 1px rgba(224,27,36,.35),0 0 24px 4px rgba(224,27,36,.10)}}
h1{font-family:"Michroma",sans-serif;font-weight:400;font-size:clamp(21px,4.6vw,32px);letter-spacing:.30em;text-transform:uppercase;color:#eef2f7;line-height:1.5;margin-bottom:26px;text-shadow:0 2px 30px rgba(0,0,0,.9)}
.lead{color:#97a2af;max-width:54ch;font-size:15.5px;text-shadow:0 1px 12px rgba(0,0,0,.9)}
.lead b{color:var(--ink);font-weight:600}
.monolith{margin:8vh auto;width:16px;height:36px;background:linear-gradient(100deg,#000 55%,#0b0d11 78%,#000);border:1px solid rgba(190,205,225,.10);border-top-color:rgba(190,205,225,.25);box-shadow:0 0 26px rgba(0,0,0,.95),0 -8px 22px rgba(140,170,220,.06)}
section{margin:0 0 8vh;background:rgba(0,2,6,.42);backdrop-filter:blur(2px);padding:4px 0}
.sec-label{display:flex;align-items:baseline;gap:16px;margin-bottom:24px}
.sec-label:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent)}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:20px;padding:15px 0;border-bottom:1px solid var(--line)}
.row:first-of-type{border-top:1px solid var(--line)}
.row code{font:13.5px/1.5 ui-monospace,Consolas,monospace;color:#e6ebf2}
.row .what{color:var(--dim);font-size:13px;white-space:nowrap}
.row .price{font-family:"Michroma",sans-serif;font-size:11px;letter-spacing:.18em;color:#9fdcb4;white-space:nowrap}
.row .price.free{color:var(--dim)}
p.body{color:#8b96a3;font-size:14.5px;max-width:56ch}
p.body code{font:12.5px ui-monospace,Consolas,monospace;color:var(--ink)}
a{color:#9db8dd;text-decoration:none;border-bottom:1px solid rgba(157,184,221,.25);transition:border-color .2s,color .2s}
a:hover{color:#cfdff5;border-bottom-color:#cfdff5}
.links{font-size:13.5px;line-height:2.4}
.links a{margin-right:14px}
.links .sep{color:var(--faint);margin-right:14px}
footer{color:var(--faint);font-size:12px;letter-spacing:.06em;border-top:1px solid var(--line);padding-top:22px;display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}
@media(max-width:560px){.row{flex-wrap:wrap}.row .what{white-space:normal}}
</style></head><body>
<canvas id="cosmos"></canvas>

<div id="gamewrap">
  <canvas id="game"></canvas>
  <div id="hud">
    <div class="tl" id="hudL">Score 0000 &nbsp;&middot;&nbsp; Level 1</div>
    <div class="tr" id="hudR">Lives &#9679;&#9679;&#9679;</div>
  </div>
  <div id="overlay">
    <h2 id="ovTitle">UFO Defense</h2>
    <p id="ovText">&larr; &rarr; or mouse to fly &nbsp;&middot;&nbsp; SPACE or click to fire<br>Level 1: patrol sweep, stray bombs &nbsp;&middot;&nbsp; Level 2: bomb rain</p>
    <button class="go" id="ovBtn">Engage</button>
  </div>
</div>

<main>
<div class="hal"><span class="eye"></span><span class="micro" id="sysstat">System operational &middot; live feed</span></div>

<h1>Kraken<br>Crypto Signals</h1>
<p class="lead">Machine-readable market-state data for AI agents: <b>BUY / SELL / HOLD</b> + confidence, from a live multi-timeframe strategy. Paid per call in USDC over x402 &mdash; no accounts, no API keys.</p>

<div class="monolith" title="1 : 4 : 9"></div>

<section>
<div class="sec-label"><span class="micro"><span class="num">01</span> &nbsp;Endpoints</span></div>
<div class="row"><code>GET /signals?pair=BTC/USD</code><span class="what">one pair &middot; signed</span><span class="price">$0.01</span></div>
<div class="row"><code>GET /signals/top</code><span class="what">market scan &middot; strongest signals</span><span class="price">$0.10</span></div>
<div class="row"><code>GET /backtest</code><span class="what">walk-forward replay &middot; win-rate</span><span class="price">$0.25</span></div>
<div class="row"><code>GET /history</code><span class="what">revealed past snapshots &middot; verifiable</span><span class="price">$0.05</span></div>
<div class="row"><code>GET /regime/changed</code><span class="what">RISK_ON / RISK_OFF &middot; cheap poll</span><span class="price">$0.02</span></div>
<div class="row"><code>GET /indicators</code><span class="what">RSI &middot; EMA &middot; MACD &middot; ATR &middot; Bollinger</span><span class="price">$0.005</span></div>
<div class="row"><code>GET /signals/preview</code><span class="what">BTC/USD direction &middot; sample</span><span class="price free">FREE</span></div>
</section>

<section>
<div class="sec-label"><span class="micro"><span class="num">02</span> &nbsp;Coverage</span></div>
<p class="body">Any active Kraken <code>*/USD</code> spot pair via <code>?pair=</code> &mdash; ETH/USD, SOL/USD, XRP/USD, DOGE/USD, and the rest of the universe.</p>
</section>

<section>
<div class="sec-label"><span class="micro"><span class="num">03</span> &nbsp;Verification</span></div>
<p class="body" style="margin-bottom:22px">Every response is ECDSA-signed &mdash; recover the signer address and prove the data is authentic and untampered. A public hash-chain commits to past signals.</p>
<div class="links">
<a href="/.well-known/x402">x402 manifest</a><span class="sep">&middot;</span>
<a href="/openapi.json">OpenAPI</a><span class="sep">&middot;</span>
<a href="/track-record">track record</a><span class="sep">&middot;</span>
<a href="https://github.com/Nikoble1926/kraken-crypto-signals">GitHub</a>
</div>
</section>

<footer><span>Educational signals. Not financial advice.</span><span class="micro" style="letter-spacing:.3em">nsgoods &middot; x402 &middot; base</span></footer>
</main>

<script>
(function(){
var c=document.getElementById('cosmos'),x=c.getContext('2d'),W,H,DPR=Math.min(devicePixelRatio||1,2);
var stat=document.createElement('canvas'),sx=stat.getContext('2d');
var twinkle=[],meteor=null,nextMeteor=0;
function G(r){return (Math.random()+Math.random()+Math.random()+Math.random()-2)/2*r}
function render(){
  W=c.width=stat.width=innerWidth*DPR;H=c.height=stat.height=innerHeight*DPR;
  c.style.width=stat.style.width=innerWidth+'px';c.style.height=stat.style.height=innerHeight+'px';
  sx.clearRect(0,0,W,H);
  var g=sx.createLinearGradient(0,0,W*0.3,H);g.addColorStop(0,'#01020a');g.addColorStop(0.5,'#020409');g.addColorStop(1,'#000000');
  sx.fillStyle=g;sx.fillRect(0,0,W,H);
  sx.save();sx.translate(W*0.5,H*0.42);sx.rotate(-0.42);
  var L=Math.max(W,H)*1.4;
  for(var i=0;i<3;i++){var gg=sx.createLinearGradient(0,-140*DPR*(i+1),0,140*DPR*(i+1));
    gg.addColorStop(0,'rgba(90,110,150,0)');gg.addColorStop(0.5,'rgba(115,135,175,'+(0.05-i*0.013)+')');gg.addColorStop(1,'rgba(90,110,150,0)');
    sx.fillStyle=gg;sx.fillRect(-L/2,-140*DPR*(i+1),L,280*DPR*(i+1));}
  for(var i=0;i<2600;i++){var px=(Math.random()-0.5)*L,py=G(120*DPR),m=Math.pow(Math.random(),3.2);
    sx.beginPath();sx.arc(px,py,(0.25+m*0.9)*DPR,0,6.283);
    sx.fillStyle='rgba(210,220,240,'+(0.05+m*0.35)+')';sx.fill();}
  for(var i=0;i<500;i++){var px=(Math.random()-0.5)*L*0.55,py=G(70*DPR);
    sx.beginPath();sx.arc(px,py,(0.3+Math.random()*0.8)*DPR,0,6.283);
    sx.fillStyle='rgba(235,215,185,'+(0.04+Math.random()*0.22)+')';sx.fill();}
  sx.globalCompositeOperation='multiply';
  for(var i=0;i<26;i++){var px=(Math.random()-0.5)*L*0.9,py=G(60*DPR),rw=(120+Math.random()*380)*DPR,rh=(10+Math.random()*30)*DPR;
    var dg=sx.createRadialGradient(px,py,0,px,py,rw);
    dg.addColorStop(0,'rgba(6,8,14,0.55)');dg.addColorStop(1,'rgba(6,8,14,0)');
    sx.save();sx.translate(px,py);sx.scale(1,rh/rw);sx.beginPath();sx.arc(0,0,rw,0,6.283);sx.fillStyle=dg;sx.fill();sx.restore();}
  sx.globalCompositeOperation='source-over';
  sx.restore();
  twinkle=[];
  var n=Math.round(innerWidth*innerHeight/1400);
  for(var i=0;i<n;i++){var m=Math.pow(Math.random(),3),px=Math.random()*W,py=Math.random()*H;
    var hue=Math.random()<0.10?'185,205,255':(Math.random()<0.07?'255,222,195':'226,232,244');
    var r=(0.3+m*1.5)*DPR,b=0.22+m*0.75;
    sx.beginPath();sx.arc(px,py,r,0,6.283);sx.fillStyle='rgba('+hue+','+b+')';sx.fill();
    if(m>0.55){
      sx.strokeStyle='rgba('+hue+','+(b*0.28)+')';sx.lineWidth=DPR*0.7;
      sx.beginPath();sx.moveTo(px-r*4,py);sx.lineTo(px+r*4,py);sx.moveTo(px,py-r*4);sx.lineTo(px,py+r*4);sx.stroke();
      twinkle.push({x:px,y:py,r:r,hue:hue,b:b,ph:Math.random()*6.28,sp:0.4+Math.random()*1.1});}
  }
  var pr=Math.max(W,H)*0.23,pxx=W*0.86,pyy=H*1.06;
  var atm=sx.createRadialGradient(pxx,pyy,pr*0.95,pxx,pyy,pr*1.13);
  atm.addColorStop(0,'rgba(96,150,210,0.20)');atm.addColorStop(0.55,'rgba(80,130,190,0.06)');atm.addColorStop(1,'rgba(60,110,170,0)');
  sx.fillStyle=atm;sx.beginPath();sx.arc(pxx,pyy,pr*1.13,0,6.283);sx.fill();
  var pg=sx.createRadialGradient(pxx-pr*0.55,pyy-pr*0.75,pr*0.1,pxx,pyy,pr);
  pg.addColorStop(0,'#8fa3b8');pg.addColorStop(0.35,'#51677e');pg.addColorStop(0.68,'#22303f');pg.addColorStop(0.92,'#070b10');pg.addColorStop(1,'#020304');
  sx.beginPath();sx.arc(pxx,pyy,pr,0,6.283);sx.fillStyle=pg;sx.fill();
  sx.save();sx.beginPath();sx.arc(pxx,pyy,pr,0,6.283);sx.clip();
  for(var i=0;i<9;i++){var by=pyy-pr+Math.random()*2*pr,bh=(6+Math.random()*22)*DPR;
    sx.fillStyle='rgba(255,255,255,'+(0.015+Math.random()*0.035)+')';
    sx.fillRect(pxx-pr,by,2*pr,bh);}
  var sh=sx.createRadialGradient(pxx+pr*0.7,pyy+pr*0.2,pr*0.2,pxx+pr*0.25,pyy,pr*1.15);
  sh.addColorStop(0,'rgba(0,0,0,0.9)');sh.addColorStop(0.6,'rgba(0,0,0,0.45)');sh.addColorStop(1,'rgba(0,0,0,0)');
  sx.fillStyle=sh;sx.fillRect(pxx-pr,pyy-pr,2*pr,2*pr);
  sx.restore();
  var v=sx.createRadialGradient(W/2,H/2,Math.min(W,H)*0.35,W/2,H/2,Math.max(W,H)*0.78);
  v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,0.42)');
  sx.fillStyle=v;sx.fillRect(0,0,W,H);
}
render();addEventListener('resize',render);
var t=0;
(function frame(){t+=0.016;
  x.clearRect(0,0,W,H);x.drawImage(stat,0,0);
  for(var i=0;i<twinkle.length;i++){var s=twinkle[i],tw=0.5+0.5*Math.sin(t*s.sp+s.ph);
    x.beginPath();x.arc(s.x,s.y,s.r*(0.9+tw*0.35),0,6.283);
    x.fillStyle='rgba('+s.hue+','+(s.b*(0.55+tw*0.45))+')';x.fill();}
  if(!meteor&&t>nextMeteor){meteor={x:Math.random()*W*0.8+W*0.1,y:Math.random()*H*0.3,vx:(4+Math.random()*5)*DPR,vy:(2.5+Math.random()*3)*DPR,life:1};}
  if(meteor){meteor.x+=meteor.vx;meteor.y+=meteor.vy;meteor.life-=0.02;
    var mg=x.createLinearGradient(meteor.x,meteor.y,meteor.x-meteor.vx*12,meteor.y-meteor.vy*12);
    mg.addColorStop(0,'rgba(235,240,255,'+(0.85*meteor.life)+')');mg.addColorStop(1,'rgba(235,240,255,0)');
    x.strokeStyle=mg;x.lineWidth=1.4*DPR;x.beginPath();x.moveTo(meteor.x,meteor.y);
    x.lineTo(meteor.x-meteor.vx*12,meteor.y-meteor.vy*12);x.stroke();
    if(meteor.life<=0){meteor=null;nextMeteor=t+6+Math.random()*10;}}
  requestAnimationFrame(frame);
})();
})();

(function(){
var wrap=document.getElementById('gamewrap'),cv=document.getElementById('game'),g=cv.getContext('2d');
var hudL=document.getElementById('hudL'),hudR=document.getElementById('hudR');
var ov=document.getElementById('overlay'),ovT=document.getElementById('ovTitle'),ovX=document.getElementById('ovText'),ovB=document.getElementById('ovBtn');
var DPR=Math.min(devicePixelRatio||1,2),W,H;
function size(){W=cv.width=wrap.clientWidth*DPR;H=cv.height=wrap.clientHeight*DPR;}
size();addEventListener('resize',size);
var AC=null;function beep(f,d,type,vol){try{AC=AC||new (window.AudioContext||window.webkitAudioContext)();
  var o=AC.createOscillator(),ga=AC.createGain();o.type=type||'square';o.frequency.value=f;
  ga.gain.setValueAtTime(vol||0.05,AC.currentTime);ga.gain.exponentialRampToValueAtTime(0.0001,AC.currentTime+d);
  o.connect(ga);ga.connect(AC.destination);o.start();o.stop(AC.currentTime+d);}catch(e){}}
var ST='idle',score=0,lives=3,level=1,kills=0,inv=0,tick=0;
var P={x:0,y:0,w:56,h:22},bullets=[],foes=[],fbul=[],parts=[],keys={},mouseX=null,fireHold=false,lastFire=0,lastSpawn=0;
var CFG={1:{need:12,spawn:1500,speed:[1.2,2.2],hp:1,fires:true,frate:[4800,3800],fspd:2.8,zig:0,pts:10},
         2:{need:20,spawn:780,speed:[3.2,5.2],hp:1,fires:true,frate:[1200,900],fspd:5.2,zig:1,pts:25}};
function reset(all){bullets=[];foes=[];fbul=[];parts=[];lastSpawn=0;
  P.x=W/2;P.y=H-60*DPR;if(all){score=0;lives=3;level=1;kills=0;}}
function hud(){hudL.innerHTML='Score '+String(score).padStart(4,'0')+' &nbsp;&middot;&nbsp; Level '+level+' &nbsp;&middot;&nbsp; '+kills+'/'+CFG[level].need;
  hudR.innerHTML='Lives '+'&#9679;'.repeat(Math.max(0,lives))+'&#9675;'.repeat(Math.max(0,3-lives));}
function show(t1,t2,btn){ovT.innerHTML=t1;ovX.innerHTML=t2;ovB.textContent=btn;ov.classList.remove('hidden');}
function hide(){ov.classList.add('hidden');}
function drawPlayer(px,py,blink){
  if(blink&&Math.floor(tick/6)%2)return;
  var s=DPR;g.save();g.translate(px,py);
  var gl=g.createRadialGradient(0,0,2*s,0,0,46*s);gl.addColorStop(0,'rgba(140,190,255,0.18)');gl.addColorStop(1,'rgba(140,190,255,0)');
  g.fillStyle=gl;g.beginPath();g.arc(0,0,46*s,0,6.283);g.fill();
  var dg=g.createRadialGradient(-6*s,-16*s,2*s,0,-10*s,16*s);dg.addColorStop(0,'#dff0ff');dg.addColorStop(0.5,'#7fb2e6');dg.addColorStop(1,'#274766');
  g.beginPath();g.ellipse(0,-9*s,14*s,11*s,0,3.34,6.08);g.fillStyle=dg;g.fill();
  var sg=g.createLinearGradient(0,-8*s,0,12*s);sg.addColorStop(0,'#c3ccd8');sg.addColorStop(0.45,'#79879a');sg.addColorStop(1,'#2c3543');
  g.beginPath();g.ellipse(0,0,28*s,10*s,0,0,6.283);g.fillStyle=sg;g.fill();
  g.beginPath();g.ellipse(0,2.5*s,28*s,8.5*s,0,0,3.14159);g.fillStyle='rgba(10,14,20,0.55)';g.fill();
  for(var i=-2;i<=2;i++){var lx=i*11*s;g.beginPath();g.arc(lx,4.5*s,1.8*s,0,6.283);
    g.fillStyle=(Math.floor(tick/10)+i)%3===0?'#9fdcb4':'#3d5a49';g.fill();}
  g.restore();}
function drawFoe(f){var s=DPR;g.save();g.translate(f.x,f.y);
  var gl=g.createRadialGradient(0,0,2*s,0,0,36*s);gl.addColorStop(0,'rgba(255,90,80,0.14)');gl.addColorStop(1,'rgba(255,90,80,0)');
  g.fillStyle=gl;g.beginPath();g.arc(0,0,36*s,0,6.283);g.fill();
  var dg=g.createRadialGradient(-4*s,-12*s,1*s,0,-7*s,12*s);dg.addColorStop(0,'#ffd9d0');dg.addColorStop(0.5,'#c96a5a');dg.addColorStop(1,'#5a1f18');
  g.beginPath();g.ellipse(0,-7*s,10*s,8*s,0,3.34,6.08);g.fillStyle=dg;g.fill();
  var sg=g.createLinearGradient(0,-6*s,0,9*s);sg.addColorStop(0,'#b9a9a4');sg.addColorStop(0.45,'#8a6d66');sg.addColorStop(1,'#33211d');
  g.beginPath();g.ellipse(0,0,21*s,7.5*s,0,0,6.283);g.fillStyle=sg;g.fill();
  for(var i=-1;i<=1;i++){g.beginPath();g.arc(i*9*s,3.5*s,1.5*s,0,6.283);
    g.fillStyle=(Math.floor(tick/8)+i)%2===0?'#ff5c47':'#5a201a';g.fill();}
  g.restore();}
function boom(x,y,col){for(var i=0;i<26;i++){var a=Math.random()*6.283,v=(1+Math.random()*4)*DPR;
  parts.push({x:x,y:y,vx:Math.cos(a)*v,vy:Math.sin(a)*v,life:1,col:col});}}
addEventListener('keydown',function(e){
  if(ST==='playing'&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown',' '].indexOf(e.key)>-1)e.preventDefault();
  if(['ArrowLeft','ArrowRight','a','A','d','D'].indexOf(e.key)>-1)mouseX=null;
  keys[e.key]=true;if(e.key===' ')fireHold=true;});
addEventListener('keyup',function(e){keys[e.key]=false;if(e.key===' ')fireHold=false;});
wrap.addEventListener('mousemove',function(e){var r=wrap.getBoundingClientRect();mouseX=(e.clientX-r.left)*DPR;});
wrap.addEventListener('mousedown',function(){if(ST==='playing')fireHold=true;});
addEventListener('mouseup',function(){fireHold=false;});
wrap.addEventListener('touchmove',function(e){var r=wrap.getBoundingClientRect();mouseX=(e.touches[0].clientX-r.left)*DPR;e.preventDefault();},{passive:false});
wrap.addEventListener('touchstart',function(e){var r=wrap.getBoundingClientRect();mouseX=(e.touches[0].clientX-r.left)*DPR;fireHold=true;e.preventDefault();},{passive:false});
addEventListener('touchend',function(){fireHold=false;});
ovB.addEventListener('click',function(){
  if(ST==='idle'||ST==='dead'){reset(true);}else if(ST==='inter'){level=2;kills=0;reset(false);}else if(ST==='won'){reset(true);}
  ST='playing';hide();hud();beep(520,0.08,'sine',0.06);});
(function loop(){tick++;requestAnimationFrame(loop);
  g.clearRect(0,0,W,H);
  g.fillStyle='#000';g.fillRect(0,0,W,H);
  g.strokeStyle='rgba(120,140,165,0.05)';g.lineWidth=1;
  for(var i=1;i<5;i++){g.beginPath();g.moveTo(0,H-i*14*DPR);g.lineTo(W,H-i*14*DPR);g.stroke();}
  if(ST!=='playing'){drawPlayer(W/2,H-60*DPR,false);return;}
  var C=CFG[level];
  var spd=8*DPR,kb=(keys.ArrowLeft||keys.a||keys.A||keys.ArrowRight||keys.d||keys.D);
  if(keys.ArrowLeft||keys.a||keys.A)P.x-=spd;
  if(keys.ArrowRight||keys.d||keys.D)P.x+=spd;
  if(!kb&&mouseX!==null&&Math.abs(mouseX-P.x)>2*DPR)P.x+=(mouseX-P.x)*0.22;
  P.x=Math.max(0,Math.min(W,P.x));
  var cd=level===1?200:150;
  if(fireHold&&Date.now()-lastFire>cd){lastFire=Date.now();
    bullets.push({x:P.x,y:P.y-18*DPR,vy:-11*DPR});beep(880,0.05,'square',0.03);}
  if(Date.now()-lastSpawn>C.spawn){lastSpawn=Date.now();
    var fromLeft=Math.random()<0.5,sp=C.speed[0]+Math.random()*(C.speed[1]-C.speed[0]);
    foes.push({x:fromLeft?-40*DPR:W+40*DPR,y:(0.12+Math.random()*0.5)*H,
      vx:(fromLeft?1:-1)*sp*DPR,hp:C.hp,ph:Math.random()*6.28,fire:Date.now()+800+Math.random()*1500});}
  for(var i=foes.length-1;i>=0;i--){var f=foes[i];f.x+=f.vx;
    if(C.zig)f.y+=Math.sin(tick*0.05+f.ph)*1.8*DPR;
    if(C.fires&&Date.now()>f.fire&&f.x>30*DPR&&f.x<W-30*DPR){f.fire=Date.now()+C.frate[0]+Math.random()*C.frate[1];
      fbul.push({x:f.x,y:f.y+10*DPR,vx:0,vy:C.fspd*DPR});
      beep(300,0.07,'sawtooth',0.025);}
    if(f.x<-60*DPR||f.x>W+60*DPR)foes.splice(i,1);}
  for(var i=bullets.length-1;i>=0;i--){var b=bullets[i];b.y+=b.vy;
    if(b.y<-20)bullets.splice(i,1);else{
      for(var j=foes.length-1;j>=0;j--){var f=foes[j],dx=b.x-f.x,dy=b.y-f.y;
        if(dx*dx+dy*dy<(24*DPR)*(24*DPR)){bullets.splice(i,1);f.hp--;
          if(f.hp<=0){boom(f.x,f.y,'255,120,90');foes.splice(j,1);kills++;score+=C.pts;beep(160,0.18,'sawtooth',0.06);}
          else beep(420,0.05,'square',0.03);
          break;}}}}
  for(var i=fbul.length-1;i>=0;i--){var b=fbul[i];b.x+=b.vx;b.y+=b.vy;
    if(b.x<0||b.x>W||b.y>H)fbul.splice(i,1);
    else if(inv<=0){var dx=b.x-P.x,dy=b.y-P.y;
      if(dx*dx+dy*dy<(26*DPR)*(26*DPR)){fbul.splice(i,1);hit();}}}
  if(inv<=0)for(var j=0;j<foes.length;j++){var f=foes[j],dx=f.x-P.x,dy=f.y-P.y;
    if(dx*dx+dy*dy<(40*DPR)*(40*DPR)){boom(f.x,f.y,'255,120,90');foes.splice(j,1);hit();break;}}
  if(inv>0)inv--;
  for(var i=parts.length-1;i>=0;i--){var p=parts[i];p.x+=p.vx;p.y+=p.vy;p.vy+=0.05*DPR;p.life-=0.025;
    if(p.life<=0)parts.splice(i,1);}
  if(kills>=C.need){
    if(level===1){ST='inter';show('Level 2 — Deep Space','They are faster. They zig-zag. <b style="color:#ff8a76">They rain bombs.</b> Watch the sky and keep moving, commander.','Engage Level 2');}
    else{ST='won';show('Transmission complete','Sector cleared. Final score: <b style="color:#9fdcb4">'+score+'</b><br>The real signals are below — scroll down.','Fly again');}
    hud();}
  for(var i=0;i<parts.length;i++){var p=parts[i];
    g.beginPath();g.arc(p.x,p.y,2*DPR*p.life,0,6.283);g.fillStyle='rgba('+p.col+','+p.life+')';g.fill();}
  g.fillStyle='#cfe6ff';for(var i=0;i<bullets.length;i++){var b=bullets[i];g.fillRect(b.x-1.2*DPR,b.y-7*DPR,2.4*DPR,10*DPR);}
  g.fillStyle='#ff7a5c';for(var i=0;i<fbul.length;i++){var b=fbul[i];g.beginPath();g.arc(b.x,b.y,2.6*DPR,0,6.283);g.fill();}
  for(var i=0;i<foes.length;i++)drawFoe(foes[i]);
  drawPlayer(P.x,P.y,inv>0);
  hud();
})();
function hit(){lives--;inv=90;boom(P.x,P.y,'150,200,255');beep(110,0.35,'sawtooth',0.08);hud();
  if(lives<=0){ST='dead';show('Craft lost','Final score: <b>'+score+'</b> · Level '+level+'<br>The monolith remains. Try again.','Re-engage');}}
})();

fetch('/health').then(function(r){return r.json()}).then(function(j){
  if(j&&j.status==='ok')document.getElementById('sysstat').textContent='System operational · live feed';
}).catch(function(){});
</script>
</body></html>"""


@app.route("/")
def home():
    return _LANDING_HTML


def _price_amount(p):
    return p.lstrip("$") if isinstance(p, str) else str(p)


# ---- OpenAPI 3.1 discovery doc (x402scan reads /openapi.json first) --------
OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "Kraken Crypto Signals",
        "version": "1.0.0",
        "description": "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) for AI agents over x402 on Base.",
        "contact": {
            "name": "Kraken Crypto Signals (Nikoble1926)",
            "url": "https://github.com/Nikoble1926/kraken-crypto-signals",
        },
    },
    "servers": [{"url": "https://signals.nsgoods.org"}],
    "components": {
        "securitySchemes": {
            "x402Payment": {
                "type": "apiKey",
                "in": "header",
                "name": "X-PAYMENT",
                "description": "x402 payment payload (USDC on Base).",
            }
        },
        "schemas": {
            "Signal": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "example": "BTC/USD"},
                    "signal": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence": {"type": "number", "format": "float"},
                    "timeframe": {"type": "string"},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer": {"type": "string"},
                },
            },
            "TopSignals": {
                "type": "object",
                "properties": {
                    "scanned_pairs": {"type": "integer"},
                    "actionable": {"type": "integer"},
                    "top": {"type": "array",
                            "items": {"$ref": "#/components/schemas/Signal"}},
                    "universe": {"type": "array", "items": {"type": "string"}},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer": {"type": "string"},
                },
            },
        },
    },
    "paths": {
        "/signals": {
            "get": {
                "operationId": "getSignal",
                "summary": "Trading signal for one pair (BUY/SELL/HOLD + confidence)",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}}
                ],
                "responses": {
                    "200": {"description": "Trading signal",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Signal"}}}},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Market data unavailable (no charge)."},
                },
            }
        },
        "/signals/top": {
            "get": {
                "operationId": "getTopSignals",
                "summary": "Market scan: strongest BUY/SELL signals across major pairs",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(TOP_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "description": "How many top signals to return (1-10, default 3).",
                     "schema": {"type": "integer", "default": _TOP_DEFAULT_LIMIT,
                                "minimum": 1, "maximum": 10}}
                ],
                "responses": {
                    "200": {"description": "Strongest signals",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/TopSignals"}}}},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Market data unavailable (no charge)."},
                },
            }
        },
        "/signals/preview": {
            "get": {
                "operationId": "getSignalPreview",
                "summary": "Free sample signal (BTC/USD direction, no payment)",
                "security": [],
                "responses": {
                    "200": {"description": "Free preview signal"},
                    "503": {"description": "Market data unavailable."},
                },
            }
        },
        "/history": {
            "get": {
                "operationId": "getHistory",
                "summary": "Historical revealed signal snapshots + verifiable hashes",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(HISTORY_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "description": "How many past snapshots to return (1-100, default 20).",
                     "schema": {"type": "integer", "default": 20,
                                "minimum": 1, "maximum": 100}}
                ],
                "responses": {
                    "200": {"description": "Revealed signal snapshots (commit-reveal hash chain)."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Not enough history yet (no charge)."},
                },
            }
        },
        "/regime/changed": {
            "get": {
                "operationId": "getRegimeChanged",
                "summary": "Market regime (RISK_ON/RISK_OFF) + change-since flag",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(REGIME_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "since", "in": "query", "required": False,
                     "description": "Regime you last saw (e.g. RISK_ON); response reports if it changed.",
                     "schema": {"type": "string", "example": "RISK_ON"}}
                ],
                "responses": {
                    "200": {"description": "Current regime + change indicator."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Regime feed unavailable (no charge)."},
                },
            }
        },
        "/backtest": {
            "get": {
                "operationId": "getBacktest",
                "summary": "Walk-forward replay: win-rate + avg forward return per signal",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(BACKTEST_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}},
                    {"name": "forward", "in": "query", "required": False,
                     "description": "Forward window in 1m bars to score each signal (5-120, default 30).",
                     "schema": {"type": "integer", "default": 30,
                                "minimum": 5, "maximum": 120}},
                    {"name": "step", "in": "query", "required": False,
                     "description": "Walk-forward step in 1m bars (1-15, default 5).",
                     "schema": {"type": "integer", "default": 5,
                                "minimum": 1, "maximum": 15}},
                ],
                "responses": {
                    "200": {"description": "Recent walk-forward replay summary."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Backtest unavailable (no charge)."},
                },
            }
        },
        "/indicators": {
            "get": {
                "operationId": "getIndicators",
                "summary": "Atomic technical indicators (RSI, EMA, MACD, ATR, Bollinger, volume)",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(INDICATORS_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}},
                    {"name": "tf", "in": "query", "required": False,
                     "description": "Candle timeframe.",
                     "schema": {"type": "string", "default": "1h",
                                "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]}},
                    {"name": "name", "in": "query", "required": False,
                     "description": "Optional: return only one indicator (e.g. rsi14, ema50, macd_hist).",
                     "schema": {"type": "string", "example": "rsi14"}},
                ],
                "responses": {
                    "200": {"description": "Indicator snapshot for the requested pair/timeframe."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Indicators unavailable (no charge)."},
                },
            }
        },
    },
}


@app.route("/openapi.json")
def openapi_spec():
    return jsonify(OPENAPI_SPEC)


_FAVICON_ICO = base64.b64decode(
    "AAABAAIAEBAAAAAAIACwAgAAJgAAACAgAAAAACAACQEAANYCAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIBgAAAB/z/2EAAAJ3SURBVHicZZO/i1xVGIaf9zvn3rl3d3Zms7sBEYmFP0CIwopCGhFt1MK/IKWNna2NlWBhk8LCJtj4J1gYwXQKiuCCWggSFDSSZCcbd5OZuXPnnPNZzKxuyIHD27zvez6+wyOAjXNPvSGrPsD9ErgAY30M8PUFCsiRvvOy/HB278ZXGu49/aqXcA2pwYtjEmVlFzAvTi0R9F+JIxPunSy/aV7sikSD54SQzxMUxwS9w6VRzbnKSH46koTnJNF4sSsG2nd3l1n0vtDu7xHGNcpOn52XhxVbQfTFscrwRQZZdOGgfQMvgBCU6ZL2hR1ss8L7wjCKgrMZRBPE8vaU5tkx5EK53wvzEs8uLIxqcFHaSNtGvnh+h+1KvP+E+Piw47O3n+Sx1x5nfnPKybU/mf04sYjAk2MbkQufv04c1YzffY509VeG39+h3WvYKoW2NsbvXaStAhtvXSAd90y/uf3/6wBhq0JNIAwrNAik4hQgORSHPOnweSIddfgsgXi4wNfOU82nQYcC2CBAtJWGVTQ+VLBOeFnp+doYBLFrxqaJfLLE60B5sMT7DDpbIIi7AxREvTOgbyMf/XHCKyb+niWu/7Pg7uXrVJef4cEPhyxuHBPGNRGnyGS+LEw++YX2xT3y3Y7pt7f49E5Htz3gy0nHrVRop4kiKLOELzJqY4kgQ+5k1+HVnzn/zkXuf32T/NeMOKppgd3KOMqONiLzn44o0yVU5iAz8ANJ8uKp2t6k++0YggijipSdxoQDuThWG/ODCel4kSxG4X5wBiYacPcuS5VhQSwKvDSM/N5l7iUnCBAuTL6GSY/gbC7K6ntPaawk4orGR3D+F23mRMyTTFjTAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAA0ElEQVR4nGNkwAK4hdT+YxOnFHx9d4sRXQxFgFYW43MIE70tR7eLid6WozuCiZBCWgMmavj+qokoWfq4hdT+D3wIEKNIeZf3wDqAloAuDsAXgizkGoqe8JD52mdeE20O2Q4gBNB9jcy/67aVsAOINYBSMDIS4agDRh0wqB1As4IIuaxQ3uWNs+wg2wHIxe1VE1GSil9kMHijgNggpBQMeAjQxQH4Qm/gQwBbdwkd0Cr+v767xTjgIQD3Pb17R7CQZ0IXoKflDAxovWMYoGf3HACyV0Trq971hwAAAABJRU5ErkJggg=="
)


@app.route("/favicon.ico")
def favicon():
    return Response(_FAVICON_ICO, mimetype="image/x-icon")


_TRACK_RECORD_PUBLIC = os.getenv(
    "TRACK_RECORD_PUBLIC", "/root/signals_api/track_record_public.jsonl"
)


@app.route("/track-record")
def track_record():
    """Public, tamper-evident commitments to past signal snapshots (commit-reveal):
    only a hash chain is public now; the full snapshot can be revealed later to
    prove a past call without leaking live signals."""
    entries = []
    head = "GENESIS"
    try:
        with open(_TRACK_RECORD_PUBLIC) as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    entries.append(e)
                    head = e.get("h", head)
    except FileNotFoundError:
        pass
    return jsonify({
        "scheme": ("Append-only hash chain. Each h = sha256 of the full signal "
                   "snapshot (incl. a nonce). Only hashes are public now; reveal "
                   "an entry later to prove a past call. Verify: each entry's "
                   "'prev' must equal the previous entry's 'h'."),
        "count": len(entries),
        "head": head,
        "entries": entries[-50:],
    })


_YIELD_STATE = os.getenv("YIELD_STATE", "/root/signals_api/yield_agent_state.json")


@app.route("/yield-paper")
def yield_paper():
    """Paper-mode yield agent: which vault, live APY, simulated balance/ROI.
    NO real funds -- a free simulation of stablecoin yield-rotation on Base."""
    try:
        st = json.load(open(_YIELD_STATE))
    except FileNotFoundError:
        return jsonify({"mode": "paper", "status": "no data yet"})
    try:
        roi = round((st.get("balance", 0) / 1000.0 - 1) * 100, 3)
    except Exception:
        roi = None
    return jsonify({
        "mode": "paper (simulation only, NO real funds)",
        "in_protocol": st.get("project"),
        "live_apy_pct": st.get("apy"),
        "sim_balance_usd": round(st.get("balance", 0), 4),
        "roi_pct_since_start": roi,
        "rotations": st.get("rotations"),
        "started": st.get("started"),
        "last_update": st.get("last_ts"),
    })


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


_LLMS_TXT = (
    "# Kraken Crypto Signals — x402 pay-per-call API\n"
    "Machine-readable crypto market-state data for AI agents, paid per call in USDC on Base via x402. "
    "No accounts, no API keys. Every response is ECDSA-signed and verifiable.\n"
    "\n"
    "Base: https://signals.nsgoods.org\n"
    "Manifest: https://signals.nsgoods.org/.well-known/x402\n"
    "OpenAPI: https://signals.nsgoods.org/openapi.json\n"
    "Agent card (A2A): https://signals.nsgoods.org/.well-known/agent.json\n"
    "MCP bridge: https://github.com/Nikoble1926/kraken-crypto-signals/tree/main/mcp\n"
    "Quickstart (runnable demo agent, free preview + 402 challenge, ~5 min): "
    "https://github.com/Nikoble1926/kraken-crypto-signals/tree/main/examples/quickstart\n"
    "\n"
    "Paid endpoints (USDC on Base):\n"
    "- GET /signals?pair=BTC/USD   trading signal BUY/SELL/HOLD + confidence   $0.01\n"
    "- GET /signals/top            market scan, strongest signals             $0.10\n"
    "- GET /history?limit=20       revealed historical snapshots + hashes      $0.05\n"
    "- GET /regime/changed?since=RISK_ON   market regime + change flag         $0.02\n"
    "- GET /backtest?pair=BTC/USD  walk-forward win-rate + avg forward return  $0.25\n"
    "\n"
    "Free:\n"
    "- GET /signals/preview        sample BTC/USD direction\n"
    "- GET /track-record           public commit-reveal hash chain\n"
    "\n"
    "How to pay: call an endpoint; on HTTP 402, settle the x402 payment (USDC on Base) and retry. "
    "See https://x402.org. Not financial advice.\n"
)


@app.route("/llms.txt")
def llms_txt():
    return Response(_LLMS_TXT, mimetype="text/plain; charset=utf-8")


@app.route("/.well-known/agent.json")
def agent_card():
    return jsonify({
        "name": "Kraken Crypto Signals",
        "description": (
            "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) "
            "for AI agents over x402 on Base. ECDSA-signed, provable track-record, "
            "backtest proof-of-edge."
        ),
        "url": "https://signals.nsgoods.org",
        "version": "1.0.0",
        "provider": {
            "organization": "Nikoble1926",
            "url": "https://github.com/Nikoble1926/kraken-crypto-signals",
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "signals", "name": "Trading signal",
             "description": "BUY/SELL/HOLD + confidence for one Kraken */USD pair. $0.01 USDC via x402.",
             "tags": ["crypto", "trading", "x402", "signals"]},
            {"id": "signals_top", "name": "Market scan",
             "description": "Strongest BUY/SELL signals across major pairs. $0.10 USDC.",
             "tags": ["crypto", "scan", "x402"]},
            {"id": "history", "name": "Historical signals",
             "description": "Revealed commit-reveal signal snapshots + verifiable hashes. $0.05 USDC.",
             "tags": ["crypto", "history", "x402"]},
            {"id": "regime_changed", "name": "Regime change",
             "description": "RISK_ON/RISK_OFF regime + change-since flag. $0.02 USDC.",
             "tags": ["crypto", "regime", "x402"]},
            {"id": "backtest", "name": "Strategy backtest",
             "description": "Walk-forward win-rate + avg forward return per signal. $0.25 USDC.",
             "tags": ["crypto", "backtest", "x402"]},
        ],
        "x402": {
            "network": NETWORK,
            "payTo": EVM_ADDRESS,
            "manifest": "https://signals.nsgoods.org/.well-known/x402",
        },
    })


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name": "Kraken Crypto Signals",
        "description": "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) for AI agents over x402 on Base. ECDSA-signed responses, free preview endpoint, no API keys.",
        "pairs": (
            "All active Kraken */USD spot pairs (e.g. BTC/USD, ETH/USD, SOL/USD, "
            "XRP/USD, DOGE/USD). Pass any with ?pair=."
        ),
        "signer": _SIGNER_ADDRESS,
        "signature_scheme": (
            "Every /signals, /signals/top and /signals/preview response includes "
            "'signed_by' (address) and 'signature'. Verify with Ethereum "
            "personal_sign / ecrecover over the JSON body WITHOUT the signed_by & "
            "signature keys, serialized json.dumps(sort_keys=True, "
            "separators=(',',':')). recover == signed_by => authentic & untampered."
        ),
        "endpoints": [
            {"path": "/signals", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)"},
             "description": "Trading signal for one pair"},
            {"path": "/signals/top", "method": "GET", "price": TOP_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "description": "Market scan: strongest BUY/SELL signals across major pairs"},
            {"path": "/indicators", "method": "GET", "price": INDICATORS_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)",
                        "tf": "1m|5m|15m|30m|1h (default)|4h|1d",
                        "name": "optional, return only one (e.g. rsi14, ema50, macd_hist)"},
             "description": "Atomic technical indicators (RSI, EMA20/50/200, MACD, ATR, Bollinger, volume)"},
            {"path": "/regime/changed", "method": "GET", "price": REGIME_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"since": "previous regime label, e.g. RISK_ON"},
             "description": "Market regime RISK_ON/RISK_OFF + change-since flag"},
            {"path": "/history", "method": "GET", "price": HISTORY_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"limit": "1-100 (default 20)"},
             "description": "Revealed historical signal snapshots + verifiable hashes"},
            {"path": "/backtest", "method": "GET", "price": BACKTEST_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "description": "Walk-forward replay: win-rate + avg forward return per signal"},
        ],
        "disclaimer": _DISCLAIMER,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4021, debug=False)
