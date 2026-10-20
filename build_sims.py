"""
build_sims.py  v7 (tuned by backtest on 2025 wk5 - 2026 wk5, 2026-10-15)
Writes sims.json: every game this week played 10,000 times as full box scores.

How one simulated game works (all draws happen together, so props inside a game move together):
  1. Game script  - each team's expected points come from the Vegas spread and total (the market's read
                    on the game), then the game is scored like football: touchdowns, field goals, extra
                    points and overtime, so margins land on 3s and 7s and alt lines price correctly.
  2. Volume       - each team's pass attempts and carries come from its recent averages, bent by the
                    script: teams that trail throw more, teams that lead run more, high totals mean more plays.
  3. Who gets it  - targets and carries are split by each player's recent share (recency-weighted and
                    shrunk toward his position when the sample is small). Injured players are removed and
                    their share flows to everyone else; Questionable players sit out 20% of sims (but his
                    own props are priced as if he plays, since books void them if he sits).
  4. What he does - catches, yards per catch, yards per carry are drawn from each player's own rates
                    (shrunk toward league norms) adjusted for the opponent's defense. Yards use skewed
                    draws, so big games and duds show up at realistic rates (alt lines need this).
  5. Touchdowns   - the team's touchdowns follow from its simulated points, split pass/rush by team
                    tendency and handed out by each player's volume that game times his scoring rate.
QB passing yards and completions are the sum of his receivers' catches, so stacks are consistent.

Backtest: python backtest_sims.py (replays past weeks; see that file).
Reads: player_logs.json, props.json, context.json (spread/total; optional), injuries.json (optional).
Writes: sims.json + sims_box.json + sim_lines.json (projected box scores, kept after kickoff for sims vs actual)
        (the 1,000 stored box scores, loaded only when needed)  -  per-prop chances from all 10,000 sims, plus 1,000 stored box scores per game
                      so the app can price any parlay or "what if" question on the phone.
Needs: pip install numpy
"""
import json, math
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

N, NS = 10_000, 1_000            # sims per game, sims stored for parlay questions
SHARE_CONC = (200, 50)            # how much a player's target / carry share swings game to game (higher = steadier)
CATCH_SHAPE = 3.0                # yards per catch spread (gamma shape per catch; real WR catches ~ CV 0.8)
RUSH_SHAPE = .5                  # yards per carry spread (gamma shape per carry)
GAME_YPA_SD, GAME_YPC_SD = .22, .25   # whole-offense good/bad day: one shock shared by every receiver / runner
OTHER_MIN = (0.0, 0.0)           # floor for the "everyone else" bucket (targets, carries)
DRIVES = 9                       # scoring chances per team (drive model gives real 3s and 7s)
TD_PTS_SHARE, FG_PTS_SHARE = .80, .19   # where points come from (tuned so anytime-TD chances match 2025-26)
GAME_SHOCK = .05                 # shared pace / weather shock, so both teams' scores move together a little
                                 # (9 drives + .05: final margins and totals spread like 2018-26 games around the closing line)
# how much more (or less) often real NFL games end on each margin than raw drive-by-drive scoring gives
# (2015-26 regular season, shrunk 20% toward 1). Whole simulated games are resampled with these weights,
# so 3s and 7s show up as often as they really do and every player's box score stays consistent.
MARGIN_W = {0: 1.04, 1: .93, 2: 1.1, 3: 1.8, 4: .85, 5: 1.16, 6: 1.14, 7: 1.5, 8: 1.07, 9: .55, 10: .91, 11: .59,
            12: .74, 13: .59, 14: 1.14, 15: .73, 16: .86, 17: 1.0, 18: .9, 19: .69, 20: .86}
OTHER_TD = (0.0, 0.0)            # TD rate per target / carry for the "everyone else" bucket
OFF_TD = .97                     # share of touchdowns scored by the offense (rest: defense / special teams)
DEF_STRENGTH = {"rec": 1.0, "rush": 0.0}   # how hard the opponent's yards-allowed record bends yards per catch / carry
                                 # (backtest: helps receiving, hurts rushing, so rushing ignores it)
TD_POW = .9                      # <1 spreads touchdowns more evenly (softens the top guys)
rng = np.random.default_rng()

