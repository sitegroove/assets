WITH opportunities AS (
    SELECT * FROM {{ ref('stg_opportunities') }}
),

pipeline AS (
    SELECT
        account_id,
        SUM(amount_usd) AS total_pipeline,
        SUM(CASE WHEN is_won THEN amount_usd ELSE 0 END) AS won_amount,
        COUNT(*) AS pipeline_count,
        SUM(CASE WHEN is_won THEN 1 ELSE 0 END) AS won_count,
        AVG(amount_usd) AS avg_deal_size,
        {{ safe_divide(
            'SUM(CASE WHEN is_won THEN 1 ELSE 0 END)',
            'COUNT(*)'
        ) }} AS win_rate
    FROM opportunities
    GROUP BY account_id
)

SELECT * FROM pipeline
