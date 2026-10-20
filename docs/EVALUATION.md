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
| Current ordering | 84% | 75% | 0.79 | 51 rows (11%) |

The current ordering includes the pass-through features described below; before them it was 84% / 79% / 0.82 with 48 rows forwarded. The sections dated before 2026-10-18 record numbers measured at the time.

Recall is the share of laundering rows forwarded for review instead of being dropped. It matters most here, because a dropped row is never looked at again.

### Development seed and held-out seeds

Seed 7 is the development seed. The rule thresholds, the anomaly threshold, the cycle window and tolerance, and the generator changes described below were all chosen while looking at seed 7's results, so its numbers are optimistic by construction. Seeds 11, 13, 17, 19 and 23 were not used for any of those choices. Each produces a ledger from the same generator with different random draws:

```bash
python scripts/evaluate.py --seeds 7 11 13 17 19 23
```

Measured on 2026-10-18 with nothing in the pipeline changed (before the pass-through features; the numbers with them are under "Pass-through features" below):

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

Measured on 2026-10-18, nothing in the pipeline changed. This is the baseline the pass-through features below were judged against.

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
- **Country resolution works on every format here.** The country rule fires on 50/50 country variants held out and on 0/50 clean variants. `Deutschland` is not resolved; it gets an unresolved-jurisdiction note and no flag. (Fixed later; see "Two fixes found on the hard ledgers".)
- **The name rule catches 45 of 50 variants and none of the near misses.** Casing, punctuation, suffix and reordering all score 100. The typo `Viktor Bouf` scores 90.9 and is caught; `Lazarus Groop` scores 70 and is missed, because "group" is stripped as a company suffix while the misspelt "groop" is kept. (Fixed later; see "Two fixes found on the hard ledgers".) No near-miss name reaches the threshold of 88, but the margin is small: `Melli Bakery` scores 81.8 and `Victor Boutros` 80.0.
- **Near-miss names cost precision, but not through the name rule.** All 39 forwarded `near_miss_clean` rows were selected by the behaviour model ("statistical anomaly") with no rule reason, and every one has a sender that appears only once in its ledger: they are forwarded for being one-off accounts, not for their names.
- **Results swing a lot between seeds** (recall 48% to 71%), so a single seed says little.

### Pass-through features

Four features added to the behaviour model (`utils/features.py`), aimed at money passed straight on, including through established accounts. A payment is linked to the latest payment its sender received in the previous 3 hours if it sends on 95% to 100% of that amount; following the links gives chains.

| Feature | Meaning |
|---|---|
| `hop_depth` | legs in the chain this payment belongs to (1 if none) |
| `has_pass_through` | 1 if the payment passes on a recent incoming payment |
| `dwell_hours` | how long the money sat with the sender |
| `pass_through_ratio` | amount sent on / amount received |

**Acceptance rule, fixed before measuring:** keep a feature set only if pooled held-out F1 on the hard ledgers rises by at least 0.03 and pooled held-out F1 on the default ledgers falls by no more than 0.02, with every parameter chosen on seed 7.

**Choosing on seed 7.** Two candidate sets were tried. Cycle features (`in_cycle`, `cycle_length`, from the time-ordered fund-flow cycle check with its existing 1-day window and 20% tolerance) lowered seed 7's F1 on both ledgers (hard -0.079, default -0.100). Pass-through features were tried with six window/tolerance pairs:

| Window, tolerance | Seed 7 hard F1 change | Seed 7 default F1 change |
|---|---|---|
| 3 h, 5% | **+0.049** | -0.026 |
| 6 h, 5% | +0.039 | -0.034 |
| 6 h, 10% | +0.034 | -0.072 |
| 12 h, 10% | -0.017 | -0.093 |
| 24 h, 10% | -0.046 | -0.173 |
| 24 h, 20% | -0.030 | -0.126 |
| cycle + pass-through, best pair | -0.013 | -0.126 |

3 hours and 5% was the only candidate taken to the held-out seeds. Wider windows match more ordinary two-way trade by chance. On seed 7 its default-ledger drop (-0.026) was already slightly past the limit; the rule is applied to the held-out seeds, where it passed.

