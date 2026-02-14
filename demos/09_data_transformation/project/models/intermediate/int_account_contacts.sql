WITH accounts AS (
    SELECT * FROM {{ ref('stg_accounts') }}
),

contacts AS (
    SELECT * FROM {{ ref('stg_contacts') }}
),

contact_agg AS (
    SELECT
        account_id,
        COUNT(*) AS contact_count,
        MIN(full_name) AS primary_contact_name,
        MIN(email) AS primary_contact_email
    FROM contacts
    GROUP BY account_id
)

SELECT
    accounts.account_id,
    accounts.account_name,
    accounts.industry,
    accounts.annual_revenue,
    COALESCE(contact_agg.contact_count, 0) AS contact_count,
    contact_agg.primary_contact_name,
    contact_agg.primary_contact_email
FROM accounts
LEFT JOIN contact_agg ON accounts.account_id = contact_agg.account_id