POS_PRIOR = {  # catch rate, yards/catch, rec TD per target, yards/carry, rush TD per carry
    "WR": (.64, 12.8, .050, 6.0, .02), "TE": (.70, 10.8, .060, 4.0, .02),
    "RB": (.77, 7.8, .030, 4.2, .030), "QB": (.60, 9.0, .000, 5.0, .045)}
STATS = {"QB": ["py", "ptd", "pa", "ry", "ca", "td"],
         "RB": ["ry", "ca", "rec", "rey", "td"],
         "WR": ["rec", "rey", "ry", "td"], "TE": ["rec", "rey", "td"]}
MKT = {"pass_yds": "py", "pass_td": "ptd", "pass_att": "pa", "rush_yds": "ry", "carries": "ca",
       "rec": "rec", "rec_yds": "rey", "rr_yds": "rr", "atd": "td"}
OUT = {"Out", "IR", "Doubtful"}

def load(name, default=None):
    try: return json.load(open(name))
    except Exception: return default

def recency(logs, n=12):
    """most recent n games with weights: newer games count more, last season counts less"""
    L = logs[-n:]; cur = max((g["s"] for g in logs), default=0)
    return [(g, (0.86 ** (len(L) - 1 - i)) * (1 if g["s"] == cur else .7)) for i, g in enumerate(L)]

def shrink(num, den, prior, k): return (num + prior * k) / (den + k) if den + k > 0 else prior

def main():
    data, props = load("player_logs.json"), load("props.json")
    if not data or not props or not props.get("props"):
        print("missing player_logs.json or props.json; skipping sims"); return
    ctx = (load("context.json", {}) or {}).get("games", {})
    inj = (load("injuries.json", {}) or {}).get("players", {})
    out = simulate(data, props, ctx, inj)
    # the stored box scores (most of the size) go in their own file, which the app only loads for the Sims tab,
    # parlays and questions; sims.json keeps each player's game and team so everything else works without it
    box = {"updated": out["updated"], "players": {pid: {"s": v["s"]} for pid, v in out["players"].items()}}
    light = dict(out, players={pid: {"g": v["g"], "t": v["t"]} for pid, v in out["players"].items()})
    light.pop("lines", None)
    save_lines(out["lines"], out["updated"])
    json.dump(light, open("sims.json", "w"), separators=(",", ":"))
    json.dump(box, open("sims_box.json", "w"), separators=(",", ":"))
    print(f"wrote sims.json: {len(out['games'])} games, {len(out['props'])} props, {len(out['players'])} players")

def save_lines(new, updated, path="sim_lines.json", keep_weeks=20):
    """sim_lines.json: the projected box score for every game. A game's projection is locked at kickoff and kept,
    so the app can show sims vs actual after the game. Upcoming games are replaced on every run."""
    now = datetime.now(timezone.utc)
    started = lambda g: bool(g.get("kick")) and datetime.fromisoformat(g["kick"].replace("Z", "+00:00")) <= now
    old = (load(path, {}) or {}).get("games", {})
    games = {k: g for k, g in old.items() if started(g)}                     # locked
    for label, g in new.items():
        if not started(g): games[f'{label}|{(g.get("kick") or "")[:10]}'] = g
    cutoff = now.timestamp() - keep_weeks * 7 * 86400
    games = {k: g for k, g in games.items() if not g.get("kick") or datetime.fromisoformat(g["kick"].replace("Z", "+00:00")).timestamp() > cutoff}
    json.dump({"updated": updated, "games": games}, open(path, "w"), separators=(",", ":"))
    print(f"wrote {path}: {len(games)} games ({sum(1 for g in games.values() if not started(g))} upcoming)")

