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
def fetch_alerts(demand_multiplier: float, lead_delta: int, lost_sale_rate: float) -> dict:
    with _client() as client:
        r = client.get(
            "/api/alerts",
            params={
                "demand_multiplier": demand_multiplier,
                "lead_time_delta_days": lead_delta,
                "lost_sale_rate": lost_sale_rate,
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


def export_world() -> dict:
    with _client() as client:
        r = client.get("/api/export")
        r.raise_for_status()
        return r.json()


def ingest(catalog_csv: str, sales_csv: str) -> dict:
    with _client() as client:
        r = client.post("/api/ingest", json={"catalog_csv": catalog_csv, "sales_csv": sales_csv})
        r.raise_for_status()
        return r.json()


def export_po(po_id: str) -> dict:
    with _client() as client:
        r = client.post(f"/api/replenish/{po_id}/export")
        r.raise_for_status()
        return r.json()


st.set_page_config(page_title="Synara — Stockout Decision Engine", layout="wide")
st.markdown(
    """
    <style>
    .metric-note { color: #334155; font-size: 0.95rem; line-height: 1.45; }
    div[data-testid="stMetric"] {
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      padding: 0.85rem 1rem;
      border-radius: 10px;
    }
    div[data-testid="stMetric"] label { color: #475569 !important; }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #0f172a !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Synara stockout decision engine")
st.caption(
    "48-hour demand walk on 48h velocity. Money is contribution margin, not list price. "
    "PO is a purchasing draft — it is not posted to an ERP."
)

with st.sidebar:
    st.subheader("Assumptions")
    demand = st.slider("Demand multiplier", 0.8, 2.0, 1.0, 0.05)
    lead_delta = st.slider("Lead time delta (days)", -2, 10, 0, 1)
    lost_pct = st.slider("Lost sales (rest = delayed)", 50, 100, 100, 5)
    lost_sale_rate = lost_pct / 100.0
    st.caption("100% = every stockout is lost margin. Lower if customers wait.")

    st.subheader("World")
    if st.button("Load synthetic demo", width="stretch"):
        try:
            info = seed()
            fetch_alerts.clear()
            st.session_state.pop("last_po", None)
            st.success(f"{info['sku_count']} SKUs · {info['order_count']} orders")
        except httpx.HTTPError as exc:
            st.error(f"Seed failed: {exc}")
    cat_file = st.file_uploader("catalog.csv", type=["csv"])
    sales_file = st.file_uploader("sales.csv", type=["csv"])
    if st.button("Load your CSV", width="stretch"):
        if not cat_file or not sales_file:
            st.warning("Attach both catalog.csv and sales.csv (download the template first).")
        else:
            try:
                ingest(cat_file.getvalue().decode(), sales_file.getvalue().decode())
                fetch_alerts.clear()
                st.session_state.pop("last_po", None)
                st.success("Loaded CSV world")
            except httpx.HTTPError as exc:
                st.error(exc.response.text if exc.response is not None else str(exc))
    if st.button("Download CSV template (current world)", width="stretch"):
        try:
            dumped = export_world()
            st.session_state["template"] = dumped
        except httpx.HTTPError as exc:
            st.error(str(exc))
    if "template" in st.session_state:
        st.download_button("catalog.csv", st.session_state["template"]["catalog_csv"], "catalog.csv")
        st.download_button("sales.csv", st.session_state["template"]["sales_csv"], "sales.csv")
    st.markdown(f"`API`: {API}")

tab_warn, tab_scene = st.tabs(["Early warning", "Scenario simulator"])

try:
    payload = fetch_alerts(demand, lead_delta, lost_sale_rate)
except httpx.HTTPError as exc:
    st.error(f"Cannot reach API at {API}. {exc}")
    st.stop()

c = payload.get("operating_contract") or {}
hold = c.get("holdout") or {}
mape = hold.get("mape_48h_velocity")
naive = hold.get("mape_7d_naive")
mape_s = f"{mape:.0%}" if isinstance(mape, (int, float)) else "n/a"
naive_s = f"{naive:.0%}" if isinstance(naive, (int, float)) else "n/a"

if c.get("mode") == "synthetic":
    st.warning(
        "Synthetic demo catalog — not this company's inventory. "
        "Download the CSV template, replace rows, Load your CSV."
    )
elif c.get("mode") == "csv":
    st.info(c.get("source", "CSV world"))

st.caption(
    f"**{c.get('source', '—')}** · as of {c.get('as_of', '—')} · "
    f"last sale {c.get('last_sale_at') or '—'} · {c.get('sku_count', '?')} SKUs · "
    f"{c.get('site')} · lost-sale {c.get('lost_sale_rate', 1):.0%} · "
    f"holdout MAPE 48h {mape_s} (7d naive {naive_s}, n={hold.get('n_skus', 0)}) · "
    f"{c.get('po_channel')} · {c.get('compute_ms', '?')} ms · {c.get('scale')}"
)

totals = payload["totals"]
at_risk = payload["at_risk"]
note = payload.get("metric_note", "")

with tab_warn:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SKUs stocking out in 48h", totals["at_risk_sku_count"])
    c2.metric("At-risk contribution margin", f"${totals['at_risk_margin_usd']:,.0f}")
    c3.metric("Recoverable if expedite now", f"${totals['recoverable_margin_usd']:,.0f}")
    c4.metric("Unrecoverable (too late)", f"${totals['unrecoverable_margin_usd']:,.0f}")
    after_sub = totals.get("at_risk_margin_after_substitution_usd")
    if after_sub is not None and after_sub != totals["at_risk_margin_usd"]:
        st.caption(f"After catalog substitution: ${after_sub:,.0f} at-risk margin.")
    st.markdown(f"<p class='metric-note'>{note}</p>", unsafe_allow_html=True)
    st.caption(f"Pending outbox: {payload.get('pending_outbox', 0)}")

    if not at_risk:
        st.info("No SKU is projected to stock out in the 48h horizon under this scenario.")
    else:
        df = pd.DataFrame(at_risk)
        cols = [
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
            "margin_after_substitution_usd",
            "substitute_sku",
            "recommended_po_qty",
            "po_arrives_after_horizon",
        ]
        show = df[[c for c in cols if c in df.columns]].copy()
        if "hours_to_stockout" in show:
            show["hours_to_stockout"] = show["hours_to_stockout"].map(
                lambda x: None if x is None else round(float(x), 1)
            )
        st.dataframe(show, width="stretch", hide_index=True)

        st.subheader("Emergency PO (purchasing draft)")
        col_a, col_b, col_c = st.columns([2, 1, 1])
        skus = list(df["sku"])
        chosen = col_a.selectbox("SKU", skus)
        default_qty = int(df.loc[df["sku"] == chosen, "recommended_po_qty"].iloc[0])
        qty = col_b.number_input("Quantity", min_value=1, value=max(default_qty, 1))
        if col_c.button("Place emergency PO", type="primary"):
            try:
                result = replenish(chosen, int(qty))
                st.session_state["last_po"] = result
                fetch_alerts.clear()
                st.success(
                    f"{result['po_number']} · {result['quantity']} × {result['sku']} · "
                    f"ETA {result['expected_arrival']} · to {result.get('buyer_email')} · "
                    "not posted to ERP."
                )
                st.rerun()
            except httpx.HTTPError as exc:
                st.error(f"PO failed: {exc}")

        last = st.session_state.get("last_po")
        if last:
            if st.button("Export last PO as CSV + email"):
                try:
                    st.session_state["po_export"] = export_po(last["po_id"])
                except httpx.HTTPError as exc:
                    st.error(str(exc))
            artifact = st.session_state.get("po_export")
            if artifact:
                st.download_button(
                    "Download PO CSV",
                    artifact["csv"],
                    f"{artifact['po_number']}.csv",
                    "text/csv",
                )
                st.text_area("Email draft", artifact["email"], height=160)

    ops = payload.get("operational_alerts") or []
    if ops:
        st.subheader("Operational ROP crossings (outbox → DuckDB)")
        st.dataframe(pd.DataFrame(ops), width="stretch", hide_index=True)

with tab_scene:
    st.write(
        "Sliders change the walk only. A real PO is the sidebar of Early warning, "
        "and it is a draft for purchasing — not an ERP posting."
    )
    baseline = fetch_alerts(1.0, 0, lost_sale_rate)
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
            width="stretch",
            hide_index=True,
        )
    else:
        st.write("No additional SKUs versus baseline (the same set may still get worse).")
