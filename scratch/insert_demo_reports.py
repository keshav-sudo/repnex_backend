import os
import sys
import uuid
from datetime import datetime, timezone
from pymongo import MongoClient

# Parse DATABASE_URL from .env
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
db_url = None
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                db_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not db_url:
    db_url = os.environ.get("DATABASE_URL")

if not db_url:
    print("Error: DATABASE_URL not found in .env or environment")
    sys.exit(1)

# Connect to MongoDB
client = MongoClient(db_url)
# Exact same parsing logic as app/main.py lifespan
db_name = "repnex"
cleaned_url = db_url
if "://" in cleaned_url:
    cleaned_url = cleaned_url.split("://", 1)[1]
if "/" in cleaned_url:
    parts = cleaned_url.split("/", 1)
    if len(parts) > 1 and parts[1]:
        possible_db = parts[1].split("?")[0]
        if possible_db and "=" not in possible_db and "&" not in possible_db:
            db_name = possible_db

db = client[db_name]

print(f"Connected to database: {db_name}")

ORG_ID = "883ce4b5-a116-4bd1-8123-afe8cd47cc10"
USER_ID = "2aaeb6bc-2dab-4ef4-a6cd-abd0d54307c8"

# Delete any existing demo reports/snapshots for this org to prevent pollution
db["reports"].delete_many({"org_id": ORG_ID})
db["report_snapshots"].delete_many({"org_id": ORG_ID})

reports_to_insert = []
snapshots_to_insert = []

# --- 1. AP Ageing Report ---
ap_report_id = str(uuid.uuid4())
ap_cols = [
    {"id": str(uuid.uuid4()), "column_name": "vendor_name", "display_name": "Vendor Name", "position": 0, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "current_due", "display_name": "Current Due", "position": 1, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "due_30_60", "display_name": "30-60 Days", "position": 2, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "due_60_90", "display_name": "60-90 Days", "position": 3, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "over_90", "display_name": "90+ Days Overdue", "position": 4, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "total_outstanding", "display_name": "Total Outstanding", "position": 5, "is_visible": True, "data_type": "number", "format_config": {}},
]
ap_rows = [
    {"vendor_name": "Acme Industrial Supplies", "current_due": 12500.00, "due_30_60": 4500.00, "due_60_90": 0.00, "over_90": 0.00, "total_outstanding": 17000.00},
    {"vendor_name": "Apex Logistics Corp", "current_due": 8400.00, "due_30_60": 1200.00, "due_60_90": 3200.00, "over_90": 1500.00, "total_outstanding": 14300.00},
    {"vendor_name": "Global Tech Services", "current_due": 0.00, "due_30_60": 0.00, "due_60_90": 2100.00, "over_90": 4500.00, "total_outstanding": 6600.00},
    {"vendor_name": "Vertex Warehousing", "current_due": 23000.00, "due_30_60": 0.00, "due_60_90": 0.00, "over_90": 0.00, "total_outstanding": 23000.00},
]
ap_report = {
    "_id": ap_report_id,
    "org_id": ORG_ID,
    "created_by": USER_ID,
    "name": "Supplier Accounts Payable (AP) Ageing",
    "description": "Weekly tracking of outstanding invoices grouped by aging buckets.",
    "query_template_id": "ap_ageing",
    "parameters": {},
    "is_public": True,
    "is_pinned": True,
    "refresh_interval_days": 7,
    "next_refresh_at": datetime.now(timezone.utc),
    "last_refreshed_at": datetime.now(timezone.utc),
    "auto_refresh_connection_id": None,
    "columns": ap_cols,
    "created_at": datetime.now(timezone.utc)
}
ap_snapshot = {
    "_id": str(uuid.uuid4()),
    "report_id": ap_report_id,
    "org_id": ORG_ID,
    "triggered_by": "manual",
    "rows_data": ap_rows,
    "rows_returned": len(ap_rows),
    "execution_time_ms": 124,
    "created_at": datetime.now(timezone.utc)
}
reports_to_insert.append(ap_report)
snapshots_to_insert.append(ap_snapshot)

