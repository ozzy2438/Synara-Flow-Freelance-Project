"""Streamlit decision cockpit — API client only; does not open DuckDB."""

from __future__ import annotations

import os

import httpx
import pandas as pd
import streamlit as st

API = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "dev-synara-key")
HEADERS = {"X-API-Key": API_KEY}


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, headers=HEADERS, timeout=60.0)


@st.cache_data(ttl=5.0)
def fetch_alerts(demand_multiplier: float, lead_delta: int) -> dict:
    with _client() as client:
        r = client.get(
            "/api/alerts",
            params={
                "demand_multiplier": demand_multiplier,
                "lead_time_delta_days": lead_delta,
            },
        )
        r.raise_for_status()
        return r.json()


def seed() -> dict:
    with _client() as client:
        r = client.post("/api/seed")
        r.raise_for_status()
        return r.json()


def replenish(sku: str, quantity: int | None) -> dict:
    payload: dict = {"sku": sku}
    if quantity:
        payload["quantity"] = quantity
    with _client() as client:
        r = client.post("/api/replenish", json=payload)
        r.raise_for_status()
        return r.json()


st.set_page_config(page_title="Synara — Stockout Decision Engine", layout="wide")
st.markdown(
    """
    <style>
    .metric-note { color: #6b7280; font-size: 0.85rem; }
    .stMetric { background: #0f172a; padding: 0.6rem 0.8rem; border-radius: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Synara stockout decision engine")
st.caption(
    "48-hour demand walk using recent (48h) velocity, not a 7-day average that "
    "washes out spikes. Money figures are contribution margin, not list-price revenue."
)

with st.sidebar:
    st.subheader("Scenario")
    demand = st.slider("Demand multiplier", 0.8, 2.0, 1.0, 0.05)
    lead_delta = st.slider("Lead time delta (days)", -2, 10, 0, 1)
    st.caption("Applies to the simulation only until you place a real PO.")
    if st.button("Load / reset synthetic world", use_container_width=True):
        try:
            info = seed()
            fetch_alerts.clear()
            st.success(
                f"Seeded {info['sku_count']} SKUs, {info['order_count']} orders. "
                f"Spiked: {', '.join(info['spiked_skus'])}"
            )
        except httpx.HTTPError as exc:
            st.error(f"Seed failed: {exc}")
    st.markdown(f"`API`: {API}")

tab_warn, tab_scene = st.tabs(["Early warning", "Scenario simulator"])

try:
    payload = fetch_alerts(demand, lead_delta)
except httpx.HTTPError as exc:
    st.error(
        f"Cannot reach API at {API}. Start Postgres, the API, and (optionally) "
        f"the outbox worker. {exc}"
    )
    st.stop()

totals = payload["totals"]
at_risk = payload["at_risk"]
note = payload.get("metric_note", "")

with tab_warn:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs stocking out in 48h", totals["at_risk_sku_count"])
    c2.metric("At-risk contribution margin", f"${totals['at_risk_margin_usd']:,.0f}")
    c3.metric("Recoverable if PO placed now", f"${totals['recoverable_margin_usd']:,.0f}")
    c4.metric("Unrecoverable (arrives too late)", f"${totals['unrecoverable_margin_usd']:,.0f}")
    st.markdown(f"<p class='metric-note'>{note}</p>", unsafe_allow_html=True)
    st.caption(
        f"Pending outbox events (not yet applied to DuckDB): {payload.get('pending_outbox', 0)}"
    )

    if not at_risk:
        st.info("No SKU is projected to stock out in the 48h horizon under this scenario.")
    else:
        df = pd.DataFrame(at_risk)
        show = df[
            [
                "sku",
                "name",
                "hours_to_stockout",
                "on_hand",
                "daily_velocity_48h",
                "daily_velocity_7d",
                "lead_time_days",
                "at_risk_margin_usd",
                "recoverable_margin_usd",
                "unrecoverable_margin_usd",
                "recommended_po_qty",
                "po_arrives_after_horizon",
            ]
        ].copy()
        show["hours_to_stockout"] = show["hours_to_stockout"].map(
            lambda x: None if x is None else round(float(x), 1)
        )
        st.dataframe(show, use_container_width=True, hide_index=True)

        st.subheader("Place emergency purchase order")
        col_a, col_b, col_c = st.columns([2, 1, 1])
        skus = list(df["sku"])
        chosen = col_a.selectbox("SKU", skus)
        default_qty = int(df.loc[df["sku"] == chosen, "recommended_po_qty"].iloc[0])
        qty = col_b.number_input("Quantity", min_value=1, value=max(default_qty, 1))
        if col_c.button("Place emergency PO", type="primary"):
            try:
                result = replenish(chosen, int(qty))
                fetch_alerts.clear()
                st.success(
                    f"Expedite PO {result['po_id']} for {result['quantity']} units of {result['sku']}. "
                    f"Expected arrival {result['expected_arrival']} (24h freight, not standard supplier lead time). "
                    "Inbound receipts are included in the next 48h walk."
                )
                st.rerun()
            except httpx.HTTPError as exc:
                st.error(f"PO failed: {exc}")

    ops = payload.get("operational_alerts") or []
    if ops:
        st.subheader("Operational ROP crossings (from outbox → DuckDB)")
        st.dataframe(pd.DataFrame(ops), use_container_width=True, hide_index=True)

with tab_scene:
    st.write(
        "Move the sidebar sliders, then compare which SKUs break when demand "
        "jumps or suppliers slow down. Lead-time increases shrink recoverable margin "
        "because the PO arrives after more of the 48h window has already stocked out."
    )
    baseline = fetch_alerts(1.0, 0)
    left, right = st.columns(2)
    with left:
        st.markdown("**Baseline** (demand ×1.0, current lead times)")
        st.metric("At-risk margin", f"${baseline['totals']['at_risk_margin_usd']:,.0f}")
        st.metric("Recoverable margin", f"${baseline['totals']['recoverable_margin_usd']:,.0f}")
        st.metric("SKUs at risk", baseline["totals"]["at_risk_sku_count"])
    with right:
        st.markdown(f"**Scenario** (demand ×{demand:.2f}, lead Δ {lead_delta:+d}d)")
        st.metric("At-risk margin", f"${totals['at_risk_margin_usd']:,.0f}")
        st.metric("Recoverable margin", f"${totals['recoverable_margin_usd']:,.0f}")
        st.metric("SKUs at risk", totals["at_risk_sku_count"])

    base_skus = {r["sku"] for r in baseline["at_risk"]}
    scene_skus = {r["sku"] for r in at_risk}
    newly = scene_skus - base_skus
    st.markdown("**SKUs that only break in this scenario**")
    if newly:
        extra = [r for r in at_risk if r["sku"] in newly]
        st.dataframe(
            pd.DataFrame(extra)[
                ["sku", "name", "hours_to_stockout", "at_risk_margin_usd", "lead_time_days"]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.write("No additional SKUs versus baseline (the same set may still get worse).")
