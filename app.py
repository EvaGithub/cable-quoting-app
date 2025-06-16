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
    # supports dynamic columns via dict

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
    # supports dynamic columns

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

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 2. BOM & Routing Cost

def bom_cost(df_c: pd.DataFrame, df_co: pd.DataFrame, asm: Assembly) -> pd.DataFrame:
    cab_row = df_c[df_c.Part_number == asm.cable.Part_number].iloc[0]
    a_row = df_co[df_co.Part_number == asm.conn_a.Part_number].iloc[0]
    b_row = df_co[df_co.Part_number == asm.conn_b.Part_number].iloc[0]
    items = [cab_row, a_row, b_row]
    rows = []
    for comp in items:
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


def routing_cost(df_op: pd.DataFrame, asm: Assembly, cfg: Dict) -> pd.DataFrame:
    df = df_op.copy()
    df['Qty'] = asm.quantity
    rate = cfg['hourly_rate'][asm.plant] / 60
    df['RoutingCost'] = df['Setup_time_min'] * rate + df['Time_per_piece_min'] * asm.quantity * rate
    return df[['WorkCenter','Description','Qty','RoutingCost']]

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 3. Feasibility Rules

def feasibility_checks(asm: Assembly, cfg: Dict) -> pd.DataFrame:
    tests = []
    tests.append({"Check":"Length<=Stock","Result": asm.length_mm <= asm.cable.Stock_length_mm})
    if asm.bends>0:
        ok = all(d >= asm.cable.Min_bend_spacing_mm for d in asm.bend_distances)
        tests.append({"Check":"MinBendSpacing","Result": ok})
    else:
        tests.append({"Check":"NoBends","Result": True})
    tests.append({"Check":"CNCpossible","Result": asm.cnc_required(cfg)})
    return pd.DataFrame(tests)

# –––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 4. Streamlit UI

def main():
    st.set_page_config(page_title="RF Cable Quoter", layout="wide")
    cfg = load_cfg()

    st.sidebar.title("📁 Upload & Settings")
    for fname, sheet in EXCEL_META:
        up = st.sidebar.file_uploader(fname, type=fname.split('.')[-1], key=fname)
        if up: save_file(up.read(), fname)
    master = st.sidebar.checkbox("Master mode: edit rates & stop")
    if master:
        st.sidebar.subheader("⏱️ Hourly Rates")
        for k in cfg['hourly_rate']:
            cfg['hourly_rate'][k] = st.sidebar.number_input(k, value=cfg['hourly_rate'][k])
        st.sidebar.subheader("💱 Exchange")
        for k in cfg['exchange']:
            cfg['exchange'][k] = st.sidebar.number_input(k, value=cfg['exchange'][k])
        st.sidebar.download_button("Save config.yaml", yaml.safe_dump(cfg), file_name="config.yaml")
        st.stop()

    df_c = load_excel(*EXCEL_META[0])
    df_co = load_excel(*EXCEL_META[1])
    df_op = load_excel(*EXCEL_META[2])

    st.title("🔌 RF Cable Quoting & Feasibility Tool")
    tabs = st.tabs(["Quote Input","BOM","Routing","Feasibility","Summary"])

    with tabs[0]:
        col1,col2,col3 = st.columns([1,1,1])
        plant = col1.selectbox("Plant", list(cfg['hourly_rate'].keys()))
        currency = col1.selectbox("Currency", list(cfg['exchange'].keys()))
        size = col1.selectbox("Cable Size", sorted(df_c.Size.unique()))
        fams = df_c[df_c.Size==size].Family.unique()
        family = col2.selectbox("Family", sorted(fams))
        cables = df_c[(df_c.Size==size)&(df_c.Family==family)].Part_number
        cable = col2.selectbox("Cable PN", sorted(cables))
        group = df_c[df_c.Part_number==cable].Cable_group.iloc[0]
        conns = df_co[df_co.Cable_group==group].Part_number
        conn_a = col3.selectbox("Connector A", sorted(conns))
        conn_b = col3.selectbox("Connector B", sorted(conns))
        length = col1.number_input("Length (mm)", min_value=1, value=100)
        qty = col1.number_input("Quantity", min_value=1, value=1)
        bends = col1.number_input("# Bends", min_value=0, value=0)
        if bends>0:
            bd = col2.text_input("Bend Distances (comma)")
            ad = col2.text_input("Bend Angles (comma)")
        else:
            bd,ad="",""
        run = st.button("Calculate")

    if run:
        bd_list = [int(x) for x in bd.split(',') if x.strip().isdigit()]
        ad_list = [int(x) for x in ad.split(',') if x.strip().isdigit()]
        cab = Cable(**df_c[df_c.Part_number==cable].iloc[0].to_dict())
        ca = Connector(**df_co[df_co.Part_number==conn_a].iloc[0].to_dict())
        cb = Connector(**df_co[df_co.Part_number==conn_b].iloc[0].to_dict())
        asm = Assembly(cable=cab, conn_a=ca, conn_b=cb,
                       length_mm=length, quantity=int(qty), bends=int(bends),
                       bend_distances=bd_list, bend_angles=ad_list,
                       plant=plant, currency=currency)
        with tabs[1]: st.dataframe(bom_cost(df_c, df_co, asm), use_container_width=True)
        with tabs[2]: st.dataframe(routing_cost(df_op, asm, cfg), use_container_width=True)
        with tabs[3]: st.dataframe(feasibility_checks(asm, cfg), use_container_width=True)
        with tabs[4]:
            m = bom_cost(df_c, df_co, asm)['TotalCost'].sum()
            r = routing_cost(df_op, asm, cfg)['RoutingCost'].sum()
            st.metric("Material", f"{m:.2f} {currency}")
            st.metric("Routing", f"{r:.2f} {currency}")
            st.metric("Total", f"{m+r:.2f} {currency}")

if __name__=='__main__':
    main()
```
