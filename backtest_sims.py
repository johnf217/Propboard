"""
backtest_sims.py  v1
Replays past weeks through build_sims.py and grades the sims against what actually happened.

For every completed week (2025 week 5 on, plus this season), it:
  - cuts every player's game log off before that week (the sim only sees what it would have known),
  - uses nflverse's closing spread and total for each game,
  - uses who actually suited up that week as the injury report (books know the inactives too),
  - simulates every game 10,000 times, then grades each player's real stat line against his sims.

What it checks
  1. Bias        - real average / sim average. 1.00 = right on; below 1 = sims run hot.
  2. Where real results landed inside the sims (the honest calibration test):
                   "pct below"  - how often the real result came in under the sim median (50% ideal)
                   "mid 50%"    - how often it landed between the 25th and 75th percentile (50% ideal)
                   "mid 80%"    - how often it landed between the 10th and 90th percentile (80% ideal)
  3. Practice lines - each player's own last-8 median (a stand-in for a book line), graded:
                   sims' chance of the over vs what happened (Brier score, lower = better),
                   against two simple baselines: always 50%, and his last-10 hit rate.
  4. Anytime TD  - sims' TD chance vs whether he scored, same scoring.
  5. Real lines  - every pick in picks.json from a replayed week, priced by the sims at the actual
                   posted line, compared with the Prop Board model's chance (pm) on the same picks.

Usage (Actions or a laptop with internet):
    pip install numpy
    python backtest_sims.py                 # all weeks
    python backtest_sims.py 2026            # one season
Reads: player_logs.json, picks.json (optional), nflverse games.csv (downloaded, or games.csv next to it)
Writes: backtest.json (summary the app can show) and prints a report.
"""
import json, sys, os, csv, io, copy, urllib.request
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_sims as B

GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
MARKETS = {  # market -> (stat key, positions, min prior average of the stat to count as a "prop guy")
    "pass_yds": ("py", {"QB"}, 150), "rush_yds": ("ry", {"QB", "RB"}, 20), "rec_yds": ("rey", {"WR", "TE", "RB"}, 15),
    "rec": ("rec", {"WR", "TE", "RB"}, 1.5), "carries": ("ca", {"RB"}, 6), "targets": ("tgt", {"WR", "TE", "RB"}, 3), "atd": ("td", {"RB", "WR", "TE", "QB"}, None)}


def load_games():
    if os.path.exists("games.csv"):
        txt = open("games.csv", encoding="utf-8").read()
    else:
        txt = urllib.request.urlopen(GAMES_URL, timeout=60).read().decode("utf-8")
    return [r for r in csv.DictReader(io.StringIO(txt)) if r["game_type"] == "REG" and r["home_score"] not in ("", "NA")]


def actual(g, k):
    if k == "td": return g.get("retd", 0) + g.get("rtd", 0)
    return g.get(k, 0)