**Held-out result (seeds 11, 13, 17, 19, 23):**

| Ledger | Before: TP / FP / FN, F1 | After: TP / FP / FN, F1 | F1 change | Rule |
|---|---|---|---|---|
| hard | 235 / 92 / 140, 0.670 | 307 / 104 / 68, 0.781 | **+0.112** | needed >= +0.03 |
| default | 198 / 64 / 32, 0.805 | 196 / 69 / 34, 0.792 | **-0.013** | needed >= -0.02 |

Accepted. For information, the cycle set on the same seeds: hard -0.022, default -0.145 (clean rows forwarded rose from 64 to 170). It is not in the code.

Per-seed numbers with the pass-through features:

| Seed | Default: TP / FP / FN | Recall | Precision | F1 | Hard: TP / FP / FN | Recall | Precision | F1 |
|---|---|---|---|---|---|---|---|---|
| 7 (dev) | 38 / 13 / 7 | 84% | 75% | 0.79 | 53 / 16 / 21 | 72% | 77% | 0.74 |
| 11 | 41 / 7 / 9 | 82% | 85% | 0.84 | 48 / 14 / 31 | 61% | 77% | 0.68 |
| 13 | 40 / 12 / 6 | 87% | 77% | 0.82 | 58 / 16 / 17 | 77% | 78% | 0.78 |
| 17 | 40 / 14 / 6 | 87% | 74% | 0.80 | 69 / 23 / 6 | 92% | 75% | 0.83 |
| 19 | 39 / 10 / 6 | 87% | 80% | 0.83 | 67 / 25 / 7 | 91% | 73% | 0.81 |
| 23 | 36 / 26 / 7 | 84% | 58% | 0.69 | 65 / 26 / 7 | 90% | 71% | 0.80 |
| **Pooled held out** | 196 / 69 / 34 | **85.2%** (80.1-89.2%) | **74.0%** (68.4-78.9%) | 0.79 | 307 / 104 / 68 | **81.9%** (77.7-85.4%) | **74.7%** (70.3-78.7%) | 0.78 |

What changed, pooled held out:

- **Hard ledgers:** `established_pass_through` 0/45 to **42/45**; `fan_in` 13/55 to 33/55; `circular_small` 28/46 to 37/46; `sanctioned_name_variant` 49/50 to 50/50. False positives rose from 92 to 104 (near-miss names 39 to 44, clean-country variants 11 to 15, clean 42 to 45).
- **Default ledgers:** `circular_small` 46/46 to 45/46 and `circular_camouflaged` 2/34 to 1/34; clean rows forwarded 64 to 69.
- **Seed 7, which CI gates:** recall unchanged (38/45, every gated pattern still 100%), precision 79% to 75% (clean rows forwarded 10 to 13).
- **Cost:** the first version matched payments in a Python loop over every payment each sender had received, so its time grew with the square of the ledger size: about 1 s on 465 rows and 110 s on 5,115. It was rewritten with sorting and binary search over each sender's 3-hour window; tests check that its output equals the loop version exactly on the seed 7 and held-out default and hard ledgers. Measured on 2026-10-18 on the seed-7 ledger repeated with unique ids and shifted dates:

  | Rows | Pass-through features, loop | Pass-through features, vectorised | Whole Tier 0+1, vectorised |
  |---|---|---|---|
  | 465 | 0.93 s | under 0.01 s | 0.3 to 0.5 s |
  | 5,115 | 110 s | 0.01 to 0.02 s | 0.5 to 1.0 s |
  | 19,995 | over 40 minutes (stopped) | 0.02 s | 1.0 s |
  | 49,755 | not run (about 3 hours projected) | 0.11 s | 4.7 s |

  CI's seed-7 run now reports Tier 0+1 at about 210 ms.
- **`fan_in` gains are a side effect.** No pass-through feature describes fan-in; adding features changes how Isolation Forest splits the batch, and the fan-in rows happened to stand out more.

