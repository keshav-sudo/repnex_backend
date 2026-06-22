"""
Comprehensive Syspro ERP search quality test.
Tests 50+ diverse questions across all modules.
Run: python scripts/test_search_quality.py
"""
from pinecone import Pinecone

PINECONE_API_KEY = "pcsk_8uNjs_JEnKJ8fThatyMGp8pdLM59ukbadsy3ga5awHrUCfBVbrvKmnaXqasWmUfHTAkuX"
PINECONE_HOST    = "https://repnex-0kl271f.svc.aped-4627-b74a.pinecone.io"
PINECONE_INDEX   = "repnex-sql-templates"
NAMESPACE        = "repnex"
EMBED_MODEL      = "llama-text-embed-v2"

# Format: (question, expected_template_id)
TEST_CASES = [
    # ── Accounts Payable — Invoices ───────────────────────────────────
    ("Show me all supplier invoices from last 6 months",           "ap_invoices_by_date_range"),
    ("List AP invoices between January and June 2025",             "ap_invoices_by_date_range"),
    ("Generate last 6 months AP invoices",                         "ap_invoices_by_date_range"),
    ("Purchase invoices for this year",                            "ap_invoices_by_date_range"),
    ("AP invoices for supplier ABC",                               "ap_invoices_by_supplier"),
    ("All invoices from a specific vendor",                        "ap_invoices_by_supplier"),
    ("Invoices without purchase order",                            "ap_invoices_missing_po"),
    ("AP invoices not linked to PO",                               "ap_invoices_missing_po"),
    ("Invoices above 10000 threshold",                             "ap_invoices_above_threshold"),
    ("High value purchase invoices",                               "ap_invoices_above_threshold"),
    ("Discount taken on invoices",                                 "ap_invoices_discount_taken"),
    ("Duplicate AP invoices",                                      "ap_duplicate_invoice_detection"),
    ("Pending approval invoices",                                  "ap_approval_pending_invoices"),

    # ── Accounts Payable — Ageing & Overdue ──────────────────────────
    ("AP ageing report",                                           "ap_ageing_report"),
    ("Show overdue AP buckets 30 60 90 days",                      "ap_ageing_report"),
    ("How overdue are my supplier payments",                       "ap_suppliers_largest_overdue"),
    ("Which invoices are due in next 30 days",                     "ap_due_next_days"),
    ("Suppliers with no activity last 6 months",                   "ap_zero_activity_suppliers"),
    ("AP ageing by supplier group",                                "ap_invoice_ageing_by_supplier_group"),

    # ── Accounts Payable — Payments ───────────────────────────────────
    ("Supplier payment history",                                   "ap_payment_history"),
    ("Payments made to vendors last month",                        "ap_payment_history"),
    ("AP cash flow forecast upcoming payments",                    "cash_forecasted_payments"),
    ("Average payment days per supplier",                          "ap_avg_payment_days"),
    ("Unallocated supplier payments",                              "ap_unallocated_payments"),

    # ── Withholding Tax ───────────────────────────────────────────────
    ("Withholding tax deducted on supplier invoices",              "tax_wht_deducted"),
    ("WHT deducted this quarter",                                  "tax_wht_deducted"),
    ("TDS on supplier payments",                                   "tax_wht_deducted"),
    ("Withholding tax per supplier",                               "ap_wht_per_supplier"),
    ("WHT certificates issued",                                    "tax_wht_certificates_issued"),
    ("Withholding tax rates check",                                "tax_wht_rates_check"),
    ("WHT deposited to government",                                "tax_wht_deposited"),

    # ── VAT / Tax ─────────────────────────────────────────────────────
    ("VAT return data for this quarter",                           "tax_vat_return_data"),
    ("Output VAT on sales",                                        "tax_output_vat"),
    ("Input VAT on purchases",                                     "tax_input_vat"),
    ("Net VAT payable",                                            "tax_net_vat_payable"),
    ("Zero rated sales transactions",                              "tax_zero_rated_exempt"),
    ("Tax code breakdown",                                         "tax_tax_code_breakdown"),

    # ── Accounts Receivable ───────────────────────────────────────────
    ("Customer ageing report",                                     "ar_customer_aging"),
    ("Overdue customer invoices",                                   "ar_overdue_customers"),
    ("Top 10 customers by revenue",                                "ar_top_customers_by_revenue"),
    ("Which customers owe the most money",                         "ar_overdue_customers"),
    ("Customer receipts this month",                               "ar_receipts_by_customer"),
    ("DSO days sales outstanding",                                 "ar_dso_trend"),
    ("Customers over credit limit",                                "ar_credit_limit_exceeded"),
    ("Bad debts written off",                                      "ar_bad_debts_written_off"),

    # ── General Ledger ────────────────────────────────────────────────
    ("Trial balance for this period",                              "gl_trial_balance"),
    ("Income statement profit and loss",                           "gl_profit_loss_monthly"),
    ("Balance sheet summary",                                      "gl_balance_sheet"),
    ("GL account activity transactions",                           "gl_account_activity"),
    ("Journal entries this month",                                 "gl_top_journals"),
    ("Budget vs actual profit and loss",                           "gl_budget_vs_actual_pl"),
    ("Unposted GL batches",                                        "gl_unposted_batches"),
    ("Intercompany eliminations",                                  "gl_intercompany_eliminations"),

    # ── Cash & Bank ───────────────────────────────────────────────────
    ("Bank reconciliation status",                                 "cash_reconciliation_status"),
    ("Cash flow statement",                                        "gl_cash_flow_direct"),
    ("Uncleared bank transactions",                                "cash_uncleared_transactions"),
    ("13 week cash forecast",                                      "cash_13_week_forecast"),
    ("Forex gain loss on payments",                                "cash_forex_gain_loss"),

    # ── Budget ────────────────────────────────────────────────────────
    ("Budget by cost centre",                                      "budget_by_cost_centre"),
    ("Budget vs actuals variance",                                 "budget_pl_variance"),

    # ── Intercompany ──────────────────────────────────────────────────
    ("Intercompany balances between entities",                     "interco_balances"),
    ("Intercompany transactions",                                  "interco_transactions"),
    ("Unmatched intercompany entries",                             "interco_unmatched"),

    # ── Audit ─────────────────────────────────────────────────────────
    ("Audit trail of changes",                                     "audit_audit_trail"),
    ("Period closing status",                                      "closing_period_status"),

    # ── Forex ────────────────────────────────────────────────────────
    ("Foreign exchange gain loss",                                 "forex_gain_loss"),
]

