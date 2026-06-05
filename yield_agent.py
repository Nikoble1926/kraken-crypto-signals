#!/usr/bin/env python3
"""Paper-mode stablecoin yield-rotation agent (Base / USDC). Reads LIVE APYs from
DeFiLlama (free), applies guardrails, SIMULATES rotating a virtual balance to the
best audited USDC vault. NO real funds, NO keys, NO execution."""
import json
import os
import urllib.request
from datetime import datetime, timezone

STATE = "/root/signals_api/yield_agent_state.json"
LOG = "/root/signals_api/yield_agent_log.jsonl"
CHAIN = "Base"
ALLOWLIST = {"aave-v3", "compound-v3", "morpho-blue", "moonwell", "fluid", "spark"}
MIN_TVL = 10_000_000
MAX_APY = 30.0
ROTATE_EDGE_PP = 1.0
GAS_COST_USD = 2.0
START_BALANCE = 1000.0
STABLES = {"USDC", "USDT", "DAI", "USDBC", "EURC", "GHO", "USDS"}


def fetch_pools():
    req = urllib.request.Request("https://yields.llama.fi/pools",
                                 headers={"User-Agent": "yield-agent"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)["data"]


def candidates(pools):
    out = []
    for p in pools:
        if p.get("chain") != CHAIN:
            continue
        sym = (p.get("symbol") or "").upper()
        if "USDC" not in sym:
            continue
        if "-" in sym and not all(s in STABLES for s in sym.split("-")):
            continue
        if (p.get("project") or "").lower() not in ALLOWLIST:
            continue
        if (p.get("tvlUsd") or 0) < MIN_TVL:
            continue
        apy = p.get("apy")
        if apy is None or float(apy) > MAX_APY or float(apy) < 0:
            continue
        out.append({"project": p["project"], "symbol": sym, "pool": p["pool"],
                    "tvlUsd": p["tvlUsd"], "apy": round(float(apy), 2)})
    out.sort(key=lambda x: x["apy"], reverse=True)
    return out


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"balance": START_BALANCE, "pool": None, "project": None,
            "apy": 0.0, "last_ts": None, "rotations": 0, "started": None}


def main():
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    cands = candidates(fetch_pools())
    st = load_state()
    if st["started"] is None:
        st["started"] = ts
    if st["last_ts"] and st["pool"]:
        dt_years = (now - datetime.fromisoformat(st["last_ts"])).total_seconds() / (365 * 24 * 3600)
        st["balance"] *= (1 + st["apy"] / 100.0 * dt_years)
    action = "hold"
    best = cands[0] if cands else None
    if best:
        cur_apy = (st["apy"] if st["pool"] == best["pool"]
                   else next((c["apy"] for c in cands if c["pool"] == st["pool"]), 0.0))
        if st["pool"] is None:
            st.update({"pool": best["pool"], "project": best["project"], "apy": best["apy"]})
            action = "enter"
        elif best["pool"] != st["pool"]:
            edge = best["apy"] - cur_apy
            if edge >= ROTATE_EDGE_PP and (edge / 100.0 * st["balance"]) > GAS_COST_USD:
                st["balance"] -= GAS_COST_USD
                st.update({"pool": best["pool"], "project": best["project"],
                           "apy": best["apy"], "rotations": st["rotations"] + 1})
                action = "rotate"
            else:
                st["apy"] = cur_apy
        else:
            st["apy"] = best["apy"]
    st["last_ts"] = ts
    json.dump(st, open(STATE, "w"))
    rec = {"ts": ts, "action": action, "in": st["project"], "apy": st["apy"],
           "balance": round(st["balance"], 4), "rotations": st["rotations"],
           "top3": [{"p": c["project"], "apy": c["apy"]} for c in cands[:3]]}
    open(LOG, "a").write(json.dumps(rec) + "\n")
    print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