def score_game(total, spread_home, home, away, n=None):
    """N final scores scored like football (TDs, FGs, extra points, overtime), resampled so 3s and 7s come up as
    often as in real games, then re-centred so the average margin and total match the spread and total."""
    n = n or N
    mu = {home: max((total - spread_home) / 2, 3), away: max((total + spread_home) / 2, 3)}
    for it in range(3):      # the key-number resampling nudges the averages; two corrections put them back on the line
        shock = np.exp(rng.normal(-GAME_SHOCK ** 2 / 2, GAME_SHOCK, n))
        pts, tds = {}, {}
        for t in (home, away):
            ptd = np.clip(TD_PTS_SHARE * mu[t] / (6.99 * DRIVES) * shock, 0, .6)
            pfg = np.clip(FG_PTS_SHARE * mu[t] / (3 * DRIVES) * shock, 0, .35)
            td = rng.binomial(DRIVES, ptd); fg = rng.binomial(DRIVES - td, pfg / np.maximum(1 - ptd, .05))
            pts[t] = 6 * td + rng.binomial(td, .91) + 2 * rng.binomial(td, .04) + 3 * fg + 2 * (rng.random(n) < .03)
            tds[t] = td
        tie = pts[home] == pts[away]
        if tie.any():   # overtime: field goal most often, sometimes a touchdown, rarely still tied
            k = int(tie.sum()); hwin = rng.random(k) < np.clip(.5 - spread_home * .03, .15, .85)
            r = rng.random(k); add = np.where(r < .65, 3, np.where(r < .9, 6, 0))
            ot_td = (add == 6).astype(int); idx = np.flatnonzero(tie)
            pts[home][idx] += np.where(hwin, add, 0); pts[away][idx] += np.where(hwin, 0, add)
            tds[home][idx] += np.where(hwin, ot_td, 0); tds[away][idx] += np.where(hwin, 0, ot_td)
        w = np.vectorize(lambda m: MARGIN_W.get(abs(int(m)), 1.0))(pts[home] - pts[away])
        pick = rng.choice(n, n, p=w / w.sum())
        pts = {t: v[pick].astype(float) for t, v in pts.items()}; tds = {t: v[pick] for t, v in tds.items()}
        em = (pts[home] - pts[away]).mean() + spread_home; et = (pts[home] + pts[away]).mean() - total
        if it == 2 or (abs(em) < .15 and abs(et) < .3): break
        mu[home] = max(mu[home] - em / 2 - et / 2, 2); mu[away] = max(mu[away] + em / 2 - et / 2, 2)
    return pts, tds

