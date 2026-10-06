# Project Guide: GitHub metadata, resume bullets, interview prep

## 1. GitHub repository

**Repository name:** `api-data-warehouse-etl`

**Description (≤350 chars):**
Production-style ETL: Fake Store REST API → validation/Pandas transforms → incremental PostgreSQL star schema, with S3 archiving, data-quality gates, Docker Compose, pytest and 15 analytics SQL queries.

**Topics / tags:**
`data-engineering` `etl` `etl-pipeline` `python` `pandas` `postgresql` `sqlalchemy` `aws-s3` `boto3` `docker` `docker-compose` `data-warehouse` `star-schema` `data-quality` `rest-api` `incremental-load` `pytest` `github-actions` `sql` `analytics`

### Professional commit message suggestions (Conventional Commits)

```
chore: scaffold project structure, .gitignore and .env.example
feat(config): add env-driven settings and structured logging
feat(extract): add REST client with retry, back-off, timeout and raw JSON storage
feat(validate): add null/type/range/duplicate/referential validation with reject handling
feat(transform): add Pandas cleaning, standardisation, derived columns and dim_date
feat(db): add star schema DDL with PK/FK/indexes plus audit and watermark tables
feat(load): add staging + idempotent upserts in a single transaction
feat(quality): add pre-load and post-load data quality checks with persisted results
feat(s3): add partitioned S3 uploads for raw, processed, rejected and logs
feat(pipeline): orchestrate end-to-end run with watermark-based incremental extract
feat(sql): add aggregate refresh and 15 analytics queries
build(docker): add Dockerfile and docker-compose with PostgreSQL healthcheck
test: add unit tests for extract, validate, transform, quality and S3 plus e2e test
ci: add GitHub Actions workflow with PostgreSQL service
docs: add README with architecture, AWS setup and project guide
```

Suggested first-time push:

```bash
git init && git add . && git commit -m "feat: initial API to data warehouse ETL pipeline"
git branch -M main
git remote add origin git@github.com:<you>/api-data-warehouse-etl.git
git push -u origin main
```

## 2. Resume bullet points

1. **Built an end-to-end, containerised ETL pipeline** (Python, Pandas, SQLAlchemy, PostgreSQL, Docker Compose) that ingests
   REST API data with exponential back-off/timeouts, quarantines invalid records with rejection reasons, and loads a
   **star schema** (3 dimensions, 1 fact, 3 analytics-ready aggregates) via idempotent `ON CONFLICT` upserts in a single atomic transaction.
2. **Implemented a data-quality framework** running 40+ automated checks per run (record-count reconciliation, schema,
   duplicate/null keys, FK integrity, measure consistency, aggregate-to-fact reconciliation) with persisted results and
   automatic rollback on failure, plus a run-audit table and watermark-based **incremental loading**.
3. **Delivered a cloud-ready data lake layout on AWS S3** (boto3, SSE, Hive-style `dt=` partitions for raw/processed/rejected/logs),
   a pytest suite (API retries, validation, transformations, S3 via moto, PostgreSQL end-to-end) with GitHub Actions CI,
   and 15 business SQL queries (CTEs, window functions, cohorts, basket analysis) on the warehouse.

## 3. Interview questions & answers (project-specific)

**1. Walk me through the architecture end to end.**
The pipeline extracts three entities (products, users, carts) from the Fake Store API, stores each response as timestamped raw JSON, validates and quarantines bad rows, transforms with Pandas into dimension/fact frames, runs a pre-load quality gate, then loads PostgreSQL in one transaction (dims → fact → aggregates → post-load checks). Raw, processed, rejected files and logs are archived to partitioned S3 paths. Everything runs through Docker Compose (Postgres + ETL container).

**2. Why a star schema rather than loading flat tables?**
The questions are analytical (revenue by month/category/customer). A star schema separates descriptive attributes (dimensions) from measurable events (fact), keeps joins simple and fast, avoids repeating customer/product attributes on every sale, and maps cleanly to BI tools. Grain is one cart line: `(cart_id, product_key)`.

**3. How do you guarantee the pipeline is idempotent?**
A unique constraint on the fact grain plus `INSERT … ON CONFLICT DO UPDATE` for dimensions and facts. Re-running the same data inserts nothing and updates nothing (the `WHERE … IS DISTINCT FROM` clause skips unchanged rows). My integration test runs the pipeline twice and asserts row counts are identical.

**4. Explain your incremental loading strategy.**
`etl_watermark` stores the max order date loaded. The next run passes `startdate = watermark − LOOKBACK_DAYS` to the carts endpoint so late-arriving rows are captured; upserts make the overlap harmless. Products and users are small snapshots, upserted every run. `--full-refresh` ignores the watermark. The watermark is advanced inside the load transaction, so it can never get ahead of the data.

**5. How does retry logic work and what do you retry?**
Exponential back-off with jitter for timeouts, connection errors, invalid JSON, and statuses 408/425/429/5xx; `Retry-After` is honoured. Other 4xx errors fail fast because retrying a 404 is pointless. Timeouts are always set (connect and read) so a hung API can't hang the job. The sleep function is injectable, so tests run instantly.

