"""Info program Phase A: build the supervised dataset (one row per window x horizon).

Inputs: window_paths.db era slice (books/CVD/chainlink/strike), paper_0901.db
labels, tape_*.jsonl[.gz] (CLOB print flow), binance/ zips (spot 1s klines,
perp 1m klines, 5m metrics OI). Features exactly as pre-registered in
docs/research/info_program_2026-09-01.md. Output: info_dataset.parquet.
"""
import gzip
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

SP = Path(__file__).parent
D = SP / "data" / "vps-0831"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
ERA = 1786665600
END = 1788307200            # 2026-09-01 00:00 UTC — era days 08-14..08-31 only
HORIZONS = [240, 180, 120, 60, 30]
BIDSZ_OK_TS = 1787616000    # 08-21 00:00Z: bid-size cols worst-level before this
TAPE_DAYS = [f"2026-08-{d:02d}" for d in range(13, 32)]

t0 = time.time()

# ---- labels ----------------------------------------------------------------
con = sqlite3.connect(f"file:{D / 'paper_0901.db'}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
labels = {}
for r in con.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
    ep = int(r["window_id"].rsplit("-", 1)[1])
    if ERA <= ep < END and r["final_price"] is not None and r["price_to_beat"] is not None:
        labels[ep] = dict(r)
print(f"{len(labels)} labeled era windows ({time.time()-t0:.0f}s)")

# ---- window_paths era slice -------------------------------------------------
wp_con = sqlite3.connect(f"file:{D / 'window_paths.db'}?mode=ro", uri=True)
cols = [r[1] for r in wp_con.execute("pragma table_info(window_paths)")]
want = [c for c in ["window_id", "ts", "bid_up", "ask_up", "bid_down", "ask_down",
                    "depth3_bid_up", "depth3_ask_up", "depth3_bid_down", "depth3_ask_down",
                    "bid_sz_up", "ask_sz_up", "bid_sz_down", "ask_sz_down",
                    "binance_cvd_10s", "binance_cvd_30s", "chainlink_price",
                    "chainlink_age_s", "book_age_up_s", "book_age_down_s", "strike"]
        if c in cols]
wp = pd.read_sql_query(
    f"SELECT {', '.join(want)} FROM window_paths WHERE ts >= {ERA} AND ts < {END + 400}",
    wp_con)
wp_con.close()
wp["ep"] = wp["window_id"].str.rsplit("-", n=1).str[1].astype(np.int64)
wp = wp.sort_values(["ep", "ts"])
print(f"window_paths era rows: {len(wp)} ({time.time()-t0:.0f}s)")

# ---- spot 1s klines ----------------------------------------------------------
frames = []
for z in sorted((D / "binance").glob("spot1s_*.zip")):
    with zipfile.ZipFile(z) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, usecols=[0, 4, 5, 9],
                             names=["t", "close", "vol", "tbuy"])
    frames.append(df)
