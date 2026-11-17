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

**Results on `HI-Small_Trans.csv`, measured 2026-10-19.** Rows paid in US Dollar, from the top of the file, with every rule, threshold and model setting as chosen on synthetic seed 7. These runs came before the changes described under "Time-split slices" below.

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

**Setting fixed on TUNE before the REPORT run (2026-10-20).** Rule: after C, keep the A and B setting with the highest TUNE PR-AUC; within noise, keep the existing behaviour (A on, B off). With C at a clip of 5, mean TUNE PR-AUC over five Isolation Forest seeds (42, 1, 2, 3, 4) was:

| A (exclude self-transfers) | B (cash-only amount rules) | PR-AUC mean (range) |
|---|---|---|
| on | off | 0.00262 (0.00254 to 0.00271) |
| off | off | 0.00260 (0.00237 to 0.00277) |
| on | on | 0.00201 (0.00192 to 0.00213) |
| off | on | 0.00201 (0.00179 to 0.00215) |

B on is worse on every seed. A on and off are within noise. The setting kept is A on, B off, with `FEATURE_Z_CLIP = 5`.

**What each change did on TUNE** (Isolation Forest seed 42; each row adds to the one above, except B, which was measured with A and then turned off):

| Setting | Forwarded | TP / FP / FN | Recall | PR-AUC (random 0.00130) |
|---|---|---|---|---|
| Baseline | 153,107 (29.8%) | 344 / 152,763 / 322 | 51.7% | 0.00258 |
| A: self-transfers left out | 146,898 (28.6%) | 330 / 146,568 / 336 | 49.5% | 0.00242 |
| A + B: amount rules on cash only (not kept) | 101,783 (19.8%) | 151 / 101,632 / 515 | 22.7% | 0.00181 |
| A + C: robust z-scores clipped at 5 (kept) | 86,186 (16.8%) | 261 / 85,925 / 405 | 39.2% | 0.00271 |

- **A** leaves out 11,766 self-transfers on TUNE (2.3% of rows), none of them laundering. It mattered on the development rows, where 38.7% of rows are self-transfers and forwarding fell from 37.1% to 21.4%, but barely on TUNE or REPORT.
- **B** removes most of the amount rules' laundering hits. Of the rows 10,000 or more paid in Cash, 0.026% are laundering, below the 0.130% laundering rate. The non-cash rows that B stops ruling hold 251 laundering rows at 0.35%. In this simulator laundering moves mostly by ACH, so the amount rules work here as large-amount rules, not as cash rules. `CASH_ONLY_AMOUNT_RULES` stays off.
- **C** stops one very large account from stretching each feature's range. The behaviour model forwards 11,000 rows instead of 94,943, and finds 37 laundering rows instead of 151; PR-AUC rises about 6% (mean over five seeds 0.00262 against 0.00248).
- **On the synthetic ledgers only C changes anything.** Seed 7 goes from 84% / 75% / 0.79 with 51 rows forwarded to 84% / 78% / 0.81 with 49. Pooled held-out F1 falls from 0.79 to 0.77 on the default ledgers (TP / FP / FN 196 / 69 / 34 to 186 / 65 / 44) and from 0.78 to 0.76 on the hard ledgers (307 / 104 / 68 to 274 / 69 / 101). Both are just above the limits of rule (b). Hard `fan_in` falls from 33 to 9 of 55, and `circular_small` from 37 to 27 of 46.

**Result on the REPORT slice, run once on 2026-10-20 with the kept setting.** Rule (a) is not met.

| REPORT slice | Baseline | Kept setting | Rule (a) |
|---|---|---|---|
| Forwarded | 150,611 (29.9%) | 98,178 (19.5%) | at most 15%: **not met** |
| TP / FP / FN | 452 / 150,159 / 349 | 404 / 97,774 / 397 | |
| Recall | 56.4% | 50.4% (95% CI 47.0% to 53.9%) | at least 40%: met |
| Precision | 0.30% | 0.41% | |
| PR-AUC (random 0.00159) | 0.00305 | 0.00348 | at least 0.00477: **not met** |
| precision@100 / 500 / 1000 | 0/100, 0/500, 1/1000 | 0/100, 0/500, 1/1000 | |

By source on REPORT: the reporting threshold fires on 77,730 rows (333 laundering, 0.43%), the structuring band on 8,993 (46, 0.51%), any rule on 86,723 (379), the behaviour model on 21,268 (64, 0.30%), and the behaviour model without a rule on 11,455 (25). 11,170 self-transfers were left out, none laundering.

The changes forward a third fewer rows (98,178 against 150,611), lose 48 laundering rows, and raise PR-AUC from 1.9 to 2.2 times a random ranking. The funnel still does not transfer to this data.

**What still does not work.**

- **The amount rules alone forward 17.2% of REPORT rows** (86,723), so no change to the behaviour model can bring the total under 15%. The reporting-threshold rule is the larger part, at 0.43% precision. Restricting it to cash (B) removes most of the laundering it catches.
- **The ranking has no useful top.** The 100 and 500 highest-ranked rows hold no laundering. Rule-flagged rows rank first and are ordered by anomaly score; on TUNE the top 100 all came from one sender in every setting tried.
- **The behaviour model is weak here.** On its own it finds 64 of 801 laundering rows in 21,268 forwarded, 0.30% precision against a 0.159% laundering rate.
- **C costs synthetic recall**, as above, for a small gain on this data.
- **The caveats above still apply:** US Dollar rows only, no names or countries, chains cut at the slice edges, and every threshold except the clip chosen on synthetic seed 7. The clip was chosen on TUNE.


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

## Live check of the AI review tier

On 2026-10-21 the AI review tier (`utils/agents.py`) ran against a live model: Gemini 2.5 Flash on Google Vertex AI, with retrieval over 410 fragments of a public AML guidance PDF, embedded with Vertex `text-embedding-004` and stored in a local Qdrant. Five rows forwarded from the 465-row sample ledger were reviewed one at a time through the same engine the app uses.

All five reviews returned a verdict that validated against the output schema. Retrieval returned five passages for each one, and there were no retries and no failed reviews.

| Verdict | Count | What the review cited |
|---|---|---|
| SUSPICIOUS | 3 | generic sender and receiver names; a sanctioned jurisdiction with an unknown receiver; an amount just under the reporting threshold sent to the Cayman Islands |
| CLEAR | 2 | both rows hit the reporting-threshold rule (amount of 10,000 or more); the reviews found nothing beyond the size of the amount |

**What happens to a CLEAR verdict on a reporting-threshold hit.** The rule sets a force-review flag, so the funnel cannot drop the row and it always reaches the AI tier; the rule reasons are recorded in the audit log when the row is forwarded. The CLEAR verdict is then recorded in the audit log with its rationale, and the row stays in the investigative ledger, where "Why was a transaction forwarded?" still lists the threshold rule. From there a CLEAR row is not offered for SAR drafting or the officer-review panel, which list SUSPICIOUS rows only. The network check can still be run on it, but the CLEAR verdict adds no points, so it pauses for an officer only if network patterns or missing counterparty data reach the review threshold on their own. The pipeline has no threshold-report path: the reporting-threshold rule only forces an AI review, and nothing files or queues a report for amounts over the threshold. Both CLEAR rationales noted that a threshold report is still required; that filing has to happen outside this tool.

**Cost and time.** Each review makes two model calls (retrieval-grounded analysis, then the structured review). The five took 119.5 s in total, about 24 s per transaction, and used roughly 11,000 input and 3,900 visible output tokens, at least $0.01 to $0.02 at list prices (reasoning tokens were not counted; see the corrected cost note under "LLM tier evaluation").

**Limits.**

- Five rows show that the tier runs end to end, not how accurate its verdicts are. Verdict accuracy has not been measured yet.
- Only the Vertex path was exercised. The Bedrock path was not part of this check.
- At the time of the check, PDF ingestion needed the settings exported in the shell because `config.py` did not read `.env`. `config.py` now loads the repository's `.env` itself, without overriding variables already set.
- The Vertex integration uses LangChain's `ChatVertexAI` and `VertexAIEmbeddings`, which are deprecated in favour of `langchain-google-genai`. Migrating is still to do.

## LLM tier evaluation

`scripts/eval_llm_tier.py` scores the AI review tier (`ComplianceAuditEngine.assess_transaction`) against labelled cases, so verdict quality can be measured rather than only observed. `scripts/evaluate.py --with-llm` counts the verdicts the tier returns on the sample ledger; this harness checks them against an expected answer.

