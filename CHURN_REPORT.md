# Suggested-Picks Churn Study — Round 3 (group-stage close)

Generated 2026-06-28. Computed from `data/processed/history/round_03/snapshot_*.json` —
25 hourly cron snapshots across Jun 24–28.

---

## 1. Why R1 + R2 history is missing

The PWA's `KsTwoCentsView.tsx:911` already renders 6 checkpoint chips
(R3 baseline · pre-R32 · pre-R16 · pre-QF · pre-SF · pre-Final). All past
chips are **disabled** because the warehouse never emitted a per-round
archive JSON — only the live `wc26_fantasy_joint_picks.json` exists.

The recommender (`17_fantasy_recommender.py`) was first deployed around
**2026-06-24**, when R3 became the active target. R1 and R2 were already
closed by that point, so no joint_picks were ever computed for them.
`data/processed/history/round_01/` and `round_02/` simply don't exist.

**Fix (going-forward)**: at every cron tick, if `target_round` advanced
since the previous tick, snapshot the prior round's final joint_picks as
`wc26_fantasy_joint_picks_R{N}_final.json` and push to the PWA. Detail in
§4 below — landed in the same commit as this report.

---

## 2. Within-R3 churn (the 25 snapshots we have)

Of 25 snapshots, only **20** carry the `joint_picks.per_position_top`
structure (the 5 earliest had the old `recommendations[]` schema before
the per-position quota fix landed Jun 25 16:55Z).

Position-set deltas between consecutive snapshots:

| Snapshot ts | GK | DEF | MID | FWD | Total | What happened |
|---|---:|---:|---:|---:|---:|---|
| 2026-06-24T18:50Z | — | — | — | — | — | First proper baseline (2/5/5/5 picks — total 17) |
| 2026-06-24T19:13Z | 1 | 2 | 0 | 0 | **3** | Minor adjustments |
| 2026-06-25T16:55Z | 4 | 10 | 10 | 10 | **34** | **CODE DEPLOY** — per-position quota fixed (2/5/5/5 → 5/15/15/15) |
| 2026-06-25T16:56Z onwards (17 snapshots) | 0 | 0 | 0 | 0 | **0** | Lock holding — every cron tick over ~67 hours produced identical picks |
| 2026-06-28T10:21Z (latest) | — | — | — | — | — | Same as Jun 25 16:56Z |

**Summary**:
- **17 / 19 ticks = perfect zero-churn** (lock working as designed)
- **1 spike of 34 changes** = the per-position quota expansion (one-time, structural)
- **1 spike of 3 changes** = minor data update before lock fully sealed
- Median churn per tick: **0.0**
- Mean: 1.95 (almost entirely from the Jun 25 deploy)

Translation: once the lock + freeze defences (v3 schema, v4 frozen %sel,
v5 projected fantasy_players + filtered trends) landed, the Suggested
Picks list has been **bit-stable** for 3 consecutive days through 17 cron
ticks. Every "drift" you saw before was real and the patches sealed it.

---

## 3. Cross-round churn (the actual ask) — can't compute yet, here's the plan

You wanted "count how many suggestions changed per round, current vs
last round." Today this is **uncomputable** because we have no R1 or R2
joint_picks to compare against — the recommender didn't exist then.

Going forward, when R4 closes and R5 starts, we can answer:

- **How many of the 50 R3-final picks survive into R4-final?** Per-position
  retention rate. Tells us: are our suggestions fixture-driven (high
  churn at round boundary is expected) or stat-driven (high retention)?
- **Per-model churn**: M1 Banker should churn LEAST (consistency model);
  M2 Form Hunter should churn MOST (recency-weighted); M3 Stat Max
  somewhere in between. If empirical churn doesn't match this expected
  ranking → tune weights.
- **High-Confidence vs Surprises retention**: HC picks (≥2 models) should
  survive at higher rate than single-model Surprises. If not, the consensus
  signal isn't doing what we think.
