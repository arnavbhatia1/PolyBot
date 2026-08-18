"""Freeze the 60s-rule margin tables from ws1_errors60.csv.

Convention (matches the original 08-07 freeze): P995 = fitted p99.5 rounded UP
to the next $0.5, monotone-enforced in k; MAX = worst observed rounded UP to
the next $1, monotone-enforced. Binding population: REAL finals (08-14+),
stall-veto passing, coverage-gated (boundary-inclusive raw gap <= 10s).
MAX additionally takes the union with the synthetic-target corpus (its target
noise <= $4.3 p995 only UNDERSTATES low-k error; at high k its violent-window
maxes are real observations of the same estimator).
Also prints the ladder-floor translation (2 x p995) old vs new.
"""
import csv
import math
from pathlib import Path

SP = Path(__file__).parent
KNOTS = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 29.0,
         35.0, 40.0, 45.0, 50.0, 55.0, 58.0]
OLD_P995 = {2.0: 0.6, 4.0: 1.6, 6.0: 4.5, 8.0: 6.5, 10.0: 11.0,
            12.0: 11.5, 15.0: 14.0, 20.0: 23.0, 25.0: 26.0, 29.0: 32.0}


def main():
    rows = [r for r in csv.DictReader(open(SP / "data" / "ws1_errors60.csv"))
            if r["veto"] == "0" and float(r["gap"]) <= 10.0 and r["plain"]]
    for r in rows:
        r["k"] = float(r["k"])
        r["err"] = abs(float(r["final"]) - float(r["plain"]))

    def fit(pop):
        p995 = {}
        mx = {}
        for k in KNOTS:
            xs = sorted(r["err"] for r in pop if r["k"] == k)
            if len(xs) < 50:
                continue
            p995[k] = xs[min(math.ceil(0.995 * len(xs)) - 1, len(xs) - 1)]
            mx[k] = xs[-1]
        return p995, mx

    real = [r for r in rows if r["src"] == "real"]
    p995_r, max_r = fit(real)
    p995_a, max_a = fit(rows)

    # freeze: p995 from real finals; max = union of both populations
    def up(x, step):
        return math.ceil(x / step) * step

    frozen_p, frozen_m = {}, {}
    prev_p = prev_m = 0.0
    for k in KNOTS:
        if k not in p995_r:
            continue
        p = up(p995_r[k], 0.5)
        m = up(max(max_r[k], max_a.get(k, 0.0)), 1.0)
        p = max(p, prev_p)
        m = max(m, prev_m, p + 0.5)      # max knot never below p995
        frozen_p[k], frozen_m[k] = p, m
        prev_p, prev_m = p, m

    print("# 60s-rule margin tables — freeze 2026-08-18")
    print(f"# corpus: {len({r['ep'] for r in real})} real-final windows (08-14..17)"
          f" + {len({r['ep'] for r in rows})-len({r['ep'] for r in real})} synthetic"
          f" (max-union only); estimator plain-60 rx-clock ZOH + coverage guard")
    print("TWAP60_MARGIN_P995 = (")
    print("    " + ", ".join(f"({k:.0f}.0, {frozen_p[k]:.1f})" for k in KNOTS if k in frozen_p) + ",")
    print(")")
    print("TWAP60_MARGIN_MAX = (")
    print("    " + ", ".join(f"({k:.0f}.0, {frozen_m[k]:.1f})" for k in KNOTS if k in frozen_m) + ",")
    print(")")

    print("\nfit detail: k  p995_real  p995_all  max_real  max_all  -> frozen p995/max")
    for k in KNOTS:
        if k in frozen_p:
            print(f"k={k:4.1f}  {p995_r[k]:7.2f}  {p995_a.get(k, float('nan')):7.2f}  "
                  f"{max_r[k]:7.2f}  {max_a.get(k, float('nan')):7.2f}   -> "
                  f"{frozen_p[k]:5.1f} / {frozen_m[k]:5.1f}")

    print("\nladder floor 2 x p995: k, old-30s-rule floor -> new-60s floor")
    for k in (6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0):
        old = 2 * OLD_P995[k]
        print(f"k={k:4.1f}  ${old:5.1f}  ->  ${2 * frozen_p[k]:5.1f}")


if __name__ == "__main__":
    main()
