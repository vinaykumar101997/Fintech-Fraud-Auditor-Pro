# AWS setup guide

Running the project on AWS. Set `MODEL_PROVIDER=bedrock` in `.env` and
everything else follows. About 40 minutes for a first run.

For the GCP path see `SETUP.md`. The pipeline is identical; only the provider
differs.

---

## What you'll actually touch

| Service | Purpose | Required? | Cost to learn |
|---|---|---|---|
| IAM Identity Center | credentials without long-lived keys | yes | free |
| Amazon Bedrock | Claude inference + Titan embeddings | yes | ~$0.20 to test |
| Qdrant Cloud (on AWS) | vector search | yes* | free tier |
| Amazon ECR | container registry | deploy only | ~$0.10/mo |
| AWS App Runner | hosting | deploy only | ~$5/mo if left running |
| Secrets Manager | credential storage | deploy only | $0.40/secret/mo |
| RDS Postgres | durable audit log (V2) | optional | free tier 12mo |

\* pgvector on RDS is the AWS-native alternative. See section 4.

**Read this before you click anything:** OpenSearch Serverless looks like the
obvious AWS vector store, and the Bedrock Knowledge Bases wizard will offer to
create one for you. It has a floor of 2 indexing + 2 search OCUs at ~$0.24/OCU/hr,
so an idle collection costs **roughly $350/month**. There is no free tier and no
scale-to-zero. Do not click that button while learning.

---

## 1. Account and credentials

Create an AWS account if you don't have one: <https://portal.aws.amazon.com/billing/signup>

**Set a budget alarm first.** Billing → Budgets → create a $10 monthly budget with
an 80% alert. Two minutes, and it's the difference between noticing a mistake and
finding out at month end.

**Install the CLI:** <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>

**Authenticate.** IAM Identity Center is the current recommended approach and
issues short-lived credentials rather than a permanent access key:

```bash
aws configure sso
# SSO start URL: from IAM Identity Center > Settings
# Region: us-east-1
# Profile name: fraud-auditor

export AWS_PROFILE=fraud-auditor          # or set it in .env
aws sts get-caller-identity                # should print your ARN
```

Simpler alternative for a personal learning account:

```bash
aws configure       # access key, secret, region us-east-1
```

If you do this, create the key for an IAM user with only the policy in section 3,
not for the root account, and delete it when you're done.

`boto3` finds credentials automatically in this order: environment variables,
`AWS_PROFILE`, `~/.aws/credentials`, then the instance/task role. The code never
reads a credential directly, which is why deployment needs no code change.

---

## 2. Bedrock model access

This is the step that catches everyone. Models are **off by default, per account
and per region.**

1. Console → Bedrock → **Model access** (bottom left)
2. Manage model access → enable:
   - **Anthropic Claude Haiku 4.5** (inference)
   - **Amazon Titan Text Embeddings V2** (embeddings)
3. Anthropic models ask for a use-case description. One or two sentences is fine.
   Approval is usually instant.

Verify:

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId,'claude')].modelId" --output table
```

**Region matters.** `us-east-1` and `us-west-2` carry the widest selection.
`eu-central-1` and `ap-south-1` have fewer models — check availability before
committing to a region for data-residency reasons.

**Inference profiles.** Most current Claude models can only be invoked through a
cross-region inference profile, not a bare model ID. That's the `us.` prefix in
`us.anthropic.claude-haiku-4-5-20251001-v1:0`. Calling the bare ID returns a
`ValidationException` that doesn't explain the problem. `preflight.py` detects
this specific case and tells you.

**Model choice is constrained.** Verdicts come back through
`with_structured_output`, which needs tool use. Anthropic and Nova models support
it. Titan *text* models do not, and will fail at the review stage. (Titan
*embeddings* are fine — different thing.)

**Cost.** Haiku is cheap. The 450-row sample forwards ~44 rows, two calls each,
plus embedding one PDF. Well under $0.50 total.

---

## 3. IAM policy

Least privilege for what the app actually does:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-*"
      ]
    },
    {
      "Sid": "BedrockDiscovery",
      "Effect": "Allow",
      "Action": ["bedrock:ListFoundationModels", "bedrock:GetFoundationModel"],
      "Resource": "*"
    }
  ]
}
```

Note the third ARN. Invoking through an inference profile needs permission on
both the profile and the underlying model, and omitting the profile ARN produces
an `AccessDeniedException` that reads as if model access was never granted.

Save as `iam-policy.json`, then:

```bash
aws iam create-policy --policy-name FraudAuditorBedrock \
  --policy-document file://iam-policy.json
```

Attach it to your user (local dev) or to the App Runner instance role (deployed).

---

## 4. Vector store

