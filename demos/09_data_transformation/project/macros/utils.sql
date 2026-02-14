{# Reusable Jinja macros for SQL transformations #}

{% macro safe_divide(numerator, denominator) -%}
CASE WHEN {{ denominator }} = 0 THEN NULL ELSE CAST({{ numerator }} AS DOUBLE) / {{ denominator }} END
{%- endmacro %}

{% macro cents_to_dollars(amount_cents) -%}
ROUND(CAST({{ amount_cents }} AS DOUBLE) / 100, 2)
{%- endmacro %}
