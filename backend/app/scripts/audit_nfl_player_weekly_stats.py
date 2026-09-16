#!/usr/bin/env python
"""
Audit nfl.player_weekly_stats — completeness, identity, integrity, reconciliation.

Why: player_weekly_stats is the grain-fact table the NFL models derive from. Before trusting a
retrain we verify (a) every FINAL game is covered, (b) per-game player sums reconcile to the
independent team box (nfl.game_stats), (c) no dangling/mislabeled rows, (d) sane ranges.

Run:  cd backend && venv/bin/python app/scripts/audit_nfl_player_weekly_stats.py
Exit: 0 = all gates pass, 1 = at least one FAIL gate (see summary at end).
"""
import asyncio, sys, os

try:
    import asyncpg
except ImportError:
    print("asyncpg not available"); sys.exit(2)

DSN = os.environ.get("AUDIT_DSN", "postgresql://earl:goY-4oLs6tGtZlYsX8xx0LSbFbsmX801KSr3O9wcXB2ivmBuPCL12w@localhost:5432/earl_knows_football")

PWS = "nfl.player_weekly_stats"
FAILS, WARNS = [], []
# Known, documented source gaps (ESPN summary lacks the data). Keyed by (year, week, type, team).
# 2016 wk13 IND: ESPN's summary endpoint returns an empty roster for this game's competitor,
# so the whole-team offense cannot be ingested. Verified against ESPN; not a pipeline bug.
RECON_EXCEPTIONS = {(2016, 13, "REG", "IND")}


def head(t):
    print("\n" + "=" * 80 + f"\n{t}\n" + "=" * 80)


def kv(k, v):
    print(f"  {k:<62} {v}")


def gate(name, ok, detail, warn=False):
    print(f"  [{'FAIL' if not ok and not warn else ('WARN' if not ok else ' OK ')}] {name}: {detail}")
    if not ok:
        (WARNS if warn else FAILS).append(f"{name}: {detail}")