**Option A — Qdrant Cloud (recommended for learning).** Free 1GB cluster hosted
on AWS. <https://cloud.qdrant.io> → create cluster → choose an AWS region near
your Bedrock region → copy URL and API key.

```
QDRANT_URL=https://xxxxx.us-east-1-0.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=your-key
```

**Option B — local Docker.** Free, nothing leaves your machine.

```bash
docker run -d --name qdrant -p 6333:6333 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant
```

**Option C — pgvector on RDS (AWS-native).** More AWS surface area to learn, and
free-tier eligible for 12 months on `db.t4g.micro`. Requires swapping
`QdrantVectorStore` for `PGVector` in `utils/chat_agent.py` and adding
`langchain-postgres`. Worth doing as a second exercise, not your first run.

**Option D — OpenSearch Serverless.** See the cost warning above. Skip it.

---

## 5. Regulatory corpus

Same as the GCP path. Download an AML guidance PDF (FATF Recommendations:
<https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html>),
save to `data/aml_guidelines.pdf`, then:

```bash
python scripts/ingest_pdf.py --file data/aml_guidelines.pdf
```

Titan Embed v2 produces 1024-dimension vectors against Vertex's 768. The
ingestion script sizes the Qdrant collection from the first vector it generates,
so this needs no configuration. But **a collection built with one provider cannot
be queried with the other.** If you switch, delete the collection and re-ingest.

---

## 6. Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MODEL_PROVIDER=bedrock, AWS_REGION, Qdrant

python scripts/preflight.py     # verifies credentials, model access, corpus
python scripts/generate_sample_ledger.py
pytest tests/ -v                # 36 tests, no AWS needed
python scripts/evaluate.py      # recall must be 100%
streamlit run app.py
```

Tests and evaluation run entirely offline. You can verify the pipeline is correct
before spending anything on Bedrock.

---

## 7. Deploy (optional, the useful AWS practice)

Same container as the GCP path. App Runner is the shortest route from image to
running service.

**Push to ECR:**

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

aws ecr create-repository --repository-name fraud-auditor --region $REGION
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build -t fraud-auditor .
docker tag fraud-auditor:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/fraud-auditor:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/fraud-auditor:latest
```

Confirm no secrets were baked in before pushing:

```bash
docker run --rm --entrypoint sh fraud-auditor -c 'ls -a /app | grep -iE "env|aws|key"'
# should print only .env.example
```

**App Runner:** console → Create service → source from ECR → port `8501` →
health check path `/_stcore/health`.

Create an **instance role** (trust policy `tasks.apprunner.amazonaws.com`) with
the Bedrock policy from section 3 attached, and select it under Security. This is
the part worth understanding: the running container gets credentials from the
role automatically through the boto3 chain. No keys in the image, no keys in
environment variables, and nothing in the code changes between laptop and cloud.

Put `QDRANT_API_KEY` in Secrets Manager and reference it from the App Runner
config rather than as a plaintext environment variable.

**App Runner costs about $5/month** for the smallest instance and does not scale
to zero. Pause or delete the service when you're not using it.

ECS Fargate is the more common production choice and teaches more (task
definitions, ALB, security groups), at the price of considerably more setup.

---

## 8. Durable state (V2)

`MemorySaver` loses paused human-review cases on restart, and SQLite is
single-node. On AWS:

- **RDS Postgres** (`db.t4g.micro`, free tier 12 months) serves both the
  LangGraph checkpointer and the audit log. `build_compliance_graph()` already
  accepts a `checkpointer` argument. Install `langgraph-checkpoint-postgres`.
- **DynamoDB** suits the audit log alone and is genuinely serverless. Partition
  key `transaction_id`, sort key `occurred_at`. Port `utils/audit_log.py`.

Whichever you pick, revoke `UPDATE` and `DELETE` on the audit table. That's what
makes it an audit record rather than a table.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AccessDeniedException` on invoke | Model access not requested in Bedrock console, or the inference-profile ARN is missing from your IAM policy |
| `ValidationException: ... inference profile` | Using the bare model ID; prefix with `us.` |
| `ValidationException: model identifier is invalid` | Model not available in `AWS_REGION` |
| `NoCredentialsError` | `aws configure sso` not run, or `AWS_PROFILE` unset |
| `ExpiredToken` | SSO session lapsed; run `aws sso login --profile fraud-auditor` |
| `ThrottlingException` | Default Bedrock quotas are low on new accounts; the retry logic in `agents.py` handles bursts, but sustained load needs a quota increase |
| Embeddings return 1024 but Qdrant expects 768 | Collection was built with Vertex; delete it and re-ingest |
| Structured output fails or returns nothing | Chat model doesn't support tool use; Titan text models won't work |

`python scripts/preflight.py` detects most of these and prints the specific fix.
