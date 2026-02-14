WITH campaigns AS (
    SELECT * FROM {{ ref('stg_campaigns') }}
),

performance AS (
    SELECT * FROM {{ ref('stg_ad_performance') }}
),

aggregated AS (
    SELECT
        performance.campaign_id,
        campaigns.campaign_name,
        SUM(performance.impressions) AS total_impressions,
        SUM(performance.clicks) AS total_clicks,
        SUM(performance.cost_usd) AS total_cost,
        SUM(performance.conversions) AS total_conversions,
        {{ safe_divide('SUM(performance.clicks)', 'SUM(performance.impressions)') }} AS ctr,
        {{ safe_divide('SUM(performance.cost_usd)', 'SUM(performance.clicks)') }} AS cpc
    FROM performance
    INNER JOIN campaigns ON performance.campaign_id = campaigns.campaign_id
    GROUP BY performance.campaign_id, campaigns.campaign_name
)

SELECT * FROM aggregated
