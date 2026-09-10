from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StockPathResult:
    lost_units: float
    hours_to_stockout: float | None


@dataclass(frozen=True)
class RevenueSplit:
    """Honest finance labels — not slogans.

    at_risk_gross_usd: list-price * units that stock out in the horizon with no new PO.
    at_risk_margin_usd: contribution margin on those units (price * margin_rate).
    recoverable_margin_usd: margin saved if an emergency PO is placed *now*
        and arrives after lead_time_days.
    unrecoverable_margin_usd: margin still lost even with that PO, because
        demand hits before the supplier can deliver.
    """

    sku: str
    shortfall_units: float
    at_risk_gross_usd: float
    at_risk_margin_usd: float
    recoverable_margin_usd: float
    unrecoverable_margin_usd: float
    hours_to_stockout: float | None
    lead_time_days: int
    recommended_po_qty: int
    po_arrives_after_horizon: bool

    def as_log_fields(self) -> dict[str, float | int | str | bool | None]:
        return {
            "sku": self.sku,
            "at_risk_margin_usd": round(self.at_risk_margin_usd, 2),
            "recoverable_margin_usd": round(self.recoverable_margin_usd, 2),
            "unrecoverable_margin_usd": round(self.unrecoverable_margin_usd, 2),
            "shortfall_units": round(self.shortfall_units, 2),
        }


def _hourly_velocity(daily_velocity: float, demand_multiplier: float) -> float:
    return max(0.0, daily_velocity * demand_multiplier) / 24.0


def walk_horizon(
    on_hand: float,
    daily_velocity: float,
    horizon_hours: int,
    inbound_by_hour: dict[int, float] | None = None,
    demand_multiplier: float = 1.0,
) -> StockPathResult:
    """Discrete hourly walk. inbound_by_hour maps hour index (1..H) -> units arriving."""
    stock = float(on_hand)
    lost = 0.0
    hours_to_stockout: float | None = None
    hourly = _hourly_velocity(daily_velocity, demand_multiplier)
    inbound_by_hour = inbound_by_hour or {}

    for hour in range(1, horizon_hours + 1):
        stock += inbound_by_hour.get(hour, 0.0)
        stock -= hourly
        if stock < 0:
            lost += -stock
            stock = 0.0
            if hours_to_stockout is None:
                hours_to_stockout = float(hour)
        elif stock == 0 and hourly > 0 and hours_to_stockout is None:
            hours_to_stockout = float(hour)

    return StockPathResult(lost_units=lost, hours_to_stockout=hours_to_stockout)


def recommended_po_quantity(
    daily_velocity: float,
    lead_time_days: int,
    safety_stock: int,
    on_hand: int,
    on_order: int,
    demand_multiplier: float = 1.0,
) -> int:
    target = daily_velocity * demand_multiplier * lead_time_days + safety_stock
    gap = target - on_hand - on_order
    return max(0, int(round(gap)))


def split_revenue(
    *,
    sku: str,
    unit_price: float,
    margin_rate: float,
    on_hand: int,
    on_order: int,
    daily_velocity: float,
    horizon_hours: int,
    lead_time_days: int,
    safety_stock: int,
    inbound_by_hour: dict[int, float] | None = None,
    demand_multiplier: float = 1.0,
    lead_time_override_days: int | None = None,
    expedite_hours: int = 24,
) -> RevenueSplit:
    lead = lead_time_override_days if lead_time_override_days is not None else lead_time_days
    lead = max(0, lead)
    inbound = dict(inbound_by_hour or {})
    without_po = walk_horizon(
        on_hand, daily_velocity, horizon_hours, inbound, demand_multiplier
    )
    po_qty = recommended_po_quantity(
        daily_velocity, lead, safety_stock, on_hand, on_order, demand_multiplier
    )
    standard_arrival_hour = int(round(lead * 24))
    arrival_hour = max(1, int(expedite_hours))
    with_po_inbound = dict(inbound)
    if po_qty > 0:
        hour = min(arrival_hour, horizon_hours)
        with_po_inbound[hour] = with_po_inbound.get(hour, 0.0) + po_qty

    with_po = walk_horizon(
        on_hand, daily_velocity, horizon_hours, with_po_inbound, demand_multiplier
    )

    shortfall = without_po.lost_units
    recoverable_units = max(0.0, without_po.lost_units - with_po.lost_units)
    unrecoverable_units = with_po.lost_units
    unit_margin = unit_price * margin_rate
    standard_misses_horizon = standard_arrival_hour > horizon_hours and shortfall > 0

    return RevenueSplit(
        sku=sku,
        shortfall_units=shortfall,
        at_risk_gross_usd=shortfall * unit_price,
        at_risk_margin_usd=shortfall * unit_margin,
        recoverable_margin_usd=recoverable_units * unit_margin,
        unrecoverable_margin_usd=unrecoverable_units * unit_margin,
        hours_to_stockout=without_po.hours_to_stockout,
        lead_time_days=lead,
        recommended_po_qty=po_qty,
        po_arrives_after_horizon=standard_misses_horizon,
    )
