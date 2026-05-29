from __future__ import annotations

import pytest

from app.core.exceptions import ValidationFailed
from app.query_engine.parameter_binder import bind
from app.query_engine.template_loader import init_template_registry


def test_template_loads_and_binds(settings):
    reg = init_template_registry()
    t = reg.get("ar_top_customers_by_revenue")
    bound = bind(t, {"start_date": "2025-01-01", "end_date": "2025-06-30", "limit": 5}, db_type="postgres")
    assert "%(limit)s" in bound.sql
    assert bound.params["limit"] == 5
    assert bound.params["start_date"].isoformat() == "2025-01-01"
    assert bound.params["end_date"].isoformat() == "2025-06-30"


def test_template_rejects_bad_date(settings):
    reg = init_template_registry()
    t = reg.get("ar_top_customers_by_revenue")
    with pytest.raises(ValidationFailed):
        bind(t, {"start_date": "invalid-date", "end_date": "2025-06-30"}, db_type="postgres")


def test_template_rejects_unsupported_db(settings):
    reg = init_template_registry()
    t = reg.get("ar_top_customers_by_revenue")
    with pytest.raises(ValidationFailed):
        bind(t, {"start_date": "2025-01-01", "end_date": "2025-06-30"}, db_type="mysql")


def test_template_resolves_natural_dates(settings):
    reg = init_template_registry()
    t = reg.get("ar_top_customers_by_revenue")
    bound = bind(t, {"start_date": "last 6 months"}, db_type="postgres")
    assert bound.params["start_date"] is not None
    assert bound.params["end_date"] is not None
    assert bound.params["start_date"] < bound.params["end_date"]

