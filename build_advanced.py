"""Writes advanced.json from free nflverse data (no key). Powers the projection engine.

players: per player, up to 12 recent games:
  [season, week, targets, air_yards, yac, redzone_targets, endzone_targets, carries, redzone_carries(<=10), goalline_carries(<=5), pass_att, pass_air_yards]
  plus "ngs": season Next Gen Stats (separation, YAC over expected, rush yards over expected per carry, time to throw, CPOE, aggressiveness)
teams: offensive plays per game, neutral pass rate, pass rate over expected (last 6 games)
defense: EPA per dropback / per rush allowed, yards per target / per carry allowed, plays faced per game
league: averages of the above
Keyed by the same player ids as player_logs.json."""
import csv, gzip, io, json, re, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = "https://github.com/nflverse/nflverse-data/releases/download/"

def open_csv(*paths):
    for p in paths:
        try:
            r = urllib.request.urlopen(BASE + p, timeout=180)
            raw = gzip.GzipFile(fileobj=r) if p.endswith(".gz") else r
            print("reading", p)
            return csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
        except Exception as e:
            print("could not load", p, "-", e)
    return None

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def norm(name):
    n = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " "))
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})

def main():
    logs = json.load(open("player_logs.json"))
    players = logs["players"]
    cur = logs.get("season") or max((g["s"] for p in players for g in p.get("logs", [])), default=datetime.now().year)
    seasons = [cur - 1, cur]

    ours = {p["id"] for p in players}
    by_name = {}
    for p in players:
        by_name.setdefault((norm(p["name"]), p.get("team")), p["id"]); by_name.setdefault((norm(p["name"]), None), p["id"])
    gsis_to_ours = {}
    rows = open_csv("players/players.csv")
    if rows:
        for r in rows:
            g = r.get("gsis_id")
            if not g: continue
            if g in ours: gsis_to_ours[g] = g
            else:
                nm = norm(r.get("display_name") or r.get("full_name"))
                hit = by_name.get((nm, r.get("latest_team") or r.get("team_abbr"))) or by_name.get((nm, None))
                if hit: gsis_to_ours[g] = hit
    def pid(g): return gsis_to_ours.get(g) or (g if g in ours else None)

    pw = defaultdict(lambda: [0.0]*10)            # (our id, s, w) -> stats
    tg = defaultdict(lambda: {"plays":0, "np":0, "npass":0, "proe":0.0, "nproe":0, "s":0, "w":0})   # (team, game)
    df = defaultdict(lambda: {"db":0, "dbe":0.0, "ru":0, "rue":0.0, "tgt":0, "tyd":0.0, "car":0, "cyd":0.0, "plays":0, "games":set()})
    for s in seasons:
        rd = open_csv(f"pbp/play_by_play_{s}.csv.gz")
        if not rd: continue
        for r in rd:
            if r.get("season_type") not in ("REG", "POST") or r.get("play_type") not in ("pass", "run"): continue
            if r.get("two_point_attempt") == "1": continue
            w = int(num(r.get("week")) or 0); pos, de, gid = r.get("posteam"), r.get("defteam"), r.get("game_id")
            if not pos or not de: continue
            is_pass = r.get("pass") == "1"; yl = num(r.get("yardline_100")) or 99; epa = num(r.get("epa"))
            t = tg[(pos, gid)]; t["plays"] += 1; t["s"], t["w"] = s, w
            qtr, wp = num(r.get("qtr")) or 0, num(r.get("wp"))
            if qtr <= 3 and wp is not None and .2 <= wp <= .8:
                t["np"] += 1; t["npass"] += is_pass
                xp = num(r.get("xpass"))
                if xp is not None: t["proe"] += (1 if is_pass else 0) - xp; t["nproe"] += 1
            d = df[(de, s)]; d["plays"] += 1; d["games"].add(gid)
            if r.get("qb_dropback") == "1" and epa is not None: d["db"] += 1; d["dbe"] += epa
            if r.get("rush") == "1" and r.get("qb_scramble") != "1":
                if epa is not None: d["ru"] += 1; d["rue"] += epa
                d["car"] += 1; d["cyd"] += num(r.get("yards_gained")) or 0
            if r.get("pass_attempt") == "1" and r.get("sack") != "1":
                rec = pid(r.get("receiver_player_id")); ay = num(r.get("air_yards"))
                if r.get("receiver_player_id"):
                    d["tgt"] += 1; d["tyd"] += (num(r.get("yards_gained")) or 0) if r.get("complete_pass") == "1" else 0
                if rec:
                    x = pw[(rec, s, w)]; x[0] += 1
                    if ay is not None: x[1] += ay
                    x[2] += num(r.get("yards_after_catch")) or 0
                    if yl <= 20: x[3] += 1
                    if ay is not None and ay >= yl: x[4] += 1
                qb = pid(r.get("passer_player_id"))
                if qb:
                    x = pw[(qb, s, w)]; x[8] += 1
                    if ay is not None: x[9] += ay
            if r.get("rush") == "1":
                ru = pid(r.get("rusher_player_id"))
                if ru:
                    x = pw[(ru, s, w)]; x[5] += 1
                    if yl <= 10: x[6] += 1
                    if yl <= 5: x[7] += 1

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "season": cur, "players": {}, "teams": {}, "defense": {}, "league": {}}
    per = defaultdict(list)
    for (i, s, w), x in pw.items(): per[i].append([s, w] + [round(v, 1) for v in x])
    for i, rws in per.items(): out["players"][i] = {"w": sorted(rws)[-12:]}

    # team environment: last 6 games
    byteam = defaultdict(list)
    for (team, gid), t in tg.items(): byteam[team].append(t)
    for team, gs in byteam.items():
        gs = sorted(gs, key=lambda t: (t["s"], t["w"]))[-6:]
        np_ = sum(t["np"] for t in gs); nx = sum(t["nproe"] for t in gs)
        out["teams"][team] = {"plays": round(sum(t["plays"] for t in gs)/len(gs), 1),
                              "npr": round(sum(t["npass"] for t in gs)/np_, 3) if np_ else None,
                              "proe": round(sum(t["proe"] for t in gs)/nx, 3) if nx else None, "n": len(gs)}
    # defense: this season if 3+ games, else last season
    for team in {k[0] for k in df}:
        d = df.get((team, cur))
        if not d or len(d["games"]) < 3: d = df.get((team, cur - 1))
        if not d or not d["games"]: continue
        n = len(d["games"])
        out["defense"][team] = {"epa_pass": round(d["dbe"]/d["db"], 3) if d["db"] else None, "epa_rush": round(d["rue"]/d["ru"], 3) if d["ru"] else None,
                                "ypt": round(d["tyd"]/d["tgt"], 2) if d["tgt"] else None, "ypc": round(d["cyd"]/d["car"], 2) if d["car"] else None,
                                "plays": round(d["plays"]/n, 1), "n": n}
    def avg(key, src):
        v = [x[key] for x in src.values() if x.get(key) is not None]
        return round(sum(v)/len(v), 3) if v else None
    out["league"] = {"plays": avg("plays", out["teams"]), "npr": avg("npr", out["teams"]), "epa_pass": avg("epa_pass", out["defense"]),
                     "epa_rush": avg("epa_rush", out["defense"]), "ypt": avg("ypt", out["defense"]), "ypc": avg("ypc", out["defense"])}

    # Next Gen Stats (season level, optional)
    for kind, fields in (("receiving", {"sep":"avg_separation", "yacoe":"avg_yac_above_expectation"}),
                         ("rushing", {"ryoe":"rush_yards_over_expected_per_att"}),
                         ("passing", {"ttt":"avg_time_to_throw", "cpoe":"completion_percentage_above_expectation", "aggr":"aggressiveness"})):
        for s in (cur, cur - 1):
            rd = open_csv(f"nextgen_stats/ngs_{s}_{kind}.csv.gz", f"nextgen_stats/ngs_{kind}.csv.gz")
            if not rd: continue
            got = 0
            for r in rd:
                if int(num(r.get("season")) or 0) != s or int(num(r.get("week")) or 0) != 0: continue
                i = pid(r.get("player_gsis_id"))
                if not i: continue
                ng = out["players"].setdefault(i, {"w": []}).setdefault("ngs", {})
                for k, f in fields.items():
                    v = num(r.get(f))
                    if v is not None and k not in ng: ng[k] = round(v, 2)
                got += 1
            if got: break

    json.dump(out, open("advanced.json", "w"), separators=(",", ":"))
    print(f"wrote advanced.json: {len(out['players'])} players, {len(out['teams'])} teams, {len(out['defense'])} defenses")

if __name__ == "__main__":
    main()
