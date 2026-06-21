# Pre-registration — Regime-aware signals experiment

**Date:** 2026-06-20 (locked before any test was run).
**Purpose:** Publicly time-stamp the hypothesis, guardrails and adoption threshold BEFORE running the test, so the rule cannot be moved after seeing results. The GitHub commit time is the proof of priority; `normative_sha256` below is the tamper-evident digest of the normative block.
**normative_sha256:** sha256:9e1399fea90aaf44bfa2a0ae58b123f81d73b905828adced0ffe6acfd555ffa9
**Reproduce:** extract the bytes strictly between the NORMATIVE-START and NORMATIVE-END markers (excluding the marker lines) and run `sha256sum`.

<!-- NORMATIVE-START -->
Hypothesis (locked, single rule): crypto signals are more reliable in calm/trending markets and noisier in volatile ones. Rule: in a VOLATILE regime, suppress the signal (force HOLD); in CALM/TRENDING, keep it as-is.
Guardrails (fixed before testing): (1) out-of-sample / walk-forward — judged only on data that did not shape the rule; (2) must beat THREE baselines: the plain signal, always-buy, and chance; (3) must win at BOTH horizons (4h & 24h), with a downturn phase in-sample, sufficient samples; (4) caveats always reported (effective sample size, autocorrelation, market phase).
Adoption threshold (predefined): ADOPT only if materially AND repeatably better (not ~0.5%, not within noise given autocorrelation) at both horizons and in >1 regime. Otherwise REJECT (default — the plain signal is more robust).
Execution trigger: only when >=2-3 months of data across varied regimes exist AND after 2026-07-05. Until then: queued, unrun.
This is research only; it does not change any live service unless it passes, and only after the freeze.
<!-- NORMATIVE-END -->
