import glob, pickle, numpy as np, pandas as pd
from sklearn.metrics import mean_absolute_error
from app.handicapping.nfl import nfl_xgb_model_ats as M
from app.handicapping.nfl import data_loader as DLmod
from app.handicapping.nfl.nfl_xgb_model_ats import get_model_features
import psycopg2
from app.core import config

DLmod._loader_instance = None
dl = DLmod.get_data_loader(ats_only=True)
df = dl.load_data(game_types=("REG", "POST"))
df = M._ensure_ats_features(df)
df = df.sort_values(["season_year", "week", "game_id"], kind="stable").reset_index(drop=True)

_dsn = config.settings.database_url.replace("postgresql+asyncpg", "postgresql").replace("postgresql+psycopg2", "postgresql")
with psycopg2.connect(_dsn) as conn:
    with conn.cursor() as cur:
        feats = get_model_features(cur, ats_only=True)

test = df[df["season_year"] == 2025].copy()
test = test[test["closing_spread"].notna()].copy()
y = test["home_score_margin"].to_numpy()

# current live model
LIVE = glob.glob("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl/nfl_ats_best.pkl")

def score(tid):
    p = glob.glob(f"/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl/{tid}-2025.pkl")
    if not p:
        print(tid, "MISSING"); return
    obj = pickle.load(open(p[0], "rb"))
    booster = obj["model"] if isinstance(obj, dict) and "model" in obj else obj
    fn = [c for c in booster.feature_names if c in test.columns]
    X = test[fn].to_numpy(dtype=float)
    import xgboost as xgb
    pred = booster.predict(xgb.DMatrix(X, feature_names=fn))
    print(f"{tid}: n_test={len(test)} MAE={mean_absolute_error(y,pred):.4f}")

for tid, label in [
    ("4df75d32-fbe2-440e-b35a-df360dae9f5a", "#453 (49.82)"),
    ("df715fd7-23fe-4543-8fff-465b8e11d35e", "#454 (55.09)"),
    ("96236351-4d1f-42fc-bf2e-3e6b0baab345", "#459 (55.09)"),
]:
    print(label, end="  ")
    score(tid)
print("rows in 2025 test:", len(test), " nfeat:", len(feats))