**Point in time.** These are batch features: counts, volumes, fan-in and the pass-through chain all use every row in the ledger, including rows dated after the payment being scored (`hop_depth` follows money forward as well as back). That is fine for auditing a closed ledger. Scoring payments as they arrive would need each feature computed only from earlier rows, and the numbers above would not carry over.

### Two fixes found on the hard ledgers

Both problems were found by looking at the held-out hard ledgers, so **for these two items the hard ledgers are no longer fully held out**: the numbers below show that the fixes work on the cases that revealed them, not that they generalise. The 88 screening threshold is unchanged.

- **`Deutschland` resolves to Germany.** It is added to the country aliases in `utils/jurisdictions.py`; before, it got an unresolved-jurisdiction note.
- **A misspelt company suffix no longer sinks a match.** "Lazarus Groop" was normalised to "groop lazarus" (the real suffix "group" is stripped, a misspelling of it is not) and scored 70 against "lazarus". Each name is now also compared with its suffixes kept ("groop lazarus" against "group lazarus"), and the higher of the two scores counts. "Lazarus Groop" now scores 92.3; "Lazarus Gruop", "Lazarus Grp" and "Wagner Grupo" also match. A name made only of suffixes ("The Group") is still not screened.
- **Near misses keep their margin.** Kept suffixes raise some scores: `Lazarus Dental Group` goes from 66.7 to 78.8 and `Lazarus Grove` from 70.0 to 84.6, both below 88. A test checks that the ten near-miss names, and names such as `Northwind Group` and `Tornado Group`, stay below the threshold. Removing near-suffix words instead was rejected: it made `Grout Lazarus` an exact match and dropped `Trost` from `Hans Trost`.
- **Still open.** `Sinaloa Cartel Limted` (a misspelt suffix on a name that otherwise matches) still scores 84.2 and is not matched. The closest near-miss name, `Lazarus Grove`, now scores 84.6 against the threshold of 88, a margin of 3.4 points that should be re-checked on a larger watchlist.

Measured on 2026-10-18:

- **Default ledgers:** every number is unchanged (seed 7: 84% / 75% / 0.79, 51 forwarded; pooled held out 196 / 69 / 34).
- **Hard ledgers:** the name rule fires on 50/50 held-out `sanctioned_name_variant` rows (was 45) and 10/10 on seed 7 (was 9). Those rows were already forwarded by the behaviour model, so pooled held-out TP / FP / FN stays 307 / 104 / 68. Seed 7 gains one TP (53 to 54).
- **`clean_country_variant`:** the country rule still fires on 0/50; the `Deutschland` rows lose the unresolved-jurisdiction note, and forwarding is unchanged at 15/50.
- **Speed:** bulk screening of 39,990 names (36 distinct) is about 1 ms before and after.

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

## External data: IBM AML transactions

`scripts/eval_external.py` runs Tiers 0 and 1 on the IBM AML transaction files published on Kaggle. Its tests use a 20-row hand-made file in the same layout (`tests/fixtures/ibm_aml_sample.csv`). Results on `HI-Small_Trans.csv` are below; the funnel does not transfer to this data.

**The dataset.** [IBM Transactions for Anti Money Laundering (AML)](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml), published by Erik Altman. The transactions come from IBM's AMLworld simulator, not from a bank. Six transaction files (`HI-Small_Trans.csv`, `HI-Medium_Trans.csv`, `HI-Large_Trans.csv` and the `LI-` versions) come with a `*_Patterns.txt` file each. HI files have the higher laundering rate, about 0.1% of rows.

**Licence:** Community Data License Agreement – Sharing – Version 1.0 (CDLA-Sharing-1.0), as listed by Kaggle's dataset API on 2026-10-18. The data is not committed; `data/external/` is in `.gitignore`.

**Column mapping.** The script checks the header first. Case and spacing are ignored, but the 11 columns must be in this order, or it stops and prints the header it found.

