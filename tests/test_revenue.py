import pytest

from synara.domain.revenue import split_revenue, walk_horizon


def test_walk_detects_stockout_hour():
    result = walk_horizon(on_hand=24, daily_velocity=24, horizon_hours=48)
    assert result.hours_to_stockout == 24
    assert result.lost_units == 24


def test_shortfall_uses_margin_not_only_price():
    split = split_revenue(
        sku="A",
        unit_price=100,
        margin_rate=0.25,
        on_hand=0,
        on_order=0,
        daily_velocity=10,
        horizon_hours=48,
        lead_time_days=7,
        safety_stock=2,
    )
    assert split.at_risk_gross_usd == pytest.approx(2000)
    assert split.at_risk_margin_usd == pytest.approx(500)
    assert split.po_arrives_after_horizon is True
    assert split.recoverable_margin_usd == pytest.approx(0)
    assert split.unrecoverable_margin_usd == pytest.approx(500)


def test_expedite_inside_horizon_recovers_tail():
    split = split_revenue(
        sku="A",
        unit_price=100,
        margin_rate=0.5,
        on_hand=0,
        on_order=0,
        daily_velocity=24,
        horizon_hours=48,
        lead_time_days=1,
        safety_stock=0,
    )
    assert split.shortfall_units == pytest.approx(48)
    assert split.recoverable_margin_usd > 0
    assert split.unrecoverable_margin_usd > 0
    assert split.recoverable_margin_usd + split.unrecoverable_margin_usd == pytest.approx(
        split.at_risk_margin_usd
    )