async def main():
    c = await asyncpg.connect(DSN)
    S = {r["id"]: r["year"] for r in await c.fetch("select id, year from nfl.seasons")}

    # ---------- 1. structure ----------
    head("1. STRUCTURAL INTEGRITY")
    total = await c.fetchval(f"select count(*) from {PWS}")
    kv("total rows", f"{total:,}")
    kv("distinct players", f"{await c.fetchval(f'select count(distinct player_id) from {PWS}'):,}")
    kv("distinct games (non-null game_id)", f"{await c.fetchval(f'select count(distinct game_id) from {PWS}'):,}")

    orphan_p = await c.fetchval(f"select count(*) from {PWS} w left join nfl.players p on p.id=w.player_id where p.id is null")
    orphan_g = await c.fetchval(f"select count(*) from {PWS} w left join nfl.games g on g.id=w.game_id where w.game_id is not null and g.id is null")
    mism_team = await c.fetchval(f"""select count(*) from {PWS} w join nfl.games g on g.id=w.game_id
        where w.team_id not in (g.home_team_id, g.away_team_id)""")

    dup_ident = await c.fetchval(f"""select count(*) from (select w.game_id, w.team_id, p.name
        from {PWS} w join nfl.players p on p.id=w.player_id where w.game_id is not null group by 1,2,3
        having count(distinct w.player_id)>1
           and count(distinct (coalesce(w.pass_attempts,0),coalesce(w.rush_attempts,0),coalesce(w.receptions,0),
                coalesce(w.targets,0),coalesce(w.tackles_combined,0)))=1
           and max(coalesce(w.pass_attempts,0)+coalesce(w.rush_attempts,0)+coalesce(w.receptions,0)
                +coalesce(w.tackles_combined,0))>0) x""")
    gate("no duplicate-identity rows (same team+name+stat line in one game)", dup_ident == 0, f"{dup_ident:,} groups")

    null_gid = await c.fetchval(f"select count(*) from {PWS} where game_id is null")
    null_twin = await c.fetchrow(f"""
        with nulls as (select player_id, season_id, week, team_id from {PWS} where game_id is null)
        select count(*) total, count(*) filter (where exists (
            select 1 from {PWS} w2 where w2.game_id is not null and w2.player_id=n.player_id
              and w2.season_id=n.season_id and w2.week=n.week and w2.team_id=n.team_id)) twins from nulls n""")

    # real duplicates (only meaningful where game_id is set)
    dup = await c.fetchval(f"""select count(*) from (select 1 from {PWS} where game_id is not null
        group by game_id, player_id, game_type having count(*)>1) x""")

    kv("orphan player_id", f"{orphan_p:,}")
    gate("orphan game_id", orphan_g == 0, f"{orphan_g:,} rows point at a non-existent game")
    gate("team_id in its game's two teams", mism_team == 0, f"{mism_team:,} mismatches")
    gate("no NULL game_id rows", null_gid == 0,
         f"{null_gid:,} rows w/ NULL game_id ({null_twin['twins']:,} have a linked twin, {null_gid - null_twin['twins']:,} are sole-copy)")
    gate("no dup (game_id, player_id, game_type)", dup == 0, f"{dup:,} dup groups")

    # ---------- 2. identity ----------
    head("2. IDENTITY / PLAYER RESOLUTION")
    ph = await c.fetchval("select count(*) from nfl.players where name like 'ESPN-%'")
    unk_rows = await c.fetchval(f"""select count(*) from {PWS} w join nfl.players p on p.id=w.player_id
        where p.position='UNK' or p.position is null""")
    unk_live = await c.fetchval(f"""select count(*) from {PWS} w join nfl.players p on p.id=w.player_id
        where (p.position='UNK' or p.position is null)
          and coalesce(w.pass_attempts,0)+coalesce(w.rush_attempts,0)+coalesce(w.tackles_combined,0)+coalesce(w.targets,0)>0""")
    kv("placeholder players (name 'ESPN-%')", f"{ph:,}")
    kv("pws rows on UNK/NULL-position players", f"{unk_rows:,}  (of which {unk_live:,} have real stats)")
    gate("no placeholder players", ph == 0, f"{ph:,} placeholders")
    gate("all pws players have a resolved position", unk_rows == 0,
         f"{unk_rows:,} rows w/ UNK position ({unk_live:,} carry real stats)", warn=True)

    # ---------- 3. coverage ----------
    head("3. COMPLETENESS — FINAL games covered (REG+POST)")
    cov = await c.fetch(f"""
      select g.season_id sid, g.game_type, count(*) total, count(*) filter (where p.game_id is null) missing
      from nfl.games g
      left join (select distinct game_id from {PWS}) p on p.game_id=g.id
      where g.status='FINAL' and g.game_type in ('REG','POST') group by 1,2""")
    tot_g = sum(r["total"] for r in cov); miss_g = sum(r["missing"] for r in cov)
    kv("FINAL games (REG+POST; preseason out of scope)", f"{tot_g:,}")
    kv("...missing ANY player stats", f"{miss_g:,}")
    for r in cov:
        if r["missing"]:
            print(f"       season {r['sid']} ({S.get(r['sid'],'?')}) {r['game_type']}: {r['missing']} missing")
    gate("every FINAL game has player stats", miss_g == 0, f"{miss_g:,} FINAL games have no rows")
    halves = await c.fetch(f"""select w.game_id, w.season_id, w.week, w.game_type, count(distinct w.team_id) nt, count(*) n
        from {PWS} w where w.game_id is not null and w.game_type in ('REG','POST') group by 1,2,3,4 having count(distinct w.team_id)<>2""")
    gate("every game has both teams", len(halves) == 0, f"{len(halves)} games lack a side")
    for r in halves:
        print(f"       game {r['game_id']} season {r['season_id']} wk{r['week']} {r['game_type']}: {r['nt']} team(s), {r['n']} rows")

    # ---------- 4. reconciliation vs team box ----------
    head("4. RECONCILIATION vs nfl.game_stats (team box), by (season,week,type,team)")
    pairs = [("pass_attempts", "pass_attempts"), ("pass_completions", "pass_completions"),
             ("pass_yards", "pass_yards"), ("pass_tds", "pass_tds"), ("pass_interceptions", "pass_interceptions"),
             ("rush_attempts", "rush_attempts"), ("rush_yards", "rush_yards"), ("rush_tds", "rush_tds")]
    gs = await c.fetch("""select (case when season<100 then 2000+season else season end) yr, week wt, season_type st,
             team_abbr abbr, sum(targets) tg,
             sum(pass_attempts) pass_attempts, sum(pass_completions) pass_completions, sum(pass_yards) pass_yards,
             sum(pass_tds) pass_tds, sum(pass_interceptions) pass_interceptions,
             sum(rush_attempts) rush_attempts, sum(rush_yards) rush_yards, sum(rush_tds) rush_tds
        from nfl.game_stats where season_type in ('REG','POST') group by 1,2,3,4""")
    pw = await c.fetch(f"""select s.year yr, w.week wt, w.game_type st, t.abbreviation abbr,
             sum(pass_attempts) pass_attempts, sum(pass_completions) pass_completions, sum(pass_yards) pass_yards,
             sum(pass_tds) pass_tds, sum(pass_int) pass_interceptions,
             sum(rush_attempts) rush_attempts, sum(rush_yards) rush_yards, sum(rush_tds) rush_tds
        from {PWS} w join nfl.seasons s on s.id=w.season_id join nfl.teams t on t.id=w.team_id
        where w.game_type in ('REG','POST') and w.game_id is not null group by 1,2,3,4""")
    pwmap = {(r["yr"], r["wt"], r["st"], r["abbr"]): r for r in pw}
    matched = nosum = 0
    per = {a: {"ok": 0, "bad": 0, "worst": [], } for a, _ in pairs}
    for g in gs:
        p = pwmap.get((g["yr"], g["wt"], g["st"], g["abbr"]))
        if p is None:
            nosum += 1; continue
        matched += 1
        for pa, ga in pairs:
            pv, gv = p[pa] or 0, g[ga] or 0
            if pv == gv:
                per[pa]["ok"] += 1
            else:
                per[pa]["bad"] += 1
                per[pa]["worst"].append((abs(pv - gv), (g["yr"], g["wt"], g["st"], g["abbr"]), pv, gv))
    kv("game_stats team rows compared", f"{len(gs):,}")
    kv("matched to a pws key", f"{matched:,}   (unmatched {nosum:,})")
    for a, _ in pairs:
        d = per[a]; n = d["ok"] + d["bad"]
        pct = 100.0 * d["ok"] / n if n else 0.0
        d["worst"].sort(reverse=True)
        print(f"     {a:<18} exact {d['ok']:>5}/{n:<5} ({pct:6.2f}%)   worst: " +
              (", ".join(f"{x[1]} p={x[2]} gs={x[3]}" for x in d["worst"][:2]) or "-"))
    worst_att = sorted(per["pass_attempts"]["worst"], reverse=True)
    big = [w for w in worst_att if w[0] > 5 and tuple(w[1]) not in RECON_EXCEPTIONS]
    gate("team passing attempts reconcile (|diff|<=5)", not big,
         f"{len(big)} team-games off by >5  e.g. " + (", ".join(f"{w[1]} p={w[2]}/gs={w[3]}" for w in big[:4]) or "-"))

    # ---------- 5. ranges ----------
    head("5. RANGE SANITY")
    comp_gt_att = await c.fetchval(f"select count(*) from {PWS} where pass_completions>pass_attempts")
    rec_gt_tgt = await c.fetchval(f"select count(*) from {PWS} where receptions>targets")
    neg_pass = await c.fetchval(f"select count(*) from {PWS} where pass_yards<0")
    zero = await c.fetchval(f"""select count(*) from {PWS}
        where coalesce(pass_attempts,0)+coalesce(rush_attempts,0)+coalesce(targets,0)+coalesce(receptions,0)
            +coalesce(tackles_combined,0)+coalesce(field_goals_attempted,0)+coalesce(punts,0)
            +coalesce(kick_returns,0)+coalesce(punt_returns,0)=0""")
    kv("pass_completions > pass_attempts", f"{comp_gt_att:,}")
    kv("receptions > targets", f"{rec_gt_tgt:,}")
    kv("pass_yards < 0 (legit only on completed pass behind LOS)", f"{neg_pass:,}")
    kv("all-stat zero rows (participation w/o a recorded stat)", f"{zero:,} ({100.0*zero/total:.1f}%)")
    gate("no completions>attempts", comp_gt_att == 0, f"{comp_gt_att:,}")
    gate("no receptions>targets", rec_gt_tgt == 0, f"{rec_gt_tgt:,}")

    # ---------- summary ----------
    head("SUMMARY")
    if not FAILS:
        print("  ✅ all FAIL gates passed")
    for f in FAILS:
        print(f"  ❌ FAIL  {f}")
    for w in WARNS:
        print(f"  ⚠️  WARN  {w}")
    await c.close()
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