spot = pd.concat(frames, ignore_index=True)
spot["sec"] = (spot["t"] // 1_000_000).astype(np.int64)   # micro-s -> s
spot = spot.drop_duplicates("sec").sort_values("sec").reset_index(drop=True)
sec0 = int(spot["sec"].iloc[0])
n_sec = int(spot["sec"].iloc[-1]) - sec0 + 1
close_a = np.full(n_sec, np.nan); vol_a = np.zeros(n_sec); tbuy_a = np.zeros(n_sec)
idx = (spot["sec"] - sec0).to_numpy()
close_a[idx] = spot["close"].to_numpy()
vol_a[idx] = spot["vol"].to_numpy()
tbuy_a[idx] = spot["tbuy"].to_numpy()
close_s = pd.Series(close_a).ffill().to_numpy()
lr = np.zeros(n_sec)
lr[1:] = np.diff(np.log(close_s))
cs_vol = np.concatenate([[0.0], np.cumsum(vol_a)])
cs_tbuy = np.concatenate([[0.0], np.cumsum(tbuy_a)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
print(f"spot 1s: {len(spot)} bars ({time.time()-t0:.0f}s)")

def spot_feats(ts):
    i = int(ts) - sec0 - 1                 # last fully closed 1s bar
    if i < 400 or i >= n_sec:
        return None
    px = close_s[i]
    out = {}
    for d in (10, 30, 60, 120, 300):
        out[f"ret_{d}"] = np.log(close_s[i] / close_s[i - d])
    rv60 = np.sqrt(max(cs_lr2[i + 1] - cs_lr2[i + 1 - 60], 0) / 60)
    rv300 = np.sqrt(max(cs_lr2[i + 1] - cs_lr2[i + 1 - 300], 0) / 300)
    out["rv60"] = rv60
    out["rv300"] = rv300
    out["rv_ratio"] = rv60 / rv300 if rv300 > 0 else 1.0
    out["z_ret60"] = out["ret_60"] / (rv300 * np.sqrt(60)) if rv300 > 0 else 0.0
    for d in (30, 60, 120):
        v = cs_vol[i + 1] - cs_vol[i + 1 - d]
        b = cs_tbuy[i + 1] - cs_tbuy[i + 1 - d]
        out[f"imb_{d}"] = (2 * b - v) / v if v > 0 else 0.0
    out["spot_px"] = px
    return out

# ---- perp 1m + metrics -------------------------------------------------------
pframes = []
for z in sorted((D / "binance").glob("perp1m_*.zip")):
    with zipfile.ZipFile(z) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, usecols=[0, 4], names=["t", "close"])
    pframes.append(df)
perp = pd.concat(pframes, ignore_index=True)
perp = perp[pd.to_numeric(perp["t"], errors="coerce").notna()].astype({"t": np.int64})
perp["sec"] = perp["t"] // 1_000_000
perp = perp.drop_duplicates("sec").sort_values("sec")
perp_sec = perp["sec"].to_numpy()
perp_close = perp["close"].astype(float).to_numpy()

mframes = []
for z in sorted((D / "binance").glob("metrics_*.zip")):
    with zipfile.ZipFile(z) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(f, usecols=["create_time", "sum_open_interest"])
    mframes.append(df)
met = pd.concat(mframes, ignore_index=True)
met["sec"] = pd.to_datetime(met["create_time"], utc=True).astype(np.int64) // 10 ** 9
met = met.sort_values("sec")
met_sec = met["sec"].to_numpy()
met_oi = met["sum_open_interest"].astype(float).to_numpy()
print(f"perp bars {len(perp)}, metrics rows {len(met)} ({time.time()-t0:.0f}s)")

def perp_feats(ts, spot_px):
    j = np.searchsorted(perp_sec, ts - 60) - 1     # last CLOSED 1m bar
    out = {}
    if 5 <= j < len(perp_sec):
        out["perp_ret_5m"] = np.log(perp_close[j] / perp_close[j - 5])
        out["basis"] = (perp_close[j] - spot_px) / spot_px if spot_px else 0.0
    else:
        out["perp_ret_5m"] = 0.0
        out["basis"] = 0.0
    m = np.searchsorted(met_sec, ts) - 1
    if m >= 6:
        out["oi_d30m"] = (met_oi[m] - met_oi[m - 6]) / met_oi[m - 6]
    else:
        out["oi_d30m"] = 0.0
    return out

# ---- CLOB print flow from tape ------------------------------------------------
tok2 = {}
for ep, lab in labels.items():
    if lab["token_up"]:
        tok2[lab["token_up"]] = (ep, 1)
    if lab["token_down"]:
        tok2[lab["token_down"]] = (ep, 0)
flow = {ep: [] for ep in labels}      # (ts, signed_shares) at px 0.2-0.8
for day in TAPE_DAYS:
    p = REC / f"tape_{day}.jsonl.gz"
    if not p.exists():
        p = REC / f"tape_{day}.jsonl"
    if not p.exists():
        continue
    opener = (lambda q: gzip.open(q, "rt", encoding="utf-8")) if p.suffix == ".gz" \
        else (lambda q: open(q, encoding="utf-8"))
    with opener(p) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            w = tok2.get(r.get("token"))
            if w is None:
                continue
            try:
                px, sz, ts = float(r["price"]), float(r["size"]), float(r["ts"])
            except (TypeError, ValueError):
                continue
            if not (0.2 <= px <= 0.8):
                continue
            ep, is_up = w
            sgn = 1.0 if ((r["side"] == "BUY") == bool(is_up)) else -1.0
            flow[ep].append((ts, sgn * sz))
for ep in flow:
    flow[ep].sort()
print(f"tape flow built ({time.time()-t0:.0f}s)")

def flow_feats(ep, ts):
    ev = flow.get(ep) or []
    f60 = f120 = 0.0
    for t, s in ev:
        if t >= ts:
            break
        if t >= ts - 120:
            f120 += s
            if t >= ts - 60:
                f60 += s
    return {"clob_flow_60": f60, "clob_flow_120": f120}

# ---- assemble ------------------------------------------------------------------
rows = []
for ep, g in wp.groupby("ep", sort=True):
    lab = labels.get(int(ep))
    if lab is None:
        continue
    close = ep + 300
    strike = lab["price_to_beat"]
    tsv = g["ts"].to_numpy()
    for k in HORIZONS:
        ts0 = close - k
        i = int(np.argmin(np.abs(tsv - ts0)))
        if abs(tsv[i] - ts0) > 5:
            continue
        row = g.iloc[i]
        bu, au, bd, ad = row["bid_up"], row["ask_up"], row["bid_down"], row["ask_down"]
        if not (0 < bu <= au < 1 and 0 < bd <= ad < 1):
            continue
        if row["book_age_up_s"] > 5 or row["book_age_down_s"] > 5:
            continue
        mid_u, mid_d = (bu + au) / 2, (bd + ad) / 2
        if not (0.90 < mid_u + mid_d < 1.10):
            continue
        sf = spot_feats(tsv[i])
        if sf is None:
            continue
        # staleness-shifted executable asks: the next 1 Hz row (>= +1 s later)
        if i + 1 < len(g) and tsv[i + 1] - tsv[i] <= 2.5:
            nxt = g.iloc[i + 1]
            exec_au = nxt["ask_up"] if 0 < nxt["ask_up"] < 1 else au
            exec_ad = nxt["ask_down"] if 0 < nxt["ask_down"] < 1 else ad
        else:
            exec_au, exec_ad = au, ad
        rv_dollar = sf["rv300"] * sf["spot_px"]
        cl = row["chainlink_price"]
        feats = dict(
            ep=int(ep), k=k, ts=tsv[i],
            et_day=time.strftime("%m-%d", time.gmtime(ep - 4 * 3600)),
            label=int(lab["resolved_up"]),
            book_p=float(np.clip(mid_u, 0.01, 0.99)),
            ask_up=au, ask_dn=ad, exec_ask_up=exec_au, exec_ask_dn=exec_ad,
            ask_sz_up=row.get("ask_sz_up", np.nan), ask_sz_dn=row.get("ask_sz_down", np.nan),
            spread_up=au - bu, spread_dn=ad - bd,
            cvd10=row["binance_cvd_10s"], cvd30=row["binance_cvd_30s"],
            cl_minus_strike_z=((cl - strike) / (rv_dollar * np.sqrt(k)))
            if (cl and strike and rv_dollar > 0) else 0.0,
            mid_dev=mid_u - 0.5,
            tod_sin=np.sin(2 * np.pi * (ep % 86400) / 86400),
            tod_cos=np.cos(2 * np.pi * (ep % 86400) / 86400),
        )
        if tsv[i] >= BIDSZ_OK_TS and "bid_sz_up" in row.index:
            bsu, bsd = row["bid_sz_up"], row["bid_sz_down"]
            asu, asd = row["ask_sz_up"], row["ask_sz_down"]
            tot = (bsu or 0) + (asu or 0)
            feats["size_imb_up"] = ((bsu - asu) / tot) if tot else 0.0
            d = (row["depth3_bid_up"] or 0) + (row["depth3_ask_up"] or 0)
            feats["depth_imb_up"] = (((row["depth3_bid_up"] or 0) - (row["depth3_ask_up"] or 0)) / d) if d else 0.0
        else:
            feats["size_imb_up"] = np.nan
            feats["depth_imb_up"] = np.nan
        feats.update({kk: vv for kk, vv in sf.items()})
        feats.update(perp_feats(tsv[i], sf["spot_px"]))
        feats.update(flow_feats(int(ep), tsv[i]))
        rows.append(feats)

df = pd.DataFrame(rows)
print(f"dataset: {len(df)} rows, {df['ep'].nunique()} windows, "
      f"days {sorted(df['et_day'].unique())}")
print(df.groupby("k").size())
df.to_parquet(D / "info_dataset.parquet")
print(f"saved ({time.time()-t0:.0f}s)")