def main():
    data = B.load("player_logs.json")
    if not data: print("missing player_logs.json"); return
    picks = (B.load("picks.json", {}) or {}).get("picks", {})
    only = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    for a in sys.argv[1:]:                       # try settings without editing build_sims.py, e.g. GAME_YPC_SD=.2
        if "=" in a: k, v = a.split("="); setattr(B, k, type(getattr(B, k))(float(v)) if not isinstance(getattr(B, k), tuple) else tuple(float(x) for x in v.split(",")))
    games = load_games()
    seasons = sorted({g["s"] for p in data["players"] for g in p["logs"]})
    weeks = sorted({(int(r["season"]), int(r["week"])) for r in games if int(r["season"]) in seasons})
    weeks = [w for w in weeks if (w[0] > seasons[0] or w[1] >= 5) and (only is None or w[0] == only)]
    have = {(g["s"], g["w"]) for p in data["players"] for g in p["logs"]}
    weeks = [w for w in weeks if w in have]
    for p in data["players"]: p["logs"].sort(key=lambda g: (g["s"], g["w"]))

    rows = []        # one per graded player-stat
    real = []        # picks at real lines
    pk_by = defaultdict(list)
    for v in picks.values():
        if v.get("pid") and v.get("kick"): pk_by[v["pid"]].append(v)

    for (s, w) in weeks:
        wk = [r for r in games if int(r["season"]) == s and int(r["week"]) == w]
        opp = {}
        for r in wk: opp[r["home_team"]] = r["away_team"]; opp[r["away_team"]] = r["home_team"]
        players, played = [], {}
        for p in data["players"]:
            prior = [g for g in p["logs"] if (g["s"], g["w"]) < (s, w)]
            now = next((g for g in p["logs"] if (g["s"], g["w"]) == (s, w)), None)
            team = opp.get(now["o"]) if now and now.get("o") in opp else None
            q = {"id": p["id"], "name": p["name"], "pos": p["pos"], "team": team, "logs": prior}
            players.append(q)
            if team and len(prior) >= 3: played[p["id"]] = (q, now)
        props = {"games": [], "props": []}
        ctx = {}
        for r in wk:
            label = f'{r["away_team"]} @ {r["home_team"]}'
            props["games"].append({"label": label, "kick": f'{r["gameday"]}T{r["gametime"] or "13:00"}'})
            try: ctx[label] = {"spread_home": -float(r["spread_line"]), "total": float(r["total_line"])}
            except ValueError: pass
        for pid, (q, now) in played.items():
            for mk, (k, pos, _) in MARKETS.items():
                if q["pos"] in pos: props["props"].append({"pid": pid, "market": mk, "line": .5})
        raw = {}
        B.simulate({"players": players}, props, ctx, {}, raw=raw, quiet=True)

        for pid, (q, now) in played.items():
            if pid not in raw: continue
            pos, st, label = raw[pid]
            av = st["_av"] if st["_av"].any() else np.ones(len(st["rec"]), bool)
            L8 = q["logs"][-8:]; L10 = q["logs"][-10:]
            for mk, (k, poss, floor) in MARKETS.items():
                if pos not in poss or k not in st: continue
                prior_avg = float(np.mean([actual(g, k) for g in L8]))
                if floor is not None and prior_avg < floor: continue
                v = st[k][av]; a = actual(now, k)
                row = {"s": s, "w": w, "pid": pid, "pos": pos, "m": mk, "a": a, "mean": float(v.mean())}
                if mk == "atd":
                    row.update(p=float((v > .5).mean()), hit=int(a > .5),
                               base=float(np.mean([actual(g, "td") > 0 for g in L10])))
                else:
                    row["pit"] = float((v < a).mean() + .5 * (v == a).mean())
                    q10, q25, q50, q75, q90 = np.percentile(v, [10, 25, 50, 75, 90])
                    row.update(in50=int(q25 <= a <= q75), in80=int(q10 <= a <= q90), below=int(a < q50))
                    line = np.floor(np.median([actual(g, k) for g in L8])) + .5      # practice line
                    row.update(line=float(line), p=float((v > line).mean()), hit=int(a > line),
                               base=float(np.mean([actual(g, k) > line for g in L10])))
                rows.append(row)
            # real posted lines from Prop Board's saved picks in this week
            for pk in pk_by.get(pid, []):
                if pk["kick"][:10] < wk[0]["gameday"] or pk["kick"][:10] > max(r["gameday"] for r in wk): continue
                k = B.MKT.get(pk.get("m"))
                if k not in st or pk.get("l") is None: continue
                v = st[k][av]; a = actual(now, k) if k in ("py", "ry", "rey", "rec", "td") else now.get(k, 0)
                if a == pk["l"]: continue                                 # push
                over = float((v > pk["l"]).mean()); side_over = pk.get("sd") == "over"
                real.append({"m": pk["m"], "sim": over if side_over else 1 - over, "pm": pk.get("pm"), "pr": pk.get("pr"),
                             "won": int((a > pk["l"]) == side_over), "top": pk.get("top", 0)})
        print(f"{s} week {w:>2}: {len(wk)} games, {sum(1 for r in rows if r['s']==s and r['w']==w)} player stats graded")

    report(rows, real, weeks)


def brier(ps, ys): return float(np.mean([(p - y) ** 2 for p, y in zip(ps, ys)])) if ps else None


