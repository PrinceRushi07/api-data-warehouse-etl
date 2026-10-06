-- =====================================================================
-- 15 business-focused analytics queries (PostgreSQL)
-- Star schema: fact_sales + dim_customer / dim_product / dim_date
-- Pre-aggregated: agg_daily_sales, agg_category_sales, agg_customer_summary
-- =====================================================================

-- Q1. Monthly revenue with month-over-month growth  [CTE + LAG window]
WITH monthly AS (
    SELECT d.year_month,
           SUM(f.line_total)              AS revenue,
           COUNT(DISTINCT f.cart_id)      AS orders
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
    GROUP BY d.year_month
)
SELECT year_month,
       revenue,
       orders,
       LAG(revenue) OVER (ORDER BY year_month) AS prev_month_revenue,
       ROUND(100.0 * (revenue - LAG(revenue) OVER (ORDER BY year_month))
             / NULLIF(LAG(revenue) OVER (ORDER BY year_month), 0), 2) AS mom_growth_pct
FROM monthly
ORDER BY year_month;

-- Q2. Top 10 customers by lifetime value with share of total revenue  [JOIN + DENSE_RANK + window SUM]
SELECT DENSE_RANK() OVER (ORDER BY SUM(f.line_total) DESC)               AS revenue_rank,
       c.customer_id,
       c.full_name,
       c.city,
       SUM(f.line_total)                                                  AS lifetime_value,
       ROUND(100.0 * SUM(f.line_total) / SUM(SUM(f.line_total)) OVER (), 2) AS pct_of_total_revenue
FROM fact_sales f
JOIN dim_customer c ON c.customer_key = f.customer_key
GROUP BY c.customer_id, c.full_name, c.city
ORDER BY revenue_rank, c.customer_id
LIMIT 10;

-- Q3. Revenue, units and share by product category  [JOIN + GROUP BY + window SUM]
SELECT p.category_display                                                AS category,
       COUNT(DISTINCT f.cart_id)                                         AS orders,
       SUM(f.quantity)                                                   AS units_sold,
       SUM(f.line_total)                                                 AS revenue,
       ROUND(100.0 * SUM(f.line_total) / SUM(SUM(f.line_total)) OVER (), 2) AS revenue_share_pct
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.category_display
ORDER BY revenue DESC;

-- Q4. Top 3 products per category by revenue  [CTE + ROW_NUMBER partition]
WITH product_revenue AS (
    SELECT p.category_display AS category,
           p.title,
           SUM(f.line_total)  AS revenue,
           SUM(f.quantity)    AS units_sold
    FROM fact_sales f
    JOIN dim_product p ON p.product_key = f.product_key
    GROUP BY p.category_display, p.title
),
ranked AS (
    SELECT pr.*,
           ROW_NUMBER() OVER (PARTITION BY category ORDER BY revenue DESC) AS rn
    FROM product_revenue pr
)
SELECT category, rn AS rank_in_category, title, revenue, units_sold
FROM ranked
WHERE rn <= 3
ORDER BY category, rn;