| IBM column | Ledger column | Notes |
|---|---|---|
| (line number) | `transaction_id` | `L2` is the first data row, so a row can be found in the file |
| `Timestamp` | `timestamp` | `YYYY/MM/DD HH:MM` |
| `From Bank`, first `Account` | `sender` | joined as `bank-account`; bank codes kept as written |
| `To Bank`, second `Account` | `receiver` | the same |
| `Amount Paid` | `amount` | in `Payment Currency`; no conversion |
| `Payment Currency` | (filter) | `--currency`, default `US Dollar`; `all` keeps every currency |
| `Is Laundering` | (label) | never passed to the pipeline |
| (none) | `country` | blank |
| `Amount Received`, `Receiving Currency`, `Payment Format` | | not used |

**What is measured.** The files have no party names and no countries, so sanctions screening and the country rules cannot fire. Only the amount rules and the behaviour model run, through the same `load_ledger`, `apply_rules` and `run_funnel` calls as the app. Laundering is rare, so the script prints:

- the forwarded set: TP / FP / FN / TN, recall and precision with Wilson 95% intervals, F1;
- **PR-AUC** (average precision), for a ranking with every rule-flagged row first and the rest by anomaly score, plus PR-AUC of the anomaly score alone. A random ranking scores the laundering rate, so compare against that rate, not against 0.5;
- **precision@k** for k = 100, 500, 1000 (`--k` to change);
- for each rule and for the behaviour model: rows fired, laundering rows fired, precision, and share of the forwarded rows.

A note is printed when fewer than 30 laundering rows are in the sample.

**Download and run.**

1. Sign in to Kaggle and open the dataset page above. The licence is shown there.
2. Download `HI-Small_Trans.csv` from the page's file list into `data/external/`. Or, with the Kaggle command-line tool (not a dependency of this project; it needs a Kaggle API token in `~/.kaggle/kaggle.json`):

   ```bash
   pip install kaggle
   kaggle datasets download ealtman2019/ibm-transactions-for-anti-money-laundering-aml -f HI-Small_Trans.csv -p data/external
   ```

   If a `.zip` arrives, unzip it into `data/external/`.
3. Look at the header before trusting the mapping: `head -2 data/external/HI-Small_Trans.csv`, or in PowerShell `Get-Content data/external/HI-Small_Trans.csv -TotalCount 2`. It should be `Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering`.
4. Run:

   ```bash
   python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --max-rows 50000
   python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --max-rows 500000   # more laundering rows
   python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --currency all      # every currency, raw amounts
   ```

   On 30,000 kept rows of a randomly generated file in this layout, the run took about 8 s on Windows 11.

**Results on `HI-Small_Trans.csv`, measured 2026-10-19.** Rows paid in US Dollar, from the top of the file, with every rule, threshold and model setting as chosen on synthetic seed 7. Nothing was changed after these runs.

| Run (`--max-rows`) | Rows read / kept | Laundering | Forwarded | TP / FP / FN | Recall | Precision | PR-AUC (random ranking) | precision@100 / 500 / 1000 |
|---|---|---|---|---|---|---|---|---|
| 50,000 | 50,143 / 50,000 | 2 | 20,480 (41%) | 2 / 20,478 / 0 | 2 of 2 | 0.01% | 0.00854 (0.00004) | 1/100, 2/500, 2/1000 |
| 500,000 | 1,334,105 / 500,000 | 307 | 185,524 (37%) | 196 / 185,328 / 111 | 63.8% (95% CI 58.3% to 69.0%) | 0.11% | 0.00125 (0.00061) | 0/100, 3/500, 4/1000 |

The 50,000-row run has 2 laundering rows, so its figures are noise.

**The funnel does not transfer to this data.** It forwards 37% of rows, against about 10% on the synthetic ledgers it was built for (51 of 465 rows on seed 7). The forwarded rows are barely richer in laundering than the whole batch: 0.11% against 0.061%. PR-AUC is about twice that of a random ranking, and the 100 highest-ranked rows contain no laundering.

By source, 500,000-row run (a row can fire several):

