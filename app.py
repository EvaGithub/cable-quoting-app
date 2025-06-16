"""app.py – RF Cable Quoting & Feasibility Tool  
=================================================
UI  
-- left‑sidebar radio navigation (Quote ▸ BOM ▸ Routing ▸ Feasibility ▸ Price)  
-- quote form in two responsive columns  
-- upload widgets only in *Master* role  

Back‑end  
-- reads Excel/YAML from ~/.streamlit/cq_data (or repo)  
-- tolerates «Length_mm» *or* «Length (mm)» column names  
-- Connector model extended to map new sheet columns:  
   • Time_per_piece_min  
   • strip level 1 / 2 flags (1_stage_stripped / 2_stage_stripped)  
   • pre‑tinned → Tin_platting == 1  
   • deburr / sharpen → Deburr_sharpen == 1  
-- cable selector now shows *all* part‑numbers that match size *or* family (whichever filter is chosen).  
"""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List

import pandas as pd
import streamlit as st
from pydantic import BaseModel
import yaml

###############################################################################
# 0 – persistence helpers
###############################################################################
DATA_DIR = Path.home() / ".streamlit" / "cq_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXCEL_META = [
    ("Cable_Data_with_Costs.xlsx", "Cables"),
    ("Connector_Data_with_Costs.xlsx", "Connectors"),
    ("Routing_Operations_Translated_3.xlsx", "Routing_Operations"),
]


def save_file(buf: bytes, name: str):
    (DATA_DIR / name).write_bytes(buf)


def load_excel(name: str, sheet: str) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        st.error(f"Missing **{name}** – upload in **Master** mode.")
        st.stop()
    return pd.read_excel(path, sheet_name=sheet)


def load_cfg() -> Dict:
    path = DATA_DIR / "config.yaml"
    if not path.exists():
        st.error("Upload **config.yaml** in **Master** mode.")
        st.stop()
    return yaml.safe_load(path.read_text())

###############################################################################
# 1 – Data models
###############################################################################

class Cable(BaseModel):
    part_number: str
    description: str
    family: str
    size: str
    stock_length_mm: int
    user_length_mm: int
    frequency_ghz: float
    min_bend_spacing_mm: int = 20


class Connector(BaseModel):
    part_number: str
    description: str
    cable_group: str
    lel_mm: int | None = None
    lol_mm: int | None = None
    needs_strip_lvl1: bool = False
    needs_strip_lvl2: bool = False
    pre_tinned: bool = False
    deburr: bool = False
    time_per_piece_min: float | None = None  # from sheet column Time_per_piece


class Assembly(BaseModel):
    cable: Cable
    conn_a: Connector
    conn_b: Connector
    quantity: int
    bends: int
    plant: str
    currency: str

    def cnc_required(self, cfg: Dict) -> bool:
        return (
            self.bends >= cfg["number_bending_CNC"]
            or self.quantity >= cfg["quantity_assemblies_CNC"]
        )

###############################################################################
# 2 – Cost helpers (logic unchanged, sheet‑column mapping extended)
###############################################################################

def choose_stock_row(rows: pd.DataFrame, user_len: int) -> pd.Series:
    cand = rows[rows["Length_mm"] >= user_len].sort_values("Length_mm")
    return cand.iloc[0] if not cand.empty else rows.sort_values("Length_mm", False).iloc[0]


def bom_cost(df_cables: pd.DataFrame, df_conns: pd.DataFrame, asm: Assembly) -> Dict[str, float]:
    plant_cols = [c for c in df_cables.columns if c.endswith(tuple(["_HS", "_PL", "_DE", "_US", "_ML", "_UK"]))]
    cable_row = choose_stock_row(df_cables[df_cables.Part_number == asm.cable.part_number], asm.cable.user_length_mm)
    conn_rows = df_conns[df_conns.Part_number.isin([asm.conn_a.part_number, asm.conn_b.part_number])]
    return {p: float(cable_row[p]) + conn_rows[p].astype(float).sum() for p in plant_cols}


def routing_cost(df_ops: pd.DataFrame, asm: Assembly, cfg: Dict) -> Dict[str, float]:
    plants = cfg["hourly_rate_per_plant"].keys(); cost = {p: 0.0 for p in plants}

    def add(op):
        row = df_ops.loc[df_ops.Operation_ID == op].iloc[0]
        for p in plants:
            rate = cfg["hourly_rate_per_plant"][p] / 60
            cost[p] += ((row.Setup_min / asm.quantity) + row.Time_per_unit_min) * rate * asm.quantity

    # assembly‑level ops
    add("OP01"); add("OP02")

    # connector‑level ops
    for c in [asm.conn_a, asm.conn_b]:
        if c.needs_strip_lvl1: add("OP03")
        if c.needs_strip_lvl2: add("OP04")
        if c.deburr:          add("OP05")
        if not c.pre_tinned:  add("OP06")

    add("OP10" if asm.cnc_required(cfg) else "OP11")  # bending
    add("OP12"); add("OP13"); add("OP14" if asm.bends == 0 else "OP15")
    return cost