-- Q5. Daily revenue with running total and 7-day moving average  [window frames]
SELECT d.full_date,
       SUM(f.line_total)                                              AS daily_revenue,
       SUM(SUM(f.line_total)) OVER (ORDER BY d.full_date)             AS cumulative_revenue,
       ROUND(AVG(SUM(f.line_total)) OVER (
             ORDER BY d.full_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS moving_avg_7_rows
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.full_date
ORDER BY d.full_date;

-- Q6. Average order value and basket size by month  [CTE: orders first, then aggregate]
WITH orders AS (
    SELECT f.cart_id,
           d.year_month,
           SUM(f.line_total) AS order_value,
           SUM(f.quantity)   AS items
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
    GROUP BY f.cart_id, d.year_month
)
SELECT year_month,
       COUNT(*)                         AS orders,
       ROUND(AVG(order_value), 2)       AS avg_order_value,
       ROUND(AVG(items), 2)             AS avg_items_per_order,
       MAX(order_value)                 AS largest_order
FROM orders
GROUP BY year_month
ORDER BY year_month;

-- Q7. Customer value quartiles (NTILE) with spend ranges  [CTE + NTILE]
WITH spend AS (
    SELECT customer_key, SUM(line_total) AS revenue
    FROM fact_sales
    GROUP BY customer_key
),
tiles AS (
    SELECT customer_key, revenue, NTILE(4) OVER (ORDER BY revenue DESC) AS quartile
    FROM spend
)
SELECT quartile,
       COUNT(*)                    AS customers,
       ROUND(MIN(revenue), 2)      AS min_spend,
       ROUND(MAX(revenue), 2)      AS max_spend,
       ROUND(SUM(revenue), 2)      AS total_revenue,
       ROUND(100.0 * SUM(revenue) / SUM(SUM(revenue)) OVER (), 2) AS pct_of_revenue
FROM tiles
GROUP BY quartile
ORDER BY quartile;

-- Q8. One-time vs repeat customers and their revenue  [CTE + CASE]
WITH per_customer AS (
    SELECT customer_key,
           COUNT(DISTINCT cart_id) AS orders,
           SUM(line_total)         AS revenue
    FROM fact_sales
    GROUP BY customer_key
)
SELECT CASE WHEN orders = 1 THEN 'one-time' ELSE 'repeat' END AS customer_type,
       COUNT(*)                        AS customers,
       ROUND(AVG(orders), 2)           AS avg_orders,
       ROUND(SUM(revenue), 2)          AS revenue,
       ROUND(AVG(revenue), 2)          AS avg_revenue_per_customer
FROM per_customer
GROUP BY CASE WHEN orders = 1 THEN 'one-time' ELSE 'repeat' END
ORDER BY customers DESC;

-- Q9. Sales by day of week, weekday vs weekend  [JOIN + GROUP BY + window]
SELECT d.day_of_week,
       d.day_name,
       d.is_weekend,
       COUNT(DISTINCT f.cart_id)  AS orders,
       SUM(f.line_total)          AS revenue,
       ROUND(100.0 * SUM(f.line_total) / SUM(SUM(f.line_total)) OVER (), 2) AS pct_of_revenue
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.day_of_week, d.day_name, d.is_weekend
ORDER BY d.day_of_week;

-- Q10. Performance by price tier  [JOIN + aggregation]
SELECT p.price_tier,
       COUNT(DISTINCT p.product_key)  AS products,
       SUM(f.quantity)                AS units_sold,
       SUM(f.line_total)              AS revenue,
       ROUND(SUM(f.line_total) / NULLIF(SUM(f.quantity), 0), 2) AS avg_selling_price,
       ROUND(AVG(p.rating_rate), 2)   AS avg_rating
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.price_tier
ORDER BY revenue DESC;

-- Q11. Frequently bought together: top product pairs  [self-join + CTE]
WITH lines AS (
    SELECT f.cart_id, p.product_id, p.title
    FROM fact_sales f
    JOIN dim_product p ON p.product_key = f.product_key
)
SELECT a.title AS product_a,
       b.title AS product_b,
       COUNT(*) AS carts_together
FROM lines a
JOIN lines b ON a.cart_id = b.cart_id AND a.product_id < b.product_id
GROUP BY a.title, b.title
ORDER BY carts_together DESC, product_a, product_b
LIMIT 10;

-- Q12. Customer recency: days since last order vs latest date in the data  [CTE + MIN/MAX + CASE]
WITH bounds AS (
    SELECT MAX(d.full_date) AS data_as_of
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
),
activity AS (
    SELECT f.customer_key,
           MIN(d.full_date)          AS first_order,
           MAX(d.full_date)          AS last_order,
           COUNT(DISTINCT f.cart_id) AS orders
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
    GROUP BY f.customer_key
)
SELECT c.full_name,
       a.first_order,
       a.last_order,
       a.orders,
       (b.data_as_of - a.last_order) AS days_since_last_order,
       CASE WHEN b.data_as_of - a.last_order <= 30 THEN 'active'
            WHEN b.data_as_of - a.last_order <= 90 THEN 'at risk'
            ELSE 'lapsed' END        AS recency_status
FROM activity a
CROSS JOIN bounds b
JOIN dim_customer c ON c.customer_key = a.customer_key
ORDER BY days_since_last_order DESC, c.full_name;

-- Q13. Do better-rated products sell more?  [JOIN + GROUP BY + share window]
SELECT p.rating_band,
       COUNT(DISTINCT p.product_key)  AS products,
       SUM(f.quantity)                AS units_sold,
       SUM(f.line_total)              AS revenue,
       ROUND(SUM(f.quantity)::numeric / COUNT(DISTINCT p.product_key), 2) AS units_per_product,
       ROUND(100.0 * SUM(f.line_total) / SUM(SUM(f.line_total)) OVER (), 2) AS pct_of_revenue
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.rating_band
ORDER BY revenue DESC;

-- Q14. Revenue by customer city with rank and cumulative share (Pareto)  [CTE + RANK + running SUM]
WITH city_revenue AS (
    SELECT c.city,
           COUNT(DISTINCT c.customer_key) AS customers,
           SUM(f.line_total)              AS revenue
    FROM fact_sales f
    JOIN dim_customer c ON c.customer_key = f.customer_key
    GROUP BY c.city
)
SELECT RANK() OVER (ORDER BY revenue DESC) AS city_rank,
       city,
       customers,
       revenue,
       ROUND(100.0 * SUM(revenue) OVER (ORDER BY revenue DESC, city)
             / SUM(revenue) OVER (), 2)    AS cumulative_pct_of_revenue
FROM city_revenue
ORDER BY city_rank, city;

-- Q15. Pipeline health: last 10 runs with row counts and data-quality results  [JOIN + FILTER + JSONB]
SELECT a.run_id,
       a.status,
       a.started_at,
       a.duration_seconds,
       (a.extracted_counts ->> 'carts')::int                         AS carts_extracted,
       (a.rejected_counts  ->> 'cart_items')::int                    AS cart_items_rejected,
       COUNT(q.id) FILTER (WHERE q.status = 'PASS')                  AS dq_pass,
       COUNT(q.id) FILTER (WHERE q.status = 'WARN')                  AS dq_warn,
       COUNT(q.id) FILTER (WHERE q.status = 'FAIL')                  AS dq_fail
FROM etl_run_audit a
LEFT JOIN data_quality_results q ON q.run_id = a.run_id
GROUP BY a.run_id, a.status, a.started_at, a.duration_seconds, a.extracted_counts, a.rejected_counts
ORDER BY a.started_at DESC
LIMIT 10;
