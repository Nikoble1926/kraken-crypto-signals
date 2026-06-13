"""
Extra paid x402 endpoints for the Kraken Crypto Signals API — kept in a separate,
self-contained module so it can be added with ZERO risk to the existing service.

signals_api.py loads this via a try/except: if anything here fails, it is skipped
and the original API runs unchanged.

Adds three paid routes (USDC on Base, x402):
  GET /history          ($0.05) — revealed historical signal snapshots (commit-reveal),
                                   with verifiable hashes; the latest snapshot is withheld.
  GET /regime/changed   ($0.02) — current market regime (RISK_ON/RISK_OFF) from the
                                   provable regime chain + whether it changed since ?since=.
  GET /backtest         ($0.25) — walk-forward replay of the live multi-timeframe
                                   strategy over recent 1m history; win-rate and
                                   average forward return per BUY/SELL signal.

Data sources (already present on the server):
  - /root/signals_api/track_record_private.jsonl  (revealed signals; commit-reveal)
  - https://regime.nsgoods.org/provable/head        (regime chain head)
  - DataEngine / StrategyEngine (passed in via engine=, strategy=)
"""

import os
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

from flask import jsonify, request


def register(app, routes, *, EVM_ADDRESS, NETWORK, signed, unavailable_body,
             DISCLAIMER, RouteConfig, PaymentOption,
             declare_discovery_extension, OutputConfig,
             engine=None, strategy=None):
    """Mutate `routes` (so the x402 middleware paywalls the new routes) and attach
    the Flask handlers. Call this BEFORE payment_middleware(app, routes=routes, ...).

    engine= and strategy= are optional. If absent, /backtest will respond 503 with
    unavailable_body (so x402 does NOT charge). The other two endpoints don't need them.
    """

    HISTORY_PRICE = os.getenv("HISTORY_PRICE", "$0.05")
    REGIME_PRICE = os.getenv("REGIME_PRICE", "$0.02")
    BACKTEST_PRICE = os.getenv("BACKTEST_PRICE", "$0.25")
    REGIME_HEAD_URL = os.getenv("REGIME_HEAD_URL",
                                "https://regime.nsgoods.org/provable/head")
    TRACK_RECORD_PRIVATE = os.getenv(
        "TRACK_RECORD_PRIVATE", "/root/signals_api/track_record_private.jsonl")

    # ---- regime head fetch (cached 30s) ------------------------------------
    _ttl = 30
    _lock = threading.Lock()
    _cache = {}

    def fetch_regime_head():
        now = time.time()
        c = _cache.get("head")
        if c and c[0] > now:
            return c[1]
        with _lock:
            c = _cache.get("head")
            if c and c[0] > now:
                return c[1]
            req = urllib.request.Request(REGIME_HEAD_URL,
                                         headers={"User-Agent": "signals-api"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            head = data.get("head", data) or {}
            out = {
                "regime": head.get("regime"),
                "seq": head.get("seq", data.get("count")),
                "coin": head.get("coin"),
                "ts_utc": head.get("ts_utc"),
            }
            _cache["head"] = (now + _ttl, out)
            return out

    # ---- backtest cache (60s per (pair, forward, step)) --------------------
    _bt_ttl = 60
    _bt_lock = threading.Lock()
    _bt_cache = {}

    # ---- Bazaar discovery extensions ---------------------------------------
    hist_ext = declare_discovery_extension(
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "default": 20,
                          "description": "How many past snapshots (1-100)."},
            },
            "required": [],
        },
        output=OutputConfig(
            example={
                "count": 1,
                "history": [{
                    "seq": 12, "ts": "2026-06-10T18:00:00+00:00",
                    "h": "d9ed7d97...", "prev": "1a9521f9...",
                    "signals": [{"pair": "BTC/USD", "signal": "BUY",
                                 "confidence": 0.62}],
                }],
                "generated_at": "2026-06-13T20:00:00+00:00",
            },
            schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "history": {"type": "array", "items": {"type": "object"}},
                    "generated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["count", "history", "generated_at"],
            },
        ),
    )
    hist_ext["bazaar"]["info"]["input"]["method"] = "GET"

    regime_ext = declare_discovery_extension(
        input_schema={
            "type": "object",
            "properties": {
                "since": {"type": "string",
                          "description": "Regime you last saw (e.g. RISK_ON); "
                                         "response says if it changed."},
            },
            "required": [],
        },
        output=OutputConfig(
            example={
                "current_regime": "RISK_OFF", "since": "RISK_ON", "changed": True,
                "seq": 444, "coin": "BNB", "ts_utc": "2026-06-12T19:00:02+00:00",
                "generated_at": "2026-06-13T20:00:00+00:00",
            },
            schema={
                "type": "object",
                "properties": {
                    "current_regime": {"type": "string"},
                    "since": {"type": ["string", "null"]},
                    "changed": {"type": ["boolean", "null"]},
                    "seq": {"type": ["integer", "null"]},
                    "coin": {"type": ["string", "null"]},
                    "ts_utc": {"type": ["string", "null"]},
                    "generated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["current_regime", "generated_at"],
            },
        ),
    )
    regime_ext["bazaar"]["info"]["input"]["method"] = "GET"

    bt_ext = declare_discovery_extension(
        input_schema={
            "type": "object",
            "properties": {
                "pair": {"type": "string", "default": "BTC/USD",
                         "description": "Kraken */USD pair, e.g. BTC/USD, ETH/USD."},
                "forward": {"type": "integer", "minimum": 5, "maximum": 120,
                            "default": 30,
                            "description": "Forward window in 1m bars to score each signal "
                                           "against (5-120)."},
                "step": {"type": "integer", "minimum": 1, "maximum": 15, "default": 5,
                         "description": "Walk-forward step in 1m bars (1-15). Smaller = "
                                        "denser sample but slower."},
            },
            "required": [],
        },
        output=OutputConfig(
            example={
                "pair": "BTC/USD",
                "forward_minutes": 30,
                "step_minutes": 5,
                "window_hours": 12.0,
                "signals_evaluated": 14,
                "win_rate_pct": 64.3,
                "avg_return_pct": 0.18,
                "best_pct": 1.42,
                "worst_pct": -0.71,
                "sample": [{
                    "ts": "2026-06-13T18:25:00", "signal": "BUY",
                    "entry": 64120.4, "future": 64235.1, "return_pct": 0.179,
                }],
                "generated_at": "2026-06-13T21:00:00+00:00",
            },
            schema={
                "type": "object",
                "properties": {
                    "pair": {"type": "string"},
                    "forward_minutes": {"type": "integer"},
                    "step_minutes": {"type": "integer"},
                    "window_hours": {"type": "number"},
                    "signals_evaluated": {"type": "integer"},
                    "win_rate_pct": {"type": ["number", "null"]},
                    "avg_return_pct": {"type": ["number", "null"]},
                    "best_pct": {"type": ["number", "null"]},
                    "worst_pct": {"type": ["number", "null"]},
                    "sample": {"type": "array", "items": {"type": "object"}},
                    "generated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["pair", "signals_evaluated", "generated_at"],
            },
        ),
    )
    bt_ext["bazaar"]["info"]["input"]["method"] = "GET"

    # ---- register paid routes with the x402 middleware ---------------------
    routes["GET /history"] = RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS,
                               price=HISTORY_PRICE, network=NETWORK)],
        mime_type="application/json",
        description="Historical revealed signal snapshots (commit-reveal hash chain): "
                    "past BUY/SELL/HOLD + confidence across major pairs, each with its "
                    "verifiable hash. Bulk pay-per-call data for AI agents (USDC on Base).",
        extensions=hist_ext,
    )
    routes["GET /regime/changed"] = RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS,
                               price=REGIME_PRICE, network=NETWORK)],
        mime_type="application/json",
        description="Market-regime state (RISK_ON / RISK_OFF) from a provable chain, plus "
                    "whether it changed since the regime you last saw. Cheap poll endpoint "
                    "for AI agents to react to regime flips (USDC on Base).",
        extensions=regime_ext,
    )
    routes["GET /backtest"] = RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS,
                               price=BACKTEST_PRICE, network=NETWORK)],
        mime_type="application/json",
        description="Walk-forward replay of the live multi-timeframe strategy over "
                    "recent 1m history: win-rate and average forward return per "
                    "BUY/SELL signal. Lets an agent measure recent edge before "
                    "subscribing to the live signal stream (USDC on Base).",
        extensions=bt_ext,
    )

    # ---- handlers ----------------------------------------------------------
    @app.route("/history")
    def history():
        try:
            limit = int(request.args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(100, limit))

        entries = []
        try:
            with open(TRACK_RECORD_PRIVATE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except FileNotFoundError:
            entries = []

        if len(entries) < 2:
            return jsonify(unavailable_body("not enough history yet")), 503

        revealed = entries[:-1][-limit:]   # exclude latest snapshot, take last `limit`
        out = [{
            "seq": e.get("seq"), "ts": e.get("ts"),
            "h": e.get("h"), "prev": e.get("prev"),
            "signals": e.get("signals"),
        } for e in revealed]

        return jsonify(signed({
            "count": len(out),
            "note": ("Revealed historical signal snapshots (commit-reveal). The most "
                     "recent snapshot is withheld to protect the live edge. Verify each "
                     "entry's 'h' against /track-record."),
            "history": out,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": DISCLAIMER,
        }))

    @app.route("/regime/changed")
    def regime_changed():
        try:
            head = fetch_regime_head()
        except Exception as e:
            return jsonify(unavailable_body(
                "regime feed: {}".format(type(e).__name__))), 503

        since = request.args.get("since")
        current = head.get("regime")
        changed = (str(since).upper() != str(current).upper()) if since else None

        return jsonify(signed({
            "current_regime": current,
            "since": str(since).upper() if since else None,
            "changed": changed,
            "seq": head.get("seq"),
            "coin": head.get("coin"),
            "ts_utc": head.get("ts_utc"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": DISCLAIMER,
        }))

    @app.route("/backtest")
    def backtest():
        if engine is None or strategy is None:
            return jsonify(unavailable_body(
                "backtest engine not wired (engine/strategy missing)")), 503

        # ---- params ----
        try:
            pair = (request.args.get("pair", "BTC/USD") or "BTC/USD").upper().replace("-", "/")
        except Exception:
            pair = "BTC/USD"
        try:
            forward = int(request.args.get("forward", 30))
        except (TypeError, ValueError):
            forward = 30
        forward = max(5, min(120, forward))
        try:
            step = int(request.args.get("step", 5))
        except (TypeError, ValueError):
            step = 5
        step = max(1, min(15, step))

        cache_key = (pair, forward, step)
        now = time.time()
        cached = _bt_cache.get(cache_key)
        if cached and cached[0] > now:
            return jsonify(cached[1])

        try:
            d1 = engine.get_candles(pair, "1m", limit=720)
            d5 = engine.get_candles(pair, "5m", limit=720)
            d15 = engine.get_candles(pair, "15m", limit=720)
        except Exception as e:
            return jsonify(unavailable_body(
                "candle fetch failed: {}".format(type(e).__name__))), 503

        try:
            n1 = len(d1)
            if n1 < 120 + forward + 2:
                return jsonify(unavailable_body(
                    "not enough 1m history for backtest "
                    "(have {}, need {})".format(n1, 120 + forward + 2))), 503

            results = []
            i = 120
            stop = n1 - forward
            while i < stop:
                t = d1["timestamp"].iloc[i]
                mtf_1 = d1.iloc[i - 120:i].reset_index(drop=True)
                d5_slice = d5[d5["timestamp"] <= t].tail(80).reset_index(drop=True)
                d15_slice = d15[d15["timestamp"] <= t].tail(80).reset_index(drop=True)

                if len(d5_slice) < 60 or len(d15_slice) < 60:
                    i += step
                    continue

                try:
                    r = strategy.analyze_multi_timeframe({
                        "1m": mtf_1, "5m": d5_slice, "15m": d15_slice,
                    })
                except Exception:
                    i += step
                    continue

                if not (isinstance(r, dict) and r.get("valid")):
                    i += step
                    continue

                sig = str(r.get("signal", "HOLD")).upper()
                if sig not in ("BUY", "SELL"):
                    i += step
                    continue

                try:
                    entry = float(d1["close"].iloc[i])
                    future = float(d1["close"].iloc[i + forward])
                except Exception:
                    i += step
                    continue
                if entry <= 0:
                    i += step
                    continue

                raw_ret = (future - entry) / entry
                signed_ret = raw_ret if sig == "BUY" else -raw_ret

                results.append({
                    "ts": (t.isoformat() if hasattr(t, "isoformat") else str(t)),
                    "signal": sig,
                    "entry": round(entry, 6),
                    "future": round(future, 6),
                    "return_pct": round(signed_ret * 100.0, 4),
                })
                i += step

            evaluated = len(results)
            if evaluated > 0:
                wins = sum(1 for r in results if r["return_pct"] > 0)
                rets = [r["return_pct"] for r in results]
                avg_ret = sum(rets) / evaluated
                best = max(rets)
                worst = min(rets)
                win_rate = round(100.0 * wins / evaluated, 2)
            else:
                avg_ret = None
                best = None
                worst = None
                win_rate = None

            window_hours = round(n1 / 60.0, 2)
            body = signed({
                "pair": pair,
                "forward_minutes": forward,
                "step_minutes": step,
                "window_hours": window_hours,
                "candles_window_1m": n1,
                "signals_evaluated": evaluated,
                "win_rate_pct": win_rate,
                "avg_return_pct": (round(avg_ret, 4) if avg_ret is not None else None),
                "best_pct": (round(best, 4) if best is not None else None),
                "worst_pct": (round(worst, 4) if worst is not None else None),
                "sample": results[:20],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": ("Walk-forward replay of the live multi-timeframe strategy over "
                         "the most recent ~hours of 1m Kraken history. Recent only "
                         "(not long-horizon). Past performance is not predictive of "
                         "future results."),
                "disclaimer": DISCLAIMER,
            })

            _bt_cache[cache_key] = (now + _bt_ttl, body)
            return jsonify(body)
        except Exception as e:
            return jsonify(unavailable_body(
                "backtest failed: {}".format(type(e).__name__))), 503

    return ["GET /history", "GET /regime/changed", "GET /backtest"]
