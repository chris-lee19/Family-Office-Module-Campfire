from django.urls import path

from gl import (bill_views, budget_views, call_views, cash_views, close_views, journal_views, liquidity_views, pack_views, panel_views, partnership_views,
                reconcile_views, statement_views, views)

app_name = "gl"

urlpatterns = [
    path("frame/gl", views.frame_gl, name="frame_gl"),
    path("gl", views.standalone_gl, name="gl"),
    path("frame/calls", call_views.frame_calls, name="frame_calls"),
    path("calls", call_views.standalone_calls, name="calls"),
    # Inside Campfire's own records (panels and drawer buttons); always framed.
    path("frame/bill/<int:record_id>", panel_views.bill_panel, name="frame_bill_panel"),
    path("frame/bills", panel_views.bills_list_panel, name="frame_bills_panel"),
    path("frame/journal-entry/<int:record_id>", panel_views.journal_panel, name="frame_journal_panel"),
    path("frame/cash", cash_views.frame_cash, name="frame_cash"),
    path("cash", cash_views.standalone_cash, name="cash"),
    path("frame/reconcile", reconcile_views.frame_reconcile, name="frame_reconcile"),
    path("reconcile", reconcile_views.standalone_reconcile, name="reconcile"),
    path("frame/budget", budget_views.frame_budget, name="frame_budget"),
    path("budget", budget_views.standalone_budget, name="budget"),
    path("frame/liquidity", liquidity_views.frame_liquidity, name="frame_liquidity"),
    path("liquidity", liquidity_views.standalone_liquidity, name="liquidity"),
    path("frame/bills-page", bill_views.frame_bills, name="frame_bills"),
    path("bills", bill_views.standalone_bills, name="bills"),
    path("frame/journals", journal_views.frame_journals, name="frame_journals"),
    path("journals", journal_views.standalone_journals, name="journals"),
    path("frame/partnerships", partnership_views.frame_partnerships, name="frame_partnerships"),
    path("partnerships", partnership_views.standalone_partnerships, name="partnerships"),
    path("frame/pack", pack_views.frame_pack, name="frame_pack"),
    path("pack", pack_views.standalone_pack, name="pack"),
    path("frame/statements", statement_views.frame_statements, name="frame_statements"),
    path("statements", statement_views.standalone_statements, name="statements"),
    path("frame/statements/account/<int:account_id>", statement_views.frame_ledger, name="frame_ledger"),
    path("statements/account/<int:account_id>", statement_views.standalone_ledger, name="ledger"),
    path("frame/close", close_views.frame_close, name="frame_close"),
    path("close", close_views.standalone_close, name="close"),
]
