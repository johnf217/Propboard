"""Saves the model's plays to picks.json before kickoff so the Record tab can grade them later.

Runs the real app (index.html) in a headless browser so the saved scores are exactly what the board shows.
A pick is updated on every run until its game kicks off, then it's locked."""
import functools, http.server, json, threading
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

GRAB = """() => {
  const top = new Set(topPlays(PROPS.props.filter(p=>p.ok)).map(p=>p.key));
  return PROPS.props.filter(p=>p.ok && p.pick && p.score!=null && p.kick).map(p=>({
    k: p.key + "|" + p.kick, pid: p.pid, n: p.name, t: p.team, o: p.opp, m: p.market, sd: p.lean,
    l: p.pick.line, pr: p.pick.price, bk: p.pick.title, sc: p.score, pm: Math.round(p.pModel*1000)/1000,
    ed: Math.round(p.edge*1000)/1000, top: top.has(p.key) ? 1 : 0, kick: p.kick,
    cl: p.line, cf: p.fair==null ? null : Math.round((p.lean==="over" ? p.fair : 1-p.fair)*1000)/1000,
    af: p.pl.logs.length ? p.pl.logs.at(-1).s*100 + p.pl.logs.at(-1).w : 0 }));
}"""

def ts(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def main():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    handler = functools.partial(Quiet, directory=".")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 390, "height": 844})
            page.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
            page.goto("http://127.0.0.1:8765/index.html")
            page.wait_for_function("typeof PROPS!=='undefined' && PROPS && PROPS.props && PROPS.props.some(p=>'score' in p)", timeout=60000)
            rows = page.evaluate(GRAB)
            b.close()
    finally:
        srv.shutdown()

    try: book = json.load(open("picks.json"))
    except Exception: book = {"picks": {}}
    now = datetime.now(timezone.utc)
    added = updated = 0
    for r in rows:
        if ts(r["kick"]) <= now: continue                 # game already started: don't save live lines
        old = book["picks"].get(r["k"])
        if old and ts(old["kick"]) <= now: continue       # locked
        r["ts"] = now.isoformat(timespec="minutes")
        # remember where the market was when we first made this pick (reset if the model flips sides)
        if old and old.get("sd") == r["sd"] and "fcl" in old:
            r["fcl"], r["fcf"] = old["fcl"], old.get("fcf")
        else:
            r["fcl"], r["fcf"] = r.get("cl"), r.get("cf")
        book["picks"][r["k"]] = r
        added += old is None; updated += old is not None
    book["updated"] = now.isoformat(timespec="seconds")
    json.dump(book, open("picks.json", "w"), separators=(",", ":"))
    print(f"picks.json: {added} new, {updated} refreshed, {len(book['picks'])} total")

if __name__ == "__main__":
    main()
