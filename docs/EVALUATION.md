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
