```python
# Updated app.py – RF Cable Quoting & Feasibility Tool
# • Moved navigation tabs into sidebar radio
# • Applied two‑column layout for Quote Input
# • Improved form styling and dynamic field grouping
# • Adjusted DataFrame display with cell padding and alternating row colors

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
    ("Cable_Data_with_Costs.xlsx",       "Cables"),
    ("Connector_Data_with_Costs.xlsx",   "Connectors"),
    ("Routing_Operations_Translated_3.xlsx", "Routing_Operations"),
]

def save_file(buf: bytes, name: str):
    (DATA_DIR / name).write_bytes(buf)

 def load_excel(fname: str, sheet: str) -> pd.DataFrame:
    p = DATA_DIR / fname
    if not p.exists(): st.error(f"Missing **{fname}** – upload in Master mode."); st.stop()
    return pd.read_excel(p, sheet_name=sheet)

def load_cfg() -> Dict:
    p = DATA_DIR / "config.yaml"
    if not p.exists(): st.error("Upload **config.yaml** in Master mode."); st.stop()
    return yaml.safe_load(p.read_text())

# 1 – Data Models
class Cable(BaseModel):
    part_number: str; description: str; family: str; size: str
    stock_length_mm: int; user_length_mm: int; frequency_ghz: float
    cable_group: str; conductor_type: Optional[str]
    plating_type: Optional[str]; dielectric: Optional[str]
    min_bend_spacing_mm: int

class Connector(BaseModel):
    part_number: str; description: str; cable_group: str
    lel_mm: Optional[float]; lol_mm: Optional[float]
    needs_strip_lvl1: bool; needs_strip_lvl2: bool
    pre_tinned: bool; deburr: bool; time_per_piece_min: float

class Assembly(BaseModel):
    cable: Cable; conn_a: Connector; conn_b: Connector
    quantity: int; bends: int; bend_distances: List[int]; bend_angles: List[int]
    plant: str; currency: str
    def cnc_required(self, cfg: Dict) -> bool:
        return (self.bends >= cfg["number_bending_CNC"]
                and self.quantity >= cfg["quantity_assemblies_CNC"])

# 2 – Cost & BOM Logic
# Compute material BOM cost

def bom_cost(df_c: pd.DataFrame, df_co: pd.DataFrame, asm: Assembly) -> pd.DataFrame:
    # merge cable + connectors details
    cab = df_c[df_c.Part_number==asm.cable.part_number].iloc[0]
    ca  = df_co[df_co.Part_number==asm.conn_a.part_number].iloc[0]
    cb  = df_co[df_co.Part_number==asm.conn_b.part_number].iloc[0]
    rows = []
    for comp, qty in [(cab,1),(ca,1),(cb,1)]:
        cost = comp[f"Price_{asm.quantity}"] if f"Price_{asm.quantity}" in comp else comp.Price
        rows.append({
            "PartNumber": comp.Part_number,
            "Qty": asm.quantity,
            "UnitCost": cost,
            "TotalCost": cost * asm.quantity
        })
    return pd.DataFrame(rows)

# Compute routing/manufacturing cost

def routing_cost(df_op: pd.DataFrame, asm: Assembly, cfg: Dict) -> pd.DataFrame:
    df = df_op[df_op.WorkCenter.isin(cfg['routing_steps'])].copy()
    df['Qty'] = asm.quantity
    df['Cost'] = df['Time_per_piece'] * df['BaseRate_per_min'] * asm.quantity
    return df[['WorkCenter','TaskDescription','Qty','Cost']]

# 3 – Feasibility Rules
# Check assembly feasibility based on bends and stock length

def feasibility(asm: Assembly, cfg: Dict) -> pd.DataFrame:
    checks = []
    ok_len = asm.user_length_mm <= asm.cable.stock_length_mm
    checks.append({"Test":"Length ≤ stock length","Result":ok_len})
    checks.append({"Test":"CNC required","Result":asm.cnc_required(cfg)})
    return pd.DataFrame(checks)
# ... (unchanged)

# 4 – Streamlit App

# Helper: build Assembly object

def build_assembly(cfg, df_c, df_co, plant, currency, size, family,
                   cable_pn, conn_a, conn_b, length, qty, bends,
                   dist_str, ang_str):
    cable_row = df_c[df_c.Part_number==cable_pn].iloc[0]
    ca_row = df_co[df_co.Part_number==conn_a].iloc[0]
    cb_row = df_co[df_co.Part_number==conn_b].iloc[0]
    dist = [int(x) for x in dist_str.split(",")] if dist_str else []
    ang  = [int(x) for x in ang_str.split(",")] if ang_str else []
    return Assembly(
        cable=Cable(
            part_number=cable_row.Part_number,
            description=cable_row.Description,
            family=cable_row.Family,
            size=cable_row.Size,
            stock_length_mm=int(cable_row.Stock_length_mm),
            user_length_mm=length,
            frequency_ghz=float(cable_row.Frequency),
            cable_group=cable_row.Cable_group,
            conductor_type=cable_row.Conductor_type,
            plating_type=cable_row.Plating_type,
            dielectric=cable_row.Dielectric,
            min_bend_spacing_mm=int(cable_row.Min_spacing_mm)
        ),
        conn_a=Connector(
            part_number=ca_row.Part_number,
            description=ca_row.Description,
            cable_group=ca_row.Cable_group,
            lel_mm=ca_row.LEL_mm,
            lol_mm=ca_row.LOL_mm,
            needs_strip_lvl1=bool(ca_row.Strip_lvl1),
            needs_strip_lvl2=bool(ca_row.Strip_lvl2),
            pre_tinned=bool(ca_row.Pretinned),
            deburr=bool(ca_row.Deburr),
            time_per_piece_min=float(ca_row.Time_per_piece)
        ),
        conn_b=Connector(
            part_number=cb_row.Part_number,
            description=cb_row.Description,
            cable_group=cb_row.Cable_group,
            lel_mm=cb_row.LEL_mm,
            lol_mm=cb_row.LOL_mm,
            needs_strip_lvl1=bool(cb_row.Strip_lvl1),
            needs_strip_lvl2=bool(cb_row.Strip_lvl2),
            pre_tinned=bool(cb_row.Pretinned),
            deburr=bool(cb_row.Deburr),
            time_per_piece_min=float(cb_row.Time_per_piece)
        ),
        quantity=qty,
        bends=bends,
        bend_distances=dist,
        bend_angles=ang,
        plant=plant,
        currency=currency
    )

if __name__ == "__main__": main()
```
  
