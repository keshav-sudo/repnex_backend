from __future__ import annotations

import pytest

from app.core.exceptions import ValidationFailed
from app.query_engine.parameter_binder import bind
from app.query_engine.template_loader import init_template_registry


def test_template_loads_and_binds(settings):
    reg = init_template_registry()
    t = reg.get("top_customers_by_revenue")
    bound = bind(t, {"limit": 5, "period": "last_month"}, db_type="postgres")
    assert "%(limit)s" in bound.sql
    assert bound.params["limit"] == 5
    assert "start" in bound.params and "end" in bound.params


def test_template_rejects_unknown_param(settings):
    reg = init_template_registry()
    t = reg.get("top_customers_by_revenue")
    with pytest.raises(ValidationFailed):
        bind(t, {"limit": 5, "period": "last_month", "evil": 1}, db_type="postgres")


def test_template_rejects_bad_period(settings):
    reg = init_template_registry()
    t = reg.get("top_customers_by_revenue")
    with pytest.raises(ValidationFailed):
        bind(t, {"limit": 5, "period": "forever"}, db_type="postgres")


def test_template_rejects_unsupported_db(settings):
    reg = init_template_registry()
    t = reg.get("top_customers_by_revenue")
    with pytest.raises(ValidationFailed):
        bind(t, {"limit": 5, "period": "last_month"}, db_type="mssql")
