# Fintech Fraud Auditor

A tool that screens bank transactions for money laundering using rules, statistics, network analysis and an AI model, with a compliance officer making the final call.

## What it is

The tool reads a ledger of bank transactions and works out which ones look like money laundering. Quick rules and statistics pick out the suspicious transactions, an AI model reviews those against anti-money-laundering guidance, and a compliance officer must approve before a Suspicious Activity Report (SAR) is drafted. Every decision is recorded in an audit log.

## Why it's harder than it sounds

Banks handle huge numbers of transactions. Having an AI model review every one would be slow and expensive, but filtering too aggressively lets real laundering slip through unseen. Laundering is also designed to look ordinary: a large sum is split into transfers just under the reporting limit, or money is passed around a circle of accounts until its origin is hard to trace. The challenge is narrowing the list down cheaply without throwing away the cases that matter.

## How it works

1. **Rules** flag the obvious cases: names on sanctions lists, high-risk countries, and amounts split to stay just under the reporting limit.
2. **A pattern model** looks at how each account behaves and flags the unusual ones.
3. **An AI model** reviews the flagged transactions against compliance guidance and gives a clear verdict.
4. **A network check** looks for money travelling in a circle through several accounts.
5. **A compliance officer** makes the final decision, and every step is written to an audit log.

## Results

Measured on a synthetic ledger of 465 transactions, 45 of them labelled as laundering:

| Pipeline | Laundering caught (recall) | Flags that were real (precision) | Sent to the AI model |
|---|---|---|---|
| Original order (statistics first) | 7% | 33% | 9 rows |
| Current order (rules first) | 84% | 78% | 49 rows |

By type of laundering:

| Pattern | What it looks like | Caught |
|---|---|---|
| structuring | One large sum split into transfers just under the $10,000 limit | 10 of 10 |
| sanctioned | Transfers involving a high-risk jurisdiction | 6 of 6 |
| fan_in | Many accounts sending money into one account | 11 of 11 |
| circular | Money sent around a loop in large amounts | 3 of 3 |
| circular_small | The same loop in smaller amounts, through new accounts | 7 of 7 |
| circular_camouflaged | The same loop through established businesses | 1 of 8 (known gap) |

The same author wrote both the detector and the test data, so treat these numbers as a regression check rather than a real-world detection rate. The method, the caveats and the reasoning behind each number are in [docs/EVALUATION.md](docs/EVALUATION.md).

## Architecture

```mermaid
graph TD
    A[Ledger CSV] --> B[Schema validation<br/>Decimal parsing]
    B --> C{Tier 1: deterministic rules}
    C -->|rule hit| F[Force review]
    C -->|no hit| D[Tier 0: behavioural anomaly model]
    D -->|anomalous| F
    D -->|normal| E[Dropped, logged]
    F --> G[Tier 2: RAG compliance assessment<br/>structured verdict]
    G --> N[Investigative ledger<br/>every verdict recorded: SUSPICIOUS / CLEAR / ERROR]
    N -->|analyst selects any row| H[Tier 3: LangGraph topology agent]
    H --> I{Topology score >= 70?}
    I -->|yes| J[Execution pauses for officer]
    I -->|no| R[Forensic summary]
    J -->|reject| L[Escalate, no filing]
    J -->|approve| K[SAR drafting]
    N -->|any SUSPICIOUS row| O[Officer review<br/>approve / reject / escalate]
    O -->|approve| K
    O -->|reject / escalate| L
    K --> M[Durable audit trail]
    L --> M
    N --> M
```