PASS_COLOR   = "\033[92m"  # green
FAIL_COLOR   = "\033[91m"  # red
WARN_COLOR   = "\033[93m"  # yellow
RESET_COLOR  = "\033[0m"
BOLD         = "\033[1m"


def run():
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(name=PINECONE_INDEX, host=PINECONE_HOST)

    passed = 0
    top3   = 0
    failed = 0

    print(f"\n{BOLD}{'='*80}")
    print(" Syspro ERP — Pinecone Search Quality Test")
    print(f"{'='*80}{RESET_COLOR}\n")
    print(f"  Model   : {EMBED_MODEL}")
    print(f"  Index   : {PINECONE_INDEX}")
    print(f"  Queries : {len(TEST_CASES)}")
    print()

    results_detail = []

    for question, expected in TEST_CASES:
        emb = pc.inference.embed(
            model=EMBED_MODEL,
            inputs=[question],
            parameters={"input_type": "query"},
        )
        res = index.query(
            vector=emb.data[0].values,
            top_k=5,
            include_metadata=True,
            namespace=NAMESPACE,
        )
        matches = res.get("matches", [])
        ids = [m["id"] for m in matches]
        scores = {m["id"]: m["score"] for m in matches}

        if ids and ids[0] == expected:
            status = "✅ TOP-1"
            passed += 1
            color  = PASS_COLOR
        elif expected in ids[:3]:
            status = "🟡 TOP-3"
            top3  += 1
            color  = WARN_COLOR
        else:
            status = "❌ MISS "
            failed += 1
            color  = FAIL_COLOR

        score_str = f"{scores.get(expected, 0):.4f}" if expected in scores else "  N/A "
        results_detail.append((status, color, question, expected, ids[:3], score_str))

    # ── Print results ─────────────────────────────────────────────────
    print(f"  {'STATUS':<10} {'SCORE':>6}  {'QUESTION':<52}  EXPECTED → GOT")
    print(f"  {'-'*120}")
    for status, color, question, expected, got_ids, score in results_detail:
        q_short = question[:50] + ".." if len(question) > 50 else question
        got1    = got_ids[0] if got_ids else "none"
        marker  = "" if got1 == expected else f" → got: {got1}"
        print(f"{color}  {status}  {score:>6}  {q_short:<52}  {expected}{marker}{RESET_COLOR}")

    # ── Summary ───────────────────────────────────────────────────────
    total    = len(TEST_CASES)
    pct_top1 = passed / total * 100
    pct_top3 = (passed + top3) / total * 100

    print()
    print(f"{BOLD}{'='*80}")
    print(f"  RESULTS: {total} queries tested")
    print(f"  ✅ TOP-1 : {passed:>3} / {total}  ({pct_top1:.0f}%)")
    print(f"  🟡 TOP-3 : {top3:>3} / {total}  (additional)")
    print(f"  ❌ MISS  : {failed:>3} / {total}  ({failed/total*100:.0f}%)")
    print(f"  📊 Top-3 coverage: {pct_top3:.0f}%")
    print(f"{'='*80}{RESET_COLOR}")


if __name__ == "__main__":
    run()
