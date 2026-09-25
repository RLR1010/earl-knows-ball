"""Shared MLB advanced-stat analytics (Baseball Savant / Statcast + Baseball-Reference WAR).

This module is the SINGLE read layer for the "new" MLB stats that were ingested into
``mlb.savant_*`` and ``mlb.bref_war_*`` tables:

    * Expected stats (xBA / xSLG / xwOBA / xERA)          -> savant_bat_expected / savant_pitch_expected
    * Quality of contact (EV, barrels, hard-hit, sweet-spot)-> savant_bat_exitvelo  / savant_pitch_exitvelo
    * Percentile rankings (all of the above + sprint/…)    -> savant_bat_percentile / savant_pitch_percentile
    * Pitch mix / run value (pitcher + batter)             -> savant_pitch_arsenal_stats / savant_bat_pitch_arsenal
    * Pitch movement / active spin                         -> savant_pitch_movement (pitch-level)
    * Pitch velocity by type                               -> savant_pitch_arsenal (per-type avg speed)
    * Fielding (OAA, directional, catch probability, jump) -> savant_fielding_oaa / _oaa_directional / _catch_prob / _jump
    * Catching (framing, pop time)                         -> savant_catcher_framing / savant_catcher_poptime
    * Baserunning (sprint speed, running splits)           -> savant_sprint_speed / savant_running_splits
    * WAR (batting + pitching)                             -> bref_war_bat / bref_war_pitch

Both the chat tools (``app.chat_tools.mlb``) and the deterministic writeup research brief
(``app.writeups.mlb.research``) call into this module so the two surfaces can never drift.

KEY: ``player_id`` / ``pitcher`` / ``pitcher_id`` / ``resp_fielder_id`` / ``entity_id`` in the
savant tables are MLBAM ids == ``mlb.players.mlb_id``. The bref WAR tables key on ``mlb_id`` too.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _f(v: Any) -> Any:
    """Round floats minimally / coerce numerics for JSON-ish output."""
    try:
        if v is None:
            return None
        return round(float(v), 4)
    except (TypeError, ValueError):
        return v


async def resolve_mlb_id(db: AsyncSession, player_name: str) -> dict | None:
    """Resolve a free-text player name to an ``mlb.players`` row (fuzzy, case-insensitive).

    Returns ``{player_id, mlb_id, name, team_id, position}`` or ``None``.
    """
    if not player_name:
        return None
    row = (
        await db.execute(
            text(
                """
                SELECT p.id AS player_id, p.mlb_id, p.name, p.team_id,
                       p.position, t.abbreviation AS team_abbr
                FROM mlb.players p
                LEFT JOIN mlb.teams t ON t.id = p.team_id
                WHERE p.mlb_id IS NOT NULL
                  AND (lower(p.name) = lower(:q) OR p.name ILIKE '%' || :q || '%')
                ORDER BY
                    CASE WHEN lower(p.name) = lower(:q) THEN 0 ELSE 1 END,
                    CASE WHEN COALESCE(p.active, 0) <> 0 THEN 0 ELSE 1 END,
                    p.name
                LIMIT 1
                """
            ),
            {"q": player_name.strip()},
        )
    ).mappings().first()
    return dict(row) if row else None


async def mlb_id_for_player_id(db: AsyncSession, player_id: int) -> int | None:
    """Map a local ``mlb.players.id`` to its MLBAM id (the key used by the savant tables)."""
    val = (
        await db.execute(
            text("SELECT mlb_id FROM mlb.players WHERE id = :pid"),
            {"pid": player_id},
        )
    ).scalar_one_or_none()
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# batting / expected
# --------------------------------------------------------------------------- #

async def batter_expected(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT pa, bip, ba, est_ba, est_ba_minus_ba_diff,
                       slg, est_slg, est_slg_minus_slg_diff,
                       woba, est_woba, est_woba_minus_woba_diff
                FROM mlb.savant_bat_expected
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def batter_quality(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT attempts, avg_hit_speed AS avg_ev, max_hit_speed AS max_ev,
                       ev50, ev95plus, ev95percent, barrels, brl_percent, brl_pa,
                       avg_hit_angle, anglesweetspotpercent AS sweet_spot_percent,
                       avg_distance, max_distance, avg_hr_distance, fbld, gb
                FROM mlb.savant_bat_exitvelo
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def batter_percentiles(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT xwoba, xba, xslg, xiso, xobp, brl, brl_percent,
                       exit_velocity, max_ev, hard_hit_percent, k_percent, bb_percent,
                       whiff_percent, chase_percent, arm_strength, sprint_speed,
                       oaa, bat_speed, squared_up_rate, swing_length
                FROM mlb.savant_bat_percentile
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def batter_vs_pitch_types(db: AsyncSession, mlb_id: int, year: int, limit: int = 12) -> list[dict]:
    """Per-pitch-type batter production (run value, xwOBA, whiff, hard-hit)."""
    rows = (
        await db.execute(
            text(
                """
                SELECT pitch_name, pitch_type, pitches, pitch_usage, pa,
                       run_value, run_value_per_100, ba, slg, woba,
                       est_ba, est_slg, est_woba, whiff_percent, k_percent,
                       put_away, hard_hit_percent
                FROM mlb.savant_bat_pitch_arsenal
                WHERE player_id = :pid AND year = :yr
                ORDER BY pitches DESC NULLS LAST
                LIMIT :lim
                """
            ),
            {"pid": mlb_id, "yr": year, "lim": limit},
        )
    ).mappings().all()
    return [{k: _f(v) for k, v in dict(r).items()} for r in rows]


# --------------------------------------------------------------------------- #
# pitching / expected
# --------------------------------------------------------------------------- #

async def pitcher_expected(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT pa, bip, ba, est_ba, est_ba_minus_ba_diff,
                       slg, est_slg, est_slg_minus_slg_diff,
                       woba, est_woba, est_woba_minus_woba_diff,
                       era, xera, era_minus_xera_diff
                FROM mlb.savant_pitch_expected
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def pitcher_quality_allowed(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT attempts, avg_hit_speed AS avg_ev, max_hit_speed AS max_ev,
                       ev50, ev95plus, ev95percent, barrels, brl_percent, brl_pa,
                       avg_hit_angle, anglesweetspotpercent AS sweet_spot_percent,
                       avg_distance, max_distance, avg_hr_distance, fbld, gb
                FROM mlb.savant_pitch_exitvelo
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def pitcher_percentiles(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT xwoba, xba, xslg, xiso, xera, xobp, brl, brl_percent,
                       exit_velocity, max_ev, hard_hit_percent, k_percent, bb_percent,
                       whiff_percent, chase_percent, arm_strength,
                       fb_velocity, fb_spin, curve_spin
                FROM mlb.savant_pitch_percentile
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def pitcher_arsenal(db: AsyncSession, mlb_id: int, year: int, limit: int = 12) -> list[dict]:
    """Per-pitch-type pitch mix + outcomes (usage, run value, whiff, xwOBA, put-away)."""
    rows = (
        await db.execute(
            text(
                """
                SELECT pitch_name, pitch_type, pitches, pitch_usage, pa,
                       run_value, run_value_per_100, ba, slg, woba,
                       est_ba, est_slg, est_woba, whiff_percent, k_percent,
                       put_away, hard_hit_percent
                FROM mlb.savant_pitch_arsenal_stats
                WHERE player_id = :pid AND year = :yr
                ORDER BY pitches DESC NULLS LAST
                LIMIT :lim
                """
            ),
            {"pid": mlb_id, "yr": year, "lim": limit},
        )
    ).mappings().all()
    return [{k: _f(v) for k, v in dict(r).items()} for r in rows]


async def pitcher_movement(db: AsyncSession, mlb_id: int, year: int, limit: int = 12) -> list[dict]:
    """Per-pitch-type movement (induced vertical break / horizontal tail vs league) + speed."""
    rows = (
        await db.execute(
            text(
                """
                SELECT pitch_type_name AS pitch_name, pitch_type, pitch_hand,
                       avg_speed, pitches_thrown, pitch_per,
                       pitcher_break_z AS induced_vertical_break,
                       league_break_z, diff_z, rise,
                       pitcher_break_x AS horizontal_tail_in,
                       league_break_x, diff_x, tail,
                       percent_rank_diff_z, percent_rank_diff_x
                FROM mlb.savant_pitch_movement
                WHERE pitcher_id = :pid AND year = :yr
                ORDER BY pitches_thrown DESC NULLS LAST
                LIMIT :lim
                """
            ),
            {"pid": mlb_id, "yr": year, "lim": limit},
        )
    ).mappings().all()
    return [{k: _f(v) for k, v in dict(r).items()} for r in rows]


async def pitcher_velocities(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    """Per-pitch-type average velocities (one row per pitcher/arsenal_type)."""
    row = (
        await db.execute(
            text(
                """
                SELECT arsenal_type,
                       ff_avg_speed, si_avg_speed, fc_avg_speed, sl_avg_speed,
                       ch_avg_speed, cu_avg_speed, fs_avg_speed, kn_avg_speed,
                       st_avg_speed, sv_avg_speed
                FROM mlb.savant_pitch_arsenal
                WHERE pitcher = :pid AND year = :yr
                ORDER BY CASE WHEN strpos(lower(arsenal_type), 'average') > 0 THEN 0 ELSE 1 END
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def pitcher_active_spin(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    """Per-pitch-type ACTIVE SPIN % — the share of raw spin that produces real movement."""
    row = (
        await db.execute(
            text(
                """
                SELECT pitch_hand,
                       active_spin_fourseam, active_spin_sinker, active_spin_cutter,
                       active_spin_changeup, active_spin_splitter, active_spin_curve,
                       active_spin_slider, active_spin_sweeper
                FROM mlb.savant_pitch_active_spin
                WHERE entity_id = :pid AND year = :yr
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    if not row:
        return None
    d = dict(row)
    out: dict[str, Any] = {"pitch_hand": d.get("pitch_hand")}
    for k, v in d.items():
        if k.startswith("active_spin_") and v is not None:
            out[k[len("active_spin_"):]] = _f(v)
    return out if len(out) > 1 else None


# --------------------------------------------------------------------------- #
# fielding / catching / baserunning
# --------------------------------------------------------------------------- #

async def fielding_oaa(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT primary_pos_formatted AS position, display_team_name AS team,
                       outs_above_average AS oaa, fielding_runs_prevented AS frp,
                       outs_above_average_infront AS oaa_in_front,
                       outs_above_average_lateral_toward3bline AS oaa_toward_3b_line,
                       outs_above_average_lateral_toward1bline AS oaa_toward_1b_line,
                       outs_above_average_behind AS oaa_behind,
                       outs_above_average_rhh AS oaa_vs_rhh,
                       outs_above_average_lhh AS oaa_vs_lhh,
                       actual_success_rate_formatted AS actual_success_rate,
                       adj_estimated_success_rate_formatted AS est_success_rate,
                       diff_success_rate_formatted AS success_rate_diff
                FROM mlb.savant_fielding_oaa
                WHERE player_id = :pid AND year = :yr
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return dict(row) if row else None


async def fielding_catch_prob(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT oaa, n_5star_percent AS five_star_pct, n_4star_percent AS four_star_pct,
                       n_3star_percent AS three_star_pct, n_2star_percent AS two_star_pct,
                       n_1star_percent AS one_star_pct,
                       n_fieldout_5stars AS five_star_made, n_opp_5stars AS five_star_opps,
                       n_fieldout_4stars AS four_star_made, n_opp_4stars AS four_star_opps,
                       n_fieldout_3stars AS three_star_made, n_opp_3stars AS three_star_opps,
                       n_fieldout_2stars AS two_star_made, n_opp_2stars AS two_star_opps,
                       n_fieldout_1stars AS one_star_made, n_opp_1stars AS one_star_opps
                FROM mlb.savant_fielding_catch_prob
                WHERE player_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def fielding_jump(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT outs_above_average AS oaa, outs_per_play, n, n_outs,
                       rel_league_burst_distance, rel_league_reaction_distance,
                       rel_league_routing_distance, rel_league_bootup_distance,
                       f_bootup_distance
                FROM mlb.savant_fielding_jump
                WHERE resp_fielder_id = :pid AND year = :yr
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def catcher_framing(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT name, pitches, rv_tot AS framing_runs, pct_tot AS framing_pct,
                       rv_11, rv_12, rv_13, rv_14, rv_16, rv_17, rv_18, rv_19
                FROM mlb.savant_catcher_framing
                WHERE player_id = :pid AND year = :yr
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def catcher_poptime(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT entity_name, age, pop_2b_sba AS pop_time_2b, pop_2b_sba_count,
                       pop_2b_cs, pop_2b_sb, pop_3b_sba AS pop_time_3b, pop_3b_sba_count,
                       pop_3b_cs, pop_3b_sb,
                       exchange_2b_3b_sba AS exchange_2b_3b,
                       maxeff_arm_2b_3b_sba AS max_eff_arm_2b_3b
                FROM mlb.savant_catcher_poptime
                WHERE entity_id = :pid AND year = :yr
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def sprint_speed(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT team, position, age, competitive_runs, bolts, hp_to_1b, sprint_speed
                FROM mlb.savant_sprint_speed
                WHERE player_id = :pid AND year = :yr
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


# --------------------------------------------------------------------------- #
# WAR
# --------------------------------------------------------------------------- #

async def war_batting(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT team_id AS team, g, pa, runs_above_avg, runs_above_avg_off,
                       runs_above_avg_def, waa, war
                FROM mlb.bref_war_bat
                WHERE mlb_id = :pid AND year_id = :yr
                ORDER BY war DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


async def war_pitching(db: AsyncSession, mlb_id: int, year: int) -> dict | None:
    row = (
        await db.execute(
            text(
                """
                SELECT team_id AS team, g, gs, ra, xra, bip, bip_perc,
                       era_plus, waa, waa_adj, war
                FROM mlb.bref_war_pitch
                WHERE mlb_id = :pid AND year_id = :yr
                ORDER BY war DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"pid": mlb_id, "yr": year},
        )
    ).mappings().first()
    return {k: _f(v) for k, v in dict(row).items()} if row else None


# --------------------------------------------------------------------------- #
# aggregate profiles
# --------------------------------------------------------------------------- #

async def batter_profile(db: AsyncSession, mlb_id: int, year: int) -> dict:
    """Full advanced batting profile: expected, quality of contact, percentiles, WAR, running, fielding."""
    return {
        "year": year,
        "mlb_id": mlb_id,
        "expected": await batter_expected(db, mlb_id, year),
        "quality_of_contact": await batter_quality(db, mlb_id, year),
        "percentiles": await batter_percentiles(db, mlb_id, year),
        "war": await war_batting(db, mlb_id, year),
        "sprint_speed": await sprint_speed(db, mlb_id, year),
        "fielding": await fielding_oaa(db, mlb_id, year),
        "vs_pitch_types": await batter_vs_pitch_types(db, mlb_id, year),
    }


async def pitcher_profile(db: AsyncSession, mlb_id: int, year: int) -> dict:
    """Full advanced pitching profile: expected, quality allowed, percentiles, arsenal, movement, WAR."""
    return {
        "year": year,
        "mlb_id": mlb_id,
        "expected": await pitcher_expected(db, mlb_id, year),
        "quality_allowed": await pitcher_quality_allowed(db, mlb_id, year),
        "percentiles": await pitcher_percentiles(db, mlb_id, year),
        "war": await war_pitching(db, mlb_id, year),
        "arsenal": await pitcher_arsenal(db, mlb_id, year),
        "movement": await pitcher_movement(db, mlb_id, year),
        "velocities": await pitcher_velocities(db, mlb_id, year),
        "active_spin": await pitcher_active_spin(db, mlb_id, year),
    }


async def player_advanced_profile(db: AsyncSession, mlb_id: int, year: int, role: str = "auto") -> dict:
    """Best-effort profile for either role.

    ``role`` in {``auto``, ``batter``, ``pitcher``}. ``auto`` returns both sides when data exists.
    """
    if role == "batter":
        return {"role": "batter", "batting": await batter_profile(db, mlb_id, year)}
    if role == "pitcher":
        return {"role": "pitcher", "pitching": await pitcher_profile(db, mlb_id, year)}

    bat = await batter_profile(db, mlb_id, year)
    pit = await pitcher_profile(db, mlb_id, year)
    has_bat = any(bat[k] for k in ("expected", "quality_of_contact", "percentiles", "war", "vs_pitch_types"))
    has_pit = any(pit[k] for k in ("expected", "quality_allowed", "percentiles", "war", "arsenal", "movement"))
    out: dict[str, Any] = {"year": year, "mlb_id": mlb_id}
    if has_bat:
        out["batting"] = bat
    if has_pit:
        out["pitching"] = pit
    out["role"] = "two_way" if (has_bat and has_pit) else ("batter" if has_bat else "pitcher")
    return out


# --------------------------------------------------------------------------- #
# league leaders
# --------------------------------------------------------------------------- #

# leader metric -> (table, value_col, label, min_col, min_val, ascending)
# NOTE: savant_*_percentile columns are PERCENTILE RANKS (0-100), not the underlying
# rate stat. The real xBA/xSLG/xwOBA values live in savant_bat_expected / savant_pitch_expected.
_BAT_LEADER_METRICS: dict[str, tuple[str, str, str, str, float, bool]] = {
    "xwoba":        ("mlb.savant_bat_expected",  "est_woba",       "xwOBA",                         "pa",       100, False),
    "xba":          ("mlb.savant_bat_expected",  "est_ba",         "xBA",                           "pa",       100, False),
    "xslg":         ("mlb.savant_bat_expected",  "est_slg",        "xSLG",                          "pa",       100, False),
    "barrel_pct":   ("mlb.savant_bat_exitvelo",  "brl_percent",    "Barrel %",                      "attempts",  50, False),
    "hard_hit":     ("mlb.savant_bat_exitvelo",  "ev95percent",    "Hard-Hit % (95+ mph)",          "attempts",  50, False),
    "avg_ev":       ("mlb.savant_bat_exitvelo",  "avg_hit_speed",  "Average Exit Velocity",         "attempts",  50, False),
    "max_ev":       ("mlb.savant_bat_exitvelo",  "max_hit_speed",  "Max Exit Velocity",             "attempts",  50, False),
    "sprint_speed": ("mlb.savant_sprint_speed",  "sprint_speed",   "Sprint Speed (ft/s)",           "competitive_runs", 20, False),
    "oaa":          ("mlb.savant_fielding_oaa",  "outs_above_average", "Outs Above Average (fielding)", None,     0, False),
    "war":          ("mlb.bref_war_bat",         "war",            "WAR (bWAR, batting)",           "pa",       100, False),
}

_PIT_LEADER_METRICS: dict[str, tuple[str, str, str, str, float, bool]] = {
    "xera":        ("mlb.savant_pitch_expected",  "xera",         "xERA (lower is better)",           "pa",        100, True),
    "xwoba":       ("mlb.savant_pitch_expected",  "est_woba",     "xwOBA allowed (lower is better)",  "pa",        100, True),
    "xba":         ("mlb.savant_pitch_expected",  "est_ba",       "xBA allowed (lower is better)",    "pa",        100, True),
    "xslg":        ("mlb.savant_pitch_expected",  "est_slg",      "xSLG allowed",                     "pa",        100, True),
    "barrel_pct":  ("mlb.savant_pitch_exitvelo",  "brl_percent",  "Barrel % allowed (lower is better)", "attempts",  50, True),
    "hard_hit":    ("mlb.savant_pitch_exitvelo",  "ev95percent",  "Hard-Hit % allowed (lower is better)", "attempts", 50, True),
    "avg_ev":      ("mlb.savant_pitch_exitvelo",  "avg_hit_speed","Avg EV allowed (lower is better)",  "attempts",  50, True),
    "k_percent":   ("mlb.savant_pitch_percentile","k_percent",    "K % (percentile rank)",                 None,         0, False),
    "whiff":       ("mlb.savant_pitch_percentile","whiff_percent","Whiff % (percentile rank)",             None,         0, False),
    "fb_velocity": ("mlb.savant_pitch_percentile","fb_velocity",  "Fastball velocity (percentile rank)",  None,         0, False),
    "war":         ("mlb.bref_war_pitch",         "war",          "WAR (bWAR, pitching)",              "pa",        50, False),
}


def _leader_metric_aliases(side: str, metric: str) -> str:
    key = (metric or "").strip().lower().replace(" ", "_").replace("%", "")
    common = {
        "barrels": "barrel_pct", "brl_percent": "barrel_pct", "barrel": "barrel_pct",
        "hardhit": "hard_hit", "hard_hit_percent": "hard_hit", "ev95": "hard_hit",
        "exit_velocity": "avg_ev", "avg_exit_velocity": "avg_ev", "ev": "avg_ev",
        "average_ev": "avg_ev", "max_exit_velocity": "max_ev",
        "expected_woba": "xwoba", "expected_ba": "xba", "expected_slg": "xslg",
    }
    bat = {"sprint": "sprint_speed", "outs_above_average": "oaa", "bwar": "war", "fwar": "war",
           "war_bat": "war"}
    pit = {"expected_era": "xera", "k": "k_percent", "k_rate": "k_percent", "so": "k_percent",
           "whiff_percent": "whiff", "fastball_velocity": "fb_velocity", "fb_velo": "fb_velocity",
           "war_pitch": "war"}
    key = common.get(key, key)
    key = (pit if side == "pitch" else bat).get(key, key)
    return key


async def league_leaders(
    db: AsyncSession,
    metric: str,
    year: int,
    side: str = "bat",
    limit: int = 10,
) -> dict:
    """Top players for a savant/WAR metric (season-level).

    ``side`` in {``bat``, ``pitch``}. Lower-is-better pitching rate stats (xERA, xwOBA/xBA/
    xSLG allowed, barrel%/hard-hit%/avg-EV allowed) are sorted ASCENDING.
    """
    side = "pitch" if side in ("pitch", "pitching", "pit", "p") else "bat"
    metrics = _PIT_LEADER_METRICS if side == "pitch" else _BAT_LEADER_METRICS
    key = _leader_metric_aliases(side, metric)
    if key not in metrics:
        return {
            "error": "unknown_metric",
            "metric": metric,
            "side": side,
            "valid_metrics": sorted(metrics.keys()),
        }
    table, col, label, min_col, min_val, asc = metrics[key]
    order = "ASC" if asc else "DESC"

    params: dict[str, Any] = {"yr": year, "lim": limit}
    if table.startswith("mlb.bref_war"):
        # bref WAR tables key on year_id + mlb_id
        join_col = "b.mlb_id"
        name_expr = "COALESCE(wp.name, b.mlb_id::text)"
        name_join = "LEFT JOIN mlb.players wp ON wp.mlb_id = b.mlb_id"
        where = [f"b.{col} IS NOT NULL"]
        if min_col:
            where.append(f"b.{min_col} >= :minv")
            params["minv"] = min_val
        sql = f"""
            SELECT b.mlb_id AS player_id, {name_expr} AS player_name, b.{col} AS value
            FROM {table} b
            {name_join}
            WHERE b.year_id = :yr AND {' AND '.join(where)}
            ORDER BY b.{col} {order} NULLS LAST
            LIMIT :lim
        """
    else:
        name_col = "player_name" if "percentile" in table else "last_name_first_name"
        where = [f"t.{col} IS NOT NULL"]
        if min_col:
            where.append(f"t.{min_col} >= :minv")
            params["minv"] = min_val
        sql = f"""
            SELECT t.player_id,
                   COALESCE(wp.name, t.{name_col}, t.player_id::text) AS player_name,
                   t.{col} AS value
            FROM {table} t
            LEFT JOIN mlb.players wp ON wp.mlb_id = t.player_id
            WHERE t.year = :yr AND {' AND '.join(where)}
            ORDER BY t.{col} {order} NULLS LAST
            LIMIT :lim
        """

    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "year": year,
        "side": side,
        "metric": key,
        "label": label,
        "direction": "ascending (lower is better)" if asc else "descending (higher is better)",
        "leaders": [{k: _f(v) for k, v in dict(r).items()} for r in rows],
    }


