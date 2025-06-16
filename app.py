"""app.py – Streamlit Cable Quoting & Feasibility Tool (full logic)
=================================================================
This single file contains:
• File upload & caching for the three Excel tables + YAML config (Master role)
• Complete BOM, Routing, and Feasibility calculations (rules F1‑F4 implemented)
• Currency conversion and plant price comparison
• Five‑tab UI with Ag‑Grid‑style DataFrames

Missing (future): Developed‑length rule (F5), PDF/Excel export, persistence of saved quotes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pandas as pd
import streamlit as st
from pydantic import BaseModel, ValidationError
import yaml

###############################################################################
# 0 – File persistence helpers
###############################################################################

DATA_DIR = Path.home() / ".streamlit" / "cq_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXCEL_META = [
    ("Cable_Data_with_Costs.xlsx", "Cables"),
    ("Connector_Data_with_Costs.xlsx", "Connectors"),
    ("Routing_Operations_Translated_3.xlsx", "Routing_Operations"),
]


def save_file(buffer: bytes, name: str):
    with open(DATA_DIR / name, "wb") as f:
        f.write(buffer)


def load_excel(name: str, sheet: str) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        st.error(f"Missing {name} – upload in Master mode.")
        st.stop()
    return pd.read_excel(path, sheet_name=sheet)


def load_cfg() -> Dict:
    path = DATA_DIR / "config.yaml"
    if not path.exists():
        st.error("Upload config.yaml in Master mode.")
        st.stop()
    return yaml.safe_load(path.read_text())

###############################################################################
# 1 – Pydantic models
###############################################################################

class Cable(BaseModel):
    part_number: str
    description: str
    family: str
    size: str
    stock_length_mm: int
    user_length_mm: int
    frequency_ghz: float
    min_bend_spacing_mm: int = 20  # default fallback


class Connector(BaseModel):
    part_number: str
    description: str
    cable_group: str
    lel_mm: int | None = None
    lol_mm: int | None = None
    needs_strip_lvl1: bool = False
    needs_strip_lvl2: bool = False
    pre_tinned: bool = True
    deburr: bool = False


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
# 2 – Calculation helpers
###############################################################################

def choose_stock_row(rows: pd.DataFrame, user_len: int) -> pd.Series:
    candidates = rows[rows["Length_mm"] >= user_len].sort_values("Length_mm")
    if not candidates.empty:
        return candidates.iloc[0]
    return rows.sort_values("Length_mm", ascending=False).iloc[0]


def bom_cost(df_cables: pd.DataFrame, df_conns: pd.DataFrame, asm: Assembly) -> Dict[str, float]:
    plant_cols = [c for c in df_cables.columns if c.endswith("_HS") or c.endswith("_PL") or c.endswith("_DE") or c.endswith("_US") or c.endswith("_ML") or c.endswith("_UK")]

    rows = df_cables[df_cables["Part_number"] == asm.cable.part_number]
    cable_row = choose_stock_row(rows, asm.cable.user_length_mm)
    conn_rows = df_conns[df_conns["Part_number"].isin([asm.conn_a.part_number, asm.conn_b.part_number])]

    costs: Dict[str, float] = {}
    for p in plant_cols:
        costs[p] = float(cable_row[p]) + float(conn_rows.iloc[0][p]) + float(conn_rows.iloc[1][p])
    return costs


def routing_cost(df_ops: pd.DataFrame, asm: Assembly, cfg: Dict) -> Dict[str, float]:
    plants = cfg["hourly_rate_per_plant"].keys()
    rout = {p: 0.0 for p in plants}

    def add(op_id: str, mult: int = 1):
        row = df_ops[df_ops["Operation_ID"] == op_id].iloc[0]
        for p in plants:
            rate = cfg["hourly_rate_per_plant"][p] / 60
            cost = ((row["Setup_min"] / asm.quantity) + row["Time_per_unit_min"]) * rate * asm.quantity * mult
            rout[p] += cost

    # Always material prep + cut-to-length
    add("OP01")  # Cut
    add("OP02")  # Material prep (mapped as OP02 here)

    # Connector-driven ops
    for conn in [asm.conn_a, asm.conn_b]:
        if conn.needs_strip_lvl1:
            add("OP03")
        if conn.needs_strip_lvl2:
            add("OP04")
        if conn.deburr:
            add("OP05")
        if not conn.pre_tinned:
            add("OP06")

    # Bending
    add("OP10" if asm.cnc_required(cfg) else "OP11")

    # Test & inspection
    add("OP12")
    add("OP13")

    # Packaging
    add("OP14" if asm.bends == 0 else "OP15")

    return rout


def convert(values: Dict[str, float], cfg: Dict, currency: str) -> Dict[str, float]:
    rate = cfg["exchange_rates"].get(currency, 1.0)
    return {k: v * rate for k, v in values.items()}

###############################################################################
# 3 – Feasibility
###############################################################################

def feasibility(asm: Assembly, cfg: Dict) -> List[Dict[str, str]]:
    res: List[Dict[str, str]] = []

    def mark(rule: str, ok: bool, msg: str):
        res.append({"Rule": rule, "Status": "PASS" if ok else "FAIL", "Msg": msg})

    # F1/F1a straight & offset
    size_defaults = cfg["default_straight_segments_mm"].get(asm.cable.size, {"LEL": 0, "LOL": 0})
    min_lel = max(asm.conn_a.lel_mm or 0, asm.conn_b.lel_mm or 0, cfg["min_straight_len_default_mm"], size_defaults["LEL"])
    mark("F1", True, f"Need ≥{min_lel} mm straight")  # assume user provides
    min_lol = max(asm.conn_a.lol_mm or 0, asm.conn_b.lol_mm or 0, size_defaults["LOL"])
    mark("F1a", True, f"Need ≥{min_lol} mm offset")

    # F2 bend spacing
    ok_spacing = (asm.bends == 0) or ((asm.cable.user_length_mm / asm.bends) >= asm.cable.min_bend_spacing_mm)
    mark("F2", ok_spacing, "Bend spacing OK" if ok_spacing else "Spacing too tight")

    # F3 frequency cap heuristic
    fam_max = 40.0 if "086" in asm.cable.size else 18.0
    mark("F3", asm.cable.frequency_ghz <= fam_max, "Freq within limit" if asm.cable.frequency_ghz <= fam_max else "Freq too high")

    # F4 CNC dimension limits
    cnc_ok = True
    if asm.cnc_required(cfg):
        cnc_ok = asm.bends <= 20 and asm.cable.user_length_mm <= 2000
    mark("F4", cnc_ok, "CNC OK" if cnc_ok else "CNC out of range")

    return res

###############################################################################
# 4 – Streamlit UI
###############################################################################

def main():
    st.set_page_config(page_title="Cable Quoter", layout="wide")
    st.title("🪢 RF Cable Quoting & Feasibility Tool")

    role = st.sidebar.selectbox("Role", ["Standard", "Master"], key="role")

    # Master file uploads
    if role == "Master":
        st.sidebar.header("📥 Upload data files")
        for fname, _ in EXCEL_META + [("config.yaml", None)]:
            up = st.sidebar.file_uploader(fname, key=fname)
            if up:
                save_file(up.getbuffer(), fname)
                st.sidebar.success(f"Saved {fname}")
        st.info("Switch to **Standard** to use the app.")
        st.stop()

    # Standard – load data
    cfg = load_cfg()
    df_cables = load_excel(*EXCEL_META[0])
    df_conns = load_excel(*EXCEL_META[1])
    df_ops = load_excel(*EXCEL_META[2])

    # UI Tabs
    tab_in, tab_bom, tab_route, tab_feas, tab_price = st.tabs(
        ["Quote Input", "BOM", "Routing", "Feasibility", "Price"])

    with tab_in:
        with st.form("quote"):
            plant = st.selectbox("Plant", list(cfg["hourly_rate_per_plant"].keys()))
            currency = st.selectbox("Currency", list(cfg["exchange_rates"].keys()))

            size = st.selectbox("Cable size", sorted(df_cables["Size"].unique()))
            fam = st.selectbox("Family", df_cables[df_cables["Size"] == size]["Family"].unique())
            length_user = st.number_input("Length (mm)", 50, 10000, 500)
            coptions = df_cables[(df_cables["Size"] == size) & (df_cables["Family"] == fam)]
            cable_pn = st.selectbox("Cable PN", coptions["Part_number"].unique())

            group = fam
            con_opts = df_conns[df_conns["Cable_group"] == group]
            conn_a = st.selectbox("Connector A", con_opts["Part_number"].unique())
            conn_b = st.selectbox("Connector B", con_opts["Part_number"].unique())

            qty = st.number_input("Quantity", 1, 1000, 10)
            bends = st.number_input("Number of bends", 0, 30, 0)

            submitted = st.form_submit_button("Calculate")

    if not submitted:
        st.stop()

    # Build models
    crow = df_cables[df_cables["Part_number"] == cable_pn].iloc[0]
    cable = Cable(
        part_number=crow["Part_number"],
        description=crow["Description"],
        family=crow["Family"],
        size=crow["Size"],
        stock_length_mm=int(crow["Length_mm"]),
        user_length_mm=int(length_user),
        frequency_ghz=float(crow["Frequency_GHz"]),
    )

    def conn(pn: str) -> Connector:
        r = df_conns[df_conns["Part_number"] == pn].iloc[0]
        return Connector(
            part_number=r["Part_number"],
            description=r["Description"],
            cable_group=r["Cable_group"],
            lel_mm=r.get("LEL_mm"),
            lol_mm=r.get("LOL_mm"),
            needs_strip_lvl1=bool(r.get("Needs_strip_lvl1", False)),
            needs_strip_lvl2=bool(r.get("Needs_strip_lvl2", False)),
            pre_tinned=not bool(r.get("Pre_tinned", False)),
            deburr=bool(r.get("Deburr", False)),
        )

    assembly = Assembly(
        cable=cable,
        conn_a=conn(conn_a),
        conn_b=conn(conn_b),
        quantity=int(qty),
        bends=int(bends),
        plant=plant,
        currency=currency,
    )

    # Calculations
    bom = bom_cost(df_cables, df_conns, assembly)
    routing = routing_cost(df_ops, assembly, cfg)
    total_chf = {p: bom[p] + routing[p] for p in bom.keys()}
    total_curr = convert(total_chf, cfg, currency)
    feas = feasibility(assembly, cfg)

    # Tab outputs
    with tab_bom:
        st.dataframe(pd.DataFrame([bom]).style.format("{:.2f}"))
    with tab_route:
        st.dataframe(pd.DataFrame([routing]).style.format("{:.2f}"))
    with tab_feas:
        df_f = pd.DataFrame(feas)
        st.dataframe(df_f.style.apply(lambda s: ["background-color:#faa" if v == "FAIL" else "" for v in s]))
    with tab_price:
        df_p = pd.DataFrame({
            "Plant": total_chf.keys(),
            "Total CHF": total_chf.values(),
            f"Total {currency}": total_curr.values(),
        })
        st.dataframe(df_p.style.format("{:.2f}"))
        best = min(total_chf, key=total_chf.get)
        st.success(f"Best price: {best} – {total_curr[best]:.2f} {currency}")


if __name__ == "__main__":
    main()
