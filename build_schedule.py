"""Writes schedule.json: every game for last season and this season with home/away, closing spread and
total, final margin and roof, from nflverse (free, no key). Powers the player-page splits
(home/away, favorite/underdog, win/loss, high/low total, indoors/outdoors).

Each game: [season, week, home, away, spread_line, total_line, result, indoors]
spread_line > 0 means the home team was favored; result = home score minus away score (null if unplayed)."""
import csv, io, json, urllib.request
from datetime import datetime, timezone

URLS = ["https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
        "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"]

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def main():
    try: cur = json.load(open("player_logs.json")).get("season") or datetime.now().year
    except Exception: cur = datetime.now().year
    rows = []
    for u in URLS:
        try:
            with urllib.request.urlopen(u, timeout=60) as r:
                rows = list(csv.DictReader(io.TextIOWrapper(r, encoding="utf-8")))
            print("loaded", u, len(rows), "games"); break
        except Exception as e:
            print("could not load", u, "-", e)
    games = []
    for g in rows:
        s = int(num(g.get("season")) or 0)
        if s < cur - 1 or g.get("game_type") not in ("REG", "WC", "DIV", "CON", "SB"): continue
        res = num(g.get("result"))
        games.append([s, int(num(g.get("week")) or 0), g.get("home_team"), g.get("away_team"),
                      num(g.get("spread_line")), num(g.get("total_line")),
                      None if res is None else int(res), 1 if g.get("roof") in ("dome", "closed") else 0])
    if not games:
        print("no games found, keeping the old schedule.json"); return
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "games": games},
              open("schedule.json", "w"), separators=(",", ":"))
    print("wrote schedule.json with", len(games), "games")

if __name__ == "__main__":
    main()