# --------------------------------------------------------------------------- #
# team-level (research brief)
# --------------------------------------------------------------------------- #

async def team_hitter_advanced(db: AsyncSession, team_abbr: str, year: int, min_pa: int = 100) -> list[dict]:
    """Advanced batting line for a team's hitters (joined by team on savant/expected tables).

    Uses ``player_id`` -> ``mlb.players`` to restrict to the requested team.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT p.name AS player_name, p.mlb_id,
                       e.pa, e.ba, e.est_ba, e.slg, e.est_slg, e.woba, e.est_woba,
                       ex.avg_hit_speed AS avg_ev, ex.max_hit_speed AS max_ev,
                       ex.brl_percent, ex.ev95percent AS hard_hit_percent,
                       pc.hard_hit_percent AS pctl_hard_hit,
                       w.war
                FROM mlb.savant_bat_expected e
                JOIN mlb.players p ON p.mlb_id = e.player_id
                JOIN mlb.teams t ON t.id = p.team_id
                LEFT JOIN mlb.savant_bat_exitvelo ex
                       ON ex.player_id = e.player_id AND ex.year = e.year
                LEFT JOIN mlb.savant_bat_percentile pc
                       ON pc.player_id = e.player_id AND pc.year = e.year
                LEFT JOIN mlb.bref_war_bat w
                       ON w.mlb_id = e.player_id AND w.year_id = e.year
                WHERE e.year = :yr AND e.pa >= :min_pa
                  AND upper(t.abbreviation) = upper(:tm)
                ORDER BY e.pa DESC
                """
            ),
            {"yr": year, "tm": team_abbr, "min_pa": min_pa},
        )
    ).mappings().all()
    return [{k: _f(v) for k, v in dict(r).items()} for r in rows]


