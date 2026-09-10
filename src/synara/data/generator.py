"""Deterministic 50-SKU catalog with a 24–48h stockout spike on high-runners."""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from synara.domain import reorder_point, safety_stock

PRODUCT_NAMES = [
    "Aero Bottle 500ml",
    "Nimbus Running Sock",
    "Cedar Desk Organizer",
    "Lumen Desk Lamp",
    "Harbor Ceramic Mug",
    "Peak Trail Mix 400g",
    "Solace Bath Towel",
    "Volt USB-C Cable",
    "Grove Olive Oil 750ml",
    "Drift Wireless Mouse",
    "Canvas Tote Navy",
    "Quartz Wall Clock",
    "Breeze Air Filter",
    "Forge Chef Knife",
    "Pebble Phone Stand",
    "Meadow Green Tea 40ct",
    "Ridge Hiking Cap",
    "Ember Scented Candle",
    "Pulse Heart Rate Band",
    "North Fleece Throw",
    "Arc Stainless Bottle",
    "Willow Plant Pot",
    "Summit Protein Bar",
    "Kite Kids Backpack",
    "Marble Cutting Board",
    "Orbit HDMI Hub",
    "Dune Sunglasses",
    "Fern Shower Gel",
    "Bolt AA Batteries 8pk",
    "Cove Yoga Mat",
    "Flint Fire Starter",
    "Haze Linen Shirt",
    "Copper Pour-Over Kettle",
    "Nova LED Strip",
    "Pine Cutting Soap",
    "Echo Bluetooth Speaker",
    "Slate Notebook A5",
    "Amber Face Serum",
    "Crest Bike Light",
    "Loom Cotton Sheets",
    "Jade Jade Roller",
    "Storm Rain Jacket",
    "Basil Pasta Sauce",
    "Pixel Webcam Cover",
    "Timber Photo Frame",
    "Glide Laptop Sleeve",
    "Oats Overnight Jar",
    "Sable Leather Belt",
    "Wisp Reed Diffuser",
    "Anchor Door Stop",
]

CATEGORIES = ["home", "grocery", "electronics", "apparel", "beauty", "sports"]


@dataclass
class CatalogSku:
    sku: str
    name: str
    unit_price: float
    margin_rate: float
    lead_time_days: int
    category: str
    is_high_runner: bool
    base_daily_velocity: float
    spiked: bool
    safety_stock: int
    reorder_point: int
    on_hand: int


@dataclass
class SyntheticOrder:
    order_id: str
    sku: str
    quantity: int
    unit_price: float
    created_at: datetime


def build_catalog(rng: random.Random) -> list[CatalogSku]:
    skus: list[CatalogSku] = []
    high_runner_idx = set(range(10))
    spike_idx = set(range(5))
    for i, name in enumerate(PRODUCT_NAMES):
        sku = f"SKU-{i+1:03d}"
        high = i in high_runner_idx
        spiked = i in spike_idx
        price = round(rng.uniform(8.0, 160.0) if not high else rng.uniform(12.0, 90.0), 2)
        margin = round(rng.uniform(0.18, 0.48) if high else rng.uniform(0.22, 0.55), 3)
        lead = rng.choice([3, 5, 7, 10, 14] if not spiked else [5, 7, 10])
        if high:
            base_v = rng.uniform(18.0, 40.0)
        else:
            base_v = rng.uniform(2.0, 12.0)
        ss = safety_stock(base_v, lead)
        rop = reorder_point(base_v, lead, ss)
        if spiked:
            # Spike ~5–7x over the last 36h. Stock covers ~30h at the spiked rate.
            spike_v = base_v * rng.uniform(5.0, 7.0)
            on_hand = max(8, int(spike_v * (30.0 / 24.0)))
        elif high:
            on_hand = int(base_v * lead * rng.uniform(1.4, 2.2)) + ss
        else:
            on_hand = int(base_v * lead * rng.uniform(1.8, 3.0)) + ss
        skus.append(
            CatalogSku(
                sku=sku,
                name=name,
                unit_price=price,
                margin_rate=margin,
                lead_time_days=lead,
                category=CATEGORIES[i % len(CATEGORIES)],
                is_high_runner=high,
                base_daily_velocity=round(base_v, 3),
                spiked=spiked,
                safety_stock=ss,
                reorder_point=rop,
                on_hand=on_hand,
            )
        )
    return skus


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth for small lambda; cap for hourly rates.
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))) )
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while p > L:
        k += 1
        p *= rng.random()
    return max(0, k - 1)


def build_order_history(
    catalog: list[CatalogSku], rng: random.Random, now: datetime, days: int = 7
) -> list[SyntheticOrder]:
    orders: list[SyntheticOrder] = []
    hours = days * 24
    start = now - timedelta(hours=hours)
    for sku in catalog:
        for h in range(hours):
            ts = start + timedelta(hours=h, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
            hours_ago = (now - ts).total_seconds() / 3600.0
            daily = sku.base_daily_velocity
            if sku.spiked and hours_ago <= 36:
                daily *= 6.0
            hourly = daily / 24.0
            qty = _poisson(rng, hourly)
            if qty <= 0:
                continue
            orders.append(
                SyntheticOrder(
                    order_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sku.sku}-{h}-{qty}")),
                    sku=sku.sku,
                    quantity=qty,
                    unit_price=sku.unit_price,
                    created_at=ts,
                )
            )
    return orders


def generate_world(seed: int = 42, now: datetime | None = None) -> tuple[list[CatalogSku], list[SyntheticOrder]]:
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    catalog = build_catalog(rng)
    orders = build_order_history(catalog, rng, now)
    return catalog, orders
