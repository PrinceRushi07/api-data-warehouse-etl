-- =====================================================================
-- Star schema for the API -> Data Warehouse ETL pipeline (PostgreSQL 14+)
-- Idempotent: safe to run on every pipeline start.
-- NOTE: keep this file free of the percent character (executed via psycopg2).
-- =====================================================================

-- ---------------------------------------------------------------- dimensions
CREATE TABLE IF NOT EXISTS dim_date (
    date_key      INTEGER      PRIMARY KEY,            -- yyyymmdd
    full_date     DATE         NOT NULL UNIQUE,
    year          SMALLINT     NOT NULL,
    quarter       SMALLINT     NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    month         SMALLINT     NOT NULL CHECK (month BETWEEN 1 AND 12),
    month_name    VARCHAR(12)  NOT NULL,
    year_month    CHAR(7)      NOT NULL,
    week_of_year  SMALLINT     NOT NULL,
    day           SMALLINT     NOT NULL CHECK (day BETWEEN 1 AND 31),
    day_of_week   SMALLINT     NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),  -- 1 = Monday
    day_name      VARCHAR(12)  NOT NULL,
    is_weekend    BOOLEAN      NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_customer (
    customer_key   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id    BIGINT       NOT NULL UNIQUE,       -- natural key from the API
    email          VARCHAR(255) NOT NULL,
    username       VARCHAR(100) NOT NULL,
    first_name     VARCHAR(100) NOT NULL,
    last_name      VARCHAR(100) NOT NULL,
    full_name      VARCHAR(200) NOT NULL,
    phone          VARCHAR(50),
    city           VARCHAR(100) NOT NULL,
    street_address VARCHAR(200),
    zipcode        VARCHAR(20),
    latitude       DOUBLE PRECISION,
    longitude      DOUBLE PRECISION,
    email_domain   VARCHAR(100),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dim_customer_city ON dim_customer (city);

CREATE TABLE IF NOT EXISTS dim_product (
    product_key      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id       BIGINT        NOT NULL UNIQUE,    -- natural key from the API
    title            VARCHAR(500)  NOT NULL,
    category         VARCHAR(100)  NOT NULL,
    category_display VARCHAR(100)  NOT NULL,
    description      TEXT,
    image_url        TEXT,
    price            NUMERIC(10,2) NOT NULL CHECK (price > 0),
    price_tier       VARCHAR(20)   NOT NULL CHECK (price_tier IN ('budget','standard','premium')),
    rating_rate      NUMERIC(3,2)  CHECK (rating_rate BETWEEN 0 AND 5),
    rating_count     INTEGER       CHECK (rating_count >= 0),
    rating_band      VARCHAR(20)   NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dim_product_category ON dim_product (category);
CREATE INDEX IF NOT EXISTS idx_dim_product_tier ON dim_product (price_tier);

-- ---------------------------------------------------------------- fact
CREATE TABLE IF NOT EXISTS fact_sales (
    sales_key    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cart_id      BIGINT        NOT NULL,                -- degenerate dimension (order id)
    customer_key BIGINT        NOT NULL REFERENCES dim_customer (customer_key),
    product_key  BIGINT        NOT NULL REFERENCES dim_product (product_key),
    date_key     INTEGER       NOT NULL REFERENCES dim_date (date_key),
    quantity     INTEGER       NOT NULL CHECK (quantity > 0),
    unit_price   NUMERIC(10,2) NOT NULL CHECK (unit_price >= 0),
    line_total   NUMERIC(12,2) NOT NULL CHECK (line_total >= 0),
    etl_run_id   VARCHAR(64)   NOT NULL,
    loaded_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uq_fact_sales_grain UNIQUE (cart_id, product_key)   -- duplicate prevention
);
CREATE INDEX IF NOT EXISTS idx_fact_sales_customer ON fact_sales (customer_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_product  ON fact_sales (product_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_date     ON fact_sales (date_key);
CREATE INDEX IF NOT EXISTS idx_fact_sales_run      ON fact_sales (etl_run_id);

-- ---------------------------------------------------------------- analytics-ready aggregates
-- Rebuilt inside the load transaction by sql/refresh_aggregates.sql
CREATE TABLE IF NOT EXISTS agg_daily_sales (
    date_key        INTEGER       PRIMARY KEY REFERENCES dim_date (date_key),
    full_date       DATE          NOT NULL,
    orders          INTEGER       NOT NULL,
    units_sold      INTEGER       NOT NULL,
    revenue         NUMERIC(14,2) NOT NULL,
    avg_order_value NUMERIC(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS agg_category_sales (
    category        VARCHAR(100)  PRIMARY KEY,
    products_sold   INTEGER       NOT NULL,
    orders          INTEGER       NOT NULL,
    units_sold      INTEGER       NOT NULL,
    revenue         NUMERIC(14,2) NOT NULL,
    revenue_share   NUMERIC(6,2)  NOT NULL      -- percent of total revenue
);

CREATE TABLE IF NOT EXISTS agg_customer_summary (
    customer_key    BIGINT        PRIMARY KEY REFERENCES dim_customer (customer_key),
    orders          INTEGER       NOT NULL,
    units_purchased INTEGER       NOT NULL,
    revenue         NUMERIC(14,2) NOT NULL,
    avg_order_value NUMERIC(12,2) NOT NULL,
    first_order_date DATE         NOT NULL,
    last_order_date  DATE         NOT NULL,
    customer_segment VARCHAR(20)  NOT NULL
);

-- ---------------------------------------------------------------- ETL metadata
CREATE TABLE IF NOT EXISTS etl_run_audit (
    run_id           VARCHAR(64)  PRIMARY KEY,
    status           VARCHAR(20)  NOT NULL CHECK (status IN ('RUNNING','SUCCESS','FAILED')),
    started_at       TIMESTAMPTZ  NOT NULL,
    finished_at      TIMESTAMPTZ,
    duration_seconds NUMERIC(10,2),
    full_refresh     BOOLEAN      NOT NULL DEFAULT FALSE,
    extracted_counts JSONB,
    valid_counts     JSONB,
    rejected_counts  JSONB,
    loaded_counts    JSONB,
    error_message    TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_results (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      VARCHAR(64)  NOT NULL REFERENCES etl_run_audit (run_id),
    check_name  VARCHAR(100) NOT NULL,
    entity      VARCHAR(100) NOT NULL,
    status      VARCHAR(10)  NOT NULL CHECK (status IN ('PASS','FAIL','WARN')),
    expected    TEXT,
    actual      TEXT,
    details     TEXT,
    checked_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dq_run ON data_quality_results (run_id);

CREATE TABLE IF NOT EXISTS etl_watermark (
    entity          VARCHAR(100) PRIMARY KEY,
    watermark_value DATE         NOT NULL,
    run_id          VARCHAR(64)  NOT NULL,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