# --- 2. AR Ageing Report ---
ar_report_id = str(uuid.uuid4())
ar_cols = [
    {"id": str(uuid.uuid4()), "column_name": "customer_name", "display_name": "Customer Name", "position": 0, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "current_outstanding", "display_name": "Current Outstanding", "position": 1, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "overdue_30", "display_name": "1-30 Days Overdue", "position": 2, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "overdue_60", "display_name": "31-60 Days Overdue", "position": 3, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "overdue_90", "display_name": "60+ Days Overdue", "position": 4, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "total_receivable", "display_name": "Total Receivable", "position": 5, "is_visible": True, "data_type": "number", "format_config": {}},
]
ar_rows = [
    {"customer_name": "Synergy Retail", "current_outstanding": 45000.00, "overdue_30": 12000.00, "overdue_60": 5000.00, "overdue_90": 0.00, "total_receivable": 62000.00},
    {"customer_name": "Alpha Traders Ltd", "current_outstanding": 15000.00, "overdue_30": 0.00, "overdue_60": 0.00, "overdue_90": 8000.00, "total_receivable": 23000.00},
    {"customer_name": "OmniCorp Distributors", "current_outstanding": 98000.00, "overdue_30": 4500.00, "overdue_60": 0.00, "overdue_90": 0.00, "total_receivable": 102500.00},
    {"customer_name": "Zeta Tech Partners", "current_outstanding": 3200.00, "overdue_30": 11000.00, "overdue_60": 9500.00, "overdue_90": 12000.00, "total_receivable": 35700.00},
]
ar_report = {
    "_id": ar_report_id,
    "org_id": ORG_ID,
    "created_by": USER_ID,
    "name": "Customer Accounts Receivable (AR) Ageing",
    "description": "Analysis of customer receivables and credit exposure buckets.",
    "query_template_id": "ar_ageing",
    "parameters": {},
    "is_public": True,
    "is_pinned": True,
    "refresh_interval_days": 7,
    "next_refresh_at": datetime.now(timezone.utc),
    "last_refreshed_at": datetime.now(timezone.utc),
    "auto_refresh_connection_id": None,
    "columns": ar_cols,
    "created_at": datetime.now(timezone.utc)
}
ar_snapshot = {
    "_id": str(uuid.uuid4()),
    "report_id": ar_report_id,
    "org_id": ORG_ID,
    "triggered_by": "manual",
    "rows_data": ar_rows,
    "rows_returned": len(ar_rows),
    "execution_time_ms": 142,
    "created_at": datetime.now(timezone.utc)
}
reports_to_insert.append(ar_report)
snapshots_to_insert.append(ar_snapshot)