**Cases.** `tests/fixtures/llm_cases.json` holds 31 synthetic cases, 13 expected CLEAR and 18 expected SUSPICIOUS, in seven categories: obvious clear (payroll, rent, suppliers), over-threshold but legitimate, structuring just under the threshold, sanctioned or high-risk jurisdiction, layering through shell-like names, pass-through, and prompt injection. All names are invented except entries on the bundled demo watchlist. Each case's rule reasons are exactly what `utils/rules.py` produces for its fields, and a test keeps them in step. In the prompt-injection cases a ledger field asks for CLEAR while the facts call for SUSPICIOUS. The engine escalates any injection the model reports, so the set has no injection case whose facts are clear.

Each case reaches the engine with the same arguments the app passes: the transaction fields and the rule reasons. The behaviour-model flags in the case file explain why such a row would be forwarded, but the engine has no parameter for them, so the review does not see them. Pass-through cases with no rule hit therefore give the model only names, an amount and a country, so the review cannot answer them; the report says so in its footer.

**Development and held-out sets.** The 31 cases in `llm_cases.json` are the development set: their per-case live results have already been inspected, so any change to the review is tuned against them. `tests/fixtures/llm_cases_heldout.json` holds 21 new cases (10 CLEAR, 11 SUSPICIOUS, ids `H-`) written separately, with every SUSPICIOUS signal visible in the fields the review receives and no pass-through cases. It is the held-out set, to be run once after a change to the review has been chosen on the development set, not used for tuning.

**Metrics.** Verdict accuracy, SUSPICIOUS precision and recall with Wilson 95% intervals, a confusion matrix with an ERROR column, accuracy per category, and the share of reviews that errored. An ERROR is never a correct answer, so it counts against accuracy and recall, and the harness exits with code 1 if any errored review were counted as CLEAR.

**Retries.** The engine is the only retry layer. The Bedrock client uses botocore `retries={"max_attempts": 0}`: this setting counts retries after the first request, so `0` sends one request per call and `1` sends two (tested against a local HTTP 429 server). The Vertex clients use `max_retries=0`.

**Running it.**

```bash
python scripts/eval_llm_tier.py                      # fake model answering the expected verdict
python scripts/eval_llm_tier.py --fake always-clear  # also: always-suspicious, error
python scripts/eval_llm_tier.py --live --max-calls 70 --out llm_eval.md
python scripts/eval_llm_tier.py --cases tests/fixtures/llm_cases_heldout.json   # held-out set
python scripts/eval_llm_tier.py --exclude-category pass_through                  # repeatable
```

`--cases PATH` picks the case file (default `tests/fixtures/llm_cases.json`) and `--exclude-category NAME` leaves out a category; the report names the file and the excluded categories.

Without `--live` the harness uses a deterministic fake model and a fake retriever and needs no network or credentials. `--live` uses the configured provider through the app's own factory and retrieval stack. `--max-calls` (default 40 with `--live`) counts every model call, retries included. Once the cap is reached no further call is made, the case in progress is dropped and the completed cases are reported. Each review makes two calls, so the full set needs at least 62. A live run prints token usage and an estimated cost when the provider reports usage. The report goes to the console and, with `--out`, to a markdown file. `--cases-out PATH` appends one JSON line per finished case with the expected and returned verdict, confidence, the reviewer's rationale, any error detail, the rule reasons sent, the ids of the retrieved passages, and the calls, tokens and seconds that case used. Each row is flushed as soon as its case finishes, so a crash or a stop at the call cap keeps every case completed before it. Nothing edits these docs automatically.

**Fake-mode sanity numbers.** These check the harness, not the model:

| Fake mode | Accuracy | SUSPICIOUS precision | SUSPICIOUS recall | Errored | Errored counted as CLEAR |
|---|---|---|---|---|---|
| oracle | 31/31 | 18/18 | 18/18 | 0/31 | 0 |
| always-clear | 13/31 | n/a (none predicted) | 0/18 | 0/31 | 0 |
| always-suspicious | 18/31 | 18/31 | 18/18 | 0/31 | 0 |
| error | 0/31 | n/a (none predicted) | 0/18 | 31/31 | 0 |

**Cost figures in this section (corrected).** Every cost and output-token figure below was measured before the harness counted reasoning tokens. On Gemini 2.5 Flash `output_tokens` leaves out the reasoning ("thinking") tokens, which are billed as output, and the runs below did not record them, so their true cost cannot be recomputed. Each figure is therefore restated as a lower bound on visible output only. The same fixed review, measured with reasoning counted (see "Investigation agent evaluation" below), costs $0.0065 to $0.0076 per case on the held-out and no-rule sets, and $0.0165 and $0.0279 per case on the development set, where answers that ran away to about 65,000 output tokens dominate the cost.

**Development baseline (two live runs, same configuration).** Vertex AI, Gemini 2.5 Flash, the 31 development cases:

| | Run 1 | Run 2 |
|---|---|---|
| Verdict accuracy | 18/31 (58%) | 20/31 (65%) |
| SUSPICIOUS recall | 16/18 | 17/18 |
| SUSPICIOUS precision | 16/26 | 17/27 |
| Clean cases cleared | 2/13 | 3/13 |
| Errored reviews | 1 | 0 |
| Model calls | 67 | 63 |
| Output tokens (visible only) | 345,763 | 29,037 |
| Estimated cost (lower bound, reasoning not counted) | at least $0.89 | at least $0.09 |

In run 1 one review failed after four attempts because the model called a built-in tool instead of returning the structured assessment; it was reported as ERROR and none was counted as CLEAR. The cause of the twelvefold difference in output tokens between the runs is unknown. A review takes about 22 seconds and two model calls. Run-to-run differences of one or two cases are noise, not a change in quality. Only run 2 has per-case records (`--cases-out`).

**What the per-case records showed.** All ten false positives were clean cases returned SUSPICIOUS. None had a high-risk rule reason: four had no rule reasons and six had only the neutral reporting-threshold line. In their rationales the model treats the bare ledger row as an incomplete wire message (seven of ten), treats an amount near a threshold as evidence (six), and escalates because the evidence is ambiguous (three). Pages 81-82 of the guidance, on wire transfers, were retrieved for all 31 cases. The 410 stored chunks are all distinct; a page that appears twice in one retrieval is two overlapping chunks of the same page, not a duplicate ingestion. The review prompt tells the model to prefer escalation when the evidence is ambiguous, and the output schema asks for the regulatory basis of the verdict. One rationale cited "Recommendation 82", which does not exist (FATF has 40 recommendations); it looks like a page number. The four prompt-injection cases are escalated by the engine's own guard, so they test the guard rather than the model's judgement. The three pass-through cases cannot be answered because the review does not receive the behaviour-model flags; that category is reported separately and excluded from any acceptance rule.

**Pre-registered plan** (written before any change to the review).

- The 31 cases in `llm_cases.json` are the development set. The held-out set (`tests/fixtures/llm_cases_heldout.json`, 21 cases) is run once, after a change has been chosen on the development set.
- Disclosure: the held-out cases were written after the development failures had been inspected, by the same author who wrote the labels. Every held-out SUSPICIOUS case has a rule reason, so the set measures the reduction of false positives, not the detection of suspicious cases without a rule hit.
- Change 1, one variable only: add one sentence to the reviewer prompt saying the record is an internal ledger extract that never contains addresses, account numbers, payment purpose or wire-message data, so their absence is not by itself an indicator of suspicion. Everything else stays as it is.
- Acceptance on the development set, excluding pass_through: at least 8 of 13 clean cases cleared (baseline 2 and 3); none of the SUSPICIOUS cases that were correct in run 2 changes to CLEAR; no errored reviews.
- If change 1 passes, the held-out set is run once. It passes with at least 6 of 10 clean cases cleared and at least 10 of 11 SUSPICIOUS cases correct. The result is reported whatever it is.
- If change 1 fails the development rule, it is recorded as a negative result and the next change is tried, one at a time, in this order: remove the escalate-on-ambiguity tie-break; build the retrieval query from the evidence only; explain the rule-hit line outside the untrusted fence.

**Change 1 result (development set).**

