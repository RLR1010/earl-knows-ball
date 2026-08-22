"""Fetch bball-ref per_game season stats for a set of players and cache to JSON.

Usage: python fetch_bball_players.py [slug ...]
Writes /tmp/bball/[slug].json : [{season, age, team, g, mp, fgpct, tp3pct, ftpct, trb, ast, pts}, ...]
"""
import json, os, re, subprocess, sys, time

PLAYERS = {
    "jamesle01": [16, 18, 20, 22, 25, 33],
    "bryanko01": [16, 18, 20, 22],
    "duncati01": [16, 20, 25],
    "paulch01": [25, 35],
    "curryst01": [19, 22, 25, 28, 31],
    "antetgi01": [25, 28, 30, 33],
    "tatuja01": [30, 33],
    "gilgish01": [32, 35],
    "jokicni01": [28, 30, 33, 35],
}
# season_id (start year) -> bball-ref label (the row 'Season' on bball-ref, e.g. 2006-07)
def bball_label(start_year):
    return f"{start_year}-{str(start_year+1)[2:]}"

os.makedirs("/tmp/bball", exist_ok=True)

def fetch(slug):
    path = f"/tmp/bball/{slug}.html"
    if not os.path.exists(path):
        url = f"https://www.basketball-reference.com/players/{slug[0]}/{slug}.html"
        subprocess.run(["curl", "-s", "-A", "Mozilla/5.0 (research)", url, "-o", path], check=True)
        time.sleep(1.2)
    html = open(path).read()
    i = html.find('id="per_game')
    t = html.find("<table", i); t2 = html.find("</table>", t)
    seg = html[t:t2]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)
    def clean(td): return re.sub(r"<[^>]+>", "", td).replace("&nbsp;", " ").strip()
    out = []
    for tr in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
        v = [clean(x) for x in cells]
        if v and re.match(r"^\d{4}-\d{2}$", v[0]):
            out.append({
                "season": v[0], "age": v[1], "team": v[2], "g": int(v[5]) if v[5].isdigit() else None,
                "mp": v[7], "fg_pct": v[10], "tp_pct": v[13], "ft_pct": v[20],
                "trb": v[23], "ast": v[24], "pts": v[29],
            })
    json.dump(out, open(f"/tmp/bball/{slug}.json", "w"))
    return out

for slug in PLAYERS:
    try:
        d = fetch(slug)
        print(f"{slug}: {len(d)} seasons")
    except Exception as e:
        print(f"{slug}: ERROR {e}")