# --- 3. Inventory Status & Valuation ---
inv_report_id = str(uuid.uuid4())
inv_cols = [
    {"id": str(uuid.uuid4()), "column_name": "item_code", "display_name": "Item Code", "position": 0, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "description", "display_name": "Description", "position": 1, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "quantity_on_hand", "display_name": "Quantity On Hand", "position": 2, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "unit_cost", "display_name": "Unit Cost", "position": 3, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "inventory_value", "display_name": "Total Inventory Value", "position": 4, "is_visible": True, "data_type": "number", "format_config": {}},
]
inv_rows = [
    {"item_code": "INV-A109", "description": "High-Grade Steel Plates", "quantity_on_hand": 450, "unit_cost": 75.00, "inventory_value": 33750.00},
    {"item_code": "INV-B892", "description": "Aluminum Extrusions (Type B)", "quantity_on_hand": 1200, "unit_cost": 18.50, "inventory_value": 22200.00},
    {"item_code": "INV-C504", "description": "Industrial Fasteners (Box of 100)", "quantity_on_hand": 340, "unit_cost": 45.00, "inventory_value": 15300.00},
    {"item_code": "INV-E210", "description": "Titanium Bolts (Grade 5)", "quantity_on_hand": 85, "unit_cost": 310.00, "inventory_value": 26350.00},
]
inv_report = {
    "_id": inv_report_id,
    "org_id": ORG_ID,
    "created_by": USER_ID,
    "name": "Inventory Status & Valuation Report",
    "description": "Stock levels on-hand and total inventory asset valuation.",
    "query_template_id": "inventory_valuation",
    "parameters": {},
    "is_public": True,
    "is_pinned": False,
    "refresh_interval_days": 1,
    "next_refresh_at": datetime.now(timezone.utc),
    "last_refreshed_at": datetime.now(timezone.utc),
    "auto_refresh_connection_id": None,
    "columns": inv_cols,
    "created_at": datetime.now(timezone.utc)
}
inv_snapshot = {
    "_id": str(uuid.uuid4()),
    "report_id": inv_report_id,
    "org_id": ORG_ID,
    "triggered_by": "manual",
    "rows_data": inv_rows,
    "rows_returned": len(inv_rows),
    "execution_time_ms": 95,
    "created_at": datetime.now(timezone.utc)
}
reports_to_insert.append(inv_report)
snapshots_to_insert.append(inv_snapshot)

# --- 4. GL Trial Balance ---
gl_report_id = str(uuid.uuid4())
gl_cols = [
    {"id": str(uuid.uuid4()), "column_name": "account_code", "display_name": "Account Code", "position": 0, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "account_name", "display_name": "Account Name", "position": 1, "is_visible": True, "data_type": "string", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "debit_balance", "display_name": "Debit ($)", "position": 2, "is_visible": True, "data_type": "number", "format_config": {}},
    {"id": str(uuid.uuid4()), "column_name": "credit_balance", "display_name": "Credit ($)", "position": 3, "is_visible": True, "data_type": "number", "format_config": {}},
]
gl_rows = [
    {"account_code": "1010", "account_name": "Cash and Bank Accounts", "debit_balance": 184500.00, "credit_balance": 0.00},
    {"account_code": "1200", "account_name": "Accounts Receivable", "debit_balance": 222700.00, "credit_balance": 0.00},
    {"account_code": "2010", "account_name": "Accounts Payable", "debit_balance": 0.00, "credit_balance": 50900.00},
    {"account_code": "3000", "account_name": "Retained Earnings", "debit_balance": 0.00, "credit_balance": 356300.00},
]
gl_report = {
    "_id": gl_report_id,
    "org_id": ORG_ID,
    "created_by": USER_ID,
    "name": "General Ledger (GL) Trial Balance",
    "description": "Summary of active account balances for financial sanity audit.",
    "query_template_id": "trial_balance",
    "parameters": {},
    "is_public": True,
    "is_pinned": False,
    "refresh_interval_days": 30,
    "next_refresh_at": datetime.now(timezone.utc),
    "last_refreshed_at": datetime.now(timezone.utc),
    "auto_refresh_connection_id": None,
    "columns": gl_cols,
    "created_at": datetime.now(timezone.utc)
}
gl_snapshot = {
    "_id": str(uuid.uuid4()),
    "report_id": gl_report_id,
    "org_id": ORG_ID,
    "triggered_by": "manual",
    "rows_data": gl_rows,
    "rows_returned": len(gl_rows),
    "execution_time_ms": 110,
    "created_at": datetime.now(timezone.utc)
}
reports_to_insert.append(gl_report)
snapshots_to_insert.append(gl_snapshot)

# Insert documents
db["reports"].insert_many(reports_to_insert)
db["report_snapshots"].insert_many(snapshots_to_insert)

print(f"Successfully inserted {len(reports_to_insert)} reports and {len(snapshots_to_insert)} snapshots for organization {ORG_ID}!")
