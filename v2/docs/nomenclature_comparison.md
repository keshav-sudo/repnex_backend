# SYSPRO vs Epicor Nomenclature & Schema Design

Understanding the structural and semantic differences between ERPs is key to Repnex's value proposition. This document compares the two systems side-by-side.

---

## High-Level Paradigms

| Aspect | SYSPRO | Epicor Kinetic |
|--------|--------|----------------|
| **Schema Organization** | Flat (mostly `dbo` or custom database) | Namespaced (`Erp` for business objects, `Ice` for system) |
| **Naming Philosophical Base** | Module-Centric Prefixing | Domain-Centric Entity Naming |
| **Composite Primary Keys** | Minimal (typically single business key) | Heavily composite (usually prefixed with `Company`) |
| **Multi-Company Isolation** | Separate database instances | Shared database, filtered by `Company` column |

---

## Module Prefixes in SYSPRO

SYSPRO utilizes strict two-to-three letter prefixes representing the module of origin. Here are the core modules mapped to tables:

- **Ar (Accounts Receivable):** `ArCustomer`, `ArInvoice`, `ArTrnDetail`
- **Ap (Accounts Payable):** `ApSupplier`, `ApInvoice`
- **Inv (Inventory):** `InvMaster`, `InvWarehouse`
- **Sor (Sales Orders):** `SorMaster`, `SorDetail`
- **Por (Purchase Orders):** `PorMasterHdr`, `PorMasterDetail`
- **Gen (General Ledger):** `GenMaster`, `GenTransaction`
- **Wip (Work In Progress):** `WipMaster`
- **Bom (Bill of Materials):** `BomStructure`, `BomOperations`

---

## Entity Names in Epicor

Epicor names tables directly after domain entities, placing them inside namespaces:

- **Customers & Vendors:** `Erp.Customer`, `Erp.Vendor`
- **Sales Orders:** `Erp.OrderHed` (Header), `Erp.OrderDtl` (Lines), `Erp.OrderRel` (Releases)
- **Purchase Orders:** `Erp.POHeader` (Header), `Erp.PODetail` (Lines), `Erp.PORel` (Releases)
- **Invoices:** `Erp.InvcHead` (AR Header), `Erp.InvcDtl` (AR Detail), `Erp.APInvHed` (AP Header)
- **Manufacturing:** `Erp.JobHead`, `Erp.JobOper` (Operations), `Erp.JobMtl` (Materials)
- **Inventory:** `Erp.Part` (Master), `Erp.PartBin` (Bin-specific quantities)

---

## Side-by-Side Semantic Mapping

| Logical Concept | SYSPRO Table | Epicor Table |
|-----------------|--------------|--------------|
| **Customer** | `ArCustomer` | `Erp.Customer` |
| **Supplier** | `ApSupplier` | `Erp.Vendor` |
| **Part Master** | `InvMaster` | `Erp.Part` |
| **Location Stock** | `InvWarehouse` | `Erp.PartBin` |
| **Sales Order Header** | `SorMaster` | `Erp.OrderHed` |
| **Sales Order Detail** | `SorDetail` | `Erp.OrderDtl` |
| **Purchase Order Header**| `PorMasterHdr` | `Erp.POHeader` |
| **Purchase Order Detail**| `PorMasterDetail` | `Erp.PODetail` |
| **AR Invoice Header** | `ArInvoice` | `Erp.InvcHead` |
| **AR Invoice Detail** | `ArTrnDetail` | `Erp.InvcDtl` |
| **BOM Relationship** | `BomStructure` | `Erp.PartMtl` |
| **Production Job** | `WipMaster` | `Erp.JobHead` |