- **Tier 1, rules** (`utils/rules.py`): reporting threshold, the structuring band just below it, high-risk jurisdictions, fuzzy sanctions-name matching and unidentified counterparties. A rule hit always forces a review. Countries are resolved to ISO 3166-1 alpha-2 codes (`utils/jurisdictions.py`: codes, official names and common aliases such as DPRK or Burma) and compared exactly, so "Prussia" never matches "Russia". A country that can't be resolved is noted as an unresolved jurisdiction without forcing a review on its own.
- **Tier 0, behaviour model** (`utils/funnel.py`, `utils/features.py`): sixteen account-behaviour features scored against the batch's own median, including four that follow money passed straight on to another account.
- **Tier 2, AI review** (`utils/agents.py`): retrieval-grounded analysis with a structured verdict. Runs on Claude Haiku 4.5 (Amazon Bedrock) or Gemini 2.5 Flash (Google Vertex AI), switchable in config. Transient model errors such as throttling or timeouts are retried with exponential backoff, up to `LLM_MAX_RETRIES` times after the first attempt (default 3, so at most 4 calls); errors a retry can't fix, such as bad credentials or an unknown model, fail at once. SDK-level retries are switched off so this is the only retry layer: the Bedrock client uses botocore `retries={"max_attempts": 0}` (this setting counts retries after the first request; tested against a local HTTP 429 server, it sends exactly one request per call, where `1` sends two), and the Vertex clients use `max_retries=0`.
- **Tier 3, network check** (`utils/graph_nodes.py`, `utils/graph_logic.py`): circular flows, account velocity and hidden beneficiaries, over the full ledger. A high score pauses for an officer.

## Design decisions

**Rules come before statistics.** Laundering is often designed to look ordinary, so a statistical filter on its own throws it away. Rules run first, and anything they flag is always reviewed.
*Technical:* a rule hit sets a force-review flag the anomaly funnel can't override. With the original ordering, recall on the labelled set was 7%; with rules first it is 84%.

**Behaviour matters more than amount.** An account is judged by how it acts: how fast it moves money, how many parties it deals with, how often it sits just under the limit.
*Technical:* sixteen behavioural features, flagged with a robust z-score against the batch's own median, instead of a fixed "flag 15%" quota.

**The AI answers in a fixed format.** The model's verdict is one of a few set answers, not free text that has to be interpreted.
*Technical:* structured output with SUSPICIOUS, CLEAR or ERROR. An ERROR is never treated as clear.

**Ledger text is treated as hostile.** The party being audited controls the names and countries in the ledger, so anything in those fields may be an attempt to steer the AI model.
*Technical:* the defences that hold are structured output and the SAR gate. The model can only answer with the fixed verdict schema; an answer outside it becomes ERROR, and extra fields are dropped. A SAR needs an officer approval recorded as a `human_review` event, which no model output or ledger field can write. Regex scrubbing and fencing of ledger fields are a secondary layer: field values are HTML-escaped so they can't close the fence, but the scrubber only recognises plain ASCII English markers, so homoglyphs, zero-width characters and instructions in other languages pass through it. `tests/test_prompt_injection.py` runs those cases against a model that obeys the injected text. The schema limits what the answer can be, not whether it is right: an obeyed instruction can still produce a CLEAR verdict.

**Following the money around a circle.** A loop only counts if the money actually travels it: one transfer after another, within a short time, with similar amounts.
*Technical:* time-ordered cycles of 2 to 6 accounts that include the audited transaction, within a configurable window (default 1 day) and amount tolerance (default 20%). On the sample data this cut false alarms on clean transactions from 420 of 420 to 25 of 420.

**A person signs off, and everything is recorded.** No report is drafted on the AI's word alone, and nothing happens without a record.
*Technical:* SAR drafting requires an officer approval in the audit log. Sanctions screening refuses to run if its matcher is missing, and compliance events are written before the action takes effect.

## Tech stack

| Area | Tools |
|---|---|
| Language and UI | Python, Streamlit |
| AI and agents | LangChain, LangGraph, Claude Haiku 4.5 on Amazon Bedrock, Gemini 2.5 Flash on Google Vertex AI, Pydantic structured output |
| Retrieval | Qdrant vector store, PDF ingestion of AML guidance |
| Data and ML | pandas, NumPy, scikit-learn |
| Graph and matching | NetworkX (cycle detection), RapidFuzz (sanctions name matching) |
| Storage | SQLite append-only audit log with evidence hashes |
| Testing and delivery | pytest, Streamlit AppTest, Docker (multi-stage, non-root), GitHub Actions with an evaluation gate |

## Quick start

Tested on Python 3.12 (CI) and 3.14 (local, Windows 11). On 3.14 the first install is slower because SQLAlchemy builds from source.

