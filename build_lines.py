"""Writes lines.json: a running history of each prop's consensus line and prices, so the app can show
which way a line has moved since it opened. Reads props.json (no API calls). Run after every props update.

Each prop key (pid|market|kick) holds up to 16 points: [timestamp, line, over_price, under_price].
The first point is always kept (the opener). Props are dropped 2 days after kickoff."""
import json, statistics
from datetime import datetime, timedelta, timezone

def ts(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs)) if xs else None

def main():
    props = json.load(open("props.json"))
    try: hist = json.load(open("lines.json"))
    except Exception: hist = {"props": {}}
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="minutes")
    changed = 0
    for p in props.get("props", []):
        if not p.get("kick") or p.get("line") is None: continue
        if ts(p["kick"]) <= now: continue                      # don't record live lines
        key = f'{p.get("pid") or p.get("name")}|{p["market"]}|{p["kick"]}'
        line = p["line"]
        at = [b for b in p.get("books", []) if b.get("line") == line]
        po, pu = med([b.get("over") for b in at]), med([b.get("under") for b in at])
        if po is None and (p.get("bestOver") or {}).get("line") == line: po = p["bestOver"].get("price")
        if pu is None and (p.get("bestUnder") or {}).get("line") == line: pu = p["bestUnder"].get("price")
        h = hist["props"].setdefault(key, [])
        if h and h[-1][1:] == [line, po, pu]: continue          # nothing moved
        h.append([stamp, line, po, pu]); changed += 1
        if len(h) > 16: del h[1:len(h)-15]                       # keep opener + latest 15
    cutoff = now - timedelta(days=2)
    hist["props"] = {k: v for k, v in hist["props"].items() if ts(k.rsplit("|", 1)[1]) > cutoff}
    hist["updated"] = now.isoformat(timespec="seconds")
    json.dump(hist, open("lines.json", "w"), separators=(",", ":"))
    print(f"lines.json: {changed} updates, tracking {len(hist['props'])} props")

if __name__ == "__main__":
    main()
