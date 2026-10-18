# Evaluation

This page explains how the numbers in the README are measured, what they do and don't show, and how the benchmark has changed.

## How the numbers are measured

All numbers come from `data/sample_ledger_labelled.csv`: 465 transactions, 45 of them labelled as laundering across six patterns. The file is generated, not checked in:

```bash
python scripts/generate_sample_ledger.py   # fixed seed (7), byte-identical on every machine
python scripts/evaluate.py                 # prints both pipelines side by side
```

| Pipeline | Recall | Precision | F1 | Forwarded to the AI tier |
|---|---|---|---|---|
| Original ordering | 7% | 33% | 0.11 | 9 rows (2%) |
| Current ordering | 84% | 79% | 0.82 | 48 rows (10%) |

Recall is the share of laundering rows forwarded for review instead of being dropped. It matters most here, because a dropped row is never looked at again.

### Development seed and held-out seeds

Seed 7 is the development seed. The rule thresholds, the anomaly threshold, the cycle window and tolerance, and the generator changes described below were all chosen while looking at seed 7's results, so its numbers are optimistic by construction. Seeds 11, 13, 17, 19 and 23 were not used for any of those choices. Each produces a ledger from the same generator with different random draws:

```bash
python scripts/evaluate.py --seeds 7 11 13 17 19 23
```

Measured on 2026-10-18 with nothing in the pipeline changed:

| Seed | Role | Rows | Laundering | TP | FP | FN | Recall | Precision | F1 | Forwarded |
|---|---|---|---|---|---|---|---|---|---|---|
| 7 | development | 465 | 45 | 38 | 10 | 7 | 84% | 79% | 0.82 | 48 |
| 11 | held out | 470 | 50 | 43 | 13 | 7 | 86% | 77% | 0.81 | 56 |
| 13 | held out | 466 | 46 | 40 | 7 | 6 | 87% | 85% | 0.86 | 47 |
| 17 | held out | 466 | 46 | 40 | 12 | 6 | 87% | 77% | 0.82 | 52 |
| 19 | held out | 465 | 45 | 39 | 9 | 6 | 87% | 81% | 0.84 | 48 |
| 23 | held out | 463 | 43 | 36 | 23 | 7 | 84% | 61% | 0.71 | 59 |
| **Pooled held out** | | 2,330 | 230 | 198 | 64 | 32 | **86.1%** (95% CI 81.0% to 90.0%) | **75.6%** (95% CI 70.0% to 80.4%) | 0.80 | 262 |

The intervals are Wilson score intervals on the pooled counts (198/230 for recall, 198/262 for precision).

Caught/total by pattern, pooled over the held-out seeds: structuring 50/50, sanctioned 30/30, fan_in 55/55, circular 15/15, circular_small 46/46, circular_camouflaged 2/34; clean rows forwarded 64/2,100.

What this shows and what it doesn't:

- **Every gated pattern stays at 100% on every held-out seed.** The only misses are `circular_camouflaged`, as on the development seed.
- **Precision is lower and less stable than on seed 7.** It ranges from 61% to 85%; seed 23 forwards 23 clean rows against 10 on seed 7. The development seed's 79% is not a good guide to precision.
- **Pooled recall is higher than seed 7's** only because the held-out ledgers happen to contain fewer `circular_camouflaged` rows relative to the patterns that are always caught.
- **"Held out" means new random draws, not new behaviour.** The same author wrote the generator, the patterns and the detector, so these seeds test whether the numbers survive different random data, not whether the detector finds laundering it wasn't designed for.

### Harder patterns

`generate(seed, hard=True)` appends five patterns written to get past the tells the default ledger relies on. They come from a separate random generator and are added after the default rows, so the default ledger (and every number above) is unchanged; a test pins its SHA-256 for seeds 7 and 11.

| Pattern | Laundering? | Rows per seed | What it tests |
|---|---|---|---|
| `established_pass_through` | yes | 9 | Money passed along chains of businesses that each have 20+ earlier clean transactions, so account newness gives nothing away |
| `sanctioned_name_variant` | yes | 10 | Watchlist names with changed casing, punctuation, Ltd/Limited, token order, or a one-character typo (2 of each) |
| `sanctioned_country_variant` | yes | 10 | Low-value transfers to high-risk countries written as `RU`, `ru`, `Russian Federation`, `russia`, `KP`, `DPRK`, `Korea, North`, `IR`, `Iran, Islamic Republic of`, `sy` |
| `near_miss_clean` | no | 10 | Clean parties whose names resemble watchlist entries (`Victor Boutros`, `Melli Bakery`, `Cash Tornado Car Wash`, ...) |
| `clean_country_variant` | no | 10 | Clean traffic with countries written as `GB`, `UK`, `united kingdom`, `Great Britain`, `DE`, `germany`, `US`, `usa`, `FR`, `Deutschland` |

