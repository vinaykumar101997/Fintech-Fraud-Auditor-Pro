# Setup guide

Everything external the project needs, in the order you should do it. Budget
about 45 minutes for a first run, most of it waiting on Google Cloud.

---

## Summary

| Service | Required? | Cost | Time |
|---|---|---|---|
| Python 3.11+ | Yes | free | 5 min |
| Google Cloud project + Vertex AI | Yes | pay per token, ~$0.50 to test | 15 min |
| gcloud CLI | Yes (local dev) | free | 5 min |
| Qdrant (Cloud free tier or Docker) | Yes | free | 10 min |
| A regulatory PDF | Yes for the RAG tier | free | 5 min |
| OFAC SDN watchlist | Recommended | free | 5 min |
| Docker Desktop | Optional | free | 10 min |
| Supabase / Postgres | Optional, V2 only | free tier | 15 min |

**Not needed:** Supabase is listed as optional. See the section at the end —
the original `requirements.txt` pinned it but nothing imported it, so it was
removed. It becomes genuinely useful only when you outgrow SQLite.

---

## 1. Python 3.11+

pandas 3.x and the LangChain 1.x line need 3.10 minimum; the Dockerfile targets
3.11. In PyCharm: **Settings → Project → Python Interpreter → Add → Virtualenv**,
base interpreter 3.11 or 3.12.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Google Cloud

This is the only part with a real cost and the only part that can block you for
non-obvious reasons.

**a. Create a project**

<https://console.cloud.google.com/projectcreate>. Note the **project ID** (not
the display name) — it goes in `.env` as `GCP_PROJECT_ID`.

**b. Enable billing.** Vertex AI refuses to serve without it even inside the free
trial credit. <https://console.cloud.google.com/billing>

**c. Enable the API**

```bash
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
```

Or console → APIs & Services → enable "Vertex AI API".

**d. Install the gcloud CLI**

<https://cloud.google.com/sdk/docs/install>. Then:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
```

That last command is what the app actually uses. It writes credentials to
`~/.config/gcloud/application_default_credentials.json`
(`%APPDATA%\gcloud\...` on Windows). No key file goes anywhere near the repo.

**e. Grant yourself the role** if you are not project owner:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="user:you@example.com" --role="roles/aiplatform.user"
```

**f. Region.** `us-central1` has the widest model availability. If you need EU
data residency, `europe-west4` works but confirm `gemini-2.5-flash` and
`text-embedding-004` are both served there before committing.

**Cost.** Gemini Flash is inexpensive. The 450-row sample ledger forwards ~44
rows, two calls each, well under $0.10. Set a budget alert at $10 anyway:
<https://console.cloud.google.com/billing/budgets>

**Common failures**

| Error | Cause |
|---|---|
| `403 PERMISSION_DENIED` | API not enabled, or missing `roles/aiplatform.user` |
| `404 model not found` | Model not served in your region; use `us-central1` |
| `billing account not configured` | Billing not linked |
| `DefaultCredentialsError` | `gcloud auth application-default login` not run |

---

## 3. Qdrant

Pick one.

**Option A — Qdrant Cloud (easiest).** <https://cloud.qdrant.io> → free 1GB
cluster, plenty for a regulatory corpus. Copy the cluster URL (include the
`:6333` port) and generate an API key. Into `.env`:

```
QDRANT_URL=https://xxxxx.us-east-1-0.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=your-key
```

**Option B — local Docker.**

```bash
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant
```

```
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
```

Local means no data leaves your machine, which matters if you ever point this at
real transaction data.

---

## 4. The regulatory corpus

The RAG tier needs something to ground against. Any AML guidance PDF works.

- FATF Recommendations: <https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html>
- FinCEN guidance: <https://www.fincen.gov/resources/statutes-and-regulations/guidance>

Save as `data/aml_guidelines.pdf`, then:

```bash
python scripts/ingest_pdf.py --file data/aml_guidelines.pdf
```

Chunking and embedding a 100-page PDF takes a few minutes and costs a few cents.
Chunk IDs are content hashes, so re-running updates rather than duplicating.

---

## 5. Sanctions watchlist

The bundled `data/sanctions_list.csv` has 10 entries and is a demo. For anything
real, download the OFAC SDN list:

<https://sanctionslist.ofac.treas.gov/Home/SdnList> (the `SDN.CSV` file)

Reshape it to the columns this project expects — `name,aliases,type,program`,
with aliases pipe-separated — and overwrite `data/sanctions_list.csv`. The EU
consolidated list is also worth adding if you operate in Europe.

Name matching alone is not identity verification. Confirm against date of birth
and nationality before acting on any hit.

---

## 6. Optional: Docker

Only needed to run the containerised app or a local Qdrant.

```bash
docker build -t fraud-auditor .

# Verify no secrets were baked in:
docker run --rm --entrypoint sh fraud-auditor -c 'ls -a /app | grep -iE "env|key"'
# should print nothing but .env.example

docker run -p 8501:8501 --env-file .env \
  -v ~/.config/gcloud:/home/auditor/.config/gcloud:ro \
  fraud-auditor
```

---

## 7. Optional: Supabase or Postgres (V2 only)

**You do not need this to run the project.** The audit trail uses SQLite and the
LangGraph checkpointer uses `MemorySaver`. Both work.

They become inadequate for two specific reasons, and Supabase (managed Postgres)
is a reasonable answer to both:

1. **`MemorySaver` loses paused cases on restart.** A human-in-the-loop pause
   that evaporates when the process restarts is not a compliance control.
2. **SQLite is single-node.** Two Streamlit workers writing the same file is a
   locking problem, and the audit trail is the artifact a regulator asks for.

If you want it:

1. <https://supabase.com> → new project → note the database password.
2. Settings → Database → copy the **connection pooler** URI (port 6543 for
   transaction mode).
3. `pip install langgraph-checkpoint-postgres psycopg[binary]`
4. Add to `.env`: `DATABASE_URL=postgresql://postgres.xxx:PASS@aws-0-region.pooler.supabase.com:6543/postgres`
5. Pass a `PostgresSaver` into `build_compliance_graph()` in `utils/graph_logic.py`
   (the function already takes a `checkpointer` argument for this).
6. Port `utils/audit_log.py` from `sqlite3` to `psycopg`. The schema is standard
   SQL; the main change is `AUTOINCREMENT` → `GENERATED ALWAYS AS IDENTITY`.

Add a row-level security policy and revoke `UPDATE`/`DELETE` on `audit_events`
so the trail is genuinely append-only. That is the part that makes it an audit
record rather than a table.

---

## 8. Verify

```bash
cp .env.example .env        # then fill it in
python scripts/preflight.py
```

It checks the interpreter, every package, the `.env` values, that no credentials
are sitting in the repo root, Google auth, Vertex reachability, Qdrant, the
corpus, data files, and the test suite. Exit code 0 means the app will start.

```bash
python scripts/generate_sample_ledger.py
pytest tests/ -v
python scripts/evaluate.py
streamlit run app.py
```

---

## Nothing here is required to run the tests

`pytest tests/` and `python scripts/evaluate.py` need no credentials and no
network. All 36 tests and the funnel evaluation run offline. That is deliberate:
the tests that matter cover the pipeline ordering, and those should never depend
on a billing account being live.
