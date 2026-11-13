from django.urls import path

from gl import call_views, cash_views, close_views, panel_views, statement_views, views

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
    path("frame/statements", statement_views.frame_statements, name="frame_statements"),
    path("statements", statement_views.standalone_statements, name="statements"),
    path("frame/statements/account/<int:account_id>", statement_views.frame_ledger, name="frame_ledger"),
    path("statements/account/<int:account_id>", statement_views.standalone_ledger, name="ledger"),
    path("frame/close", close_views.frame_close, name="frame_close"),
    path("close", close_views.standalone_close, name="close"),
]
