"""Writes context.json for each game in props.json:
- kickoff weather (Open-Meteo, free, no key)
- consensus spread, total and team totals (PropLine free tier, 1 request per run, uses PROPLINE_API_KEY)"""
import json, os, statistics, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

# team: (lat, lon, indoors). Retractable roofs are treated as indoors.
VENUES = {
 "ARI":(33.5276,-112.2626,True),"ATL":(33.7554,-84.4009,True),"BAL":(39.2780,-76.6227,False),
 "BUF":(42.7738,-78.7870,False),"CAR":(35.2258,-80.8528,False),"CHI":(41.8623,-87.6167,False),
 "CIN":(39.0955,-84.5161,False),"CLE":(41.5061,-81.6995,False),"DAL":(32.7473,-97.0945,True),
 "DEN":(39.7439,-105.0201,False),"DET":(42.3400,-83.0456,True),"GB":(44.5013,-88.0622,False),
 "HOU":(29.6847,-95.4107,True),"IND":(39.7601,-86.1639,True),"JAX":(30.3239,-81.6373,False),
 "KC":(39.0489,-94.4839,False),"LV":(36.0909,-115.1833,True),"LAC":(33.9535,-118.3392,True),
 "LA":(33.9535,-118.3392,True),"MIA":(25.9580,-80.2389,False),"MIN":(44.9735,-93.2575,True),
 "NE":(42.0909,-71.2643,False),"NO":(29.9511,-90.0812,True),"NYG":(40.8135,-74.0745,False),
 "NYJ":(40.8135,-74.0745,False),"PHI":(39.9008,-75.1675,False),"PIT":(40.4468,-80.0158,False),
 "SF":(37.4030,-121.9700,False),"SEA":(47.5952,-122.3316,False),"TB":(27.9759,-82.5033,False),
 "TEN":(36.1665,-86.7713,False),"WAS":(38.9076,-76.8645,False),
}
ALIAS = {"LAR":"LA","JAC":"JAX","WSH":"WAS","ARZ":"ARI","LVR":"LV","SD":"LAC","OAK":"LV","STL":"LA"}

NAMES = {
 "Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL","Buffalo Bills":"BUF",
 "Carolina Panthers":"CAR","Chicago Bears":"CHI","Cincinnati Bengals":"CIN","Cleveland Browns":"CLE",
 "Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB",
 "Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX","Kansas City Chiefs":"KC",
 "Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LA","Miami Dolphins":"MIA",
 "Minnesota Vikings":"MIN","New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG",
 "New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT","San Francisco 49ers":"SF",
 "Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN","Washington Commanders":"WAS",
}
SKIP_BOOKS = {"prizepicks", "sleeper", "dabble", "underdog"}

def abbr(name):
    if not name: return None
    name = name.strip()
    if name in NAMES: return NAMES[name]
    up = name.upper()
    return ALIAS.get(up, up) if len(up) <= 3 else None

def game_lines():
    """{(away, home): {spread_home, total, tt_home, tt_away}} from consensus (median) across books."""
    key = os.environ.get("PROPLINE_API_KEY")
    if not key:
        print("no PROPLINE_API_KEY, skipping game lines"); return {}
    from propline import PropLine
    res = PropLine(key).get_odds("football_nfl", markets=["spreads", "totals"])
    events = res if isinstance(res, list) else (res.get("events") or res.get("data") or [res])
    out = {}
    for ev in events:
        home, away = abbr(ev.get("home_team")), abbr(ev.get("away_team"))
        if not home or not away: continue
        sp, tot, tth, tta = [], [], [], []
        for bk in ev.get("bookmakers", []):
            if bk.get("key") in SKIP_BOOKS: continue
            for m in bk.get("markets", []):
                if m.get("period"): continue
                oc = m.get("outcomes", [])
                if m.get("key") == "spreads":
                    for o in oc:
                        if abbr(o.get("name")) == home and o.get("point") is not None: sp.append(float(o["point"]))
                elif m.get("key") == "totals":
                    over = next((o for o in oc if str(o.get("name", "")).lower() == "over" and o.get("point") is not None), None)
                    if not over: continue
                    t = abbr(m.get("team"))
                    (tot if t is None else tth if t == home else tta if t == away else []).append(float(over["point"]))
        row = {}
        if sp: row["spread_home"] = statistics.median(sp)
        if tot: row["total"] = statistics.median(tot)
        if tth: row["tt_home"] = statistics.median(tth)
        if tta: row["tt_away"] = statistics.median(tta)
        if row: out[(away, home)] = row
    print("game lines for", len(out), "games")
    return out

def parse_kick(k):
    d = datetime.fromisoformat(k.replace("Z", "+00:00"))
    return (d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)).replace(tzinfo=None)

def forecast(lat, lon):
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "UTC", "forecast_days": 16, "past_days": 1,
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
    })
    with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=30) as r:
        return json.load(r)["hourly"]

def window(h, kick, hours=4):
    t0 = kick.replace(minute=0, second=0, microsecond=0)
    want = {(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(hours)}
    idx = [i for i, t in enumerate(h["time"]) if t in want]
    if not idx: return None
    def col(name): return [h[name][i] for i in idx if h[name][i] is not None]
    temp, wind, gust, pop, pr = col("temperature_2m"), col("wind_speed_10m"), col("wind_gusts_10m"), col("precipitation_probability"), col("precipitation")
    if not wind: return None
    return {"temp": round(sum(temp)/len(temp)) if temp else None, "wind": round(sum(wind)/len(wind)),
            "gust": round(max(gust)) if gust else None, "pop": max(pop) if pop else None, "precip": round(sum(pr), 1) if pr else 0}

def main():
    props = json.load(open("props.json"))
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "games": {}}
    cache = {}
    try:
        lines = game_lines()
    except Exception as e:
        print("game lines failed:", e); lines = {}
    for g in props.get("games", []):
        label = g.get("label", ""); kick = g.get("kick")
        if " @ " not in label or not kick: continue
        away, home = [ALIAS.get(x.strip(), x.strip()) for x in label.split(" @ ")]
        row = dict(lines.get((away, home), {}))
        v = VENUES.get(home)
        if v:
            lat, lon, indoors = v
            if indoors:
                row["dome"] = True
            else:
                try:
                    if home not in cache: cache[home] = forecast(lat, lon)
                    w = window(cache[home], parse_kick(kick))
                    if w: row.update(w)
                except Exception as e:
                    print("weather failed for", label, e)
        if row: out["games"][label] = row
    json.dump(out, open("context.json", "w"), indent=1)
    print("wrote context.json with", len(out["games"]), "games")

if __name__ == "__main__":
    main()
