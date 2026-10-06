-- Rebuild analytics-ready aggregate tables from the star schema.
-- Executed by src/load.py inside the same transaction as the fact load,
-- so consumers never see a half-refreshed state.
-- NOTE: keep this file free of the percent character.

TRUNCATE agg_daily_sales, agg_category_sales, agg_customer_summary;

INSERT INTO agg_daily_sales (date_key, full_date, orders, units_sold, revenue, avg_order_value)
SELECT d.date_key,
       d.full_date,
       COUNT(DISTINCT f.cart_id)::int,
       SUM(f.quantity)::int,
       SUM(f.line_total),
       ROUND(SUM(f.line_total) / COUNT(DISTINCT f.cart_id), 2)
FROM fact_sales f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.date_key, d.full_date;

INSERT INTO agg_category_sales (category, products_sold, orders, units_sold, revenue, revenue_share)
SELECT p.category,
       COUNT(DISTINCT p.product_key)::int,
       COUNT(DISTINCT f.cart_id)::int,
       SUM(f.quantity)::int,
       SUM(f.line_total),
       ROUND(100.0 * SUM(f.line_total) / NULLIF(SUM(SUM(f.line_total)) OVER (), 0), 2)
FROM fact_sales f
JOIN dim_product p ON p.product_key = f.product_key
GROUP BY p.category;

INSERT INTO agg_customer_summary
    (customer_key, orders, units_purchased, revenue, avg_order_value,
     first_order_date, last_order_date, customer_segment)
WITH per_customer AS (
    SELECT f.customer_key,
           COUNT(DISTINCT f.cart_id)::int AS orders,
           SUM(f.quantity)::int           AS units_purchased,
           SUM(f.line_total)              AS revenue,
           MIN(d.full_date)               AS first_order_date,
           MAX(d.full_date)               AS last_order_date
    FROM fact_sales f
    JOIN dim_date d ON d.date_key = f.date_key
    GROUP BY f.customer_key
),
ranked AS (
    SELECT pc.*, NTILE(4) OVER (ORDER BY pc.revenue DESC) AS quartile
    FROM per_customer pc
)
SELECT customer_key,
       orders,
       units_purchased,
       revenue,
       ROUND(revenue / orders, 2),
       first_order_date,
       last_order_date,
       CASE quartile WHEN 1 THEN 'platinum' WHEN 2 THEN 'gold' WHEN 3 THEN 'silver' ELSE 'bronze' END
FROM ranked;
