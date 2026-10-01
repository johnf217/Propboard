"""Writes usage.json: per-player weekly target share, carry share and offensive snap share (nflverse, free, no key).

Keyed by the same player ids as player_logs.json. Each player gets up to 12 recent rows: [season, week, tgt_share, carry_share, snap_share]
(shares are 0-1, null when unknown)."""
import csv, io, json, re, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = "https://github.com/nflverse/nflverse-data/releases/download/"

def fetch_csv(*paths):
    for p in paths:
        try:
            with urllib.request.urlopen(BASE + p, timeout=60) as r:
                rows = list(csv.DictReader(io.TextIOWrapper(r, encoding="utf-8")))
                print("loaded", p, len(rows), "rows")
                return rows
        except Exception as e:
            print("could not load", p, "-", e)
    return []

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def norm(name):
    n = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " "))
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})

def col(row, *names):
    for n in names:
        if n in row and row[n] not in ("", None, "NA"): return row[n]
    return None

def main():
    logs = json.load(open("player_logs.json"))
    players = logs["players"]
    cur = logs.get("season") or max((g["s"] for p in players for g in p.get("logs", [])), default=datetime.now().year)
    seasons = [cur - 1, cur]

    # our id -> lookup keys
    by_gsis = {p["id"]: p["id"] for p in players if str(p["id"]).startswith("00-")}
    by_name = {}
    for p in players:
        by_name.setdefault((norm(p["name"]), p.get("team")), p["id"])
        by_name.setdefault((norm(p["name"]), None), p["id"])
    def match(gsis=None, name=None, team=None):
        if gsis and gsis in by_gsis: return by_gsis[gsis]
        n = norm(name)
        return by_name.get((n, team)) or by_name.get((n, None))

    rows = defaultdict(dict)  # our id -> {(s,w): [tsh, csh, snp]}

    # 1) target share and carry share from weekly player stats
    for s in seasons:
        stats = fetch_csv(f"stats_player/stats_player_week_{s}.csv", f"player_stats/player_stats_{s}.csv")
        stats = [r for r in stats if num(col(r, "season")) == s and (col(r, "season_type") or "REG") in ("REG", "POST")]
        team_t, team_c = defaultdict(float), defaultdict(float)
        for r in stats:
            k = (col(r, "team", "recent_team"), int(num(col(r, "week")) or 0))
            team_t[k] += num(col(r, "targets")) or 0
            team_c[k] += num(col(r, "carries")) or 0
        for r in stats:
            team, wk = col(r, "team", "recent_team"), int(num(col(r, "week")) or 0)
            pid = match(col(r, "player_id"), col(r, "player_display_name", "player_name"), team)
            if not pid or not wk: continue
            t, c = num(col(r, "targets")) or 0, num(col(r, "carries")) or 0
            tsh = num(col(r, "target_share"))
            if tsh is None and team_t[(team, wk)]: tsh = t / team_t[(team, wk)]
            csh = c / team_c[(team, wk)] if team_c[(team, wk)] else None
            rows[pid].setdefault((s, wk), [None, None, None])
            rows[pid][(s, wk)][0] = round(tsh, 3) if tsh is not None else None
            rows[pid][(s, wk)][1] = round(csh, 3) if csh is not None else None

    # 2) offensive snap share (PFR ids, joined through the nflverse players table, else by name + team)
    pfr_to_gsis = {}
    for r in fetch_csv("players/players.csv"):
        g, f = col(r, "gsis_id"), col(r, "pfr_id")
        if g and f: pfr_to_gsis[f] = g
    for s in seasons:
        for r in fetch_csv(f"snap_counts/snap_counts_{s}.csv"):
            if (col(r, "game_type") or "REG") not in ("REG", "POST"): continue
            wk, pct = int(num(col(r, "week")) or 0), num(col(r, "offense_pct"))
            if not wk or pct is None: continue
            if pct > 1.5: pct /= 100
            pid = match(pfr_to_gsis.get(col(r, "pfr_player_id")), col(r, "player"), col(r, "team"))
            if not pid: continue
            rows[pid].setdefault((s, wk), [None, None, None])[2] = round(pct, 3)

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "players": {}}
    for pid, d in rows.items():
        out["players"][pid] = [[s, w, *v] for (s, w), v in sorted(d.items())][-12:]
    json.dump(out, open("usage.json", "w"), separators=(",", ":"))
    print("wrote usage.json for", len(out["players"]), "of", len(players), "players")

if __name__ == "__main__":
    main()
