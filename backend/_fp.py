"""Per-column fingerprint of the NFL training matrix. Usage: python _fp.py <out.json>"""
import sys, json, hashlib
import numpy as np
import pandas as pd

from app.handicapping.nfl import nfl_xgb_model_ats as M
from app.handicapping.nfl import data_loader as DLmod


def col_hash(s: pd.Series) -> str:
    v = pd.to_numeric(s, errors="coerce").astype("float64").to_numpy()
    v = np.where(np.isnan(v), -1.234e308, v)
    return hashlib.md5(v.tobytes()).hexdigest()[:16]


async def main():
    out = sys.argv[1]
    DLmod._loader_instance = None
    dl = DLmod.get_data_loader(ats_only=True)
    df = dl.load_data(game_types=("REG", "POST"))
    df = df.sort_values(["season_year", "week", "game_id"], kind="stable").reset_index(drop=True)

    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]

    fp = {"n_rows": int(len(df)), "n_cols": int(df.shape[1])}
    for c in num_cols:
        fp[c] = col_hash(df[c])
    # key/order fingerprint
    keycols = [c for c in ["season_year", "week", "game_id", "home_team", "away_team"] if c in df.columns]
    fp["__order__"] = hashlib.md5(pd.util.hash_pandas_object(df[keycols], index=False).values.tobytes()).hexdigest()[:16]
    with open(out, "w") as f:
        json.dump(fp, f, indent=0, sort_keys=True)
    print("wrote", out, "rows", fp["n_rows"], "cols", fp["n_cols"])


import asyncio
asyncio.run(main())
