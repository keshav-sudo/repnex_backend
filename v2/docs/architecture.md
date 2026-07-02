# Repnex v2 — Core Semantic Architecture

## Overview

Repnex v2 replaces template-based SQL generation with a **declarative business ontology**. Instead of directly translating Natural Language (NL) to SQL or picking pre-defined SQL templates, the engine translates NL to abstract business concepts, resolves them using an ERP-specific adapter, constraints the joins using a predefined relationship graph, and builds the query dynamically.

---

## The 4-Layer Architecture Pipeline

```
     User Query: "Show me my top 10 customers by YTD sales"
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. Intent & Concept Resolution                              │
│    - Classifies query as "executable"                       │
│    - Extracts business concepts: [customer]                 │
│    - Extracts metrics/fields: [ytd_sales]                   │
│    - Extracts parameters: {limit: 10, sort: desc}           │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. ERP Adapter Resolution (e.g., SYSPRO)                     │
│    - Resolves concept [customer] to physical table:         │
│      - ArCustomer                                           │
│      - ArCustomerBal (joined on Customer)                   │
│    - Resolves logical [ytd_sales] to column:                │
│      - ArCustomerBal.YtdSales                               │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Relationship & Join Resolution                           │
│    - Checks relationships/syspro/joins.yaml                 │
│    - Identifies join:                                       │
│      ArCustomer LEFT JOIN ArCustomerBal                     │
│      ON ArCustomer.Customer = ArCustomerBal.Customer        │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Dialect-Specific SQL Generator                           │
│    - Compiles into SQL Server (mssql) dialect:              │
│      SELECT TOP 10 c.Customer, c.Name, b.YtdSales           │
│      FROM ArCustomer c                                      │
│      LEFT JOIN ArCustomerBal b                              │
│        ON c.Customer = b.Customer                           │
│      ORDER BY b.YtdSales DESC                               │
└─────────────────────────────────────────────────────────────┘
```

---

## Directory Structure and File Manifest

```
v2/
├── ontology/                 # Universal Business Definitions
│   ├── customer.yaml         # Fields, synonyms, and relationships for Customer
│   ├── supplier.yaml         # Fields and synonyms for Supplier/Vendor
│   ├── inventory_item.yaml   # Stock item attributes
│   └── ...                   # Total of 20 business concepts
│
├── adapters/                 # ERP Mappings
│   ├── syspro/
│   │   ├── _meta.yaml        # SYSPRO dialect and naming settings
│   │   ├── customer.yaml     # ArCustomer / ArCustomerBal table mapping
│   │   └── ...
│   └── epicor/
│       ├── _meta.yaml        # Epicor schema name and multi-tenancy info
│       ├── customer.yaml     # Erp.Customer table mapping
│       └── ...
│
├── relationships/            # Constraints & Joins (prevents hallucinations)
│   ├── syspro/
│   │   └── joins.yaml        # Exact SQL join statements for SYSPRO
│   └── epicor/
│       └── joins.yaml        # Exact SQL join statements for Epicor
│
└── engine/                   # Execution Logic (Python Code)
    ├── ontology_loader.py    # Loads universal schemas
    ├── adapter_loader.py     # Parses organization's active ERP mapping
    ├── concept_resolver.py   # Maps NL synonyms to ontology terms
    ├── relationship_resolver.py # Finds optimal JOIN path between tables
    └── sql_builder.py        # Assembles SELECT, JOIN, WHERE, ORDER BY, LIMIT
```

---

## How the YAML-based Mapping Works

### 1. The Ontology (`v2/ontology/customer.yaml`)
Defines the logical model that the LLM reasons over. It uses synonyms to catch varied natural language terms.

```yaml
concept: customer
synonyms:
  - client
  - debtor
  - buyer
attributes:
  code:
    type: string
  name:
    type: string
balance_attributes:
  ytd_sales:
    type: decimal
```

### 2. The ERP Adapter (`v2/adapters/syspro/customer.yaml`)
Specifies the physical database tables, fields, and calculations corresponding to the ontology.

```yaml
concept: customer
table: ArCustomer
primary_key: Customer
fields:
  code: Customer
  name: Name
balance_mapping:
  table: ArCustomerBal
  join_on: Customer
  fields:
    ytd_sales: YtdSales
```

### 3. The Joins Graph (`v2/relationships/syspro/joins.yaml`)
Guides the query generator to construct exact joins instead of letting the LLM invent relationships.

```yaml
relationships:
  - from_concept: customer
    to_concept: customer_balance
    join_type: left
    condition: "ArCustomer.Customer = ArCustomerBal.Customer"
```
