"""
Build player_logs.json for Prop Board from nflverse weekly player stats.

Usage:
    pip install pandas nflreadpy        # nflreadpy optional but preferred
    python build_logs.py                # current + previous season
    python build_logs.py 2025 2026      # specific seasons

Put the resulting player_logs.json in the same folder as index.html.
"""
import json, sys, datetime
import pandas as pd

POSITIONS = {"QB", "RB", "WR", "TE"}


def load(seasons):
    try:
        import nflreadpy as nfl
        return nfl.load_player_stats(seasons=seasons, summary_level="week").to_pandas()
    except Exception as e:
        print(f"nflreadpy unavailable ({e}); falling back to CSV downloads")
    frames = []
    base = "https://github.com/nflverse/nflverse-data/releases/download"
    for y in seasons:
        for url in (f"{base}/stats_player/stats_player_week_{y}.csv",
                    f"{base}/player_stats/player_stats_{y}.csv"):
            try:
                frames.append(pd.read_csv(url, low_memory=False))
                print(f"loaded {url}")
                break
            except Exception:
                continue
        else:
            print(f"no data found for {y}")
    return pd.concat(frames, ignore_index=True)


def col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series(0, index=df.index)


def main():
    this_year = datetime.date.today().year if datetime.date.today().month >= 9 else datetime.date.today().year - 1
    seasons = [int(a) for a in sys.argv[1:]] or [this_year - 1, this_year]
    df = load(seasons)

    df = df[col(df, "season_type").fillna("REG").eq("REG")]
    df = df[col(df, "position").isin(POSITIONS)]

    out = pd.DataFrame({
        "id":   col(df, "player_id"),
        "name": col(df, "player_display_name", "player_name"),
        "pos":  col(df, "position"),
        "team": col(df, "team", "recent_team"),
        "s":    col(df, "season"),
        "w":    col(df, "week"),
        "o":    col(df, "opponent_team"),
        "pa":   col(df, "attempts"),
        "py":   col(df, "passing_yards"),
        "ptd":  col(df, "passing_tds"),
        "ca":   col(df, "carries"),
        "ry":   col(df, "rushing_yards"),
        "rtd":  col(df, "rushing_tds"),
        "tgt":  col(df, "targets"),
        "rec":  col(df, "receptions"),
        "rey":  col(df, "receiving_yards"),
        "retd": col(df, "receiving_tds"),
    })
    num = ["s", "w", "pa", "py", "ptd", "ca", "ry", "rtd", "tgt", "rec", "rey", "retd"]
    out[num] = out[num].fillna(0).round().astype(int)
    out = out.sort_values(["id", "s", "w"])

    players = []
    for pid, g in out.groupby("id"):
        last = g.iloc[-1]
        logs = g[["s", "w", "o"] + num[2:]].to_dict("records")
        if len(logs) < 3:          # skip players with barely any games
            continue
        players.append({"id": pid, "name": last["name"], "pos": last["pos"],
                        "team": last["team"], "logs": logs})

    players.sort(key=lambda p: p["name"])
    latest = out[out["s"] == out["s"].max()]
    payload = {
        "season": int(out["s"].max()),
        "updated": f"{int(out['s'].max())} week {int(latest['w'].max())}",
        "players": players,
    }
    with open("player_logs.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote player_logs.json — {len(players)} players")


if __name__ == "__main__":
    main()
