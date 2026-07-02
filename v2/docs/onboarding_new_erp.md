# Onboarding a New ERP to Repnex v2

The core benefit of the Repnex v2 architecture is **extensibility**. Adding support for a new ERP (e.g., SAP, Microsoft Dynamics 365, or Sage) does not require changing backend Python code or rewriting prompts. It is done entirely through configuration.

---

## Step-by-Step Guide

### Step 1: Create the ERP Meta File
Create a new directory under `v2/adapters/<new_erp>/` and add a `_meta.yaml` file:

```yaml
# v2/adapters/sap/_meta.yaml
erp: sap
display_name: SAP ERP
dialect: hana # or postgres / mssql
naming_convention: short_codes
table_prefix: ""
schema_name: saphana
multi_company_mode: client_column
```

### Step 2: Map the Core Concepts
Create YAML files matching the names in `v2/ontology/` to map the physical tables:

```yaml
# v2/adapters/sap/customer.yaml
concept: customer
table: KNA1
primary_key: KUNNR

fields:
  code: KUNNR
  name: NAME1
  currency: WAERS
  email: ADR6.SMTP_ADDR # Nested join example if supported by engine
```

### Step 3: Define Join Conditions
Define how the tables in your newly added ERP map to each other in `v2/relationships/<new_erp>/joins.yaml`:

```yaml
# v2/relationships/sap/joins.yaml
relationships:
  - from_concept: customer
    to_concept: sales_order
    join_type: inner
    condition: "KNA1.KUNNR = VBAK.KUNNR AND KNA1.MANDT = VBAK.MANDT"
```

---

## Validation Checklist

1. **Verify Database Dialect:** Ensure the `dialect` key in `_meta.yaml` is one of `postgres`, `mssql`, `mysql`, `oracle`, or `sqlite`.
2. **Entity Consistency:** Ensure every adapter file has a corresponding `concept` matching a file in `v2/ontology/`.
3. **Primary Key Inclusion:** Ensure the physical primary key of the table is mapped in the `fields` section under the appropriate alias.