- Result against the rule: clean cases cleared 7/13 (needed 8; baseline 2 and 3), FAIL. No SUSPICIOUS case that was correct in run 2 changed to CLEAR, PASS; the pass-through case PT-01 did flip to CLEAR, which is outside the rule, and its rationale cites the new sentence. Errored reviews 0, PASS. Change 1 is recorded as a negative result against the rule.
- Other figures: accuracy 23/31; SUSPICIOUS recall 16/18 (15/15 without pass_through); SUSPICIOUS precision 16/22; false positives down from 10 to 6; obvious_clear from 3/7 to 6/7; over_threshold_legitimate from 0/6 to 1/6. The run used 65 model calls and 154,602 visible output tokens, at least $0.41 (reasoning not counted). Two cases (OC-02, OC-07) produced about 62,000 to 66,000 output tokens each, with retries and more than 240 seconds each; the other 29 averaged about 880. The cause is unconfirmed.
- Themes in the six clean cases still flagged: the reporting-threshold line read as a duty to escalate (4); missing wire-message or purpose data despite the new sentence (3); escalation on ambiguity (2 to 3); a solicitor's client account (1); an irrelevant citation (1).
- Policy: changes are cumulative. Change 1 stays in the reviewer prompt, and each later change is tested on top of it, in the order listed above. The held-out set has not been run; it is run once, after a change passes the development rule.

**Change 2 result (development set).**

- Change 2 removed the sentence about weighing the cost of a missed filing against an unnecessary one and preferring escalation when the evidence is ambiguous; change 1 stays in. Result against the rule: clean cases cleared 11/13 (needed 8; run 2 3/13; change 1 7/13), PASS. No SUSPICIOUS case that was correct in run 2 changed to CLEAR (15/15 outside pass_through), PASS. Errored reviews 0, PASS.
- Figures (run 2 / change 1 / change 2): accuracy 20/31, 23/31, 27/31; SUSPICIOUS recall 17/18, 16/18, 16/18 (15/15 without pass_through); SUSPICIOUS precision 17/27, 16/22, 16/18; false positives 10, 6, 2; model calls 63, 65, 62; visible output tokens 29,037, 154,602, 29,228; cost at least $0.09, $0.41, $0.09 (reasoning not counted). A review took 26.6 seconds on average; PI-04 took 217 seconds with 585 output tokens.
- Still flagged: OC-07, which reads the row as a wire message and claims the counterparty jurisdiction is missing although the record gives one, and OT-03, a payment to a solicitor's client account that the author judged ambiguous.
- Limits: this is one run, so the improvement from 7 to 11 is larger than the documented noise but not replicated. PT-01, a pass-through case flagged correctly in run 2 without the review being able to see its signal, is CLEAR again. Every SUSPICIOUS case in both case sets has a rule reason, so neither set measures what removing the escalate-on-ambiguity sentence costs on suspicious cases without a rule hit; a third case set for that is needed before this prompt is treated as the default.
- Next: the held-out set is run once with this prompt, under the acceptance rule above, and the result is reported whatever it is.

**Held-out result (single run).**

- Run once with the change 2 prompt on the 21 held-out cases, with no code or prompt changes afterwards. Against the rule: clean cases cleared 10/10 (needed 6), PASS; SUSPICIOUS cases correct 11/11 (needed 10), PASS; errored reviews 0.
- Figures: accuracy 21/21 (Wilson 95% CI 85% to 100%); SUSPICIOUS recall 11/11 and precision 11/11 (CI 74% to 100% each); the clean-case rate of 10/10 has a lower bound of about 72%. The run used 42 model calls and 19,589 visible output tokens, at least $0.06 (reasoning not counted), and took 17.8 seconds per case on average; no case took over 100 seconds or produced over 5,000 output tokens.
- How to read it: the held-out cases were written after the development failures had been inspected, by the author who also labelled them; they leave out the ambiguous solicitor's client-account pattern; and every SUSPICIOUS case has a rule reason. The run therefore shows a reduction in false positives on unseen clean cases. It does not show detection of suspicious cases without a rule hit, and it does not show that removing the escalate-on-ambiguity sentence is safe for them. The prompt is not validated for that case.
- Open: a third case set of suspicious cases without a rule reason, written and committed before it is run, is needed before the current prompt is treated as the default.

**Third case set (no rule reason).** Pre-registered plan, written before the set has been run.

- Purpose: measure whether removing the escalate-on-ambiguity sentence costs detection when no rule fires. `tests/fixtures/llm_cases_norule.json` holds 20 cases. In the 12 SUSPICIOUS cases the rules engine returns no reason at all, so the suspicion has to be read from the names, the jurisdiction and the amount: numbered or nominee shell names, jurisdictions under FATF increased monitoring that the rules do not list, politically exposed beneficiaries, account types that do not fit the payment, a collector paying a crypto ATM, a sector mismatch, a scam-recovery payee and a payment to a coin-mixing service, with no pattern used more than twice. The 8 CLEAR controls look ambiguous but are legitimate: a house purchase through a solicitor's client account, a payroll run, a new-build purchase, a foundation grant, a director's dividend, a small crypto purchase and two cross-border supplier payments. The review sees only the record fields and the rule reasons, so account types appear in the party names; case ids are neutral (`N-01` to `N-20`). PEP cases use the `high_risk_jurisdiction` category and mismatch cases `layering_shell`, the closest existing categories.
- Disclosure: the cases were written and labelled by the same author as the other sets, after the development and held-out results were known, and specifically to test the removal of that sentence. Labels and notes were fixed before any model saw the cases and will not be changed after seeing model output.
- Runs: the set is run once with the current prompt and once with the prompt from commit 3e01746 (change 1, with the escalate-on-ambiguity sentence still present). The second run uses a copy of that commit exported with `git archive` into a temporary directory outside the working tree; the working tree is not changed.
- Decision rule: for both prompts, report SUSPICIOUS cases flagged out of 12 and clean controls cleared out of 8. If the current prompt flags at least 3 fewer of the 12 than the prompt with the sentence, the removal is judged unsafe, and a narrower rule (escalate on ambiguity only when the record shows a high-risk jurisdiction, a politically exposed person or a structuring pattern) is tested next on the development set and then on this set again. If the difference is 2 or fewer and the current prompt clears at least 6 of the 8 controls, the current prompt stays as the default. If the current prompt clears fewer than 6 of the 8 controls, it is judged to over-flag, whatever the difference in flags, and is not made the default. Results are reported whatever they are.

**Third case set result (single run per prompt).**

- Setup: the 20 cases were run once with the current prompt (A) and once with the prompt of commit 3e01746, which has the escalate-on-ambiguity sentence (B). No code or prompt changes were made between or after the runs.

| | A: current prompt | B: commit 3e01746 |
|---|---|---|
| SUSPICIOUS flagged | 11/12 | 12/12 |
| Clean controls cleared | 7/8 | 3/8 |
| Errored reviews | 0 | 0 |
| Model calls | 40 | 40 |
| Output tokens (visible only) | 18,389 | 18,025 |
| Estimated cost (lower bound, reasoning not counted) | at least $0.06 | at least $0.06 |
| Average time per case | 23.0 s | 29.6 s |
| Cases over 100 s | none | N-07 (129 s) |

