from __future__ import annotations

from dataclasses import dataclass


class DomainError(Exception):
    """Base class for recoverable domain failures."""


class InsufficientStockError(DomainError):
    def __init__(self, sku: str, requested: int, on_hand: int) -> None:
        super().__init__(
            f"Insufficient stock for {sku}: requested {requested}, on_hand {on_hand}"
        )
        self.sku = sku
        self.requested = requested
        self.on_hand = on_hand


class SkuNotFoundError(DomainError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"Unknown SKU {sku}")
        self.sku = sku


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price: float
    margin_rate: float
    lead_time_days: int
    category: str
    is_high_runner: bool

    @property
    def unit_margin(self) -> float:
        return self.unit_price * self.margin_rate


@dataclass
class InventoryPosition:
    sku: str
    on_hand: int
    on_order: int
    safety_stock: int
    reorder_point: int

    def cover_hours(self, daily_velocity: float) -> float | None:
        if daily_velocity <= 0:
            return None
        return (self.on_hand / daily_velocity) * 24.0


def reorder_point(daily_velocity: float, lead_time_days: int, safety_stock: int) -> int:
    return max(0, int(round(daily_velocity * lead_time_days + safety_stock)))


def safety_stock(daily_velocity: float, lead_time_days: int, z: float = 1.65) -> int:
    """Normal approximation: z * sqrt(velocity * lead_time) for ~95% fill."""
    variance = max(daily_velocity, 0.0) * max(lead_time_days, 1)
    return max(1, int(round(z * (variance ** 0.5))))