**6. What happens to bad records?**
Each record ends up in exactly one place: valid or rejected. Rejected rows are written to `data/rejected/*.csv` with the raw JSON and all reasons (e.g. `missing_required:title; out_of_range:price`). A reconciliation check asserts `input = valid + rejected`, and a reject-ratio threshold aborts the load if too much data is bad (a sign of an upstream schema change).

**7. How do you handle duplicates?**
Three layers: validation (first occurrence wins; later duplicates are rejected with `duplicate_key`), a key-uniqueness check on the transformed frames, and database-level uniqueness constraints plus upserts. Post-load checks re-verify no duplicate business keys exist.

**8. Why one transaction for the whole load?**
Consumers must never see dimensions updated but the fact table stale, or aggregates that don't match the fact. Loading dims, fact, aggregates and running post-load checks inside one transaction gives all-or-nothing semantics. If a check fails, the transaction rolls back and the run is recorded as FAILED.

**9. Why stage into tables and use set-based SQL instead of row-by-row inserts?**
Row-by-row inserts mean N round trips; staging a DataFrame once and running a single `INSERT … SELECT … ON CONFLICT` is much faster, lets the database resolve surrogate keys with joins, and lets me count inserted vs updated rows using `RETURNING (xmax = 0)`.

**10. How do you resolve surrogate keys in the fact table?**
The fact frame carries natural keys (`customer_id`, `product_id`, `date_key`). During load, SQL joins staging to `dim_customer`/`dim_product` to fetch surrogate keys. Before inserting I count unmapped rows and abort if any exist, which would indicate a dimension-load bug.

**11. What data quality checks do you run and when?**
Validation-time (required, type, range, format, duplicates, referential); pre-load (count reconciliation, reject ratio, schema contract, key nulls/dups); post-load inside the transaction (warehouse schema, loaded counts, orphan FKs, `line_total = qty × price`, revenue in aggregates equals revenue in the fact). Results are persisted in `data_quality_results` for trend monitoring.

**12. Why run post-load checks before commit?**
It's a lightweight write-audit-publish pattern: bad data is never visible to consumers because the failing transaction is rolled back. The trade-off is longer transactions; at this scale that's fine, and at larger scale I'd load to a shadow table/partition and swap.

**13. How do you secure the pipeline?**
Secrets only via environment variables (never committed; `.env` is git-ignored). Passwords from the API are redacted before anything touches disk or S3. S3 uses SSE and a least-privilege IAM policy (`PutObject` on one prefix, `ListBucket`). The container runs as non-root. SQL uses bound parameters; the only interpolated identifiers are internal constants.

**14. How is S3 organised and why?**
`prefix/{raw|processed|rejected|logs}/<entity>/dt=YYYY-MM-DD/<file>`. Hive-style partitions mean Athena/Glue can query it directly with partition pruning, and lifecycle rules can be applied per zone (e.g. raw to Glacier). Raw is uploaded immediately after extraction so it's preserved even if a later stage fails.

**15. What if the S3 upload fails?**
Controlled by `S3_FAIL_ON_ERROR`. Default: log the error and continue, because the warehouse load is the primary objective and S3 is an archive; for compliance-heavy setups set it to `true` to fail the run. boto3 also retries transient errors (standard mode, 5 attempts).

**16. How did you test it without hitting the real API or AWS?**
A scriptable fake HTTP session replays responses/exceptions (timeouts, 500s, bad JSON) so retry logic is tested deterministically. S3 uses `moto`. Validation/transform tests are pure Pandas. One integration test runs the whole pipeline against a real PostgreSQL (in CI via a service container) with a fake API client.

**17. Where would this break at 100× the data volume?**
Pandas holds everything in memory and Python-side validation is row-oriented in places. I'd paginate the extract, process in chunks/Parquet, use `COPY` for staging, partition the fact table by date, and move orchestration to Airflow/ECS. The set-based SQL design and idempotent loads carry over unchanged.

**18. How would you schedule and monitor this in production?**
Run the container on a schedule (EventBridge → ECS Fargate, or Airflow/MWAA). Monitoring: non-zero exit code on failure, `etl_run_audit` + `data_quality_results` for dashboards/alerts (query 15 shows run health), logs shipped to S3/CloudWatch, and alerts on FAILED runs or reject-ratio spikes.

**19. How do you handle schema changes in the source API?**
Required-field and type validation flags the change immediately: rows get rejected with explicit reasons, the reject-ratio gate stops the load, and the raw JSON is preserved so I can reprocess after fixing the transform. For new optional fields, the flatten step ignores unknown columns, so the pipeline stays backward compatible.

**20. What are the known limitations and what would you improve?**
Price history isn't available from the API, so `unit_price` reflects load-time price (a Type-2 `dim_product` would preserve history); upstream deletes aren't propagated; CSV rather than Parquet in the processed zone. Next: Parquet + Athena, dbt for the SQL layer, Great Expectations for profiling, Airflow orchestration, and CDC if the source supported it.
