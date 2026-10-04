"""Writes injuries.json: current game-status designations (Out, Doubtful, Questionable, IR) keyed by the
same player ids as player_logs.json. Source: ESPN's public injuries feed (free, no key), with the nflverse
weekly injury report as a fallback."""
import csv, io, json, re, urllib.request
from datetime import datetime, timezone

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
NFLV = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{s}.csv"
TEAMS = {
 "Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL","Buffalo Bills":"BUF",
 "Carolina Panthers":"CAR","Chicago Bears":"CHI","Cincinnati Bengals":"CIN","Cleveland Browns":"CLE",
 "Dallas Cowboys":"DAL","Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB",
 "Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX","Kansas City Chiefs":"KC",
 "Las Vegas Raiders":"LV","Los Angeles Chargers":"LAC","Los Angeles Rams":"LA","Miami Dolphins":"MIA",
 "Minnesota Vikings":"MIN","New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG",
 "New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT","San Francisco 49ers":"SF",
 "Seattle Seahawks":"SEA","Tampa Bay Buccaneers":"TB","Tennessee Titans":"TEN","Washington Commanders":"WAS",
}
ALIAS = {"LAR":"LA","JAC":"JAX","WSH":"WAS","ARZ":"ARI","LVR":"LV"}

def norm(name):
    n = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " "))
    return " ".join(w for w in n.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})

def status_code(s):
    s = (s or "").lower()
    if "reserve" in s or s in ("ir", "pup", "nfi") or "injured reserve" in s: return "IR"
    if s.startswith("out") or s == "suspension": return "Out"
    if s.startswith("doubt"): return "Doubtful"
    if s.startswith("quest"): return "Questionable"
    return None

def from_espn():
    with urllib.request.urlopen(urllib.request.Request(ESPN, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        data = json.load(r)
    rows = []
    def walk(node, team=None):
        if isinstance(node, dict):
            t = TEAMS.get(node.get("displayName")) or team
            ath = node.get("athlete")
            if isinstance(ath, dict) and node.get("status"):
                tm = (ath.get("team") or {}).get("abbreviation")
                rows.append({"name": ath.get("displayName") or ath.get("fullName"), "es": str(ath.get("id") or ""),
                             "team": ALIAS.get(tm, tm) if tm else t, "status": node.get("status"),
                             "note": node.get("shortComment") or (node.get("details") or {}).get("type") or ""})
            for v in node.values(): walk(v, t)
        elif isinstance(node, list):
            for v in node: walk(v, team)
    walk(data)
    return rows

def from_nflverse(season):
    with urllib.request.urlopen(NFLV.format(s=season), timeout=60) as r:
        data = list(csv.DictReader(io.TextIOWrapper(r, encoding="utf-8")))
    if not data: return []
    wk = max(int(float(x.get("week") or 0)) for x in data)
    return [{"name": x.get("full_name"), "gsis": x.get("gsis_id"), "team": ALIAS.get(x.get("team"), x.get("team")),
             "status": x.get("report_status"), "note": x.get("report_primary_injury") or ""}
            for x in data if int(float(x.get("week") or 0)) == wk]

def main():
    logs = json.load(open("player_logs.json"))
    players = logs["players"]
    by_es = {str(p["es"]): p["id"] for p in players if p.get("es")}
    by_id = {p["id"]: p["id"] for p in players}
    by_name = {}
    for p in players:
        by_name.setdefault((norm(p["name"]), p.get("team")), p["id"]); by_name.setdefault((norm(p["name"]), None), p["id"])

    rows, src = [], "espn"
    try: rows = from_espn()
    except Exception as e: print("ESPN injuries failed:", e)
    if not rows:
        src = "nflverse"
        try: rows = from_nflverse(logs.get("season") or datetime.now().year)
        except Exception as e: print("nflverse injuries failed:", e)

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": src, "players": {}}
    for r in rows:
        st = status_code(r.get("status"))
        if not st: continue
        pid = by_es.get(r.get("es") or "") or by_id.get(r.get("gsis") or "") \
              or by_name.get((norm(r.get("name")), r.get("team"))) or by_name.get((norm(r.get("name")), None))
        if pid: out["players"][pid] = {"st": st, "note": (r.get("note") or "")[:80]}
    json.dump(out, open("injuries.json", "w"), separators=(",", ":"))
    print(f"wrote injuries.json from {src}: {len(out['players'])} matched of {len(rows)} listed")

if __name__ == "__main__":
    main()