| Source | Rows fired | Laundering rows | Precision | Share of forwarded rows |
|---|---|---|---|---|
| Reporting threshold (10,000 or more) | 130,663 | 122 | 0.093% | 70.4% |
| Structuring band (8,000 to 10,000) | 10,964 | 23 | 0.210% | 5.9% |
| High-risk country, secrecy jurisdiction, sanctions name, unidentified parties | 0 each | 0 | | 0% |
| Any rule | 141,627 | 145 | 0.102% | 76.3% |
| Behaviour model | 78,873 | 104 | 0.132% | 42.5% |
| Behaviour model, no rule | 43,897 | 51 | 0.116% | 23.7% |
| Unresolved-jurisdiction note | 500,000 | 307 | | does not forward a row |

Why it does not transfer:

- **Amount scale is the main cause.** The amount rules forward 141,627 rows, 76% of everything forwarded. Here 26.1% of rows are 10,000 or more (median 1,286; 90th percentile 102,565; 99th percentile 5.47 million), against 1.9% on seed 7 (median 718; 90th percentile 4,547). The reporting-threshold rule fires on 130,663 rows at 0.093% precision, little above the 0.061% laundering rate. The rules also apply to every payment type: only 10,595 of those 130,663 rows are `Cash`. The thresholds don't fit this data's amounts; the file is read correctly.
- **Self-transfers are forwarded as if they were payments.** 193,337 rows (38.7%) have the same sender and receiver account, 186,391 of them `Reinvestment`. None is laundering. 69,041 of them are forwarded, 37% of all forwarded rows: 65,434 by the amount rules and 12,845 by the behaviour model. The ledger format has no way to mark an account paying itself. Self-transfers do not explain the rate on their own: 38.0% of the other rows are forwarded too.
- **The behaviour model picks busy accounts, and laundering accounts here are not busy.** It flags 78,873 rows (15.8%; its 30% ceiling is not reached). The senders of flagged rows have a median of 26 transactions in the sample, against 4 for the rest. Senders of laundering rows also have a median of 4. The 100 highest-ranked rows all come from one sender (`070-100428660`), are `Cheque` and `Cash` payments with a median amount of 24.3 million, and include no laundering, hence precision@100 of 0.
- **Checked, and not a cause:**
  - **Missing names and countries:** every row gets the unresolved-jurisdiction note, which never forwards a row on its own. The country, sanctions and unidentified-party rules fire on 0 rows, and no account was read as a placeholder.
  - **Round amounts:** 10 rows are multiples of 1,000.
  - **Timestamps:** all parse, at minute resolution.
  - **Account ids:** 172,786 accounts, and no account number appears under two spellings of the same bank code.
- **The sample is not a clean time window.** The file is not in time order. 499,794 of the 500,000 kept rows are dated 1 and 2 September 2022. The other 206 are dated 3 to 9 September, and 120 of those are laundering, 39% of all laundering in the sample. Laundering chains are only partly present.

**Time-split slices.** The 500,000 rows above have been studied in detail, so they are development data. Changes are judged on two date ranges instead, selected with `--start` and `--end` (parsed timestamps, both days inclusive, whole file read):

| Slice | Dates (2022) | USD rows | Laundering | Rows from the 500,000 studied | Self-transfers |
|---|---|---|---|---|---|
| TUNE | 3 to 6 September | 513,669 | 666 (0.130%) | 184 | 11,766 (2.3%) |
| REPORT | 8 to 18 September | 503,288 | 801 (0.159%) | 16 | 11,170 (2.2%) |

- **Choosing parameters:** any parameter is chosen on TUNE only. REPORT is run once, after every change is fixed.
- **The gap:** 7 September is left out between the slices, so a laundering chain running from one slice into the other is cut in neither.
- **Why REPORT includes rows already studied:** the file's dates run from 1 to 18 September, but almost every row is dated 1 to 10 September, and every day up to 9 September holds a few of the rows already studied. Starting REPORT on 10 September would remove those 16 rows but leave only 77,833 rows. More than half of the laundering in those rows (423 of them) comes from the thin tail of 11 to 18 September, where 244 of 416 rows are laundering.
- **Slice size:** both slices are kept whole (`--max-rows 520000`).
- **Self-transfers:** 1 September, which most of the studied rows come from, is 46% self-transfers. Both slices are about 2%.