```bash
python scripts/evaluate.py --seeds 7 11 13 17 19 23 --hard
```

Measured on 2026-10-18, nothing in the pipeline changed. This is the baseline for any later change.

| Seed | Role | Rows | Laundering | TP | FP | FN | Recall | Precision | F1 | Forwarded |
|---|---|---|---|---|---|---|---|---|---|---|
| 7 | development | 514 | 74 | 45 | 11 | 29 | 61% | 80% | 0.69 | 56 |
| 11 | held out | 519 | 79 | 38 | 11 | 41 | 48% | 78% | 0.59 | 49 |
| 13 | held out | 515 | 75 | 43 | 11 | 32 | 57% | 80% | 0.67 | 54 |
| 17 | held out | 515 | 75 | 53 | 20 | 22 | 71% | 73% | 0.72 | 73 |
| 19 | held out | 514 | 74 | 52 | 24 | 22 | 70% | 68% | 0.69 | 76 |
| 23 | held out | 512 | 72 | 49 | 26 | 23 | 68% | 65% | 0.67 | 75 |
| **Pooled held out** | | 2,575 | 375 | 235 | 92 | 140 | **62.7%** (95% CI 57.7% to 67.4%) | **71.9%** (95% CI 66.8% to 76.5%) | 0.67 | 327 |

Pooled over the held-out seeds, with the rows on which the sanctions-name rule and a country rule fired:

| Pattern | Caught | Name rule fired | Country rule fired |
|---|---|---|---|
| structuring | 50/50 | 0 | 24 |
| sanctioned | 30/30 | 0 | 30 |
| circular | 15/15 | 0 | 13 |
| circular_small | 28/46 | 0 | 0 |
| fan_in | 13/55 | 0 | 0 |
| circular_camouflaged | 0/34 | 0 | 0 |
| established_pass_through | 0/45 | 0 | 0 |
| sanctioned_name_variant | 49/50 | 45 | 0 |
| sanctioned_country_variant | 50/50 | 0 | 50 |
| near_miss_clean (false positives) | 39/50 forwarded | 0 | 0 |
| clean_country_variant (false positives) | 11/50 forwarded | 0 | 0 |
| clean (false positives) | 42/2,100 forwarded | 0 | 0 |

What the harder patterns show:

- **Laundering through established accounts is not caught at all** (0/45 held out, 0/9 on seed 7). Nothing in Tier 0 or Tier 1 looks at a chain of ordinary businesses passing money on.
- **Adding the new rows broke detection of `fan_in` and `circular_small`.** `fan_in` falls from 55/55 to 13/55 held out (11/11 to 0/11 on seed 7) and `circular_small` from 46/46 to 28/46. The behaviour model flags about the same number of rows either way (44 vs 45 on seed 7), but the new patterns bring many one-off accounts: on seed 7, accounts seen once rise from 11 of 36 to 31 of 57. Those two patterns were caught because their accounts were new and new accounts were rare. Once one-off counterparties are common, as they are in real ledgers, that signal fades.
- **Country resolution works on every format here.** The country rule fires on 50/50 country variants held out and on 0/50 clean variants. `Deutschland` is not resolved; it gets an unresolved-jurisdiction note and no flag.
- **The name rule catches 45 of 50 variants and none of the near misses.** Casing, punctuation, suffix and reordering all score 100. The typo `Viktor Bouf` scores 90.9 and is caught; `Lazarus Groop` scores 70 and is missed, because "group" is stripped as a company suffix while the misspelt "groop" is kept. No near-miss name reaches the threshold of 88, but the margin is small: `Melli Bakery` scores 81.8 and `Victor Boutros` 80.0.
- **Near-miss names still cost precision**, through the behaviour model rather than the name rule: 39 of 50 are forwarded, because they are one-off accounts.
- **Results swing a lot between seeds** (recall 48% to 71%), so a single seed says little.

### Latest run: 2026-10-17

The same steps CI runs, on Python 3.12.10 (the version CI uses) on Windows 11, in a venv installed from `requirements.lock.txt`, after ISO country matching, the retry-count fix and the watchlist cleanup:

```
$ python scripts/generate_sample_ledger.py
Wrote 465 rows to data/sample_ledger.csv

$ pytest tests/ -v
============================ 153 passed in 33.70s =============================

$ python scripts/evaluate.py --min-recall 1.0 --min-pattern-recall 1.0 --gate-exclude circular_camouflaged
                 recall  precision      F1  forwarded
original            7%        33%    0.11      9 (2%)
current            84%        79%    0.82     48 (10%)
```

The evaluation exited with code 0. The 153 tests include the 8 app tests in `tests/test_app.py`; CI installs only `requirements-dev.txt`, so it skips those and runs the other 145 (`145 passed, 1 skipped`). The numbers are the same as before those three changes, and the same as an earlier run on Python 3.14.4. None of them could move recall on this ledger: the generator writes only full English country names, all of which resolved before and after, and no generated name is on the watchlist.

The 7 false negatives are all `circular_camouflaged`; clean rows forwarded: 10 of 420.

## What catches each pattern

| Pattern | Rows | Recall (current) | Recall (original) | Caught by |
|---|---|---|---|---|
| structuring | 10 | 100% | 0% | Rule: structuring band |
| sanctioned | 6 | 100% | 0% | Rule: high-risk jurisdiction |
| fan_in | 11 | 100% | 0% | Behaviour model, no rule reaches it |
| circular | 3 | 100% | 100% | Rule: amount of $10,000 or more |
| circular_small | 7 | 100% | 0% | Behaviour model, because the accounts are new |
| circular_camouflaged | 8 | 12% (1/8) | 0% | Coincidence, see below |

**Why the original ordering failed.** The first version ran an IsolationForest on transaction amount before any rule. Structuring is defined by amounts that look ordinary, so the model dropped it, and a $500 transfer from a sanctioned country never reached the country check. Running rules first, with a review flag the model can't override, fixed it.

**`circular_small` is caught because its accounts are new.** Its accounts each transact once in the whole batch, so the behaviour model flags them as unusual. No feature encodes "this is a cycle". The same amounts routed through accounts with ordinary history would not be caught this way.

**`circular_camouflaged` tests exactly that.** It is the same ring shape, built from businesses that already trade normally in the background, spread over about ten days. One of eight rows is forwarded, and only because its amount ($9,210.86) happened to fall in the structuring band. The other seven are missed.

## The CI gate

CI runs:

```bash
python scripts/evaluate.py --min-recall 1.0 --min-pattern-recall 1.0 --gate-exclude circular_camouflaged
```

The build fails if recall on any gated pattern drops below 100%. `circular_camouflaged` is excluded because failing every build on a known, unfixed gap teaches people to ignore red builds. It is still printed in every report, so the gap stays visible.

The pytest suite and the evaluation gate catch different problems. The rule tests fail when a rule breaks even if the behaviour model happens to compensate. The evaluation gate fails when the behaviour model degrades even if every rule test still passes.

## Limits of this benchmark

The same author wrote the features the model looks at (`utils/features.py`), the rule thresholds (`utils/rules.py`) and the patterns injected into the test data (`scripts/generate_sample_ledger.py`). A 100% recall shows the pipeline catches what it was built to catch, on data shaped by the same assumptions. It is not evidence that it generalises to unseen patterns, adversarial ledgers or real production data. Treat the gated numbers as a guard against reintroducing the original bug, not as a real-world detection rate.

Two rules are not measured by this ledger at all:

- **Sanctions-name matching fires on 0 rows.** No generated sender or receiver is on `data/sanctions_list.csv` (a test enforces this, so the evaluation can't read the label off a name). The `sanctioned` pattern is caught only by the high-risk-country rule. Fuzzy name matching is covered by unit and app tests, but its recall and false-match rate on misspelled or transliterated names are not measured here.
- **Country resolution sees only clean names.** Every generated country is a full English name that resolves directly. ISO codes, aliases such as DPRK or Burma, and look-alike strings are covered by unit tests only.

Rule hits on the current ledger, by row: structuring band 12, reporting threshold 9, secrecy jurisdiction 8, high-risk country 6, sanctions name 0, unidentified counterparties 0.

## Benchmark change: generator commit of 2026-10-04

Commit `4aa3de8` ("test: make circular_small hops forward 90-100% of each leg") changed the generator in two ways, although its message names only the first. Comparing the ledger generated before and after it, 21 of 465 rows differ.

**1. `circular_small` amounts (5 rows).** Each leg used to be an independent draw between $3,000 and $7,000, so a ring could grow 57% in one lap. Real round-tripped money loses a little at each hop (fees, skims) and doesn't grow, so each hop now forwards 90 to 100% of the previous leg; the first leg is still drawn from $3,000 to $7,000. The seed and the number of random draws are unchanged, so the other `circular_small` legs and every other pattern keep their values. Changed rows: `CIRS-0-01`, `CIRS-0-02`, `CIRS-1-01`, `CIRS-1-02`, `CIRS-1-03`.

**2. Laundering sender names (16 rows).** The structuring sender (10 rows) and the sanctioned sender (6 rows) were renamed, because the old names were also entries on the demo watchlist, which let name screening detect the label rather than the behaviour. Those watchlist entries have since been removed.

**Could the amount change look like tuning the data to the detector? Yes, for the network check.** It landed right after fund-flow cycle detection (`ccfd94b`, the same day), which only counts a ring whose leg amounts stay within 20% of each other. On the old data `circular_small` failed that tolerance and got 0/7 network findings; after the change, every hop is within 10% and it gets 7/7. The detector's tolerance and the generator's forwarding range were chosen by the same person, and the data change came after seeing the 0/7. The rationale is real, but the 7/7 network result for `circular_small` is a consistency check, not independent evidence that the detector finds small rings. It needs checking on data nobody here generated.

The headline funnel numbers did not depend on it: the behaviour model forwards `circular_small` 7/7 on both versions, and no rule fires on it either way.

Behaviour-model recall is unchanged for every pattern:

| Pattern | Before | After |
|---|---|---|
| structuring | 10/10 | 10/10 |
| sanctioned | 6/6 | 6/6 |
| fan_in | 11/11 | 11/11 |
| circular | 3/3 | 3/3 |
| circular_small | 7/7 | 7/7 |
| circular_camouflaged | 1/8 | 1/8 |
| clean (false positives) | 10/420 | 10/420 |

## Network check: circular-flow findings

These numbers come from auditing every row against the full ledger with the network check. They were measured with a one-off analysis. The two fund-flow columns were reproduced on 2026-10-17 by running `fund_flow_cycles` on every row of the ledgers generated before and after `4aa3de8`; a script in the repository that reproduces them is on the roadmap.

| Pattern | Shape only, old data | Fund flow, old data | Fund flow, new data |
|---|---|---|---|
| clean (false positives) | 420/420 | 25/420 | 25/420 |
| circular | 3/3 | 3/3 | 3/3 |
| circular_small | 7/7 | 0/7 | 7/7 |
| circular_camouflaged | 8/8 | 0/8 | 0/8 |

Shape-only detection flagged every clean row, because businesses that trade in both directions form graph cycles all the time. Fund-flow detection only counts a cycle when the transfers run forward in time, finish within the window and keep similar amounts.

- `circular_small` drops to 0/7 on the old data only because its old legs failed the amount tolerance. See the benchmark change above for why its 7/7 on the new data is not independent evidence.
- The remaining 25 clean rows are coincidences in the random background: 11 two-way pairs and one three-account ring, where opposite trades of similar size happened to land within a day. Nothing in the data separates them from a real quick round trip.
- `circular_camouflaged` is 0/8: its legs are about two days apart with unrelated amounts, so under these rules it is not a fund-flow ring. Nothing was tuned to raise it.

The 1-day window and 20% tolerance were chosen on this synthetic data and still need validating on the IBM AML dataset.

## Sanctions screening speed

Screening used to call `rapidfuzz.process.extractOne` once for every sender and receiver on every row, rebuilding the list of watchlist names each time. It now normalises the watchlist once and scores each distinct ledger name against all of it in one `rapidfuzz.process.cdist` call. Tests check that every name gets the same match and score as before, at the configured threshold and at 60 and 0, and that `apply_rules` gives identical reasons, flags and sanctions hits on the labelled ledger plus a few rows with watchlisted names.

Measured on 2026-10-17 (Python 3.12.10, Windows 11), comparing the previous commit with the new code, three alternating rounds each. The watchlist has 31 names and aliases; the ledger has 36 distinct party names. The 19,995-row ledger is the labelled one repeated 43 times, so it has the same 36 names.

| Ledger | Name lookups | Screening before | Screening after |
|---|---|---|---|
| labelled, 465 rows | 930 | 32.0 to 32.8 ms | 0.37 to 0.39 ms |
| repeated, 19,995 rows | 39,990 | 1,287 to 1,450 ms | 3.8 to 4.0 ms |

These times cover screening only. The whole of `apply_rules`, which also loops over rows in pandas, went from 71 to 76 ms to 15 to 37 ms on 465 rows, and from 2.9 to 3.0 s to 0.6 to 1.4 s on 19,995 rows; those totals varied a lot between runs on this machine. The gain grows with the number of rows per distinct name and with the size of the watchlist, but a full OFAC list has not been measured.
