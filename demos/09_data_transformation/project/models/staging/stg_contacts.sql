WITH source AS (
    SELECT * FROM {{ source('fivetran_salesforce', 'contacts') }}
),

cleaned AS (
    SELECT
        contact_id,
        account_id,
        CONCAT(first_name, ' ', last_name) AS full_name,
        LOWER(email) AS email,
        COALESCE(title, 'Unknown') AS job_title
    FROM source
)

SELECT * FROM cleaned