```bash
git clone https://github.com/vinaykumar101997/Fintech-Fraud-Auditor-Pro.git
cd Fintech-Fraud-Auditor-Pro
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt                      # or requirements.lock.txt for exact versions
cp .env.example .env                                 # then edit it
python scripts/generate_sample_ledger.py
streamlit run app.py
```

The app runs without the AI backends: rules and the behaviour model still work, and the UI says the AI tier is unavailable. To enable it, set up credentials for Amazon Bedrock ([SETUP_AWS.md](SETUP_AWS.md)) or Google Vertex AI ([SETUP.md](SETUP.md)). Neither needs a key file in the project folder.

Optional: index an AML guidance PDF for the AI review tier:

```bash
python scripts/ingest_pdf.py --file path/to/aml_guidelines.pdf
```

## Running tests and the evaluation

`requirements-dev.txt` is enough for the tests and the evaluation; it includes `langchain-core` and `pydantic` for the prompt-injection tests but skips the model providers and the UI libraries.

```bash
pytest tests/ -v                          # 413 offline tests; the full install adds 9 app tests
python scripts/generate_sample_ledger.py  # fixed seed, so the numbers above reproduce exactly
python scripts/evaluate.py                # recall and precision by laundering type
python scripts/evaluate.py --seeds 7 11 13 17 19 23   # development seed plus held-out seeds
python scripts/evaluate.py --seeds 7 11 13 17 19 23 --hard   # with the harder patterns
python scripts/evaluate.py --min-recall 1.0 --min-pattern-recall 1.0 \
    --gate-exclude circular_camouflaged   # what CI runs
python scripts/eval_external.py --path data/external/HI-Small_Trans.csv --max-rows 50000
                                          # IBM AML data from Kaggle, Tiers 0+1; see docs/EVALUATION.md
```

## Known limitations

- **The funnel does not transfer to IBM's AML data.** On a held-back date range of `HI-Small_Trans.csv` (8 to 18 September, 503,288 US Dollar rows) it forwards 19.5% of rows at 0.41% precision and 50.4% recall, against a 0.159% laundering rate; PR-AUC is 2.2 times a random ranking and the top 500 rows hold no laundering. The amount rules alone forward 17% of rows. See `docs/EVALUATION.md`.
- **The headline numbers come from synthetic ledgers written by the same author as the detector.**
- **Laundering rings routed through established businesses (`circular_camouflaged`) are mostly missed:** 1 of 34 on the default held-out ledgers.
- **Sanctions-name matching is measured only on synthetic variants of an 8-entry watchlist.** The variants change casing, punctuation, suffix, word order or one character; the name rule fires on 50 of 50 of them held out. That says nothing about real names, transliterations from other scripts, or a large watchlist. The closest near-miss name scores 84.6 against the 88 threshold, and a misspelt suffix on an otherwise matching name ("Sinaloa Cartel Limted", 84.2) is still missed. The IBM data has no names, so it does not test this.
- **Behaviour features look at the whole batch, future rows included.** That suits auditing a closed ledger; scoring payments as they arrive would need every feature computed only from earlier rows.
- **Cycle thresholds are provisional.** The 1-day window and 20% amount tolerance were tuned on the synthetic ledger and still need checking against the IBM AML dataset.
- **Ledgers without timestamps fall back to shape-only cycle detection,** which over-flags businesses that trade in both directions. The finding says when this happens.
- **The network check only runs on rows an analyst selects.** A ring that nothing upstream flags is never examined.
- **Paused cases are lost on restart.** LangGraph's `MemorySaver` is in-memory; a Postgres checkpointer would make pauses durable.
- **The sanctions list is a demo file.** Replace `data/sanctions_list.csv` with an OFAC export, and confirm matches against date of birth and nationality.
- **SQLite is single-node, and Streamlit is synchronous.** Fine for a few thousand rows; larger batches need Postgres and a job queue.

## Roadmap

- Validate the cycle thresholds and recall on the IBM AML dataset
- Evaluate the AI tier: verdict accuracy, faithfulness of SAR drafts, cost and latency
- FastAPI backend with a job queue
- Postgres checkpointer and audit log
- Analyst feedback loop to tune thresholds against confirmed outcomes

## License

[MIT](LICENSE)
