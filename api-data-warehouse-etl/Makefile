.PHONY: up run full-refresh test test-integration down clean psql

up:            ## start PostgreSQL
	docker compose up -d postgres

run:           ## run the ETL once (incremental)
	docker compose run --rm etl

full-refresh:  ## ignore the watermark, re-extract everything
	docker compose run --rm etl --full-refresh

test:          ## unit tests (no DB needed)
	pytest -m "not integration"

test-integration: up  ## end-to-end tests against the compose PostgreSQL
	TEST_DATABASE_URL=postgresql+psycopg2://$${POSTGRES_USER:-etl_user}:$${POSTGRES_PASSWORD}@localhost:5432/$${POSTGRES_DB:-sales_dwh} pytest -m integration

psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-etl_user} -d $${POSTGRES_DB:-sales_dwh}

down:
	docker compose down

clean:
	docker compose down -v