async def team_pitcher_advanced(db: AsyncSession, team_abbr: str, year: int, min_pa: int = 50) -> list[dict]:
    """Advanced line for a team's pitchers (xERA, K%, barrels allowed, WAR)."""
    rows = (
        await db.execute(
            text(
                """
                SELECT p.name AS player_name, p.mlb_id,
                       e.pa AS bf, e.ba, e.est_ba, e.slg, e.est_slg,
                       e.woba, e.est_woba, e.era, e.xera,
                       ex.avg_hit_speed AS avg_ev_allowed,
                       ex.brl_percent AS barrel_pct_allowed,
                       ex.ev95percent AS hard_hit_percent_allowed,
                       w.war
                FROM mlb.savant_pitch_expected e
                JOIN mlb.players p ON p.mlb_id = e.player_id
                JOIN mlb.teams t ON t.id = p.team_id
                LEFT JOIN mlb.savant_pitch_exitvelo ex
                       ON ex.player_id = e.player_id AND ex.year = e.year
                LEFT JOIN mlb.bref_war_pitch w
                       ON w.mlb_id = e.player_id AND w.year_id = e.year
                WHERE e.year = :yr AND e.pa >= :min_pa
                  AND upper(t.abbreviation) = upper(:tm)
                ORDER BY e.pa DESC
                """
            ),
            {"yr": year, "tm": team_abbr, "min_pa": min_pa},
        )
    ).mappings().all()
    return [{k: _f(v) for k, v in dict(r).items()} for r in rows]
