"""
Build props.json for Prop Board from PropLine NFL player props.

Needs the PROPLINE_API_KEY environment variable (a GitHub secret in the workflow).
Reads player_logs.json (if present) to link each prop to its nflverse game log.
"""
import json, os, re, statistics as stats, datetime as dt
from propline import PropLine

SPORT = "football_nfl"
MARKETS = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_td",
    "player_rush_yds": "rush_yds",
    "player_reception_yds": "rec_yds",
    "player_receptions": "rec",
    "player_anytime_td": "atd",
}
# DFS pick'em apps use fake even-money prices, so they'd skew the math
SKIP_BOOKS = {"prizepicks", "sleeper", "dabble"}

TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def norm(name):
    n = re.sub(r"\(.*?\)", "", name or "").lower()
    n = re.sub(r"[.'’]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def implied(a):  # American odds -> implied probability
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def profit(a):  # profit per 1 unit staked
    return a / 100 if a > 0 else 100 / -a


def when(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    client = PropLine(os.environ["PROPLINE_API_KEY"])
    now = dt.datetime.now(dt.timezone.utc)

    # link to nflverse ids
    lookup = {}
    try:
        with open("player_logs.json") as f:
            for p in json.load(f)["players"]:
                lookup.setdefault(norm(p["name"]), []).append(p)
    except FileNotFoundError:
        pass

    events = [e for e in client.get_events(SPORT)
              if now < when(e["commence_time"]) < now + dt.timedelta(days=7)]
    games, raw = [], {}

    for e in events:
        away = TEAMS.get(e["away_team"], e["away_team"])
        home = TEAMS.get(e["home_team"], e["home_team"])
        label = f"{away} @ {home}"
        games.append({"id": e["id"], "label": label, "kick": e["commence_time"]})
        try:
            odds = client.get_odds(SPORT, event_id=e["id"], markets=list(MARKETS))
        except Exception as err:
            print(f"skipped {label}: {err}")
            continue

        for bk in odds.get("bookmakers", []):
            if bk["key"] in SKIP_BOOKS:
                continue
            for m in bk.get("markets", []):
                mk = MARKETS.get(m.get("key"))
                if not mk or m.get("period"):
                    continue
                for o in m.get("outcomes", []):
                    if o.get("dfs_odds_type") not in (None, "standard"):
                        continue
                    pm = o.get("payout_multiplier")
                    if pm is not None and pm != 1.0:
                        continue
                    price, point = o.get("price"), o.get("point")
                    side = (o.get("name") or "").lower()
                    desc = o.get("description") or ""
                    if price is None:
                        continue
                    if mk == "atd":
                        point = 0.5
                        if not desc:
                            desc = o.get("name") or ""
                        side = "under" if side in ("no", "under") else "over"
                    elif side not in ("over", "under") or point is None:
                        continue
                    team = re.search(r"\(([A-Z]{2,3})\)", desc)
                    r = raw.setdefault((norm(desc), mk), {
                        "name": re.sub(r"\s*\(.*?\)", "", desc).strip(),
                        "team": team.group(1) if team else None,
                        "game": label, "kick": e["commence_time"], "books": {}})
                    b = r["books"].setdefault(bk["key"], {"title": bk.get("title", bk["key"]), "lines": {}})
                    b["lines"].setdefault(float(point), {})[side] = int(price)

    props = []
    for (nn, mk), r in raw.items():
        books = []
        for key, b in r["books"].items():
            # a book can list alternate lines; keep its main one (closest to a coin flip)
            best = None
            for pt, s in b["lines"].items():
                o, u = s.get("over"), s.get("under")
                score = abs(implied(o) - implied(u)) if o is not None and u is not None else 1
                if best is None or score < best[0]:
                    best = (score, pt, o, u)
            _, pt, o, u = best
            books.append({"book": key, "title": b["title"], "line": pt, "over": o, "under": u})
        if not books:
            continue

        lines = sorted(x["line"] for x in books)
        cons = stats.median_low(lines)

        # no-vig fair chance of the over, averaged across books at the consensus line
        fair_ps = [implied(x["over"]) / (implied(x["over"]) + implied(x["under"]))
                   for x in books if x["line"] == cons and x["over"] is not None and x["under"] is not None]
        fair = round(sum(fair_ps) / len(fair_ps), 4) if fair_ps else None

        overs = [x for x in books if x["over"] is not None]
        unders = [x for x in books if x["under"] is not None]
        bo = min(overs, key=lambda x: (x["line"], -profit(x["over"]))) if overs else None
        bu = min(unders, key=lambda x: (-x["line"], -profit(x["under"]))) if unders else None

        def pick(x, side, p):
            if not x:
                return None
            ev = None
            if p is not None and x["line"] == cons:
                ev = round((p * profit(x[side]) - (1 - p)) * 100, 1)
            return {"book": x["book"], "title": x["title"], "line": x["line"], "price": x[side], "ev": ev}

        # best price vs the no-vig market at the consensus line, either side
        value = None
        if fair is not None:
            for x in books:
                if x["line"] != cons:
                    continue
                for side, prob in (("over", fair), ("under", 1 - fair)):
                    if x[side] is None:
                        continue
                    ev = round((prob * profit(x[side]) - (1 - prob)) * 100, 1)
                    if value is None or ev > value["ev"]:
                        value = {"side": side, "book": x["book"], "title": x["title"],
                                 "line": cons, "price": x[side], "ev": ev}

        cands = lookup.get(nn, [])
        match = next((p for p in cands if p["team"] == r["team"]), cands[0] if cands else None)

        props.append({
            "pid": match["id"] if match else None,
            "name": match["name"] if match else r["name"],
            "pos": match["pos"] if match else None,
            "team": match["team"] if match else r["team"],
            "game": r["game"], "kick": r["kick"], "market": mk,
            "line": cons, "fair": fair, "n": len(fair_ps),
            "gap": round(lines[-1] - lines[0], 1),
            "bestOver": pick(bo, "over", fair),
            "bestUnder": pick(bu, "under", None if fair is None else 1 - fair),
            "value": value,
            "books": sorted(books, key=lambda x: x["title"]),
        })

    with open("props.json", "w") as f:
        json.dump({"updated": now.isoformat(), "games": games, "props": props}, f, separators=(",", ":"))
    q = client.last_quota
    print(f"wrote props.json: {len(props)} props from {len(games)} games"
          + (f" ({q.used}/{q.limit} requests used today)" if q else ""))


if __name__ == "__main__":
    main()