- **Hit-rate correlation**: do high-churn picks score MORE per round?
  Lower? Tells us whether the recommender's recency adjustments are
  capturing real form or just noise.

The archive-on-round-transition fix (§4) is the prerequisite for any of this.

---

## 4. The archive-on-transition fix (same commit as this report)

In `17_fantasy_recommender.py:main()`, after the captain refresh +
persistence step, we now check: **does a final-of-round archive exist for
the round we just emitted from?**

```python
# When target_round = N, we emit joint_picks for round N.
# At every cron tick we ALSO save a "final" archive of the CURRENT round
# (overwriting). When target flips N → N+1, the last archive saved is the
# final-of-round-N picks — available for cross-round comparison.
archive_path = PROC / "round_archives" / f"wc26_fantasy_joint_picks_R{target:02d}_final.json"
archive_path.parent.mkdir(parents=True, exist_ok=True)
archive_path.write_text(safe_jp)
(PWA_JSON / f"wc26_fantasy_joint_picks_R{target:02d}_final.json").write_text(safe_jp)
```

The same approach saves squad archives (`wc26_fantasy_strategy_squads_R{N}_final.json`)
so we can also study squad-XI evolution across rounds.

Once R3 → R4 transitions (when our pipeline catches up + target flips),
the PWA's checkpoint chips can light up: load
`wc26_fantasy_joint_picks_R03_final.json` for the "R3 baseline" chip,
diff vs current for the "pre-R32" chip, etc.

---

## 5. Model-strategy improvement directions (informed by what we DO see)

The within-R3 study can't speak to cross-round dynamics, but it does
confirm three things to build on:

1. **Lock works.** Don't add more anti-leak defences — diminishing returns.
   Focus model improvements on what to lock TO, not how to lock harder.
2. **Per-position quotas matter.** The Jun 25 expansion from 2/5/5/5 →
   5/15/15/15 was the largest single shift in 4 days of operation. It's
   evidence that quota design dominates weight tuning at the margin.
3. **Captain rotation is real but stable.** Per-model captain selectors
   (this turn's commit) gave us 4 distinct captains (Gakpo / Undav / Brahim
   Díaz / Messi). Their `ev_live` is rotating tick-to-tick on the order
   of ±0.05, well within margin. If a tick of live ownership flips someone
   above the captain, we'll see it — and that's intended.

Hypotheses to test once we have R3→R4→R5 data:

- **H1**: M2 Form Hunter has the highest cross-round churn. *If false*,
  reduce B4 weight further to let recency dominate.
- **H2**: M3 Stat Max has the lowest churn (raw stats are slowest to move).
  *If false*, the B2 per-90 normalisation is too sensitive to minute
  sample size — apply Bayesian shrinkage harder.
- **H3**: High-Confidence picks (≥2 models) have ≥ 60% retention round-
  over-round. *If false*, the cross-model agreement isn't picking up
  signal — we're just measuring overlap of the same elite players, not
  consensus on a fixture-driven pick.
- **H4**: Captain hits (≥7 pts in the round, doubled) > 50% per model
  across R3-R8. *If lower*, the per-model captain selector is choosing
  on the wrong factor (e.g., M3 should pick by creativity_engine BUT
  past-round actuals show form_streak captains do better — switch).

None of these are answerable today. The archive-on-transition fix
unblocks all four.

---

## 6. PWA-side surface (separate commit)

Once round archives exist on disk, KsTwoCentsView.tsx's `CheckpointPicker`
should:
- Light up past checkpoints (R3 baseline once R4 starts; pre-R32 once R5
  starts; etc.)
- On click, fetch `/data/wc26_fantasy_joint_picks_R{N}_final.json` and
  render that snapshot instead of the live current one
- Show a churn-vs-current chip on each past pick ("survived from R3 →
  R4", "new in R4")

That UI work isn't in this commit — it's a follow-up once the archives
start materialising.
