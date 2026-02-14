WITH account_contacts AS (
    SELECT * FROM {{ ref('int_account_contacts') }}
),

pipeline AS (
    SELECT * FROM {{ ref('int_opportunity_pipeline') }}
)

SELECT
    account_contacts.account_id,
    account_contacts.account_name,
    account_contacts.industry,
    account_contacts.contact_count,
    COALESCE(pipeline.total_pipeline, 0) AS total_pipeline,
    COALESCE(pipeline.won_amount, 0) AS won_amount,
    pipeline.win_rate,
    pipeline.avg_deal_size
FROM account_contacts
LEFT JOIN pipeline ON account_contacts.account_id = pipeline.account_id
