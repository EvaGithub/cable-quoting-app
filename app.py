```python
# Updated app.py – RF Cable Quoting & Feasibility Tool
# Complete code with parts 1 (I/O), 2 (BOM & routing cost), 3 (feasibility), 4 (Streamlit UI)

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st
from pydantic import BaseModel
import yaml

# 0 – Persistence & I/O Helpers
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

# 1 – Data Models
class Cable(BaseModel):
    part_number: str
    description: str
    family: str
    size: str
    stock_length_mm: int
    user_length_mm: int
    frequency_ghz: float
    cable_group: str
    conductor_type: Optional[str]
    plating_type: Optional[str]
    dielectric: Optional[str]
    min_bend_spacing_mm: int

class Connector(BaseModel):
    part_number: str
    description: str
    cable_group: str
    lel_mm: Optional[float]
    lol_mm: Optional[float]
    needs_strip_lvl1: bool
    needs_strip_lvl2: bool
    pre_tinned: bool
    deburr: bool
    time_per_piece_min: float

class Assembly(BaseModel):
    cable: Cable
    conn_a: Connector
    conn_b: Connector
    quantity: int
    bends: int
    bend_distances: List[int]
    bend_angles: List[int]
    plant: str
    currency: str

    def cnc_required(self, cfg: Dict) -> bool:
        return (
            self.bends >= cfg.get("number_bending_CNC", 1)
            and self.quantity >= cfg.get("quantity_assemblies_CNC", 10)
        )

# 2 – Cost & BOM Logic

def bom_cost(df_c: pd.DataFrame, df_co: pd.DataFrame, asm: Assembly) -> pd.DataFrame:
    cab = df_c.loc[df_c.Part_number == asm.cable.part_number].iloc[0]
    ca = df_co.loc[df_co.Part_number == asm.conn_a.part_number].iloc[0]
    cb = df_co.loc[df_co.Part_number == asm.conn_b.part_number].iloc[0]
    rows = []
    for comp in (cab, ca, cb):
        price_col = f"Cost_{asm.plant}" if f"Cost_{asm.plant}" in comp.index else "Price"
        cost = comp[price_col]
        rows.append({
            "PartNumber": comp.Part_number,
            "Qty": asm.quantity,
            "UnitCost": cost,
            "TotalCost": cost * asm.quantity,
        })
    return pd.DataFrame(rows)


def routing_cost(df_op: pd.DataFrame, asm: Assembly, cfg: Dict) -> pd.DataFrame:
    df = df_op.copy()
    # include mandatory steps and conditional ones
    df['Qty'] = asm.quantity
    df['Cost'] = (df['Setup_time_min'] / asm.quantity + df['Time_per_piece_min'] * asm.quantity) \
                 * (cfg.get('hourly_rate')[asm.plant] / 60)
    return df[['WorkCenter', 'Description', 'Qty', 'Cost']]

# 3 – Feasibility Rules

def feasibility_checks(asm: Assembly, cfg: Dict) -> pd.DataFrame:
    tests = []
    tests.append({"Test":"Length ≤ stock", "Result": asm.user_length_mm <= asm.cable.stock_length_mm})
    tests.append({"Test":"Min bends spacing", "Result": all(dist >= asm.cable.min_bend_spacing_mm for dist in asm.bend_distances)})
    tests.append({"Test":"CNC possible", "Result": asm.cnc_required(cfg)})
    return pd.DataFrame(tests)

# 4 – Streamlit UI

def main():
    st.set_page_config(layout='wide')
    st.sidebar.title("RF Cable Quoting")
    cfg = load_cfg()
    for fname, sheet in EXCEL_META:
        uploaded = st.sidebar.file_uploader(f"Upload {fname}", type=fname.split('.')[-1], key=fname)
        if uploaded:
            save_file(uploaded.read(), fname)
    if st.sidebar.checkbox("Master mode"):
        st.sidebar.subheader("Edit rates & exchange")
        cfg['hourly_rate'] = {p: st.sidebar.number_input(p, value=v) for p, v in cfg['hourly_rate'].items()}
        cfg['exchange'] = {c: st.sidebar.number_input(c, value=r) for c, r in cfg['exchange'].items()}
        st.sidebar.download_button("Save config", data=yaml.safe_dump(cfg), file_name="config.yaml")
        st.stop()

    df_c = load_excel(*EXCEL_META[0])
    df_co = load_excel(*EXCEL_META[1])
    df_op = load_excel(*EXCEL_META[2])

    # Input form
    with st.form('quote'):
        col1, col2 = st.columns(2)
        plant = col1.selectbox("Plant", cfg['hourly_rate'].keys())
        currency = col1.selectbox("Currency", cfg['exchange'].keys())
        size = col1.selectbox("Cable Size", sorted(df_c.Size.unique()))
        family = col1.selectbox("Cable Family", sorted(df_c[df_c.Size==size].Family.unique()))
        cable_pn = col1.selectbox("Cable PN", sorted(df_c[(df_c.Size==size)&(df_c.Family==family)].Part_number.unique()))
        conn_a = col2.selectbox("Connector A", sorted(df_co[df_co.Cable_group==df_c.loc[df_c.Part_number==cable_pn,'Cable_group'].iloc[0]].Part_number.unique()))
        conn_b = col2.selectbox("Connector B", sorted(df_co[df_co.Cable_group==df_c.loc[df_c.Part_number==cable_pn,'Cable_group'].iloc[0]].Part_number.unique()))
        length = st.slider("Length (mm)", 0, 2000, 100)
        qty = st.number_input("Quantity", min_value=1, value=1)
        bends = st.number_input("Number of bends", min_value=0, value=0)
        dist = st.text_input("Bend distances (comma)") if bends>0 else ""
        ang = st.text_input("Bend angles (comma)") if bends>0 else ""
        submitted = st.form_submit_button("Calculate")

    if submitted:
        asm = build_assembly(cfg, df_c, df_co, plant, currency, size, family,
                             cable_pn, conn_a, conn_b, length, qty, bends, dist, ang)
        st.header("Results")
        tabs = st.tabs(["BOM","Routing","Feasibility","Price"])
        with tabs[0]: st.dataframe(bom_cost(df_c, df_co, asm))
        with tabs[1]: st.dataframe(routing_cost(df_op, asm, cfg))
        with tabs[2]: st.dataframe(feasibility_checks(asm, cfg))
        with tabs[3]:
            mat = bom_cost(df_c, df_co, asm)['TotalCost'].sum()
            rout = routing_cost(df_op, asm, cfg)['Cost'].sum()
            total = mat + rout
            st.metric("Total Cost", f"{total:.2f} {currency}")

if __name__ == "__main__":
    main()
```