def simulate(data, props, ctx, inj, raw=None, quiet=False):
    """raw: optional dict filled with pid -> (pos, {stat: all N sims, "_av": played mask}) for backtests"""
    players = data["players"]
    for p in players: p["logs"].sort(key=lambda g: (g["s"], g["w"]))
    by_team = defaultdict(list)
    for p in players:
        if p.get("team"): by_team[p["team"]].append(p)
    season = max(g["s"] for p in players for g in p["logs"])
    last_wk = max(g["s"] * 100 + g["w"] for p in players for g in p["logs"])   # newest game in the logs (actuals come after it)
    prop_pids = {q.get("pid") for q in props["props"]}
    pass_prop = {q.get("pid") for q in props["props"] if q.get("market") in ("pass_yds", "pass_td", "pass_att")}  # books only post these for the starter

    # ---- defense: yards allowed to each position vs league, shrunk (recent two seasons) ----
    allow = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # opp -> pos -> (s,w) -> yds
    for p in players:
        for g in p["logs"]:
            if g["s"] < season - 1 or not g.get("o"): continue
            allow[g["o"]][p["pos"]][(g["s"], g["w"], "rec")] += g.get("rey", 0)
            allow[g["o"]][p["pos"]][(g["s"], g["w"], "rush")] += g.get("ry", 0)
    def dmult(opp, pos, kind):
        vals = {o: [v for (s, w, k), v in d[pos].items() if k == kind] for o, d in allow.items()}
        lg = [x for v in vals.values() for x in v]
        mine = vals.get(opp, [])
        if not lg or not mine: return 1.0
        r = (sum(mine) / len(mine)) / max(1e-6, sum(lg) / len(lg))
        return float(np.clip(1 + DEF_STRENGTH[kind] * (r - 1) * len(mine) / (len(mine) + 8), .82, 1.18))

    # ---- team baselines from the current roster's game logs ----
    # every team-game keyed by (season, week, opponent): a player's log against X in week w belongs to the team that
    # played X that week, so traded players and departed QBs still count toward the right team's volume
    games_all = defaultdict(lambda: defaultdict(float))
    for p in players:
        for g in p["logs"]:
            if g.get("o"):
                for k in ("pa", "ca", "ptd", "rtd"): games_all[(g["s"], g["w"], g["o"])][k] += g.get(k, 0)

    def team_model(team):
        roster = by_team.get(team, [])
        cnt = defaultdict(lambda: defaultdict(int))
        for p in roster:
            for g in p["logs"]:
                if g.get("o"): cnt[(g["s"], g["w"])][g["o"]] += 1
        mine = {(s, w, max(c, key=c.get)) for (s, w), c in cnt.items() if max(c.values()) >= 2}
        tw = sorted(k for k in mine if games_all[k]["pa"] >= 12)[-10:]
        if not tw: return None
        wts = {k: (.88 ** (len(tw) - 1 - i)) * (1 if k[0] == season else .7) for i, k in enumerate(tw)}
        W = sum(wts.values())
        pa = sum(games_all[k]["pa"] * w for k, w in wts.items()) / W
        ra = sum(games_all[k]["ca"] * w for k, w in wts.items()) / W
        ptd = sum(games_all[k]["ptd"] * w for k, w in wts.items()); rtd = sum(games_all[k]["rtd"] * w for k, w in wts.items())
        pass_td_share = shrink(ptd, ptd + rtd, .6, 6)
        recent = {k[:2] for k in tw[-4:]}   # only players who've played in the team's last 4 games (or have a prop) take part
        active = [p for p in roster if p["id"] in prop_pids or any((g["s"], g["w"]) in recent for g in p["logs"])]
        return {"roster": active, "weeks": games_all, "tw": set(tw), "pa": pa, "ra": ra, "pts": pass_td_share}

    def player_model(p, team, opp):
        pos = p["pos"]; cr0, ypr0, rtd0, ypc0, rutd0 = POS_PRIOR.get(pos, POS_PRIOR["WR"])
        R = recency(p["logs"])
        sw = sum(w for _, w in R) or 1
        tgt = sum(g.get("tgt", 0) * w for g, w in R); rec = sum(g.get("rec", 0) * w for g, w in R)
        rey = sum(g.get("rey", 0) * w for g, w in R); ca = sum(g.get("ca", 0) * w for g, w in R)
        ry = sum(g.get("ry", 0) * w for g, w in R); retd = sum(g.get("retd", 0) * w for g, w in R)
        rutd = sum(g.get("rtd", 0) * w for g, w in R); pa = sum(g.get("pa", 0) * w for g, w in R)
        # shares of the team's volume in games he played
        tden = sum(team["weeks"][(g["s"], g["w"], g.get("o"))]["pa"] * .96 * w for g, w in R if (g["s"], g["w"], g.get("o")) in team["weeks"]) or 1
        cden = sum(team["weeks"][(g["s"], g["w"], g.get("o"))]["ca"] * w for g, w in R if (g["s"], g["w"], g.get("o")) in team["weeks"]) or 1
        games = len(R)
        tsh = shrink(tgt, tden, .03, tden / max(games, 1) * .25) if pos != "QB" else 0   # light pull toward a bit-part role
        csh = shrink(ca, cden, .03, cden / max(games, 1) * .25)
        return {"p": p, "pos": pos, "tsh": max(tsh, 0), "csh": max(csh, 0), "pa_w": pa / sw,
                "cr": shrink(rec, tgt, cr0, 20), "ypr": shrink(rey, rec, ypr0, 15) * dmult(opp, pos, "rec"),
                "ypc": shrink(ry, ca, ypc0, 30) * dmult(opp, pos, "rush"),
                "rtdr": shrink(retd, tgt, rtd0, 40), "rutdr": shrink(rutd, ca, rutd0, 40),
                "avail": 0.0 if (inj.get(p["id"], {}).get("st") in OUT) else .8 if inj.get(p["id"], {}).get("st") == "Questionable" else 1.0}

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": N, "ns": NS,
           "games": {}, "players": {}, "props": {}, "lines": {}}

    for gm in props.get("games", []):
        label = gm.get("label", "")
        if " @ " not in label: continue
        away, home = [x.strip() for x in label.split(" @ ")]
        c = ctx.get(label, {})
        total = float(c.get("total") or 44.5); spread_home = float(c.get("spread_home") if c.get("spread_home") is not None else -1.5)
        TM = {t: team_model(t) for t in (home, away)}
        if not all(TM.values()):
            if not quiet: print("not enough data for", label)
            continue

        # 1. game script, anchored to the market: each team's expected points from the spread and total,
        #    then real football scoring (touchdowns and field goals drive by drive, extra points, overtime)
        #    so final margins land on 3s and 7s like real games
        pts, tds = score_game(total, spread_home, home, away)
        tot = pts[home] + pts[away]
        res = {}
        for team, opp in ((home, away), (away, home)):
            T = TM[team]; tm = pts[team] - pts[opp]
            # 2. volume bent by game script and pace
            pa = np.maximum(rng.normal(T["pa"] * (1 - .011 * tm) * (total / 44.5) ** .35, 4.5), 14)
            ra = np.maximum(rng.normal(T["ra"] * (1 + .013 * tm) * (total / 44.5) ** .25, 4.5), 8)
            M = [player_model(p, T, opp) for p in T["roster"] if p["logs"]]
            M = [m for m in M if m["avail"] > 0]
            qbs = sorted([m for m in M if m["pos"] == "QB"], key=lambda m: (m["p"]["id"] not in pass_prop, -m["pa_w"]))
            starter = qbs[0] if qbs else None
            for m in qbs[1:]: m["csh"] = 0                       # backups don't run in the sim
            # 3. availability per sim, then volume shares (with an "everyone else" bucket)
            av = np.stack([rng.random(N) < m["avail"] for m in M], 1).astype(float) if M else np.zeros((N, 0))
            def split(key, conc, other_min):
                base = np.array([m[key] for m in M] + [0.0]); base[-1] = max(other_min, 1 - base[:-1].sum())
                ex = base.sum() - 1
                if ex > 0 and len(M):
                    # shares measured "when he played" overlap (one guy's big games came while another sat).
                    # take the overlap mostly out of the secondary roles, not the lead guys
                    s = base[:-1]; w = s * (1 - s / (s.max() + 1e-9)) + .15 * s
                    cut = np.minimum(ex * w / w.sum(), .6 * s); base[:-1] = s - cut
                base = base / base.sum()
                g = rng.gamma(np.maximum(base * conc, 1e-3), size=(N, len(base)))
                g[:, :-1] *= av
                return g / g.sum(1, keepdims=True)
            tsh, csh = split("tsh", SHARE_CONC[0], OTHER_MIN[0]), split("csh", SHARE_CONC[1], OTHER_MIN[1])
            tg = rng.poisson(pa[:, None] * .96 * tsh)
            cr = np.array([m["cr"] for m in M] + [.66]); ypr = np.array([m["ypr"] for m in M] + [10.5])
            rec = rng.binomial(tg, cr)
            gy = np.exp(rng.normal(-GAME_YPA_SD ** 2 / 2, GAME_YPA_SD, N))[:, None] if GAME_YPA_SD else 1.0
            rey = np.where(rec > 0, rng.gamma(np.maximum(CATCH_SHAPE * rec, 1e-6)) / CATCH_SHAPE, 0) * ypr * gy
            ca = rng.poisson(ra[:, None] * csh)
            ypc = np.array([m["ypc"] for m in M] + [4.2])
            gr = np.exp(rng.normal(-GAME_YPC_SD ** 2 / 2, GAME_YPC_SD, N))[:, None] if GAME_YPC_SD else 1.0
            ry = np.where(ca > 0, rng.gamma(np.maximum(RUSH_SHAPE * ca, 1e-6)) / RUSH_SHAPE, 0) * (ypc + .15) * gr - .15 * ca   # a few stuffs for loss, same average
            # 5. touchdowns from points
            ntd = rng.binomial(tds[team], OFF_TD)
            npt = rng.binomial(ntd, T["pts"]); nrt = ntd - npt
            def hand_out(n, weights):
                w = weights + 1e-9; w = w / w.sum(1, keepdims=True)
                return rng.multinomial(n, w)
            retd = hand_out(npt, (tg * np.array([m["rtdr"] for m in M] + [OTHER_TD[0]])) ** TD_POW)
            rutd = hand_out(nrt, (ca * np.array([m["rutdr"] for m in M] + [OTHER_TD[1]])) ** TD_POW)
            for i, m in enumerate(M):
                st = {"rec": rec[:, i], "rey": rey[:, i], "ca": ca[:, i], "ry": ry[:, i], "td": retd[:, i] + rutd[:, i],
                      "tgt": tg[:, i], "_av": av[:, i] > 0}
                if m is starter:
                    st.update({"py": rey.sum(1), "ptd": npt.astype(float), "pa": np.round(pa)})
                elif m["pos"] == "QB":
                    st.update({"py": np.zeros(N), "ptd": np.zeros(N), "pa": np.zeros(N)})
                st["rr"] = st["ry"] + st["rey"]
                res[m["p"]["id"]] = (m, st, team)
                if raw is not None: raw[m["p"]["id"]] = (m["pos"], st, label)

        # outputs: game summary, plus exact score odds so the app can price any spread, total or team total
        hm = pts[home] - pts[away]
        hist, _ = np.histogram(np.clip(hm, -35, 35), bins=np.arange(-35, 36, 5))
        def pmf(v):
            v = v.astype(int); lo, hi = int(v.min()), int(v.max()); c = np.bincount(v - lo, minlength=hi - lo + 1) / N
            return {"lo": lo, "p": [round(float(x), 5) for x in c]}
        out["games"][label] = {"home": home, "away": away, "kick": gm.get("kick"), "spread": spread_home, "total": total,
            "hw": round(float((hm > 0).mean() + (hm == 0).mean() / 2), 4), "cover": round(float((hm + spread_home > 0).mean()), 4),
            "push": round(float((hm + spread_home == 0).mean()), 4),
            "over": round(float((tot > total).mean()), 4), "med": [round(float(np.median(pts[away])), 1), round(float(np.median(pts[home])), 1)],
            "hist": [round(float(x) / N, 4) for x in hist],
            "mp": pmf(hm), "tp": pmf(tot), "ap": pmf(pts[away]), "hp": pmf(pts[home]),
            "pts": [[int(a), int(h)] for a, h in zip(pts[away][:NS], pts[home][:NS])]}
        # per-prop chances from all sims, plus stored box scores for players with props
        for q in props["props"]:
            pid = q.get("pid"); k = MKT.get(q.get("market"))
            if pid not in res or not k: continue
            m, st, team = res[pid]
            if k not in st: continue
            v = st[k]; line = q.get("line") if q.get("market") != "atd" else .5
            if st["_av"].any(): v = v[st["_av"]]       # props void if he sits, so price them as if he plays
            if line is None: continue
            out["props"][f'{pid}|{q["market"]}'] = {"po": round(float((v > line).mean()), 4),
                "mean": round(float(v.mean()), 2), "q": [round(float(x), 1) for x in np.percentile(v, np.arange(0, 101, 5))]}
        # projected box score: each player's typical line if he plays (25th / 50th / 75th percentile and average)
        lines = {"label": label, "kick": gm.get("kick"), "home": home, "away": away,
                 "spread_home": spread_home, "total": total, "med": out["games"][label]["med"], "af": last_wk, "players": {}}
        for pid, (m, st, team) in res.items():
            if m["pos"] == "QB" and not st["py"].any(): continue          # backup QBs don't play in the sims
            avm = st["_av"]
            if not avm.any(): continue
            use = (st["tgt"][avm] + st["ca"][avm]).mean()
            if pid not in prop_pids and m["pos"] != "QB" and use < 1.5: continue
            row = {"n": m["p"].get("name", ""), "pos": m["pos"], "t": team, "av": round(float(avm.mean()), 3), "s": {}}
            for k in STATS.get(m["pos"], STATS["WR"]) + (["tgt"] if m["pos"] != "QB" else []):
                if k not in st or k == "td": continue
                v = st[k][avm]
                row["s"][k] = [round(float(np.percentile(v, 25))), round(float(np.median(v))), round(float(np.percentile(v, 75))), round(float(v.mean()), 1)]
            v = st["td"][avm]; row["td"] = [round(float((v > 0).mean()), 3), round(float(v.mean()), 2)]
            lines["players"][pid] = row
        out["lines"][label] = lines
        for pid, (m, st, team) in res.items():
            if pid not in prop_pids: continue
            keep = STATS.get(m["pos"], STATS["WR"])
            out["players"][pid] = {"g": label, "t": team,
                "s": {k: ",".join(str(int(round(x))) for x in st[k][:NS]) for k in keep if k in st}}
        if not quiet: print(f"{label}: home win {out['games'][label]['hw']:.0%}, median {out['games'][label]['med']}")
    return out

if __name__ == "__main__":
    main()