def report(rows, real, weeks):
    out = {"weeks": [f"{s}-{w}" for s, w in weeks], "markets": {}, "real": {}}
    print("\n=== Calibration by market (ideal: bias 1.00, below 50%, mid50 50%, mid80 80%) ===")
    print(f"{'market':9s} {'n':>5s} {'bias':>6s} {'below':>6s} {'mid50':>6s} {'mid80':>6s} | {'Brier sims':>10s} {'50/50':>6s} {'L10':>6s}  sims' avg over% vs real over%")
    for mk in MARKETS:
        R = [r for r in rows if r["m"] == mk]
        if not R: continue
        bias = sum(r["a"] for r in R) / max(1e-9, sum(r["mean"] for r in R))
        ys = [r["hit"] for r in R]
        b_sim = brier([r["p"] for r in R], ys); b_50 = brier([.5] * len(R), ys) if mk != "atd" else brier([np.mean(ys)] * len(R), ys)
        b_l10 = brier([min(max(r["base"], .03), .97) for r in R], ys)
        m = {"n": len(R), "bias": round(bias, 3), "brier": round(b_sim, 4), "brier_flat": round(b_50, 4), "brier_l10": round(b_l10, 4),
             "avg_p": round(float(np.mean([r["p"] for r in R])), 3), "hit": round(float(np.mean(ys)), 3)}
        if mk != "atd":
            m.update(below=round(float(np.mean([r["below"] for r in R])), 3), in50=round(float(np.mean([r["in50"] for r in R])), 3),
                     in80=round(float(np.mean([r["in80"] for r in R])), 3))
        # calibration buckets: sims said X%, it happened Y%
        cal = []
        for lo in np.arange(0, 1, .1):
            B_ = [r for r in R if lo <= r["p"] < lo + .1]
            if len(B_) >= 15: cal.append([round(lo + .05, 2), round(float(np.mean([r["hit"] for r in B_])), 3), len(B_)])
        m["cal"] = cal
        out["markets"][mk] = m
        fmt = lambda x: f"{x:6.1%}" if x is not None else "     -"
        print(f"{mk:9s} {len(R):5d} {bias:6.2f} {fmt(m.get('below'))} {fmt(m.get('in50'))} {fmt(m.get('in80'))} | {b_sim:10.4f} {b_50:6.4f} {b_l10:6.4f}  {m['avg_p']:.1%} vs {m['hit']:.1%}")
    print("\n=== Sims said X% / it happened Y% (n) ===")
    for mk, m in out["markets"].items():
        print(f"{mk:9s} " + "  ".join(f"{int(a*100)}%->{b:.0%}({n})" for a, b, n in m["cal"]))
    for pos in ("QB", "RB", "WR", "TE"):
        R = [r for r in rows if r["pos"] == pos and r["m"] == "atd"]
        if R: print(f"atd {pos}: sims {np.mean([r['p'] for r in R]):.1%} vs scored {np.mean([r['hit'] for r in R]):.1%} (n={len(R)})")
    if real:
        print("\n=== Real posted lines (Prop Board picks in replayed weeks) ===")
        R = [r for r in real if r["pm"] is not None]
        ys = [r["won"] for r in R]
        out["real"] = {"n": len(R), "won": round(float(np.mean(ys)), 3), "brier_sims": round(brier([r["sim"] for r in R], ys), 4),
                       "brier_model": round(brier([r["pm"] for r in R], ys), 4)}
        print(f"n={len(R)}  picks won {np.mean(ys):.1%}  Brier sims {out['real']['brier_sims']:.4f}  vs model {out['real']['brier_model']:.4f}  (lower wins)")
        # betting result, 1 unit per pick at the posted price, split by what the sims thought of the price
        def dec(a): return 1 + (a / 100 if a > 0 else 100 / -a)
        P = [r for r in R if r.get("pr")]
        for r in P: r["edge"] = r["sim"] * dec(r["pr"]) - 1
        for lo, hi, name in ((.05, 9, "sims edge +5% or more"), (0, .05, "sims edge 0 to +5%"), (-9, 0, "sims say no value")):
            S = [r for r in P if lo <= r["edge"] < hi]
            if S:
                roi = float(np.mean([(dec(r["pr"]) - 1) if r["won"] else -1 for r in S]))
                out["real"][name] = {"n": len(S), "won": round(float(np.mean([r["won"] for r in S])), 3), "roi": round(roi, 3)}
                print(f"  {name:22s} n={len(S):4d}  won {np.mean([r['won'] for r in S]):5.1%}  ROI {roi:+.1%}")
        for mk in sorted({r["m"] for r in R}):
            S = [r for r in R if r["m"] == mk]; y = [r["won"] for r in S]
            print(f"  {mk:9s} n={len(S):4d}  sims {brier([r['sim'] for r in S], y):.4f}  model {brier([r['pm'] for r in S], y):.4f}")
    json.dump(out, open("backtest.json", "w"), separators=(",", ":"))
    print("\nwrote backtest.json")


if __name__ == "__main__":
    main()