Baseline, measured 2026-10-19 with the pipeline unchanged:

| Slice | Forwarded | TP / FP / FN | Recall | Precision | PR-AUC (random ranking) | precision@100 / 500 / 1000 |
|---|---|---|---|---|---|---|
| TUNE | 153,107 (29.8%) | 344 / 152,763 / 322 | 51.7% | 0.22% | 0.00258 (0.00130) | 0/100, 0/500, 1/1000 |
| REPORT | 150,611 (29.9%) | 452 / 150,159 / 349 | 56.4% | 0.30% | 0.00305 (0.00159) | 0/100, 0/500, 1/1000 |

| Source | TUNE rows (laundering) | REPORT rows (laundering) |
|---|---|---|
| Reporting threshold | 78,689 (221) | 81,256 (333) |
| Structuring band | 9,035 (33) | 9,312 (46) |
| Any rule | 87,724 (254) | 90,568 (379) |
| Behaviour model | 96,812 (179) | 91,122 (159) |
| Behaviour model, no rule | 65,383 (90) | 60,043 (73) |

On both slices the funnel forwards about 30% of rows, and PR-AUC is about twice the laundering rate.

```bash
python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --start 2022-09-03 --end 2022-09-06 --max-rows 520000   # TUNE
python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --start 2022-09-08 --end 2022-09-18 --max-rows 520000   # REPORT
```

**Acceptance rule for changes, fixed on 2026-10-19 before any change was measured.**

(a) **On the REPORT slice**, at the funnel's own operating point (the rows it forwards), all three of these must hold:
- forwarded share of rows at most 15% (baseline 29.9%);
- recall at least 40% (baseline 56.4%);
- PR-AUC at least 3 times the laundering rate, 0.00477 (baseline 0.00305).

These three thresholds are a judgment call, not derived from any analysis.

(b) **Synthetic results stay intact.**
- The CI gate command passes unchanged.
- Pooled held-out F1 is at least 0.77 on the default ledgers and at least 0.76 on the hard ledgers (baselines 0.79 and 0.78).
- Seed 7 and the held-out seeds are run once per change set.

(c) **Only three change sets may be tried, and no fourth.** Each one's parameters are fixed in advance or chosen on the TUNE slice only:
- **A, self-transfers:** an account paying itself is not scored by the rules or the behaviour model, and is counted separately.
- **B, payment type:** when the ledger carries a payment channel, the reporting-threshold and structuring-band rules apply only to cash-like channels. Ledgers without one behave as before.
- **C, scale robustness:** stop one very large account from dominating the behaviour model's ranking.

The REPORT slice is run once, after the change sets are fixed. If (a) is not met, the code that helped on TUNE and passes (b) is kept, and the result is recorded as it is.

An amount-rank feature (each amount's percentile rank within the batch) was also tried as part of C; it lowered TUNE PR-AUC at every clipping bound except 3, so it was removed.

**What this cannot show.**

- **Without `--start` and `--end`, rows come from the top of the file** until `--max-rows` rows are kept. The file is not in time order, so that is not a clean time window. With a date range, laundering chains that cross the range's edges are only partly present.
- **No names or countries.** Sanctions screening and the country rules are not tested by this data.
- **Amounts are compared without currency conversion.** The amount rules (the reporting threshold and the structuring band) and the amount features treat every value as the same unit. The US Dollar default keeps that true, but it leaves out most of the file: the 500,000 kept rows came from 1,334,105 read. With `--currency all` the amounts are not comparable.
- **No parameter is tuned on this data.** Every threshold was chosen on synthetic seed 7, so these numbers show how far that tuning carries.
- **The simulator is not a bank.** The AMLworld laundering patterns differ from the synthetic ledger's, and from real ones.
- **Tiers 2 and 3 do not run:** no network check, no LLM review.

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
