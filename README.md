# Natural-Language-to-Regex Data Processing Platform

Pick a CSV or Excel file from S3, describe a pattern in plain English ("find
email addresses"), and an LLM turns it into a regex that Spark applies across the
whole dataset - asynchronously, with live progress, results paged out of Parquet.

Django · React · Celery · Redis · PySpark · MinIO/S3 · Gemini

---

**Live demo: https://d85j99wr6b7ot.cloudfront.net/**

Pick `raw/customers.csv`, select the `Email` column, type *"Find email
addresses"*, replacement `REDACTED`, Run. `raw/contacts_large.csv` is the
2,000,000-row scale test.

---

## 1. Setup and run

Requirements: Docker Desktop, ~6 GB free RAM.

```bash
cp .env.example .env      # then add your GEMINI_API_KEY
docker compose up --build
```

Open **http://localhost:5173**. First run takes a few minutes (image build, S3A
jars, and it generates a 2M-row test CSV); after that it starts in seconds.

**Try it:** pick `raw/customers.csv` → select the `Email` column → type *"Find
email addresses"* → replacement `REDACTED` → **Run job**. For the scale demo use
`raw/contacts_large.csv` (2,000,000 rows).

| Surface | URL |
| --- | --- |
| App | http://localhost:5173 |
| API health | http://localhost:8000/api/health/ |
| Flower (tasks/workers) | http://localhost:5555 |
| Spark master / driver | http://localhost:8080 · http://localhost:4040 |
| MinIO console | http://localhost:9001 (`minioadmin`/`minioadmin`) |

**Tests** — 89 backend tests and 27 frontend tests, no network, no cluster, no API key needed:

```bash
docker compose run --rm backend pytest
```

```bash
docker compose run --rm frontend npm test
```


**Scale the cluster:**

```bash
docker compose up -d --scale spark-worker=3
```

### Config worth knowing

| Variable | Default | Note |
| --- | --- | --- |
| `LLM_PROVIDER` | `gemini` | `gemini` \| `anthropic` \| `openai` -- only the matching API key is required |
| `GEMINI_MODEL` | `gemini-3.6-flash` | |
| `GEMINI_MAX_OUTPUT_TOKENS` | `8192` | reasoning tokens share this budget |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_MAX_OUTPUT_TOKENS` | `8192` | |
| `OPENAI_MODEL` | `gpt-4.1` | |
| `OPENAI_MAX_OUTPUT_TOKENS` | `8192` | |
| `SEED_LARGE_ROWS` | `2000000` | many rows go in the big scale-test CSV |
| `S3_ENDPOINT_URL` | `http://minio:9000` | **clear it to use real Amazon S3** |

---

## 2. Architecture

```
React ──▶ Django API ──▶ Redis (broker) ──▶ Celery worker = Spark driver
  ▲           │              ▲                        │
  │           │ job row      │ prompt cache           ▼
  └── polls ──┤              │                Spark standalone cluster
              ▼              │                        │
          Postgres      Gemini API                    ▼
              ▲                              MinIO / Amazon S3
              └──── page reads (pyarrow, no Spark) ───┘
```

```
backend/apps/
  jobs/       API + orchestration  views, serializers, services, tasks, models
  llm/        natural language     providers, prompts, validation, cache
  sparkeng/   data engine          session, reader, engine, results, progress
  storage/    object storage       s3
```

Dependencies point one way — `jobs` uses the other three; none of them import
`jobs`. The transformation engine doesn't know an HTTP API exists.

### The decisions that matter, and why

**Submit never blocks.** `POST /api/jobs/` validates, writes a `QUEUED` row,
queues a Celery task, returns `202` + job id. No LLM call, no Spark, no file read
beyond a `HEAD`. Dispatch runs in `transaction.on_commit` so a worker can't pick
up a job id that isn't committed yet.

**Native Spark expressions, zero Python user-defined functions.** Every operation is
`regexp_replace` / `regexp_extract` / `rlike`. Rows never cross the JVM <-> Python
boundary (the usual reason "Spark is slow"), and with no Python closures to ship,
executors need none of this app's code - the cluster works with no packaging
step. The work is per-partition with no shuffle, so adding executors adds
throughput.

**Results are paged out of Parquet, not memory.** Spark writes the result with
`maxRecordsPerFile=100k`; a page index is then built from Parquet *footers*
(metadata only, no data scan). Django serves a page by decoding one row group
with pyarrow — **so paging needs neither Spark nor Celery**, and costs the same
at row 40,000,000 as at row 1.

**Progress is measured, not estimated.** A background thread polls Spark's
`StatusTracker` for `completedTasks / numTasks` and maps it into the current
phase's percentage band.

**Cancellation really cancels.** Each job runs in a Spark *job group*; cancelling
calls `cancelJobGroup(..., interruptOnCancel=True)`, which kills tasks already
running on executors. `celery revoke` alone can't do that, so both are used —
revoke for queued jobs, job groups for running ones.

**Two transformations beyond plain find/replace: `MASK` and `VALIDATE`.**
`REPLACE` and `EXTRACT` cover the obvious cases — swap a match for a fixed
value, or pull it into a new column. The other two go through the identical
async/Spark pipeline (queue → LLM → `plan_transformation` → Parquet) but ask
the model for something more structured:

- **`MASK`** asks the LLM for a *replacement template* with back-references
  (`$1***$3`), not just a flat string — so "mask a card number but keep the
  last 4 digits" becomes a pattern with capture groups plus a template that
  reassembles them around the masked middle. This is a real redaction
  primitive, not a find/replace dressed up.
- **`VALIDATE`** inverts the usual meaning of a "match": the flagged cells are
  the ones that **fail** to match, turning the regex into
  a data-quality check — "which rows have a malformed email/phone/ID" — with
  no new column mutated, only annotated.