def convert(vals: Dict[str, float], cfg: Dict, cur: str) -> Dict[str, float]:
    r = cfg["exchange_rates"].get(cur, 1.0)
    return {k: v * r for k, v in vals.items()}

###############################################################################
# 3 – Feasibility checks (unchanged)
###############################################################################

def feasibility(asm: Assembly, cfg: Dict):
    res: List[Dict] = []
    def push(rule, ok, msg): res.append({"Rule": rule, "Status": "PASS" if ok else "FAIL", "Msg": msg})

    defaults = cfg["default_straight_segments_mm"].get(asm.cable.size, {"LEL": 0, "LOL": 0})
    min_lel = max(asm.conn_a.lel_mm or 0, asm.conn_b.lel_mm or 0, cfg["min_straight_len_default_mm"], defaults["LEL"])
    min_lol = max(asm.conn_a.lol_mm or 0, asm.conn_b.lol_mm or 0, defaults["LOL"])
    push("F1", True, f"Need ≥{min_lel} mm LEL and ≥{min_lol} mm LOL")

    ok_spacing = (asm.bends == 0) or ((asm.cable.user_length_mm / max(asm.bends, 1)) >= asm.cable.min_bend_spacing_mm)
    push("F2", ok_spacing, "Bend spacing OK" if ok_spacing else "Spacing too tight")

    fam_max = 40.0 if "086" in asm.cable.size else 18.0
    push("F3", asm.cable.frequency_ghz <= fam_max, "Freq within limit" if asm.cable.frequency_ghz <= fam_max else "Freq too high")

    cnc_ok = True if not asm.cnc_required(cfg) else (asm.bends <= 20 and asm.cable.user_length_mm <= 2000)
    push("F4", cnc_ok, "CNC OK" if cnc_ok else "CNC limits exceeded for manual")
    return res

###############################################################################
# 4 – Streamlit UI
###############################################################################

def main():
    st.set_page_config(page_title="Cable Quoter", layout="wide")
    st.title("🪢 RF Cable Quoting & Feasibility Tool")

    role = st.sidebar.selectbox("Role", ["Standard", "Master"], key="role")

    # ------------------------------------------------------ Master uploads
    if role == "Master":
        st.sidebar.header("📥 Upload data files")
        for fname, _ in EXCEL_META + [("config.yaml", None)]:
            f = st.sidebar.file_uploader(fname, key=fname)
            if f: save_file(f.getbuffer(), fname); st.sidebar.success(f"Saved {fname}")
        st.info("Switch to **Standard** to use the app."); st.stop()

    # ------------------------------------------------------ Load data once
    cfg = load_cfg()
    df_cables = load_excel(*EXCEL_META[0])
    df_conns  = load_excel(*EXCEL_META[1])
    df_ops    = load_excel(*EXCEL_META[2])

    nav = st.sidebar.radio("Sections", ["Quote", "BOM", "Routing", "Feasibility", "Price"], index=0)
    c1, c2 = st.columns(2)

    # ------------------------------------------------------ Quote input page
    if nav == "Quote":
        with c1:
            st.header("Quote parameters")
            plant    = st.selectbox("Plant", list(cfg["hourly_rate_per_plant"].keys()))
            currency = st.selectbox("Currency", list(cfg["exchange_rates"].keys()))
            size     = st.selectbox("Cable size", sorted(df_cables.Size.unique()))
            fam_opts = df_cables[df_cables.Size == size].Family.unique()
            family   = st.selectbox("Family", fam_opts)
            user_len = st.number_input("Length (mm)", 50, 10000, 500)
            cable_opts = df_cables[df_cables.Size == size].Part_number.unique()
            cable_pn = st.selectbox("Cable PN", cable_opts)
        with c2:
            st.header("Connectors & Qty")
            group = family
            con_opts = df_conns[df_conns.Cable_group == group].Part_number.unique()
            conn_a = st.selectbox("Connector A", con_opts)
            conn_b = st.selectbox("Connector B", con_opts, index=len(con_opts)-1 if len(con_opts)>1 else 0)
            qty   = st.number_input("Quantity", 1, 1000, 10)
            bends = st.number_input("Number of bends", 0, 30, 0)
            calc  = st.button("Calculate", type="primary")

        if not calc: st.stop()

        crow = df_cables[df_cables.Part_number == cable_pn].iloc
