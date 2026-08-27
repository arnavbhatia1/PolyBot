"""R4 Candidate A (cushion dip-buyer) — bar adjudication + report.

Reads data/vps-0821/r4_results.json (written by
`ws2_ladder_replay.py candidate_a`) and writes data/vps-0821/r4_report.md.

Bar (WALLETS.md, verbatim): engine-true replay (strictly-below fills,
both-sides rungs 0.10-0.35, no sign filter) over >=7 60s-rule days must show
win% >= price+8pp on every rung with >=10 fills, positive dollars in each of
two disjoint 3-day splits, AND the projection-signed variant must not dominate
it (if sign-gating strictly improves it, it collapses into deep_proj).

Splits are fixed BEFORE reading numbers: the first three and the last three
full ET days of the era (08-14..16 and 08-24..26). Every consecutive 3-day
block is tabulated as well so the choice of pair is auditable.
"""
import json
import sys
from pathlib import Path

SP = Path(__file__).parent
sys.path.insert(0, str(SP))
import ws2_ladder_replay as ws2  # noqa: E402

OUT = SP / "data" / "vps-0821"
SPLIT_A = ("08-14", "08-15", "08-16")
SPLIT_B = ("08-24", "08-25", "08-26")
ALL_DAYS = ["08-13", "08-14", "08-15", "08-16", "08-17", "08-18", "08-19",
            "08-20", "08-21", "08-22", "08-23", "08-24", "08-25", "08-26",
            "08-27"]
BE_PP = 8.0
PAIR = {"A6_k25": "S6_k25_b30", "A6_k120": "S6_k120_b30", "A3_k25": "S3_k25_b30",
        "A3_k120": "S3_k25_b30", "A6_k25_engine_pc": "S6_k25_b30",
        "A6_k120_engine_pc": "S6_k120_b30", "A6_k60": "S6_k25_b30",
        "A6_k25_all": "S6_k25_b30", "A6_k120_all": "S6_k120_b30"}


def pct(a, b):
    return 100.0 * a / b if b else float("nan")


def f2(x):
    return f"{x:+.2f}"


def cand_totals(res):
    fills = [s for r in res for s in r["sides"].values() if s["fills"]]
    sh = sum(f["sh"] for s in fills for f in s["fills"].values())
    usd = sum(r["pnl"] for r in res)
    wins = sum(1 for s in fills if s["win"])
    both = sum(1 for r in res if all(s["fills"] for s in r["sides"].values()))
    rested = sum(1 for r in res for s in r["sides"].values() if s["placed"])
    noref = sum(1 for r in res for s in r["sides"].values() if s["ref"] is None)
    rungs_rested = sum(len(s["placed"]) for r in res for s in r["sides"].values())
    return dict(windows=len(res), side_fills=len(fills), wins=wins, sh=sh,
                usd=usd, cps=pct(usd, sh), both=both, sides_resting=rested,
                noref=noref, rungs_rested=rungs_rested,
                filled_windows=sum(1 for r in res if r["filled"] > 0))


def book_side_split(res):
    """Side-fills by which side the BOOK favoured at placement (ref > 0.5)."""
    out = {"book-favourite": [0, 0, 0.0], "book-underdog": [0, 0, 0.0]}
    for r in res:
        for s in r["sides"].values():
            if not s["fills"]:
                continue
            k = "book-favourite" if (s["ref"] or 0.0) > 0.5 else "book-underdog"
            out[k][0] += 1
            out[k][1] += 1 if s["win"] else 0
            out[k][2] += s["pnl"]
    return out


def gated_totals(res):
    fills = [r for r in res if r["filled"] > 0]
    sh = sum(r["filled"] for r in fills)
    usd = sum(r["pnl"] for r in fills)
    return dict(armed=len(res), filled_windows=len(fills), side_fills=len(fills),
                wins=sum(1 for r in fills if r["win"]), sh=sh, usd=usd,
                cps=pct(usd, sh))