Both reuse every piece of the existing machinery — LLM spec validation,
self-test-before-cluster-time, ReDoS guarding, `regexp_replace`/`rlike` as
native Spark expressions — so the "creative" part is in what's asked of the
model and how the result is interpreted, not a second pipeline.

**MinIO and Amazon S3 are one code path.** Same `boto3` + `s3a://`; switching is
`S3_ENDPOINT_URL` and `S3_PATH_STYLE`, nothing else.

**Partitioning choices:** `maxPartitionBytes=64m` (read parallelism on one big
CSV comes from split size), `shuffle.partitions=8` (the default 200 means 200
tasks of a few rows), adaptive execution on, and `inferSchema` **off** —
it costs a full extra pass and mangles leading zeros and long account numbers.

---

## 3. Notes and trade-offs

- **One Docker image for everything** (API, workers, beat, Flower, seeder, both
  Spark roles). Guarantees identical PySpark/Hadoop/JVM on driver and executors —
  the classic source of Spark skew bugs — at the cost of an API container
  carrying PySpark it never uses.
- **Excel is parsed on the driver** under a size cap. `.xlsx` isn't splittable
  and has no native Spark reader; since the format caps at ~1.05M rows/sheet it
  was never the scale path anyway.
- **File previews are bounded, not queued.** CSV = 256 KB ranged read; Excel
  capped by `EXCEL_MAX_BYTES`, above which the endpoint returns `413`. A file too
  big to preview is also too big for the Excel pipeline, so an async preview
  would only defer the same refusal.
- **No authentication.** Out of scope for the brief; not internet-safe as-is.
- **Rotate the key in `.env`** before publishing this repo.

---

## 4. Deploying to AWS (Terraform)

`infra/` provisions the whole thing on a single EC2 instance, and `deploy.sh`
ships this checkout to it.

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set admin_cidrs to your own IP
terraform init
terraform apply
cd .. && ./deploy.sh
```

`terraform apply` creates the VPC, subnet, security group, S3 bucket, IAM
instance profile, SSH keypair, Elastic IP and the instance. `deploy.sh` then
waits for the Docker install, generates a production `.env`, copies the source,
builds the images on the box, starts the stack, seeds the bucket and blocks until
`/api/health/` reports `ok` — so a green run means it is genuinely serving.

Redeploy after a code change with `./deploy.sh` again; tear the whole thing down
with `terraform destroy`.

### What it provisions, and why

**Storage is real Amazon S3, with no access keys anywhere.** The instance gets an
IAM instance profile scoped to its own bucket, and boto3, pyarrow and Hadoop's
S3A all fall through to instance-metadata credentials. That is why
`AWS_ACCESS_KEY_ID` is deliberately *empty* in the generated `.env` — setting it
would override the role. One catch worth knowing: IMDSv2 defaults to a hop limit
of 1, which blocks **containers** from reaching the metadata service (the Docker
bridge is one extra hop), so the instance sets
`http_put_response_hop_limit = 2`. Without it every S3 call inside a container
403s.

**Only what needs to be public is public.** Port 80 serves the app and `/api`
through nginx (one origin, so no CORS in production either). Flower is also
reachable there, reverse-proxied at `/flower/`, because CloudFront only ever
forwards 80/443 — it never sees :5555 directly. That path rides in through the
CDN on the wide-open port 80 rule, so basic auth is its only gate; the direct
route, `:5555` with its own CIDR allowlist (`flower_cidrs`), stays available as
the more locked-down option. SSH is restricted to `admin_cidrs`. The Spark
master and driver UIs have no authentication of their own, so they are bound
to `127.0.0.1` on the instance and reached over an SSH tunnel —
`terraform output spark_ui_tunnel` prints the command.

**No NAT gateway and no load balancer.** Both are reflexes worth resisting here:
together they would add ~$50/month to a single-instance demo that needs neither.
The instance sits in a public subnet behind a tight security group instead.

**Secrets are generated, not committed.** `deploy.sh` creates the Django secret
key, Postgres password and Flower credentials on first run and reuses them on
later runs, so a redeploy does not invalidate the password against the existing
Postgres volume. Terraform state and the generated `.pem` are gitignored.

### Trade-offs in this setup

- **Single instance, no autoscaling and no redundancy.** Suitable for a
  small demonstration workload;
- **HTTPS comes from CloudFront, not from the instance.** EC2 is IaaS: it hands
  over a VM and an IP, and no certificate authority will issue for the
  `*.amazonaws.com` name AWS assigns because you do not control that domain. So
  rather than buy a domain, the Terraform puts a CloudFront distribution in
  front — it supplies both a hostname and a valid certificate for free. It is
  configured as a pure reverse proxy (caching disabled, all HTTP methods
  allowed, all headers forwarded), because this is an app with mutating
  endpoints, not a static site. TLS terminates at the edge and the
  CloudFront→origin hop is plain HTTP. Set `enable_cdn = false` to serve HTTP straight
  off the instance instead.
- **Terraform state is local.** Fine for one operator; a team needs an S3
  backend with DynamoDB locking.
- **`deploy.sh` copies a tarball over SSH** rather than pulling from a registry.
  It is dependency-free and works from Git Bash on Windows, but a real pipeline
  would build once, push to ECR, and have the instance pull a tagged image.
- **`docker-compose.prod.yml` duplicates the local compose file** instead of
  layering on it. Compose *merges* list-valued keys such as `volumes`, so an
  override file cannot remove the dev bind mounts — a standalone file is longer
  but states exactly what runs in production.

## Demo video

This short recording shows a job moving from submission through completion:
file selection, natural-language pattern entry, live progress, and paginated
results.

https://github.com/user-attachments/assets/94e367ce-0534-41cf-a005-5e72a28f6231