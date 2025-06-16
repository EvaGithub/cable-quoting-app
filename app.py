```python
# app.py – RF Cable Quoting & Feasibility Tool
# Consolidated parts 1–4: I/O, Data Models, BOM & routing logic, feasibility checks & Streamlit UI
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
from pydantic import BaseModel
import yaml

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 0. Persistence & I/O Helpers
DATA_DIR = Path.home() / ".streamlit" / "cq_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXCEL_META = [
    ("Cable_Data_with_Costs.xlsx", "Cables"),
    ("Connector_Data_with_Costs.xlsx", "Connectors"),
    ("Routing_Operations_Translated_3.xlsx", "Routing_Operations"),
]

def save_file(buf: bytes, name: str):
    (DATA_DIR / name).write_bytes(buf)


def load_excel(fname: str, sheet: str) -> pd.DataFrame:
    p = DATA_DIR / fname
    if not p.exists():
        st.error(f"Missing **{fname}** – upload in Master mode.")
        st.stop()
    return pd.read_excel(p, sheet_name=sheet)


def load_cfg() -> Dict:
    p = DATA_DIR / "config.yaml"
    if not p.exists():
        st.error("Upload **config.yaml** in Master mode.")
        st.stop()
    return yaml.safe_load(p.read_text())

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 1. Data Models
class Cable(BaseModel):
    Part_number: str
    Description: str
    Family: str
    Size: str
    Stock_length_mm: int
    Frequency_GHz: Optional[float]
    Cable_group: str
    Min_bend_spacing_mm: Optional[int]
    # additional columns loaded dynamically

class Connector(BaseModel):
    Part_number: str
    Description: str
    Cable_group: str
    LEL_mm: Optional[float]
    LOL_mm: Optional[float]
    Needs_strip_lvl1: bool
    Needs_strip_lvl2: bool
    Pre_tinned: bool
    Deburr: bool
    Time_per_piece_min: Optional[float]
    # additional columns loaded dynamically

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
            self.bends >= cfg.get("bends_for_cnc", 1)
            and self.quantity >= cfg.get("qty_for_cnc", 1)
        )

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 2. BOM & Routing Cost

def bom_cost(df_c: pd.DataFrame, df_co: pd.DataFrame, asm: Assembly) -> pd.DataFrame:
    # lookup cable + connectors costs
    cab_row = df_c[df_c.Part_number == asm.cable.Part_number].iloc[0]
    a_row = df_co[df_co.Part_number == asm.conn_a.Part_number].iloc[0]
    b_row = df_co[df_co.Part_number == asm.conn_b.Part_number].iloc[0]
    rows = []
    for comp in [cab_row, a_row, b_row]:
        cost_col = f"Cost_{asm.plant}" if f"Cost_{asm.plant}" in comp.index else "Cost"
        unit_cost = comp[cost_col]
        rows.append({
            "Part": comp.Part_number,
            "Qty": asm.quantity,
            "UnitCost": unit_cost,
            "TotalCost": unit_cost * asm.quantity,
        })
    return pd.DataFrame(rows)


def routing_cost(df_op: pd.DataFrame, asm: Assembly, cfg: Dict) -> pd.DataFrame:
    df = df_op.copy()
    df['Qty'] = asm.quantity
    rate = cfg['hourly_rate'][asm.plant] / 60  # per-minute rate
    df['TotalCost'] = df['Setup_time_min'] * rate + df['Time_per_piece_min'] * asm.quantity * rate
    return df[['WorkCenter','Description','Qty','TotalCost']]

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 3. Feasibility Rules

def feasibility_checks(asm: Assembly, cfg: Dict) -> pd.DataFrame:
    tests = []
    tests.append({"Check":"Length <= stock","Result": asm.length_mm <= asm.cable.Stock_length_mm})
    if asm.bends>0:
        ok_spacings = all(d >= asm.cable.Min_bend_spacing_mm for d in asm.bend_distances)
        tests.append({"Check":"Min bend spacing","Result": ok_spacings})
    else:
        tests.append({"Check":"No bends","Result": True})
    tests.append({"Check":"CNC possible","Result": asm.cnc_required(cfg)})
    return pd.DataFrame(tests)

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 4. Streamlit UI

def main():
    st.set_page_config(page_title="RF Cable Quoter", layout="wide")
    cfg = load_cfg()

    # Sidebar file uploads
    st.sidebar.title("📁 Data Uploads & Settings")
    for fname, sheet in EXCEL_META:
        uploaded = st.sidebar.file_uploader(f"{fname}", type=fname.split('.')[-1], key=fname)
        if uploaded:
            save_file(uploaded.read(), fname)
    master = st.sidebar.checkbox("Master mode: edit rates/exchange & reload")
    if master:
        st.sidebar.subheader("⏱️ Hourly Rates (per hour)")
        for k,v in cfg['hourly_rate'].items():
            cfg['hourly_rate'][k] = st.sidebar.number_input(k, value=v)
        st.sidebar.subheader("💱 Currency Exchange")
        for k,v in cfg['exchange'].items():
            cfg['exchange'][k] = st.sidebar.number_input(k, value=v)
        st.sidebar.download_button("Save config.yaml", yaml.safe_dump(cfg), file_name="config.yaml")
        st.stop()

    # Load dataframes
    df_c = load_excel(*EXCEL_META[0])
    df_co = load_excel(*EXCEL_META[1])
    df_op = load_excel(*EXCEL_META[2])

    st.title("🔌 RF Cable Quoting & Feasibility Tool")
    tabs = st.tabs(["Quote Input","BOM","Routing","Feasibility","Price Summary"])

    with tabs[0]:
        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        plant = col1.selectbox("Plant", list(cfg['hourly_rate'].keys()))
        currency = col1.selectbox("Currency", list(cfg['exchange'].keys()))
        size = col2.selectbox("Cable Size", sorted(df_c.Size.unique()))
        families = sorted(df_c[df_c.Size==size].Family.unique())
        family = col2.selectbox("Family", families)
        cables = sorted(df_c[(df_c.Size==size)&(df_c.Family==family)].Part_number.unique())
        cable_pn = col2.selectbox("Cable PN", cables)
        groups = df_c[df_c.Part_number==cable_pn].Cable_group.unique()
        conn_opts = df_co[df_co.Cable_group.isin(groups)].Part_number.unique()
        conn_a = col3.selectbox("Connector A", sorted(conn_opts))
        conn_b = col3.selectbox("Connector B", sorted(conn_opts))
        length = col1.number_input("Length (mm)", min_value=1, value=100)
        qty = col1.number_input("Quantity", min_value=1, value=1)
        bends = col1.number_input("# Bends", min_value=0, value=0)
        if bends>0:
            dists = st.text_input("Bend Distances (comma)", "").split(',')
            angs = st.text_input("Bend Angles (comma)", "").split(',')
        else:
            dists, angs = [], []
        calc = st.button("Calculate")

    if calc:
        # build models
        cab = Cable(**df_c[df_c.Part_number==cable_pn].iloc[0].to_dict())
        ca = Connector(**df_co[df_co.Part_number==conn_a].iloc[0].to_dict())
        cb = Connector(**df_co[df_co.Part_number==conn_b].iloc[0].to_dict())
        bd = [int(x) for x in dists if x.strip().isdigit()]
        ba = [int(x) for x in angs if x.strip().isdigit()]
        asm = Assembly(cable=cab, conn_a=ca, conn_b=cb,
                       length_mm=length, quantity=int(qty), bends=int(bends),
                       bend_distances=bd, bend_angles=ba,
                       plant=plant, currency=currency)

        # render results
        with tabs[1]:
            st.subheader("Bill of Materials")
            st.dataframe(bom_cost(df_c, df_co, asm), use_container_width=True)
        with tabs[2]:
            st.subheader("Routing Cost")
            st.dataframe(routing_cost(df_op, asm, cfg), use_container_width=True)
        with tabs[3]:
            st.subheader("Feasibility Checks")
            st.dataframe(feasibility_checks(asm, cfg), use_container_width=True)
        with tabs[4]:
            mat = bom_cost(df_c, df_co, asm)['TotalCost'].sum()
            rout = routing_cost(df_op, asm, cfg)['TotalCost'].sum()
            st.metric("Material Cost", f"{mat:.2f} {currency}")
            st.metric("Routing Cost", f"{rout:.2f} {currency}")
            st.metric("Total Cost", f"{(mat+rout):.2f} {currency}")

if __name__ == '__main__':
    main()
```
