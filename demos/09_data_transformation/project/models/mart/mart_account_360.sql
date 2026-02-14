WITH account_contacts AS (
    SELECT * FROM {{ ref('int_account_contacts') }}
),

pipeline AS (
    SELECT * FROM {{ ref('int_opportunity_pipeline') }}
),

ad_performance AS (
    SELECT * FROM {{ ref('mart_ad_performance') }}
)

SELECT
    account_contacts.account_id,
    account_contacts.account_name,
    account_contacts.industry,
    account_contacts.annual_revenue,
    account_contacts.contact_count,
    COALESCE(pipeline.total_pipeline, 0) AS total_pipeline,
    COALESCE(pipeline.won_amount, 0) AS won_amount,
    COALESCE(ad_performance.total_cost, 0) AS total_ad_spend,
    COALESCE(ad_performance.total_conversions, 0) AS total_ad_conversions
FROM account_contacts
LEFT JOIN pipeline ON account_contacts.account_id = pipeline.account_id
CROSS JOIN ad_performance