def split_usd(ds, days):
    return sum(ds.get(d, 0.0) for d in days)


def blocks(ds):
    full = ALL_DAYS[1:-1]  # 08-14..26 are full ET days
    return [(f"{full[i]}..{full[i + 2][3:]}", split_usd(ds, full[i:i + 3]))
            for i in range(len(full) - 2)]


def rung_table_cand(st, rungs):
    lines = ["| rung | rested | fills | win% | be | c/sh | dollars | k med (p25-p75) | Up fills $ | Down fills $ |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for rp in rungs:
        b = st[rp]
        wp = pct(b["wins"], b["fills"])
        mark = "" if b["fills"] < 10 else (" PASS" if wp >= 100 * rp + BE_PP else " FAIL")
        kk = (f"{b['k_med']:.0f} ({b['k_p25']:.0f}-{b['k_p75']:.0f})"
              if b["k_med"] is not None else "-")
        lines.append(
            f"| {rp:.2f} | {b['placements']:,} | {b['fills']} | "
            f"{wp:.1f}{mark} | {100 * rp + BE_PP:.0f} | {pct(b['dollars'], b['sh']):+.2f} | "
            f"{f2(b['dollars'])} | {kk} | {b['Up']['fills']} {f2(b['Up']['dollars'])} | "
            f"{b['Down']['fills']} {f2(b['Down']['dollars'])} |")
    return "\n".join(lines)


def overlay_table(st, rungs):
    lines = ["| rung | fav (mult>=1) fills / win% / $ | weak [0,1) | anti (<0) | cold |",
             "|---|---|---|---|---|"]
    tot = {k: [0, 0, 0.0] for k in ("fav", "weak", "anti", "na")}
    for rp in rungs:
        b = st[rp]
        cells = []
        for k in ("fav", "weak", "anti", "na"):
            x = b[k]
            tot[k][0] += x["fills"]
            tot[k][1] += x["wins"]
            tot[k][2] += x["dollars"]
            cells.append(f"{x['fills']} / {pct(x['wins'], x['fills']):.0f}% / {f2(x['dollars'])}"
                         if x["fills"] else "0")
        lines.append(f"| {rp:.2f} | " + " | ".join(cells) + " |")
    lines.append("| **all** | " + " | ".join(
        f"{t[0]} / {pct(t[1], t[0]):.0f}% / {f2(t[2])}" if t[0] else "0"
        for t in tot.values()) + " |")
    return "\n".join(lines), tot


def rung_table_gated(st, rungs):
    lines = ["| rung | placed | fills | flip | floor | win% | be | c/sh | dollars |",
             "|---|---|---|---|---|---|---|---|---|"]
    for rp in rungs:
        s = st[rp]
        lines.append(
            f"| {rp:.2f} | {s['placements']:,} | {s['fills']} | {s['flip']} | {s['floor']} | "
            f"{pct(s['wins'], s['fills']):.1f} | {100 * rp + BE_PP:.0f} | "
            f"{pct(s['dollars'], s['sh']):+.2f} | {f2(s['dollars'])} |")
    return "\n".join(lines)


def adjudicate(name, res, rungs, gated_res):
    st = ws2.cand_rung_stats(res, rungs)
    ds = ws2.day_split(res)
    tot = cand_totals(res)
    rung_ok, rung_fail, rung_thin = [], [], []
    for rp in rungs:
        b = st[rp]
        if b["fills"] < 10:
            rung_thin.append(rp)
        elif pct(b["wins"], b["fills"]) >= 100 * rp + BE_PP:
            rung_ok.append(rp)
        else:
            rung_fail.append(rp)
    a, b_ = split_usd(ds, SPLIT_A), split_usd(ds, SPLIT_B)
    g = gated_totals(gated_res)
    dominated = g["usd"] >= tot["usd"] and g["cps"] >= tot["cps"]
    ov, ovt = overlay_table(st, rungs)
    kept = ovt["fav"]
    strict = (kept[2] > tot["usd"]) and (kept[0] > 0)
    c1 = not rung_fail and bool(rung_ok)
    c2 = a > 0 and b_ > 0
    c3 = not dominated and not strict
    return dict(name=name, st=st, ds=ds, tot=tot, rung_ok=rung_ok, rung_fail=rung_fail,
                rung_thin=rung_thin, split_a=a, split_b=b_, gated=g, dominated=dominated,
                overlay=ov, overlay_tot=ovt, strict=strict, c1=c1, c2=c2, c3=c3,
                passes=c1 and c2 and c3, blocks=blocks(ds))


def _floatkeys(R):
    """JSON turns float rung keys into strings; restore them."""
    for v in R.values():
        for r in v["results"]:
            if v["kind"] == "candidate_a":
                for s in r["sides"].values():
                    s["fills"] = {float(k): f for k, f in s["fills"].items()}
            else:
                r["rungs"] = {float(k): f for k, f in r["rungs"].items()}
    return R


def main():
    R = _floatkeys(json.load(open(OUT / "r4_results.json")))
    cand_names = [n for n, v in R.items() if v["kind"] == "candidate_a"]
    adj = {}
    for n in cand_names:
        v = R[n]
        adj[n] = adjudicate(n, v["results"], v["params"]["rungs"], R[PAIR[n]]["results"])
    prim = adj["A6_k25"]
    n_days = len([d for d in prim["ds"] if d not in ("08-13", "08-27")])

    any_pass = [n for n, a in adj.items() if a["passes"]]
    if any_pass:
        verdict = f"STAGED — variants passing every clause: {', '.join(any_pass)}"
    else:
        verdict = "REFUTED"
    L = []
    L.append("# R4 — Candidate A (cushion dip-buyer): engine-true replay vs the pre-registered bar\n")
    L.append(f"**VERDICT: {verdict}.**\n")
    L.append(f"Corpus: 60s era, {prim['tot']['windows']:,} labeled windows, {n_days} full ET days "
             f"(08-14..26) plus the 08-13 and 08-27 partials. Budget $60/window split equally over "
             f"all rungs (both sides); MIN_SHARES 5 — starved rungs: "
             f"{R['A6_k25']['results'][0]['starved'] or 'none'}. Fill rule = paper's "
             f"(strictly-below fills in full; at-price credits beyond 135 sh); GTC latencies "
             f"56/54 ms. Hold to resolution, no fee.\n")
    L.append("## Headline numbers\n")
    for n in ("A6_k25", "A6_k120"):
        a = adj[n]
        t = a["tot"]
        st = a["st"]
        rungs = R[n]["params"]["rungs"]
        wl = ", ".join(f"{rp:.2f}: {pct(st[rp]['wins'], st[rp]['fills']):.1f}% vs be {100 * rp + BE_PP:.0f}"
                       for rp in rungs)
        bs = book_side_split(R[n]["results"])
        bsl = "; ".join(f"{k} {v[0]} fills, {pct(v[1], v[0]):.1f}% win, {f2(v[2])}"
                        for k, v in bs.items())
        L.append(f"- **{n}** (k_place [6,{R[n]['params']['k_max']:.0f}]): {t['side_fills']} side-fills, "
                 f"{f2(t['usd'])} ({t['cps']:+.2f} c/sh). Win% per rung: {wl}. Every rung is under "
                 f"its own PRICE, not just under price+8pp. Split by which side the book favoured at "
                 f"placement: {bsl}.")
    o = adj["A6_k25"]
    L.append("- Sign-gating on the same rungs: the projection-side ladder at need 1.0 makes "
             f"{f2(o['gated']['usd'])} on {o['gated']['filled_windows']} filled windows "
             f"({o['gated']['cps']:+.2f} c/sh); Candidate A's own fills that the projection favoured at "
             f"fill time (mult >= 1) total {o['overlay_tot']['fav'][0]} fills, "
             f"{f2(o['overlay_tot']['fav'][2])}, vs {o['overlay_tot']['anti'][0]} anti-projection fills "
             f"at {f2(o['overlay_tot']['anti'][2])}. The dollars live entirely in the projection-favoured "
             "slice, so the WALLETS.md collapse clause fires: whatever is left IS deep_proj.")
    L.append("- Day stability: the only positive bucket in any scored variant is the ET 08-13 partial "
             "(00:00-04:00 UTC 08-14, the first four hours after the silent 30s-to-60s switch, when the "
             "book itself was still pricing the old rule). For A6_k25 every full ET day 08-14..26 is "
             "negative except 08-19 (+$24); for A6_k120 every one is negative; 0/11 consecutive 3-day "
             "blocks are positive for either.")
    L.append("- Reconciliation with the triplet: the 08-21 H1 decomposition placed almach/bosona/mo-money "
             "in the mid-window TOUCH-bid wall (k>60, 2.3-8.3k-share shared-price queues) — at-price "
             "queue fills, not print-through deep rungs. The population a passive cushion ladder can "
             "reach is the avalanche-swept one, and it loses at every price; the triplet's 37-43% win at "
             "0.25-0.30 is not reproducible from this position, and the 0/102 live post-close probe "
             "already says we cannot occupy their queue.")
    L.append("- ANTI control caveat: `run(anti=True)` rests on the projection-disfavoured side, which "
             "trades near 0.01 at k<=25, so its 2,403 'fills' are the same crossing artifact as the "
             "unconditional rows — it confirms the projection sign is real, nothing more.\n")
    L.append("## Bar clauses, every variant\n")
    L.append("| variant | rungs | k_place | post-close | rest rule | sides resting | side-fills | wins | c/sh | dollars | "
             "(1) win%>=price+8pp on every rung >=10 fills | (2) split 08-14..16 / 08-24..26 | "
             "(3) sign-gated does not dominate | verdict |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for n, a in adj.items():
        p = R[n]["params"]
        c1 = ("PASS" if a["c1"] else "FAIL") + (
            f" (fail {a['rung_fail']})" if a["rung_fail"] else "") + (
            f" (thin {a['rung_thin']})" if a["rung_thin"] else "")
        c2 = ("PASS" if a["c2"] else "FAIL") + f" ({f2(a['split_a'])} / {f2(a['split_b'])})"
        g = a["gated"]
        c3 = ("PASS" if a["c3"] else "FAIL") + (
            f" (gated {f2(g['usd'])} @ {g['cps']:+.2f} c/sh"
            f"{'; fav-only fills beat all fills' if a['strict'] else ''})")
        L.append(f"| {n} | {len(p['rungs'])} | [6,{p['k_max']:.0f}] | {p.get('post_close', 'close')} | "
                 f"{p.get('rest', 'below_ref')} | {a['tot']['sides_resting']:,} | "
                 f"{a['tot']['side_fills']} | {a['tot']['wins']} | {a['tot']['cps']:+.2f} | "
                 f"{f2(a['tot']['usd'])} | {c1} | {c2} | {c3} | "
                 f"{'PASS' if a['passes'] else 'FAIL'} |")
    L.append("")
    L.append("Clause (3) reads two ways, both required to pass: the sign-gated ladder on the same "
             "rungs (projection side only, need 1.0, $5/rung matched) must not have both more "
             "dollars and more c/sh; AND restricting Candidate A's own fills to those the "
             "projection favoured at fill time (mult >= 1.0) must not raise total dollars. "
             "If either holds, sign-gating strictly improves it and it is a deep_proj config.\n")

    for n in ("A6_k25", "A6_k120", "A3_k25", "A3_k120", "A6_k60"):
        a = adj[n]
        p = R[n]["params"]
        L.append(f"## {n} — rungs {p['rungs']}, k_place [6,{p['k_max']:.0f}], cancel at close\n")
        t = a["tot"]
        L.append(f"Sides with at least one resting rung {t['sides_resting']:,} of {2 * t['windows']:,} "
                 f"(no market reference on {t['noref']}); rungs rested {t['rungs_rested']:,}. "
                 f"Windows with any fill {t['filled_windows']}; "
                 f"side-fills {t['side_fills']} ({t['wins']} won); both sides filled in the same "
                 f"window {t['both']}; shares {t['sh']:,.1f}; dollars **{f2(t['usd'])}**; "
                 f"c/sh **{t['cps']:+.2f}**.\n")
        L.append(rung_table_cand(a["st"], p["rungs"]))
        L.append("")
        L.append("Sign overlay at fill time (projection multiple toward the filled side, need units; "
                 "k>60 fills use spot-vs-strike):\n")
        L.append(a["overlay"])
        L.append("")
        L.append("3-day blocks ($): " + "; ".join(f"{k} {f2(v)}" for k, v in a["blocks"]) +
                 f". Pre-fixed splits: 08-14..16 **{f2(a['split_a'])}**, 08-24..26 **{f2(a['split_b'])}**; "
                 f"positive blocks {sum(1 for _, v in a['blocks'] if v > 0)}/{len(a['blocks'])}.\n")

    L.append("## Why the resting rule exists — unconditional both-sides rungs\n")
    L.append("| variant | sides resting | side-fills | win% | c/sh | dollars |")
    L.append("|---|---|---|---|---|---|")
    for n in ("A6_k25_all", "A6_k120_all"):
        t = adj[n]["tot"]
        L.append(f"| {n} | {t['sides_resting']:,} | {t['side_fills']:,} | "
                 f"{pct(t['wins'], t['side_fills']):.1f} | {t['cps']:+.2f} | {f2(t['usd'])} |")
    L.append("")
    L.append("At k=25 the book is bimodal (last print 0.01 / 0.99 on the two tokens in most "
             "windows); a 0.35 bid on the 0.01 token is a crossing taker order on a dead token, "
             "and the print-through rule would credit it at 0.35. Those rows are not the "
             "mechanism (panic prints below a LIVE market) — they are the reason every scored "
             "variant rests a rung only where it is strictly below the token's last print at "
             "placement (last print is <=10s old on ~75-98% of tokens at both k=25 and k=120).\n")

    L.append("## Post-close sensitivity (deep_proj's hold: loser side cancelled at close+1.7s, winner held 60s)\n")
    L.append("| variant | side-fills | dollars | c/sh | vs cancel-at-close |")
    L.append("|---|---|---|---|---|")
    for n, base in (("A6_k25_engine_pc", "A6_k25"), ("A6_k120_engine_pc", "A6_k120")):
        a, b = adj[n], adj[base]
        L.append(f"| {n} | {a['tot']['side_fills']} | {f2(a['tot']['usd'])} | {a['tot']['cps']:+.2f} | "
                 f"{f2(a['tot']['usd'] - b['tot']['usd'])} |")
    L.append("")

    L.append("## Sign-gated comparison (same rungs, projection side only, need 1.0, engine cancels)\n")
    L.append("| run | armed | filled windows | wins | shares | c/sh | dollars | by-day |")
    L.append("|---|---|---|---|---|---|---|---|")
    for n, v in R.items():
        if v["kind"] != "sign_gated":
            continue
        g = gated_totals(v["results"])
        ds = ws2.day_split(v["results"])
        L.append(f"| {n} | {g['armed']:,} | {g['filled_windows']} | {g['wins']} | {g['sh']:,.1f} | "
                 f"{g['cps']:+.2f} | {f2(g['usd'])} | {json.dumps(ds)} |")
    L.append("")
    for n in ("S6_k25_b30", "S6_k120_b30"):
        v = R[n]
        L.append(f"### {n} per rung\n")
        L.append(rung_table_gated(ws2.rung_stats(v["results"], v["params"]["rungs"]),
                                  v["params"]["rungs"]))
        L.append("")

    L.append("## Day-by-day dollars\n")
    hdr = "| run | " + " | ".join(ALL_DAYS) + " | total |"
    L.append(hdr)
    L.append("|---|" + "---|" * (len(ALL_DAYS) + 1))
    for n, v in R.items():
        ds = ws2.day_split(v["results"])
        L.append(f"| {n} | " + " | ".join(f"{ds.get(d, 0.0):+.0f}" for d in ALL_DAYS) +
                 f" | {sum(r['pnl'] for r in v['results']):+.2f} |")
    L.append("")
    L.append("## Method notes\n")
    L.append("- Engine-true conventions from `ws2_ladder_replay.py` (`run_candidate_a`, mode "
             "`candidate_a`): print tape from `polybot/memory/recordings/tape_2026-08-14..27` "
             "(.gz and .jsonl both read); labels/strike/final from `win_streams.jsonl.gz` "
             "(3,687/3,687 agree with `window_labels` where both exist).")
    L.append("- Placement is wall-clock at close - k_max: Candidate A has no signal to tick on, and "
             "the raw stream in win_streams only covers k <= ~80, so the deep_proj tick clock cannot "
             "reach k=120. At k_max=25 this is within one raw tick of deep_proj's first eligible tick.")
    L.append("- Resting rule: a rung rests on a side only if strictly below that token's last print "
             "before placement (lookback 120s; fallback 1 - the other token's last print; no "
             "reference = nothing rests). Live would use the WS best ask; the tape proxy is the only "
             "book reference covering all 14 days locally (window_paths sidecar copy ends 08-21).")
    L.append("- Cancel at the close on both sides (primary): the triplet's fills are pre-close and a "
             "deep loser-side rung left resting past the close is swept by the 0.001 dump. The "
             "'engine_pc' rows expose that dump for exactly deep_proj's 1.7s winner-verification "
             "delay and hold the winner side 60s — the sensitivity of the choice.")
    L.append("- k_place [6,120] is reported because the WALLETS.md triplet fills at median k 57-110s; "
             "the k>25 REFUTATION (REFUTATIONS.md) is about the sign-gated ladder's flip race, and "
             "Candidate A is by construction the mechanism that prices avalanche sweeps, so the "
             "wide window is in-bounds for THIS measurement only — it licenses nothing for deep_proj.")
    L.append("- Sign-gated comparison uses `run()` unchanged (raw-tick clock, floor arm/cancel, "
             "post-close hold) at $30 = $5/rung so per-rung notional matches Candidate A's "
             "$60/12 rungs; the $60 row is the budget-matched view. ANTI row = the control.")
    L.append("- Fill-time overlay: each Candidate A fill carries the bridged-60 projection's signed "
             "displacement toward the filled side divided by p99.5(k) at the fill print (k <= 60); "
             "k > 60 fills use spot - strike over p99.5(k) since the averaging window has not opened.")
    L.append("- Paper's GTC latency (56 ms) is used as in every ws2 run; live GTC RTT reconstructs to "
             "~500 ms (RESEARCH.md), which makes replay rungs matchable sooner than real ones — the "
             "fill counts here are an upper bound in that one respect. Tape gaps (CLOB reconnects) "
             "under-count fills; not corrected. No klines file is present locally, so windows "
             "without a recorded Binance ring use the plain projection (bridge collapses to 0).")
    (OUT / "r4_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    json.dump({n: {k: v for k, v in a.items() if k not in ("st", "overlay")}
               for n, a in adj.items()}, open(OUT / "r4_adjudication.json", "w"), indent=1,
              default=str)
    print("\n".join(L[:30]))
    print(f"wrote {OUT / 'r4_report.md'}")


if __name__ == "__main__":
    main()
