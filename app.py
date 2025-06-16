```python
# app.py – RF Cable Quoting & Feasibility Tool
# Consolidated parts 1–4 with improved interactivity and layout
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
from pydantic import BaseModel
import yaml

# 0. Persistence & I/O Helpers
DATA_DIR = Path.home() / ".streamlit" / "cq_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXCEL_SHEETS = {
    "Cables": "Cable_Data_with_Costs.xlsx",
    "Connectors": "Connector_Data_with_Costs.xlsx",
    "Routing": "Routing_Operations_Translated_3.xlsx",
}


def save_uploaded_file(content: bytes, fname: str):
    (DATA_DIR / fname).write_bytes(content)


def load_sheet(sheet_name: str) -> pd.DataFrame:
    fname = EXCEL_SHEETS[sheet_name]
    p = DATA_DIR / fname
    if not p.exists():
        st.error(f"Missing **{fname}** – upload in Master mode.")
        st.stop()
    return pd.read_excel(p, sheet_name=sheet_name)


def load_config() -> Dict:
    p = DATA_DIR / "config.yaml"
    if not p.exists():
        st.error("Upload **config.yaml** in Master mode.")
        st.stop()
    return yaml.safe_load(p.read_text())

# 1. Data Models
class Cable(BaseModel):
    Part_number: str
    Description: str
    Family: str
    Size: str
    Stock_length_mm: int
    Min_bend_spacing_mm: Optional[int]
    Cable_group: str
    # dynamic cost columns remain in raw df

class Connector(BaseModel):
    Part_number: str
    Description: str
    Cable_group: str
    Needs_strip_lvl1: bool
    Needs_strip_lvl2: bool
    Pre_tinned: bool
    Deburr: bool
    Time_per_piece_min: Optional[float]

class Assembly(BaseModel):
    cable: Cable
    conn_a: Connector
    conn_b: Connector
    length_mm: int
    quantity: int
    bends: int
    bend_distances: List[int]
    bend_angles: List[int]
    plant: str
    currency: str

    def cnc_required(self, cfg: Dict) -> bool:
        return (
            self.bends >= cfg.get("bends_for_cnc", 2)
            and self.quantity >= cfg.get("qty_for_cnc", 1)
        )

# 2. Cost Calculations
def bom_cost(df_c: pd.DataFrame, df_co: pd.DataFrame, asm: Assembly) -> pd.DataFrame:
    cab = df_c.loc[df_c.Part_number == asm.cable.Part_number].iloc[0]
    a = df_co.loc[df_co.Part_number == asm.conn_a.Part_number].iloc[0]
    b = df_co.loc[df_co.Part_number == asm.conn_b.Part_number].iloc[0]
    rows = []
    for comp in [cab, a, b]:
        cost_col = f"Cost_{asm.plant}" if f"Cost_{asm.plant}" in comp.index else "Cost"
        uc = comp[cost_col]
        rows.append({
            "Part": comp.Part_number,
            "Desc": comp.Description,
            "Qty": asm.quantity,
            "UnitCost": uc,
            "TotalCost": uc * asm.quantity,
        })
    return pd.DataFrame(rows)


def routing_cost(df_r: pd.DataFrame, asm: Assembly, cfg: Dict) -> pd.DataFrame:
    df = df_r.copy()
    rate_per_min = cfg['hourly_rate'][asm.plant] / 60
    df['RoutingCost'] = df['Setup_time_min'] * rate_per_min + df['Time_per_piece_min'] * asm.quantity * rate_per_min
    return df[['WorkCenter', 'Description', 'RoutingCost']]

# 3. Feasibility

def feasibility_checks(asm: Assembly, cfg: Dict) -> pd.DataFrame:
    results = []
    results.append({"Check": "Length<=Stock", "Result": asm.length_mm <= asm.cable.Stock_length_mm})
    if asm.bends > 0:
        ok = all(d >= asm.cable.Min_bend_spacing_mm for d in asm.bend_distances)
        results.append({"Check": "MinBendSpacing", "Result": ok})
    else:
        results.append({"Check": "NoBends", "Result": True})
    results.append({"Check": "CNCpossible", "Result": asm.cnc_required(cfg)})
    return pd.DataFrame(results)

# 4. Streamlit UI

def main():
    st.set_page_config(page_title="RF Cable Quoter", layout="wide")
    cfg = load_config()

    # --- Sidebar ---
    st.sidebar.title("🔧 Configuration & Upload")
    for sheet, fname in EXCEL_SHEETS.items():
        up = st.sidebar.file_uploader(f"Upload {fname}", type=fname.split('.')[-1], key=sheet)
        if up:
            save_uploaded_file(up.read(), fname)
    master = st.sidebar.checkbox("Master mode (edit rates)")
    if master:
        st.sidebar.subheader("⚙️ Hourly Rates (CHF/h)")
        for plant in cfg['hourly_rate']:
            cfg['hourly_rate'][plant] = st.sidebar.number_input(plant, value=cfg['hourly_rate'][plant], format="%.1f")
        st.sidebar.subheader("💱 Exchange Rates")
        for currency in cfg['exchange']:
            cfg['exchange'][currency] = st.sidebar.number_input(currency, value=cfg['exchange'][currency], format="%.3f")
        st.sidebar.download_button("Save config.yaml", yaml.safe_dump(cfg), file_name="config.yaml")
        st.stop()

    # Load data
    df_c = load_sheet('Cables')
    df_co = load_sheet('Connectors')
    df_r = load_sheet('Routing')

    # Page selector in sidebar
    page = st.sidebar.radio("📄 Page", ["Quote Input","BOM","Routing","Feasibility","Summary"] )

    st.title("🔌 RF Cable Quoting & Feasibility Tool")

    # --- Quote Input ---
    if page == "Quote Input":
        with st.form("quote_form"):
            col1, col2, col3 = st.columns(3)
            plant = col1.selectbox("Plant", list(cfg['hourly_rate'].keys()))
            currency = col1.selectbox("Currency", list(cfg['exchange'].keys()), index=list(cfg['exchange']).index(currency if 'currency' in locals() else list(cfg['exchange'])[0]))

            size = col2.selectbox("Cable Size", sorted(df_c.Size.unique()), key="size_select")
            families = sorted(df_c[df_c.Size == size].Family.unique())
            family = col2.selectbox("Family", families, key=f"family_{size}")
            cables = sorted(df_c[(df_c.Size == size) & (df_c.Family == family)].Part_number)
            cable_pn = col2.selectbox("Cable PN", cables, key=f"cable_{size}_{family}")

            group = df_c.loc[df_c.Part_number == cable_pn, 'Cable_group'].iloc[0]
            connectors = sorted(df_co[df_co.Cable_group == group].Part_number)
            conn_a = col3.selectbox("Connector A", connectors, key=f"ca_{group}")
            conn_b = col3.selectbox("Connector B", connectors, key=f"cb_{group}")

            length = col1.number_input("Length (mm)", min_value=1, value=100)
            qty = col1.number_input("Quantity", min_value=1, value=1)
            bends = col1.number_input("# Bends", min_value=0, value=0)
            bend_distances = []
            bend_angles = []
            if bends > 0:
                bd = col2.text_input("Bend Distances (comma-separated)")
                ad = col2.text_input("Bend Angles (comma-separated)")
                bend_distances = [int(x) for x in bd.split(',') if x.strip().isdigit()]
                bend_angles = [int(x) for x in ad.split(',') if x.strip().isdigit()]

            submitted = st.form_submit_button("Calculate")

        if not submitted:
            st.info("Fill out the form and click Calculate.")
            return

        # Build assembly object
        cab = Cable(**df_c.loc[df_c.Part_number == cable_pn].iloc[0].to_dict())
        ca = Connector(**df_co.loc[df_co.Part_number == conn_a].iloc[0].to_dict())
        cb = Connector(**df_co.loc[df_co.Part_number == conn_b].iloc[0].to_dict())
        asm = Assembly(
            cable=cab, conn_a=ca, conn_b=cb,
            length_mm=length, quantity=int(qty), bends=int(bends),
            bend_distances=bend_distances, bend_angles=bend_angles,
            plant=plant, currency=currency
        )

        st.success("Inputs captured – navigate to other pages.")
        # store in session
        st.session_state.asm = asm

    # Retrieve assembly
    asm: Assembly = st.session_state.get('asm', None)
    if not asm and page != "Quote Input":
        st.warning("Please complete Quote Input first.")
        return

    # --- BOM ---
    if page == "BOM":
        df_bom = bom_cost(df_c, df_co, asm)
        st.dataframe(df_bom, use_container_width=True)

    # --- Routing ---
    if page == "Routing":
        df_route = routing_cost(df_r, asm, cfg)
        st.dataframe(df_route, use_container_width=True)

    # --- Feasibility ---
    if page == "Feasibility":
        df_check = feasibility_checks(asm, cfg)
        st.dataframe(df_check, use_container_width=True)

    # --- Summary ---
    if page == "Summary":
        mat = bom_cost(df_c, df_co, asm)['TotalCost'].sum()
        rout = routing_cost(df_r, asm, cfg)['RoutingCost'].sum()
        st.metric("Material Cost", f"{mat:.2f} {asm.currency}")
        st.metric("Routing Cost", f"{rout:.2f} {asm.currency}")
        st.metric("Total Cost", f"{mat + rout:.2f} {asm.currency}")

if __name__ == "__main__":
    main()
```