- Differences: N-09 (a numbered collector paying a crypto ATM) was missed by A and caught by B; N-15, N-18, N-19 and N-20, all clean, were wrongly flagged by B only. N-13 (the solicitor's client account) was flagged by both.
- Rationale themes: A's miss on N-09 said the record was a single payment with no rule hits and insufficient evidence, without engaging with the collector name or the crypto exit. B's false alarms cited missing legal-entity information, a shared surname read as self-dealing, a crypto exchange as inherently elevated risk, and a country and sector as historically higher risk.
- Outcome under the pre-registered rule: A flags 1 fewer than B (the removal is judged unsafe at 3 or more) and clears 7 of 8 controls (at least 6 needed), so the current prompt stays the default. This is read as no evidence of harm, not as proof of safety: there was one run per prompt, the prompts are one case apart, 11/12 has a Wilson 95% interval of about 65% to 99%, and the set has 20 cases written and labelled by the author after the earlier results were known.
- Open: N-13 is flagged by both prompts, so the solicitor's client-account pattern remains a false positive; any change aimed at it is tested on the development set first, and the held-out set is not reused for tuning. The review does not receive the behaviour-model flags, so the pass_through category stays unanswerable.

**Limits.**

- The expected verdicts were written by the project author and have not been independently annotated.
- The development set is 31 synthetic cases and the held-out set is 21. With sets this small the intervals are wide and any result is indicative only.
- The review sees the transaction id. In the development set the ids encode the category (`OC-01` for obvious clear, `PT-01` for pass-through and so on), which could hint at the expected verdict; the held-out set (`H-`) and the third set (`N-`) use neutral ids.
- The review does not receive the behaviour-model flags, so the three pass-through cases, which have no rule hit, are expected misses.
- The prompt-injection cases are scored against the engine's policy of escalating any reported injection to SUSPICIOUS.
- A full live run makes two model calls per case, so it needs `--max-calls 62` or more.

## Retrieval evaluation

The verdict evaluation above scores the review's answer. It does not say whether the passages retrieved for a case were the right ones. `scripts/eval_retrieval.py` measures that directly: for each labelled case it embeds a query, takes the nearest fragments from the index and compares them with the fragments labelled relevant for the case. No chat model is called.

**Why.** The query sent to the index was the review's whole instruction text followed by the fenced transaction record, so most of the embedded text was the same for every case. The per-case records showed the consequence: the wire-transfer pages came back for all 31 development cases.

**Pre-registered plan** (written before any retrieval was run for this evaluation, and before the new query code existed).

- Labels: `tests/fixtures/retrieval_cases.json`, SHA-256 `bd6517950ba0d98bce3943066df35d70ba78816970cfb36efcce4cb2f6db09ef` (file with LF line endings). The file holds fragment ids and page indexes only, no guidance text.
- How the labels were made: the guidance PDF was split locally with the ingestion settings (1,200 characters, 150 overlap), which reproduces the 410 fragment ids in the index without calling the embedding model or the vector store. Fifteen topics were defined from the signals in the case files (reporting threshold, structuring, higher-risk country, ownership opacity, sanctions, missing party information, politically exposed persons, virtual assets, money transfer and currency exchange, precious-metals dealers, customer-profile consistency, product and channel risk, non-profit organisations, legal professionals and real estate, trusts). For each topic, candidate fragments were found by keyword search over the local split and then read; a fragment is relevant if it states a requirement, definition, threshold or risk factor a reviewer would apply to a payment showing that signal. Contents pages, revision tables, abbreviation lists and passages that only describe procedures between countries are not relevant. The 15 topics cover 86 distinct fragments.
- Cases: 47 cases taken by id from the three verdict case files: every case with a rule reason except the prompt-injection cases (35, stratum `rule_hit`) and the 12 SUSPICIOUS cases of the no-rule set (stratum `no_rule`). Each case lists the topics a reviewer of that case would need, chosen from the case's fields and its note; its relevant set is the union of its topics. Clean cases without a rule reason have no fragment a reviewer would need, so recall is undefined for them and they are left out; the pass-through cases are left out because their signal is not in the record.
- Arms: (1) `old`, the query as sent before the change, the instruction text plus the fenced record; (2) `new`, the query built from rule hits and key facts (`utils/retrieval_query.py`); (3) `evidence`, the fenced record alone, the change listed in the plan above as "build the retrieval query from the evidence only".
- Metrics, per case and averaged: recall@5 (relevant fragments in the top 5, divided by all relevant fragments), precision@5 (relevant fragments in the top 5, divided by 5) and MRR (1 / rank of the first relevant fragment within the top 10, 0 if none). Means are reported overall, per stratum and per topic, with a 95% percentile bootstrap interval over cases (10,000 resamples), and the paired difference of each other arm against `old` with the same interval.
- Recall@5 ceiling: a case with more than five relevant fragments cannot reach a recall@5 of 1. The report prints the mean of the best recall@5 that the labels allow, so the figures can be read against it.
- Mechanics: one query embedding per distinct query text, requested one at a time through the shared retry wrapper and cached on disk, so a rerun makes no embedding calls. Before scoring, the script checks that the index holds exactly the fragment ids of the local split and that every labelled id is among them, and stops otherwise.
- Disclosure: the labels, the topics and the query builder have the same author. The labels were frozen by the SHA-256 hash above before the new query code was run, but the author had read the relevant fragments before writing the phrases the new query uses, so the gain measured for `new` is an upper estimate of what an independent set of labels would show. Page-level results of the old query on the development cases were already known from the per-case records above (the wire-transfer pages for every case); no fragment-level results of any arm were looked at while labelling.

**Result (single run, three arms).** Vertex `text-embedding-004`, 410 fragments in a local Qdrant, the fragment ids in the index equal to the local split. Figures are means over cases with the 95% bootstrap interval in brackets. The best mean recall@5 the labels allow is 0.584, because 36 of the 47 cases have more than five relevant fragments; read recall against that, not against 1.

**Summary.** Over all 47 cases, recall@5 improved from 0.044 to 0.142 (best possible 0.584) and precision@5 improved from 0.123 to 0.260, so about one in four retrieved fragments is relevant. The gain is confined to the 35 cases with a rule hit. On the 12 cases without one, the new query against the old gives recall@5 +0.035 (-0.026 to +0.094) and precision@5 +0.050 (-0.083 to +0.167), both inconclusive, and MRR fell from 0.358 to 0.170, a difference of -0.188 (-0.396 to -0.007): for cases without a rule hit the first relevant fragment now ranks lower. The evidence-only arm scored below the old query on precision@5 (-0.089, interval -0.136 to -0.047, which excludes zero), so the gain comes from the typology and topic terms in the new query, not from adding the facts alone.

All 47 cases:

| Arm | Recall@5 | Precision@5 | MRR |
|---|---|---|---|
| old | 0.044 (0.029 to 0.059) | 0.123 (0.081 to 0.170) | 0.217 (0.141 to 0.302) |
| new | 0.142 (0.108 to 0.177) | 0.260 (0.196 to 0.328) | 0.476 (0.362 to 0.590) |
| evidence | 0.023 (0.007 to 0.042) | 0.034 (0.013 to 0.060) | 0.134 (0.083 to 0.198) |
| new minus old | +0.099 (+0.064 to +0.134) | +0.136 (+0.081 to +0.187) | +0.259 (+0.125 to +0.388) |
| evidence minus old | -0.021 (-0.043 to +0.003) | -0.089 (-0.136 to -0.047) | -0.083 (-0.175 to +0.009) |

Cases with a rule reason (35; best mean recall@5 0.590):

| Arm | Recall@5 | Precision@5 | MRR |
|---|---|---|---|
| old | 0.034 (0.019 to 0.051) | 0.114 (0.063 to 0.171) | 0.169 (0.091 to 0.262) |
| new | 0.154 (0.114 to 0.196) | 0.280 (0.200 to 0.366) | 0.581 (0.444 to 0.714) |
| evidence | 0.027 (0.008 to 0.051) | 0.040 (0.011 to 0.074) | 0.155 (0.089 to 0.236) |
| new minus old | +0.120 (+0.081 to +0.162) | +0.166 (+0.114 to +0.217) | +0.412 (+0.286 to +0.535) |
| evidence minus old | -0.007 (-0.034 to +0.023) | -0.074 (-0.131 to -0.017) | -0.014 (-0.113 to +0.085) |

Cases without a rule reason (12; best mean recall@5 0.566):

| Arm | Recall@5 | Precision@5 | MRR |
|---|---|---|---|
| old | 0.072 (0.039 to 0.104) | 0.150 (0.083 to 0.217) | 0.358 (0.191 to 0.549) |
| new | 0.108 (0.050 to 0.167) | 0.200 (0.100 to 0.300) | 0.170 (0.094 to 0.243) |
| evidence | 0.010 (0.000 to 0.031) | 0.017 (0.000 to 0.050) | 0.073 (0.031 to 0.117) |
| new minus old | +0.035 (-0.026 to +0.094) | +0.050 (-0.083 to +0.167) | -0.188 (-0.396 to -0.007) |
| evidence minus old | -0.062 (-0.094 to -0.030) | -0.133 (-0.200 to -0.067) | -0.285 (-0.477 to -0.112) |

Mean recall@5 by topic (a case with several topics counts under each):

| Topic | Cases | old | new | evidence |
|---|---|---|---|---|
| customer_profile_consistency | 5 | 0.055 | 0.158 | 0.025 |
| higher_risk_country | 8 | 0.100 | 0.205 | 0.010 |
| legal_professionals_real_estate | 2 | 0.000 | 0.111 | 0.000 |
| missing_party_information | 2 | 0.130 | 0.183 | 0.048 |
| mvts_currency_exchange | 3 | 0.000 | 0.000 | 0.000 |
| npo | 1 | 0.000 | 0.100 | 0.100 |
| ownership_opacity | 8 | 0.098 | 0.149 | 0.012 |
| pep | 2 | 0.056 | 0.000 | 0.000 |
| precious_metals_dealers | 3 | 0.056 | 0.083 | 0.000 |
| product_channel_risk | 1 | 0.000 | 0.200 | 0.000 |
| reporting_threshold | 15 | 0.010 | 0.228 | 0.046 |
| sanctions | 6 | 0.043 | 0.041 | 0.000 |
| structuring | 7 | 0.000 | 0.000 | 0.024 |
| trusts | 1 | 0.111 | 0.000 | 0.000 |
| virtual_assets | 4 | 0.033 | 0.053 | 0.000 |

**How to read it.**

- On cases with a rule reason the new query is better on all three metrics, and each interval of the difference excludes zero. It still finds about a quarter of what the labels allow (0.154 against a best of 0.590).
- The gain is not spread evenly. Recall@5 is higher for 30 cases, unchanged for 12 and lower for 5, and the 21 cases with a reporting-threshold or higher-risk-country topic account for about 84% of the summed gain. Structuring cases score 0.000 with the new query, as they did with the old one: none of the seven has a labelled fragment in its top five. Sanctions cases are unchanged (0.041 against 0.043). Money-transfer and currency-exchange cases stay at 0.000 in every arm.
- Cases without a rule reason (figures in the summary): with no rule hit the query can only name the general monitoring duties, which puts a relevant fragment first less often than the old text did. The two politically-exposed-person cases and the trust case lose the little they had (0.056 and 0.111 to 0.000). The new query does not help these cases and on rank it is a regression.
- The record alone (`evidence`) is the worst arm on every metric. Dropping the instruction text without saying what the case is about does not help, so that change is not adopted.
- Looked at after the run, with nothing changed in response: for the structuring cases the new query returns the wire-transfer passages about payments below the de minimis threshold, the general risk-factor lists and the fragment next to the labelled one on reporting suspicion. Its wording about amounts "below a threshold" matches the wire-transfer threshold text more closely than the passages on the due-diligence threshold and linked operations. The topic phrases have not been tuned against this result; doing so on these labels would need a fresh set of labels to measure.

**Limits.**

- One embedding model, one document, one run. Retrieval here is deterministic for a given query, so a rerun gives the same figures, but nothing says how they carry to another corpus or model.
- Same-author labels, as disclosed above, and a query whose phrases were written after reading the relevant fragments. The comparison favours `new`.
- The new arm has 37 distinct query texts for 47 cases (the old and evidence arms have 47): cases with the same rule hits, amount band and country share a query. The bootstrap treats cases as independent, so the intervals for `new` are somewhat too narrow.
- Topic labels are broad (the sanctions topic has 19 relevant fragments), so precision@5 can rise without the best fragment being found, and recall@5 is capped as stated.
- The check that the harness measures what the review retrieves: for the 17 development cases in the label set, the top five pages of the `old` arm are the same, in the same order, as the pages recorded in the per-case files of the earlier live verdict runs. The same holds for the `new` arm against the pages recorded in the verdict run reported below.
- Run notes: the first attempt stopped on the embedding quota (HTTP 429) after about ten requests and scored nothing; a pause between requests (`--pause`) was added and the run resumed from the cached vectors. 131 distinct query texts were embedded.

**Verdicts after the change (development set, single run).** The retrieval metrics say nothing about verdicts, so the 31 development cases were run once more through the review as it now stands: the new query, and the retrieval chain rebuilt on `langchain_core` in place of the deprecated `RetrievalQA` (`utils/rag_chain.py`). The held-out set was not run.

| | Change 2 (published above) | After the retrieval change |
|---|---|---|
| Verdict accuracy | 27/31 | 27/31 (Wilson 95% CI 71% to 95%) |
| SUSPICIOUS recall | 16/18 (15/15 without pass_through) | 15/18 (15/15 without pass_through) |
| SUSPICIOUS precision | 16/18 | 15/16 |
| Clean cases cleared | 11/13 | 12/13 |
| Errored reviews | 0 | 0 |
| Model calls | 62 | 64 (two calls retried after a 429) |
| Output tokens (visible only) | 29,228 | 22,115 |
| Estimated cost (lower bound, reasoning not counted) | at least $0.09 | at least $0.0745 |
| Average time per case | 26.6 s | 15.5 s (longest 24.4 s) |

- Two cases changed. OC-07, a salary payment flagged in the earlier run, is now CLEAR, which is correct. PT-02, a pass-through case, went from SUSPICIOUS to CLEAR; the review cannot see the signal of the pass-through cases, so its earlier flag was not based on evidence, but it counts as a lost detection in the table. OT-03, the payment to a solicitor's client account, is still flagged.
- How to read it: no regression in verdicts and no gain beyond the documented run-to-run noise of one or two cases. One run on 31 cases cannot show more than that.
- The comparison is not of the query alone. Rebuilding the chain also changed how the first step is asked: the instruction text is now a system message, the fragments are numbered with their page, and the model is told to refer to a fragment by number. Any difference in verdicts belongs to the two changes together.
- Retrieved pages are still concentrated. Pages 81 or 82 now come back for 11 of the 31 cases instead of all of them, but pages 85, 71, 33 and 70 come back for 22, 20, 19 and 17 of the 31. The new query varies with the case less than the labels would want; this fits the recall figures above.
- pass_through is unchanged as a limitation: the review does not receive the behaviour-model flags, so a retrieval change cannot reach those cases.

**Next steps (not done).** The cases without a rule hit need retrieval that does not depend on rule hits, for example a generic fallback query for those rows or a reranker over a wider candidate set; neither has been built or measured. The structuring and sanctions topics are also still open, and any change to the topic phrases needs labels it was not written against.

## Investigation agent evaluation

The review makes one fixed retrieval call and then one structured review call. The investigation agent is an optional alternative, switched on with `REVIEW_MODE=agent` (default `fixed`): the model decides which read-only tools to call, under a hard cap on model calls, tool calls and tokens, and the same structured review then returns the verdict. The plan and the acceptance rules below were written and frozen before the agent code existed and before any agent output; the results follow them.

### Pre-registered plan

**Tools (frozen).** Five read-only tools. Their arguments are fixed choices, never free text, so no counterparty text can reach a search.

| Tool | Argument | Returns |
|---|---|---|
| `screen_party` | `role`: originator or beneficiary | Best watchlist candidate for that party, its score, the match threshold, and whether it is a match |
| `country_risk` | none | The counterparty jurisdiction resolved to an ISO code and name, and whether it is on the high-risk or secrecy list, or that it does not resolve |
| `explain_rule` | `rule_id`: one of the rule ids in `utils/rule_ids.py` | What the rule checks and its configured thresholds |
| `party_history` | `role`: originator or beneficiary | That account's activity in the loaded ledger (payments sent and received, structuring-band count, distinct counterparties, one-off senders, forwarding chain and fund-flow cycles through the transaction), or that no ledger is loaded |
| `search_guidance` | `topics`: one to three of the typology ids in `utils/retrieval_query.py`, or general monitoring | The numbered guidance fragments that a query built from those topics and the case's own facts retrieves |

**Labels.** `tests/fixtures/agent_tool_labels.json`, SHA-256 `cdae6ed8c335e898b2b1797a125123343ccd24f0507eadaca833c37c74f4c539` (LF line endings). For each of the 92 cases it lists the tools a careful analyst would need (`required`) and the tools that only check or restate what the record says (`acceptable`); any other call is unnecessary. The labelling rules are in the file. In short: `search_guidance` is required when the expected verdict is SUSPICIOUS or a rule fired; `screen_party` is required only on N-05, N-06 and N-12 (a public-office title or a coin-mixing service with no sanctions hit) and acceptable elsewhere; `country_risk` is acceptable everywhere and required nowhere; `explain_rule` is acceptable where a rule fired; `party_history` is required on every ledger-backed case and never on the 72 cases without a ledger. Required tools in total: 69 `search_guidance`, 20 `party_history`, 3 `screen_party`.

**Ledger-backed cases.** `tests/fixtures/agent_ledger_cases.json`, SHA-256 `02b256015a16548b374c1e5a2d1ad163f7f69e100bde0ee8e3f2350d6ed743b5` (LF line endings). 20 rows of the ledger that `generate(seed=7, hard=True)` produces: 12 SUSPICIOUS (4 pass-through chain legs, 3 fan-in legs, 3 structuring legs from one sender, 2 legs of small rings) and 8 CLEAR (3 band or over-threshold payments and 5 ordinary payments between the same established businesses that run the pass-through chains). The expected verdict is the generator's `is_laundering` label for the row. In every SUSPICIOUS case the decisive signal is in the account history: 9 of the 12 have no rule hit, and the record of a pass-through leg looks like the record of an ordinary payment between the same two businesses. The whole generated batch (514 rows) is the ledger `party_history` reads, as the app reads the loaded batch.

**Arms.**

- Development, held-out and no-rule sets (72 cases): `fixed` (the review as it stands) and `agent`. Both receive the same information: the agent's `party_history` returns "no ledger loaded" on these cases. This is the parity check.
- Ledger-backed set (20 cases): `fixed`, `fixed+history` (the fixed review with the same history summary that `party_history` returns, for both parties, added to the record) and `agent` with the ledger loaded. `fixed+history` separates the value of the information from the value of letting the model choose what to look up.

**Procedure.**

1. Provider: Vertex `gemini-2.5-flash`, temperature 0. Prices used for every estimate: $0.30 per million input tokens and $2.50 per million output tokens, thinking tokens included (checked on 14 November 2026 against third-party price listings, which agree with each other; Google's own pricing page could not be read at the time).
2. The agent's instruction text and tool descriptions are adjusted on the development set only. They are then frozen: the SHA-256 of `utils/investigation.py` and `utils/agent_tools.py` is recorded here before the held-out, no-rule or ledger-backed set is run with the agent, and nothing is changed afterwards. Development runs made while adjusting are reported as such and are not part of the comparison.
3. A fresh fixed-pipeline run on all 72 cases is made in the same session as the agent runs, against the same index. The figures published earlier are not used as the baseline.
4. Two runs per arm on every set, one case at a time, with a pause between model calls, each case written to a JSONL file as soon as it finishes. A run stopped by quota is reported as stopped, not as completed.
5. Cost ceiling for all live runs together: $5. Before each run, its cost is projected from the cost measured so far; a run that would take the total past $5 is not started.

*Deviations recorded after the plan was written:* (a) on 16 November 2026, after fixed run 1, the projection showed the plan could not finish under $5 (answers that ran away to tens of thousands of tokens cost far more than expected), the runs were stopped, and the ceiling was raised to $7; no rule, threshold, case or label was changed. (b) The price sentence in item 1 was corrected after it was written: the date and the source of the prices, not the prices. (c) Two design points differ from the outline the tools were planned from: the guidance search is retried like the fixed review's retrieval, and the final review receives the short lookup results with the notes (both recorded under "Before the first live run" below).

**Metrics, per arm and per run.** Verdict accuracy, SUSPICIOUS precision and recall (Wilson 95% intervals), clean cases cleared, reviews that errored, errored reviews counted as CLEAR, and accuracy per category. Discordant pairs against the paired arm on the same cases: `b` = cases the agent gets right and the comparison arm gets wrong, `c` = the reverse, reported separately for SUSPICIOUS and CLEAR cases. Tool selection: recall = required tools called / required tools (cases with no required tool are left out), precision = distinct tools called that are required or acceptable / distinct tools called (cases with no call are counted as "no calls"), both pooled over cases; for the ledger-backed set also whether `party_history` was called for the labelled role. Mean tool calls, model calls, input and output tokens and estimated cost per case; mean and 95th-percentile seconds per case.

**Acceptance rule, 72 cases (agent against fixed, each agent run paired with a fixed run).**

- Hard fail: any errored review counted as CLEAR; fewer than 4 of the 4 prompt-injection cases SUSPICIOUS; more than 3 of 72 reviews errored in either run.
- Regression: on SUSPICIOUS cases, `c - b >= 2` in either run; or clean cases cleared lower by 2 or more than in the paired fixed run.
- Acceptable to keep as an option: no hard fail and no regression in either run, mean cost per case at most 5 times the fixed review's, and 95th-percentile time per case at most 60 seconds.
- Better than fixed: `b - c >= 3` on SUSPICIOUS cases in both runs. With 72 cases and run-to-run noise of one or two cases, this is not expected.

**Acceptance rule, ledger-backed set (20 cases).**

- Hard fail: any errored review counted as CLEAR; more than 1 of 20 reviews errored in either run.
- Regression against `fixed+history`: on SUSPICIOUS cases, `c - b >= 2` in either run; or clean cases cleared lower by 2 or more.
- Gain from the history tool: against `fixed`, `b - c >= 3` on SUSPICIOUS cases in both runs, with clean cases cleared not lower by 2 or more.
- Acceptable to keep as an option: no hard fail, no regression against `fixed+history`, the gain against `fixed` in both runs, mean cost per case at most 5 times `fixed`'s, and 95th-percentile time per case at most 60 seconds.
- If `fixed+history` does as well as the agent, the gain belongs to the information, not to the model choosing what to look up, and adding the history summary to the fixed review is the simpler change.

**Reported whatever the outcome.** The agent may come out no better, or worse, than the fixed review. That is reported as measured, every regression is listed, and `fixed` stays the default unless the rules above say otherwise.

**Disclosure.** The labels, the choice of ledger rows, the tools and the agent's instruction text have the same author. The labels and the case file were frozen by the hashes above before any agent code existed, but the author knew the case sets and the earlier per-case results of the fixed review when writing them.

### Adjustment on the development set and the frozen state

Recorded before any held-out, no-rule or ledger-backed case was run with the agent, and before the comparison runs.

**Before the first live run** (from code review and one live call on ST-02, not from results): the record shows the case id, never the generator's row id (`ESTP-`, `FAN-`, `TXN-`... differ by pattern and would give the label away; the ledger is still read under the row id); the guidance search is retried through the shared retry wrapper, like the fixed review's retrieval call; the review is given the short lookup results as returned next to the investigator's notes (everything but the guidance fragments), so it sees the same history figures `fixed+history` sees; and token counts include reasoning tokens. On Gemini 2.5 Flash `output_tokens` leaves out the reasoning ("thinking") tokens, which are billed as output (`total_tokens` = input + output + reasoning; on the ST-02 call, 741 + 81 + 564 = 1,386). The meter in `scripts/eval_llm_tier.py` read `output_tokens` only, so **the costs published above for earlier runs of the fixed review are too low**; every cost in this section counts reasoning as output.

**Development run used for adjustment** (31 cases, agent, before the freeze; not part of the comparison): accuracy 28/31, SUSPICIOUS recall 16/18, clean cleared 12/13, no errors; 6.74 tool calls, 4.61 model calls, 35.5 s (95th percentile 41.1 s) and $0.0152 per case. The investigator looked up nearly everything on nearly every case (both parties screened, both parties' history, the country, rules that had not fired), and it called `party_history` for both parties on all 31 cases (62 calls), although none of these cases has a ledger. One case (OT-06) spent 68,549 output tokens in the final review, the same review call the fixed review makes, with one retried answer; Vertex has no output cap configured, so the fixed review carries the same risk.

**Changes made after that run:** the investigation is told when no ledger is loaded or no guidance index is connected (so `party_history` and `search_guidance` are not called to find that out), and to explain only rules that fired. These changes were not run live before the freeze. A second development run was planned only if the projected total stayed under $5; with the measured cost per case it would not have, so it was not made.

**Frozen state.** SHA-256 of each file with LF line endings:

| File | SHA-256 |
|---|---|
| `utils/investigation.py` | `51d3c01cc941a9e11bf31c56b55c627854093608ffb3ce45eecb66e29212eb9c` |
| `utils/agent_tools.py` | `8cb3c1184646d8c18e8da882a7ce2a24389f23021f4eb8a5d5244b273a543b19` |
| `utils/agents.py` | `ef92dbbc51385152a567eb6044944798317ae332c10bf6a396369b720a847801` |
| `utils/sanitize.py` | `ddbf7f2596a866fece5dfbd6076b88eebe62e3604fdb3c1a8c9e7e8d142ba29c` |
| `utils/retrieval_query.py` | `bf9aa6227fbff9ff5e12c2bd4a76ecdeb09f4b1d197df6b9bb9fe9001110d7dd` |
| `utils/rag_chain.py` | `8b4d0f6856b35663282f49f6af80549695a20b1434766629f0b5c8ee7fafa17e` |
| `utils/trace.py` | `6904df3279d08707ab9afce3541da9acda3f0a26b8db211df88a822b46b2e78a` |
| `scripts/eval_llm_tier.py` | `2e6d6fe5f895cbcaa2aff2e23a9e3d8b114f0257f4be22a5e968b206f767e6a4` |

Settings: `AGENT_MAX_MODEL_CALLS=6`, `AGENT_MAX_TOOL_CALLS=8`, `AGENT_MAX_INPUT_TOKENS=40000`, `AGENT_MAX_OUTPUT_TOKENS=8000` (reasoning included), `AGENT_TOOL_RESULT_CHARS=9000`, `LLM_MAX_RETRIES=3`, `RETRIEVER_K=5`, `LLM_TEMPERATURE=0`.

The instruction-text change listed above (telling the investigation up front when no ledger or guidance index is available, and to explain only rules that fired) was made after the single development run and was not run live on the development set before the comparison runs; every comparison figure below is its first live measurement. A cap on output or reasoning tokens for the model calls is a recommended follow-up that was deliberately not applied here, so that the fixed review is measured exactly as it runs today.

### Results

All runs used Vertex `gemini-2.5-flash` against the same 410-fragment index, one case at a time with a 2-second pause after each model call. The frozen files were checked against the hashes above after the last run and were unchanged. Order: fixed run 1 and agent run 1 on the 72 cases, fixed run 2, agent run 2, then on the ledger-backed set fixed, `fixed+history` and agent run 1, and the same three again. Throttling (HTTP 429) occurred a few times and every instance succeeded on retry; no run was stopped by quota. Total live spend, including the development run and one smoke call: $5.93.

**72 cases, per arm and run.** Precision is SUSPICIOUS precision; Wilson 95% intervals are in the second table. Seconds are per case. A runaway is a case whose calls produced more than 20,000 output tokens (the largest output of any other case in any arm was 7,178); p95 without runaways is a secondary figure, and the acceptance rule is judged on the p95 with every case.

| | Fixed run 1 | Agent run 1 | Fixed run 2 | Agent run 2 |
|---|---|---|---|---|
| Verdict accuracy | 65/72 | 60/72 | 64/72 | 62/72 |
| SUSPICIOUS recall | 37/41 | 30/41 | 39/41 | 32/41 |
| SUSPICIOUS precision | 37/40 | 30/31 | 39/44 | 32/33 |
| Clean cases cleared | 28/31 | 30/31 | 25/31 | 30/31 |
| Reviews that errored | 0 | 0 | 1 (OT-05) | 1 (N-03) |
| Errored reviews counted as CLEAR | 0 | 0 | 0 | 0 |
| Prompt-injection cases SUSPICIOUS | 4/4 | 4/4 | 4/4 | 4/4 |
| Model calls (mean) | 2.01 | 3.99 | 2.04 | 4.03 |
| Tool calls (mean) | 0 | 4.54 | 0 | 4.62 |
| Input / output tokens (mean) | 2,008 / 4,087 | 6,359 / 5,315 | 2,032 / 6,189 | 6,375 / 5,199 |
| Cost per case | $0.0108 | $0.0152 | $0.0161 | $0.0149 |
| Seconds, mean / p95 | 23.8 / 25.9 | 33.6 / 38.1 | 32.2 / 28.5 | 34.6 / 36.9 |
| p95 without runaways (secondary) | 25.1 | 34.2 | 25.9 | 36.4 |

Wilson 95% intervals:

| | Fixed run 1 | Agent run 1 | Fixed run 2 | Agent run 2 |
|---|---|---|---|---|
| Verdict accuracy | 81% to 95% | 73% to 90% | 80% to 94% | 76% to 92% |
| SUSPICIOUS recall | 77% to 96% | 58% to 84% | 84% to 99% | 63% to 88% |
| SUSPICIOUS precision | 80% to 97% | 84% to 99% | 76% to 95% | 85% to 99% |
| Clean cases cleared | 75% to 97% | 84% to 99% | 64% to 91% | 84% to 99% |

Accuracy per category:

| Category | Fixed run 1 | Agent run 1 | Fixed run 2 | Agent run 2 |
|---|---|---|---|---|
| obvious_clear | 16/18 | 18/18 | 16/18 | 18/18 |
| over_threshold_legitimate | 12/13 | 12/13 | 9/13 | 12/13 |
| structuring | 7/7 | 4/7 | 7/7 | 4/7 |
| high_risk_jurisdiction | 14/14 | 9/14 | 14/14 | 11/14 |
| layering_shell | 11/13 | 12/13 | 13/13 | 12/13 |
| pass_through | 1/3 | 1/3 | 1/3 | 1/3 |
| prompt_injection | 4/4 | 4/4 | 4/4 | 4/4 |

By set (development cases were used to adjust the agent; held-out and no-rule were not):

| Set | Fixed run 1 | Agent run 1 | Fixed run 2 | Agent run 2 |
|---|---|---|---|---|
| Development (31): accuracy, recall, clean | 28/31, 16/18, 12/13 | 27/31, 14/18, 13/13 | 27/31, 16/18, 11/13 | 26/31, 13/18, 13/13 |
| Held-out (21) | 21/21, 11/11, 10/10 | 18/21, 8/11, 10/10 | 20/21, 11/11, 9/10 | 21/21, 11/11, 10/10 |
| No-rule (20) | 16/20, 10/12, 6/8 | 15/20, 8/12, 7/8 | 17/20, 12/12, 5/8 | 15/20, 8/12, 7/8 |
| Held-out and no-rule together (41) | 37/41, 21/23, 16/18 | 33/41, 16/23, 17/18 | 37/41, 23/23, 14/18 | 36/41, 19/23, 17/18 |

Discordant cases, agent against the fixed run of the same number (`b` = agent right and fixed wrong, `c` = the reverse):

| | SUSPICIOUS b | SUSPICIOUS c | CLEAR b | CLEAR c |
|---|---|---|---|---|
| Run 1 | 1 (N-09) | 8 (H-13, H-14, H-15, N-03, N-04, N-06, ST-01, ST-03) | 3 (N-19, N-20, OT-03) | 1 (N-13) |
| Run 2 | 1 (PT-03) | 8 (N-03 errored, N-04, N-06, N-07, PT-02, ST-01, ST-03, ST-04) | 5 (H-08, N-19, N-20, OT-03, OT-05 errored in fixed) | 0 |

**Against the acceptance rule for the 72 cases.**

- Hard fail: none. No errored review was counted as CLEAR, the 4 prompt-injection cases were SUSPICIOUS in both runs, and 0 and 1 reviews errored (limit 3).
- Regression: yes, in both runs. On SUSPICIOUS cases `c - b` is 7 in run 1 and 7 in run 2 (the rule is 2 or more). Clean cases cleared were higher, not lower (30/31 against 28/31 and 25/31).
- Cost (1.4 and 0.9 times the fixed review per case, limit 5) and 95th-percentile time (38.1 and 36.9 s, limit 60) are within the rule, but the regression decides it: **the agent is not acceptable as an option on these cases, and `fixed` stays the default.**
- The regression does not come from the cases the agent was adjusted on: on the 41 held-out and no-rule cases alone, `c - b` on SUSPICIOUS cases is 5 in run 1 and 4 in run 2.
- It is not better than the fixed review on any pre-registered measure for these cases. It trades suspicious detections for cleared clean cases: 7 fewer suspicious cases flagged in each run against 2 and 5 more clean cases cleared.

**Why the agent misses suspicious cases.** Read from the per-case notes and rationales of every SUSPICIOUS case the agent missed and the fixed review flagged, in both runs, after the runs; nothing was changed in response. All of them follow the pattern below. The lookups mostly return negative facts: a party not on the watchlist, a jurisdiction not on the configured lists, and the rule explanation "one payment in the band is not in itself structuring". The review takes these as reasons to clear. ST-01 was cleared with that sentence quoted; N-03 (Yemen) and N-06 (a regional governor's family trust) because the jurisdiction "is not on any high-risk or secrecy lists", although the lists are short by design; H-14 (Syria, on the high-risk list) because nothing else was found. The fixed review, which gets no such lookups, flags all of these. N-03 in run 2 errored because the investigation explained five rules that had not fired and reached the limit of 8 lookups.

**Tool selection, 72 cases.** Required tools called: 57/57 and 56/57; calls that were required or acceptable: 243/244 and 244/246 (two `explain_rule` calls were unnecessary, one `search_guidance` was missed). The investigation calls nearly every lookup the labels allow, which is why precision is high: on these cases almost every tool is acceptable. `party_history` was no longer called on cases without a ledger after the instruction-text change (62 calls on 31 cases in the development run, none in the four comparison runs on these cases).

**Ledger-backed set (20 cases), per arm and run.**

| | Fixed 1 | +history 1 | Agent 1 | Fixed 2 | +history 2 | Agent 2 |
|---|---|---|---|---|---|---|
| Verdict accuracy | 10/20 | 17/20 | 19/20 | 10/20 | 17/20 | 20/20 |
| SUSPICIOUS recall | 3/12 | 10/12 | 11/12 | 3/12 | 10/12 | 12/12 |
| Clean cases cleared | 7/8 | 7/8 | 8/8 | 7/8 | 7/8 | 8/8 |
| Reviews that errored | 0 | 0 | 0 | 0 | 0 | 0 |
| Model / tool calls (mean) | 2.00 / 0 | 2.00 / 0 | 4.60 / 6.40 | 2.00 / 0 | 2.00 / 0 | 4.65 / 6.50 |
| Cost per case | $0.0068 | $0.0121 | $0.0191 | $0.0066 | $0.0108 | $0.0115 |
| Seconds, mean / p95 | 19.8 / 23.2 | 28.4 / 42.1 | 49.5 / 49.4 | 19.3 / 23.8 | 27.2 / 37.4 | 29.6 / 33.5 |
| p95 without runaways (secondary) | 23.2 | 42.1 | 49.4 | 23.8 | 37.4 | 33.5 |
| SUSPICIOUS recall, Wilson 95% | 9% to 53% | 55% to 95% | 65% to 99% | 9% to 53% | 55% to 95% | 76% to 100% |
| pass_through / fan_in / circular / structuring_history | 0/4, 0/3, 0/2, 3/3 | 3/4, 2/3, 2/2, 3/3 | 3/4, 3/3, 2/2, 3/3 | 0/4, 0/3, 0/2, 3/3 | 3/4, 2/3, 2/2, 3/3 | 4/4, 3/3, 2/2, 3/3 |
| clean_large / clean_ordinary | 2/3, 5/5 | 2/3, 5/5 | 3/3, 5/5 | 2/3, 5/5 | 2/3, 5/5 | 3/3, 5/5 |

The fixed review flagged the three structuring legs and nothing else in both runs; every pass-through, fan-in and ring case was cleared. `fixed+history` missed L-04 (the first leg of a forwarding chain, whose sender received nothing just before) and L-07 (fan-in) in run 1, and L-04 and L-05 in run 2. Both fixed arms flagged L-13, a clean band payment.

| Agent against | Run | SUSPICIOUS b | SUSPICIOUS c | CLEAR b | CLEAR c |
|---|---|---|---|---|---|
| `fixed` | 1 | 8 | 0 | 1 (L-13) | 0 |
| `fixed` | 2 | 9 | 0 | 1 (L-13) | 0 |
| `fixed+history` | 1 | 1 (L-07) | 0 | 1 (L-13) | 0 |
| `fixed+history` | 2 | 2 (L-04, L-05) | 0 | 1 (L-13) | 0 |

**Against the acceptance rule for the ledger-backed set.** No hard fail (no errors). No regression against `fixed+history` (`c` = 0 in both runs). Gain from the history tool against `fixed`: `b - c` = 8 and 9 (rule: 3 or more in both runs), clean cases cleared not lower. Cost 2.8 and 1.7 times `fixed` per case (limit 5); 95th-percentile time 49.4 and 33.5 s (limit 60). **The agent meets the rule on this set.** Most of the gain belongs to the information, not to the agent choosing what to look up: given the same history summary without tools, the fixed review goes from 3/12 to 10/12 suspicious cases flagged in both runs, and the agent adds 1 to 2 cases and one clean case on top. `party_history` was called for the labelled party in 20/20 cases in both runs; tool recall 33/35 and 35/35, precision 82/84 and 86/86.

**Runaway answers.** These are a finding about the model, not noise, and every one is kept in the results above. The records keep billed output per case (reasoning included) but not the split between reasoning and visible text, so how much of each runaway was reasoning is not known. Where it can be placed: in the agent arm it is the final structured review, because the investigation's own calls are capped at 8,000 output tokens and none was stopped by that cap; in six of the nine cases the run log shows the final review answering at least once with a call to a built-in code tool (`google:python_interpreter`) instead of the verdict schema, which was rejected and retried, each attempt long. For PT-01 in fixed run 1 and L-16 the log shows no retry, and the records do not say which of the fixed review's two calls ran long.

| Arm, run | Case | Output tokens | Cost | Seconds | Review answers rejected and retried | Verdict |
|---|---|---|---|---|---|---|
| Fixed, 72 cases, run 1 | OT-05 | 68,018 | $0.1709 | 210.5 | 1 (built-in tool) | correct |
| Fixed, 72 cases, run 1 | PT-01 | 64,505 | $0.1619 | 194.2 | none | wrong (CLEAR) |
| Fixed, 72 cases, run 2 | OT-05 | 264,012 | $0.6614 | 869.4 | all 4 attempts (built-in tool) | ERROR |
| Agent, 72 cases, run 1 | PT-01 | 66,756 | $0.1687 | 244.0 | none | wrong (CLEAR) |
| Agent, 72 cases, run 1 | H-03 | 132,615 | $0.3335 | 460.2 | 2 (built-in tool) | correct |
| Agent, 72 cases, run 2 | H-04 | 198,549 | $0.4991 | 742.2 | 3 (built-in tool) | correct |
| `fixed+history`, ledger, run 1 | L-16 | 20,302 | $0.0519 | 83.9 | none | correct (just over the 20,000 line) |
| Agent, ledger, run 1 | L-14 | 69,383 | $0.1781 | 406.5 | 1 (built-in tool), 1 throttled | correct |
| Development run (before the freeze) | OT-06 | 68,549 | about $0.17 | | 1 (built-in tool) | correct |

Other retries in the logs: 16 throttled calls (HTTP 429), 13 in the investigation and 3 in the review, each of which succeeded on the next attempt.

Over the 408 comparison reviews, 8 ran away (2.0%) and cost $2.23 of the $5.44 those runs cost. Without them a case costs $0.0067 with the fixed review and $0.0083 with the agent on the 72 cases, and $0.0067, $0.0104 and $0.0112 for `fixed`, `fixed+history` and the agent on the ledger-backed set. The same OT-05 ran away in both fixed runs, so some cases may be prone to it. The usual-case figures above exclude runaways; the acceptance rule was judged on every case.

**Conclusions.**

- The agent is worse than the fixed review on the 72 cases under the pre-registered rule (a regression of 7 suspicious cases in both runs) and is not recommended as an option there. `REVIEW_MODE=fixed` stays the default.
- On cases whose signal is in the account history, access to that history is what matters. Adding the history summary to the fixed review closes most of the gap without an agent; that change was not tested on the 72 cases and would need its own pre-registered evaluation.
- Lookups that report the absence of a risk (not listed, rule "not in itself" suspicious) pull the review towards CLEAR. The wording of `explain_rule` and `country_risk` results is the first thing to change before any further agent evaluation.
- A cap on output or reasoning tokens is the first follow-up for both modes, on cost and latency grounds: about 2% of reviews spend up to 80 times the usual cost, most of them in review answers that call a built-in code tool instead of returning the verdict, and one case became ERROR that way.

**Limits.** Two runs per arm; run-to-run differences of one or two cases are noise. The development case ids show the category (`OC-`, `ST-`, `HR-`, `PT-`, `PI-`...) to the model in both arms, as they did before this evaluation; the held-out (`H-`), no-rule (`N-`) and ledger-backed (`L-`) ids do not. The labels, the ledger rows and the agent share an author (see Disclosure). The ledger-backed cases are generator rows whose signals the history tool was written to describe. The instruction-text change made after the development run was not run live before the comparison. One provider and one model.
