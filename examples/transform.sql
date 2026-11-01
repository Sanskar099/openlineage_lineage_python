SELECT
    c.customer_id,
    c.region,
    SUM(o.amount) AS lifetime_value
FROM raw.customers AS c
JOIN raw.orders AS o
    ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.region