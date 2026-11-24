"""Plot a simulated stint from mock_generator.py (README image).

    python mock_generator.py --laps 20 --puncture-lap 17 --csv stint.csv
    python tools/plot_stint.py stint.csv images/mock_stint.png
"""
import csv
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WHEELS = ("FL", "FR", "RL", "RR")
COL = {"FL": "#22d3ee", "FR": "#ff2d75", "RL": "#34d399", "RR": "#facc15"}


def main(src="stint.csv", out="images/mock_stint.png"):
    rows = list(csv.DictReader(open(src)))
    f = lambda k: [float(r[k]) for r in rows]          # noqa: E731
    lap = f("lap")

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10.5, "axes.edgecolor": "#334155",
                         "axes.labelcolor": "#cbd5e1", "xtick.color": "#94a3b8", "ytick.color": "#94a3b8",
                         "axes.facecolor": "#0b1220", "figure.facecolor": "#05070c", "grid.color": "#1f2937",
                         "text.color": "#e5e7eb", "legend.frameon": False})
    fig, ax = plt.subplots(2, 2, figsize=(13, 7.4))
    fig.suptitle("Mock generator: one simulated 20-lap stint", x=0.06, ha="left", fontsize=15, fontweight="bold")
    fig.text(0.06, 0.925, "Simulated data from mock_generator.py, not measurements. Slow puncture on FR from lap 17.",
             color="#94a3b8", fontsize=10.5)

    # (a) one lap: speed and brake pressure
    a = ax[0, 0]
    sel = [i for i, l in enumerate(lap) if 3 <= l < 4]
    x = [(lap[i] - 3) * 4.4 for i in sel]
    a.plot(x, [f("speed_kmh")[i] for i in sel], color="#e5e7eb", lw=1.6, label="speed (km/h)")
    a2 = a.twinx()
    a2.fill_between(x, [f("brake_front_bar")[i] for i in sel], color="#ff2d75", alpha=0.35, lw=0, label="front brake (bar)")
    a2.set_ylim(0, 200)
    a2.tick_params(colors="#ff2d75")
    a.set_xlabel("distance along lap 3 (km)")
    a.set_ylabel("speed (km/h)")
    a.set_title("Speed and braking over one lap", loc="left", color="#e5e7eb")
    a.legend(loc="lower left")
    a2.legend(loc="lower right")
    a.grid(True)

    # (b) brake disc temperature, laps 3-5
    b = ax[0, 1]
    sel = [i for i, l in enumerate(lap) if 3 <= l < 5]
    b.plot([lap[i] for i in sel], [f("disc_front_c")[i] for i in sel], color="#fb923c", lw=1.3, label="front discs")
    b.plot([lap[i] for i in sel], [f("disc_rear_c")[i] for i in sel], color="#a78bfa", lw=1.3, label="rear discs")
    b.axhline(950, color="#ff2d75", ls=":", lw=1)
    b.text(3.02, 965, "alert 950 °C", color="#ff2d75", fontsize=9)
    b.set_ylim(0, 1100)
    b.set_xlabel("lap")
    b.set_ylabel("°C")
    b.set_title("Brake discs: heat under braking, cool on straights", loc="left", color="#e5e7eb")
    b.legend(loc="lower right")
    b.grid(True)

    # (c) tyre wear
    c = ax[1, 0]
    for w in WHEELS:
        c.plot(lap, f(f"{w}_wear_pct"), color=COL[w], lw=2, label=w)
    for y, txt, col in ((75, "HIGH 75 %", "#facc15"), (90, "CRITICAL 90 %", "#ff2d75")):
        c.axhline(y, color=col, ls=":", lw=1)
        c.text(1.2, y + 1.5, txt, color=col, fontsize=9)
    c.set_xlabel("lap")
    c.set_ylabel("% used")
    c.set_ylim(0, 100)
    c.set_title("Tyre wear: linear per lap, faster on the more loaded tyres", loc="left", color="#e5e7eb")
    c.legend(ncol=4, loc="lower right")
    c.grid(True)

    # (d) tyre pressure, 1 s means
    d = ax[1, 1]
    for w in WHEELS:
        d.plot(lap, f(f"{w}_pressure_psi"), color=COL[w], lw=1.4, label=w)
    d.axhline(18, color="#ff2d75", ls=":", lw=1)
    d.text(1.2, 18.4, "LOW alert 18 psi", color="#ff2d75", fontsize=9)
    d.set_xlabel("lap")
    d.set_ylabel("psi")
    d.set_ylim(10, 26)
    d.set_title("Tyre pressure: rises with temperature, then a slow puncture", loc="left", color="#e5e7eb")
    d.legend(ncol=4, loc="lower left")
    d.grid(True)

    fig.tight_layout(rect=(0.02, 0, 1, 0.925))
    fig.savefig(out, dpi=130)
    print(out)


if __name__ == "__main__":
    main(*sys.argv[1:])
