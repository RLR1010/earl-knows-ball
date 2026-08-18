"use client";
import { useEffect, useState, useRef } from "react";
import * as React from "react";
import { useParams, useSearchParams, useRouter } from "next/navigation";
import { api, Game, formatOverUnder } from "@/lib/api";
import TeamLogo from "@/components/TeamLogo";
import GameCalendar from "@/components/GameCalendar";
import ChatCardLink from "@/components/ChatCardLink";
import { buildGameContext } from "@/components/ScheduleGameCard";
import { useSeo } from "@/components/Seo";
import EarlsPicksPanel, { type EarlsPickItem } from "@/components/EarlsPicksPanel";

// Regular season (1-18) + playoffs (19-22). Preseason weeks are stored in a
// distinct range (30-33) so they never collide with the regular season.
const REGULAR_WEEKS = Array.from({ length: 22 }, (_, i) => i + 1);
const PRESEASON_WEEKS = [30, 31, 32, 33]; // stored as 29 + preseason_week
// One continuous week list: preseason comes first, then regular season + playoffs.
const ALL_WEEKS = [...PRESEASON_WEEKS, ...REGULAR_WEEKS];

const PRESEASON_LABELS: Record<number, string> = {
  30: "PS Week 1",
  31: "PS Week 2",
  32: "PS Week 3",
  33: "PS Week 4",
};
const PLAYOFF_LABELS: Record<number, string> = {
  19: "Wild Card",
  20: "Divisional",
  21: "Conf Champ",
  22: "Super Bowl",
};
function weekLabel(w: number): string {
  if (w >= 30 && w <= 33) return PRESEASON_LABELS[w] || `PS Week ${w - 29}`;
  if (w >= 19) return PLAYOFF_LABELS[w] || `Week ${w}`;
  return `Week ${w}`;
}
function todayStr(): string {
  const d = new Date();
  const offset = d.getTimezoneOffset();
  const local = new Date(d.getTime() - offset * 60_000);
  return local.toISOString().slice(0, 10);
}

/** Resolve a moneyline pick ("home" / "away" / team abbr / numeric team id) to a display team. */
function resolvePickTeam(pick: string | null | undefined, home: string | null | undefined, away: string | null | undefined): string | null {
  if (!pick) return null;
  const p = String(pick).trim();
  if (p.toLowerCase() === "home") return home ?? null;
  if (p.toLowerCase() === "away") return away ?? null;
  return p;
}

/** Build the three premium pick items (Run Line / Over-Under / Moneyline) for a game. */
function buildPickItems(o: {
  spreadPick?: string | null; overUnder?: string | null; mlPick?: string | null;
  atsEv?: number | null; ouEv?: number | null; mlEv?: number | null;
  spreadResult?: string | null; ouResult?: string | null; mlResult?: string | null;
  home?: string | null; away?: string | null;
  spreadLabel: string;
}): EarlsPickItem[] {
  const spreadPick = o.spreadPick ? String(o.spreadPick) : undefined;
  const ouPick = o.overUnder ? String(o.overUnder).toUpperCase() : undefined;
  const mlPick = resolvePickTeam(o.mlPick, o.home, o.away) ?? undefined;
  return [
    { label: o.spreadLabel, pick: spreadPick ?? "—", ev: o.atsEv ?? null, result: o.spreadResult ?? null },
    { label: "Over/Under", pick: ouPick ?? "—", ev: o.ouEv ?? null, result: o.ouResult ?? null },
    { label: "Moneyline", pick: mlPick ?? "—", ev: o.mlEv ?? null, result: o.mlResult ?? null },
  ];
}

/** True if the game has any premium pick data worth rendering. */
function hasPicks(p: { spread?: string | null; ou?: string | null; ml?: string | null }): boolean {
  return !!(p.spread || p.ou || p.ml);
}

/** Format a moneyline integer to American odds string (e.g. -132, +110). */
function formatMoneyline(ml: number | null | undefined): string {
  if (ml === null || ml === undefined) return "-";
  const n = Math.round(ml);
  return n > 0 ? `+${n}` : `${n}`;
}

/** Return "favorite team | line" or "Pick'em". spread is from the home team's perspective. */
function favoredSpread(spread: number | null | undefined, home: string | null | undefined, away: string | null | undefined): string {
  if (spread === null || spread === undefined) return "-";
  if (Math.abs(spread) < 0.05) return "Pick'em";
  const line = spread > 0 ? `-${spread}` : `+${Math.abs(spread)}`;
  const team = spread < 0 ? (home ?? "?") : (away ?? "?");
  return `${team} ${line}`;
}

interface NBAGame {
  id: number;
  nba_game_id: number | null;
  game_type: string;
  date: string;
  status: string;
  home_team: string;
  away_team: string;
  home_score: number | null;
  away_score: number | null;
  venue: string | null;
  attendance: number | null;
  spread: number | null;
  over_under: number | null;
  home_moneyline: number | null;
  away_moneyline: number | null;
  pick_spread: string | null;
  pick_over_under: string | null;
  pick_moneyline: string | null;
  pick_ats_ev: number | null;
  pick_ou_ev: number | null;
  pick_ml_ev: number | null;
  result_spread: string | null;
  result_over_under: string | null;
  result_moneyline: string | null;
}

interface MLBGame {
  id: number;
  mlb_game_id: number | null;
  game_type: string;
  date: string;
  status: string;
  home_team: string;
  away_team: string;
  home_score: number | null;
  away_score: number | null;
  venue: string | null;
  scheduled_innings: number;
  actual_innings: number | null;
  attendance: number | null;
  duration_minutes: number | null;
  day_night: string | null;
  spread: number | null;
  over_under: number | null;
  predicted_margin: number | null;
  pred_ml_result: string | null;
  pred_rl_result: string | null;
  pred_ou_result: string | null;
  home_moneyline: number | null;
  away_moneyline: number | null;
  pick_spread: string | null;
  pick_over_under: string | null;
  pick_moneyline: string | null;
  pick_ats_ev: number | null;
  pick_ou_ev: number | null;
  pick_ml_ev: number | null;
  result_spread: string | null;
  result_over_under: string | null;
  result_moneyline: string | null;
}

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  }) + " ET";
}

function statusBadge(status: string): { label: string; cls: string } {
  switch (status.toLowerCase()) {
    case "final": return { label: "FINAL", cls: "text-green-400" };
    case "in_progress": return { label: "LIVE", cls: "text-red-400 animate-pulse" };
    case "postponed": return { label: "PPD", cls: "text-yellow-400" };
    case "cancelled": return { label: "CANC", cls: "text-gray-500" };
    default: return { label: status.toUpperCase() || "SCHEDULED", cls: "text-earl-400" };
  }
}

// ── NBA season date ranges (Oct YYYY through Jun YYYY+1) ──────────
const NBA_SEASON_FIRST: Record<number, string> = {
  2006: "2006-11-01", 2007: "2007-10-30", 2008: "2008-10-29", 2009: "2009-10-27",
  2010: "2010-10-26", 2011: "2011-12-25", 2012: "2012-10-30", 2013: "2013-10-29",
  2014: "2014-10-28", 2015: "2015-10-27", 2016: "2016-10-25", 2017: "2017-10-17",
  2018: "2018-10-16", 2019: "2019-10-22", 2020: "2020-12-22", 2021: "2021-10-19",
  2022: "2022-10-18", 2023: "2023-10-24", 2024: "2024-10-22", 2025: "2025-10-22",
  2026: "2026-10-20",
};

const NBA_SEASON_LAST: Record<number, string> = {
  2006: "2007-04-18", 2007: "2008-04-16", 2008: "2009-04-15", 2009: "2010-04-14",
  2010: "2011-04-13", 2011: "2012-04-26", 2012: "2013-04-17", 2013: "2014-04-16",
  2014: "2015-04-15", 2015: "2016-04-13", 2016: "2017-04-12", 2017: "2018-04-11",
  2018: "2019-04-10", 2019: "2020-03-11", 2020: "2021-05-16", 2021: "2022-04-10",
  2022: "2023-04-09", 2023: "2024-04-14", 2024: "2025-04-13", 2025: "2026-04-15",
  2026: "2027-04-14",
};

// ── MLB season date ranges ──────────────────────────────────
const SEASON_OPENING: Record<number, string> = {
  2006: "2006-04-03", 2007: "2007-04-02", 2008: "2008-03-25", 2009: "2009-04-06",
  2010: "2010-04-05", 2011: "2011-03-31", 2012: "2012-03-28", 2013: "2013-04-01",
  2014: "2014-03-22", 2015: "2015-04-06", 2016: "2016-04-03", 2017: "2017-04-02",
  2018: "2018-03-29", 2019: "2019-03-20", 2020: "2020-07-23", 2021: "2021-04-01",
  2022: "2022-04-07", 2023: "2023-03-30", 2024: "2024-03-20", 2025: "2025-03-18",
  2026: "2026-03-26",
};

const SEASON_LAST: Record<number, string> = {
  2006: "2006-10-01", 2007: "2007-10-01", 2008: "2008-09-30", 2009: "2009-10-06",
  2010: "2010-10-03", 2011: "2011-09-29", 2012: "2012-10-04", 2013: "2013-10-01",
  2014: "2014-09-28", 2015: "2015-10-04", 2016: "2016-10-02", 2017: "2017-10-01",
  2018: "2018-10-01", 2019: "2019-09-29", 2020: "2020-09-27", 2021: "2021-10-03",
  2022: "2022-10-05", 2023: "2023-10-01", 2024: "2024-09-29", 2025: "2025-09-28",
  2026: "2026-09-27",
};

const CURRENT_YEAR = 2026;

function nbaFirstGame(year: number): string {
  return NBA_SEASON_FIRST[year] || `${year}-10-25`;
}

function nbaLastGame(year: number): string {
  return NBA_SEASON_LAST[year] || `${year+1}-04-15`;
}

function mlbOpeningDay(year: number): string {
  return SEASON_OPENING[year] || `${year}-04-01`;
}

function mlbLastDay(year: number): string {
  return SEASON_LAST[year] || `${year}-10-01`;
}

// ════════════════════════════════════════════════════════════════════
// NBA Schedule (day-by-day, like MLB)
// ════════════════════════════════════════════════════════════════════
function NBASchedule({ sport }: { sport: string }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [availableSeasons, setAvailableSeasons] = useState<number[]>([]);
  const [year, setYear] = useState(() => {
    const yp = searchParams.get('year');
    return yp ? parseInt(yp) : CURRENT_YEAR;
  });
  const [selectedDate, setSelectedDate] = useState(() => {
    return searchParams.get('date') || todayStr();
  });
  const [games, setGames] = useState<NBAGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCalendar, setShowCalendar] = useState(false);
  const [gameDates, setGameDates] = useState<string[]>([]);

  // Fetch all game dates for the calendar
  useEffect(() => {
    fetch(`/api/nba/games/dates?year=${year}`)
      .then(r => r.json())
      .then((dates: string[]) => setGameDates(dates))
      .catch(() => {});
  }, [year]);

  // Fetch available seasons from backend (only years with games)
  useEffect(() => {
    fetch('/api/nba/seasons')
      .then(r => r.json())
      .then((seasons: number[]) => {
        setAvailableSeasons(seasons);
        if (seasons.length > 0 && !seasons.includes(year)) {
          setYear(seasons[0]); // most recent season with games
        }
      })
      .catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const isCurrentYear = year === CURRENT_YEAR;
  const seasonFirst = nbaFirstGame(year);
  const seasonLast = nbaLastGame(year);
  // Compute actual first/last game dates from fetched data, falling back to hardcoded values
  const maxDate = gameDates.length > 0 ? gameDates[gameDates.length - 1] : seasonLast;
  const minDate = gameDates.length > 0 ? gameDates[0] : seasonFirst;

  // Auto-search: when initial date has no games, query the DB for nearest date with games
  const autoSearchRef = useRef<'idle' | 'done'>('idle');
  const cancelSearchRef = useRef(false);

  // Start auto-search on mount if no date was explicitly in the URL
  useEffect(() => {
    if (!searchParams.get('date')) {
      autoSearchRef.current = 'idle';
    } else {
      autoSearchRef.current = 'done';
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Sync URL when year or date changes
  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('year', String(year));
    params.set('date', selectedDate);
    router.replace(`/${sport}/schedule?${params.toString()}`, { scroll: false });
  }, [year, selectedDate]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ year: String(year), date: selectedDate });
    fetch(`/api/nba/games?${params}`)
      .then(r => r.json())
      .then((data: NBAGame[]) => {
        setGames(data.filter(g => g.game_type === "REG" || g.game_type === "POST"));
      })
      .catch(e => console.error(e))
      .finally(() => setLoading(false));
  }, [year, selectedDate]);

  // Auto-search effect: when games load empty, query backend for nearest date with games
  useEffect(() => {
    if (autoSearchRef.current === 'done' || loading) return;

    if (games.length === 0 && !loading) {
      autoSearchRef.current = 'done';
      cancelSearchRef.current = false;
      findNearestGame(year, selectedDate, sport);
    } else if (games.length > 0) {
      autoSearchRef.current = 'done';
    }
  }, [year, selectedDate, loading, games]); // eslint-disable-line react-hooks/exhaustive-deps

  async function findNearestGame(currentYear: number, date: string, s: string) {
    if (currentYear < 2009) return;
    try {
      const r = await fetch(`/api/${s}/games/nearest-date?year=${currentYear}&date=${encodeURIComponent(date)}`);
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        if (!cancelSearchRef.current) {
          setYear(res.year);
          setSelectedDate(res.date);
        }
      } else {
        if (!cancelSearchRef.current) {
          findNearestGame(currentYear - 1, date, s);
        }
      }
    } catch {}
  }

  // Auto-poll live scores + completed-game pick results every 30s for the
  // current year at ANY selected date (not just today) so pick result color
  // coding updates automatically after games finish.
  useEffect(() => {
    if (!isCurrentYear) return;
    const interval = setInterval(() => {
      const params = new URLSearchParams({ year: String(CURRENT_YEAR), date: selectedDate });
      fetch(`/api/nba/games?${params}`)
        .then(r => r.json())
        .then((data: NBAGame[]) => setGames(data.filter(g => g.game_type === "REG" || g.game_type === "POST")))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [isCurrentYear, selectedDate]);

  async function goDay(delta: number) {
    autoSearchRef.current = 'done';
    cancelSearchRef.current = true;
    const direction = delta > 0 ? 'forward' : 'backward';
    try {
      const r = await fetch(`/api/nba/games/nearest-date?year=${year}&date=${encodeURIComponent(selectedDate)}&direction=${direction}`);
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        setYear(res.year);
        setSelectedDate(res.date);
      }
    } catch {}
  }

  const dateObj = selectedDate ? (() => {
    const d = new Date(selectedDate + "T12:00:00-06:00");
    return d;
  })() : new Date();
  
  const dateLabel = dateObj.toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
    timeZone: "America/New_York",
  });

  return (
    <>
      <h1 className="font-display text-4xl font-bold">NBA Schedule</h1>

      <div className="flex items-center gap-3">
        <select
          value={year}
          onChange={async (e) => {
            const newYear = Number(e.target.value);
            setYear(newYear);
            // Default to the last game of the new season
            try {
              const r = await fetch(`/api/nba/games/nearest-date?year=${newYear}&date=2099-12-31&direction=backward`);
              const res: { date: string | null; year: number | null } = await r.json();
              if (res.date && res.year) {
                setSelectedDate(res.date);
                if (res.year !== newYear) setYear(res.year);
              }
            } catch {}
          }}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {availableSeasons.map(y => (
            <option key={y} value={y} className="text-black">{y}-{y + 1} Season</option>
          ))}
        </select>

        <button
          onClick={() => goDay(-1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >←</button>

        <div className="relative">
          <button
            onClick={() => setShowCalendar(!showCalendar)}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 hover:bg-white/10 transition"
          >
            {selectedDate}
          </button>
          {showCalendar && (
            <GameCalendar
              gameDates={gameDates}
              selectedDate={selectedDate}
              onSelect={(d) => { cancelSearchRef.current = true; autoSearchRef.current = 'done'; setSelectedDate(d); }}
              onClose={() => setShowCalendar(false)}
              minDate={minDate}
              maxDate={maxDate}
            />
          )}
        </div>

        {isCurrentYear && (
          <button
            onClick={() => { autoSearchRef.current = 'idle'; setSelectedDate(todayStr()); }}
            className="hidden sm:block px-3 py-1.5 rounded-lg bg-earl-600/20 border border-earl-500/30 text-xs text-earl-400 hover:bg-earl-600/30 transition"
          >Today</button>
        )}

        <button
          onClick={() => goDay(1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >→</button>
      </div>

      <p className="text-lg text-gray-300 font-medium mb-4">{dateLabel}</p>

      {/* Games list */}
      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      ) : games.length === 0 ? (
        <div className="text-center py-12 text-gray-500">No games scheduled for this date.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {games.map((g) => {
            const badge = statusBadge(g.status);
            const isFinal = g.status.toLowerCase() === "final";
            const isLive = g.status.toLowerCase() === "in_progress";
            const homeWon = isFinal && g.home_score! > g.away_score!;
            const awayWon = isFinal && g.away_score! > g.home_score!;

            return (
              <ChatCardLink
                key={g.id}
                href={`/${sport}/games/${g.id}?year=${year}&date=${selectedDate}`}
                sport={sport as "nfl" | "nba" | "mlb"}
                homeTeam={g.home_team ?? ""}
                awayTeam={g.away_team ?? ""}
                date={g.date ?? null}
                context={buildGameContext(sport as "mlb" | "nba" | "nfl", g)}
                hideChat={(g.status ?? "").toLowerCase() === "final"}
                className="block text-center border border-white/10 rounded-xl p-3"
              >
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && <TeamLogo abbr={g.away_team} sport="nba" size={20} />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && <TeamLogo abbr={g.home_team} sport="nba" size={20} />}
                </div>

                <div className="mt-1.5">
                  <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>{badge.label}</span>
                  {!isFinal && !isLive ? (
                    <div className="text-xs text-gray-500 mt-1">{formatTime(g.date)}</div>
                  ) : (
                    <div className="h-4 mt-1" aria-hidden="true" />
                  )}
                </div>

                {/* Betting lines: spread (favored/Pick'em), moneyline, over/under */}
                {(g.spread != null || g.over_under != null) && (
                  <div className="mt-3 pt-3 pb-1 border-t border-white/10 text-xs text-center">
                    <div className="text-gray-400">
                      <span className="text-earl-300">{favoredSpread(g.spread, g.home_team, g.away_team)}</span>
                      <span className="mx-2 text-gray-700">|</span>
                      <span>{formatMoneyline(g.home_moneyline)}/{formatMoneyline(g.away_moneyline)}</span>
                      {g.over_under != null && <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>}
                    </div>
                  </div>
                )}

                {/* Premium picks (self-gated) */}
                {hasPicks({ spread: g.pick_spread, ou: g.pick_over_under, ml: g.pick_moneyline }) && (
                  <div className="mt-2">
                    <EarlsPicksPanel
                      compact
                      items={buildPickItems({
                        spreadPick: g.pick_spread,
                        overUnder: g.pick_over_under,
                        mlPick: g.pick_moneyline,
                        atsEv: g.pick_ats_ev,
                        ouEv: g.pick_ou_ev,
                        mlEv: g.pick_ml_ev,
spreadResult: g.result_spread,
ouResult: g.result_over_under,
mlResult: g.result_moneyline,
                        home: g.home_team,
                        away: g.away_team,
                        spreadLabel: "Spread",
                      })}
                    />
                  </div>
                )}
              </ChatCardLink>
            );
          })}
        </div>
      )}
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// MLB Schedule (existing day-by-day)
// ════════════════════════════════════════════════════════════════════
function MLBSchedule({ sport }: { sport: string }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [availableSeasons, setAvailableSeasons] = useState<number[]>([]);
  const [year, setYear] = useState(() => {
    const yp = searchParams.get('year');
    return yp ? parseInt(yp) : CURRENT_YEAR;
  });
  const [selectedDate, setSelectedDate] = useState(() => {
    return searchParams.get('date') || todayStr();
  });
  const [games, setGames] = useState<MLBGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCalendar, setShowCalendar] = useState(false);
  const [gameDates, setGameDates] = useState<string[]>([]);

  // Fetch all game dates for the calendar
  useEffect(() => {
    fetch(`/api/mlb/games/dates?year=${year}`)
      .then(r => r.json())
      .then((dates: string[]) => setGameDates(dates))
      .catch(() => {});
  }, [year]);

  // Fetch available seasons from backend (only years with games)
  useEffect(() => {
    fetch('/api/mlb/seasons')
      .then(r => r.json())
      .then((seasons: number[]) => {
        setAvailableSeasons(seasons);
        if (seasons.length > 0 && !seasons.includes(year)) {
          setYear(seasons[0]); // most recent season with games
        }
      })
      .catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const isCurrentYear = year === CURRENT_YEAR;
  const seasonFirst = mlbOpeningDay(year);
  const seasonLast = mlbLastDay(year);
  // Compute actual first/last game dates from fetched data, falling back to hardcoded values
  const maxDate = gameDates.length > 0 ? gameDates[gameDates.length - 1] : seasonLast;
  const minDate = gameDates.length > 0 ? gameDates[0] : seasonFirst;

  // Auto-search: when initial date has no games, query the DB for nearest date with games
  const autoSearchRef = useRef<'idle' | 'done'>('idle');
  const cancelSearchRef = useRef(false);

  // Start auto-search on mount if no date was explicitly in the URL
  useEffect(() => {
    if (!searchParams.get('date')) {
      autoSearchRef.current = 'idle';
    } else {
      autoSearchRef.current = 'done';
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Sync URL when year or date changes
  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('year', String(year));
    params.set('date', selectedDate);
    router.replace(`/${sport}/schedule?${params.toString()}`, { scroll: false });
  }, [year, selectedDate]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ year: String(year), date: selectedDate });
    fetch(`/api/mlb/games?${params}`)
      .then(r => r.json())
      .then((data: MLBGame[]) => setGames(data))
      .catch(e => console.error(e))
      .finally(() => setLoading(false));
  }, [year, selectedDate]);

  // Auto-search effect: when games load empty, query backend for nearest date with games
  useEffect(() => {
    if (autoSearchRef.current === 'done' || loading) return;

    if (games.length === 0 && !loading) {
      autoSearchRef.current = 'done';
      cancelSearchRef.current = false;
      findNearestGame(year, selectedDate, sport);
    } else if (games.length > 0) {
      autoSearchRef.current = 'done';
    }
  }, [year, selectedDate, loading, games]); // eslint-disable-line react-hooks/exhaustive-deps

  async function findNearestGame(currentYear: number, date: string, s: string) {
    if (currentYear < 2009) return;
    try {
      const r = await fetch(`/api/${s}/games/nearest-date?year=${currentYear}&date=${encodeURIComponent(date)}`);
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        if (!cancelSearchRef.current) {
          setYear(res.year);
          setSelectedDate(res.date);
        }
      } else {
        if (!cancelSearchRef.current) {
          findNearestGame(currentYear - 1, date, s);
        }
      }
    } catch {}
  }

  useEffect(() => {
    if (!isCurrentYear) return;
    const interval = setInterval(() => {
      const params = new URLSearchParams({ year: String(CURRENT_YEAR), date: selectedDate });
      fetch(`/api/mlb/games?${params}`)
        .then(r => r.json())
        .then((data: MLBGame[]) => setGames(data))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [isCurrentYear, selectedDate]);

  async function goDay(delta: number) {
    autoSearchRef.current = 'done';
    cancelSearchRef.current = true;
    const direction = delta > 0 ? 'forward' : 'backward';
    try {
      const r = await fetch(`/api/mlb/games/nearest-date?year=${year}&date=${encodeURIComponent(selectedDate)}&direction=${direction}`);
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        setYear(res.year);
        setSelectedDate(res.date);
      }
    } catch {}
  }

  const dateObj = (() => {
    const d = new Date(selectedDate + "T12:00:00-05:00");
    return d;
  })();

  const dateLabel = dateObj.toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
    timeZone: "America/New_York",
  });

  return (
    <>
      <h1 className="font-display text-4xl font-bold">MLB Schedule</h1>

      <div className="flex items-center gap-3">
        <select
          value={year}
          onChange={async (e) => {
            const newYear = Number(e.target.value);
            setYear(newYear);
            // Default to the last game of the new season
            try {
              const r = await fetch(`/api/mlb/games/nearest-date?year=${newYear}&date=2099-12-31&direction=backward`);
              const res: { date: string | null; year: number | null } = await r.json();
              if (res.date && res.year) {
                setSelectedDate(res.date);
                if (res.year !== newYear) setYear(res.year);
              }
            } catch {}
          }}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {availableSeasons.map(y => (
            <option key={y} value={y} className="text-black">{y} Season</option>
          ))}
        </select>

        <button onClick={() => goDay(-1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >←</button>

        <div className="relative">
          <button
            onClick={() => setShowCalendar(!showCalendar)}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 hover:bg-white/10 transition"
          >
            {selectedDate}
          </button>
          {showCalendar && (
            <GameCalendar
              gameDates={gameDates}
              selectedDate={selectedDate}
              onSelect={(d) => { cancelSearchRef.current = true; autoSearchRef.current = 'done'; setSelectedDate(d); }}
              onClose={() => setShowCalendar(false)}
              minDate={minDate}
              maxDate={maxDate}
            />
          )}
        </div>

        {isCurrentYear && (
          <button onClick={() => { autoSearchRef.current = 'idle'; setSelectedDate(todayStr()); }}
            className="hidden sm:block px-3 py-1.5 rounded-lg bg-earl-600/20 border border-earl-500/30 text-xs text-earl-400 hover:bg-earl-600/30 transition"
          >Today</button>
        )}

        <button onClick={() => goDay(1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >→</button>
      </div>

      <p className="text-lg text-gray-300 font-medium mb-4">{dateLabel}</p>

      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      ) : games.length === 0 ? (
        <div className="text-center py-12 text-gray-500">No games scheduled for this date.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {games.map((g) => {
            const badge = statusBadge(g.status);
            const isFinal = g.status.toLowerCase() === "final";
            const isLive = g.status.toLowerCase() === "in_progress";
            const homeWon = isFinal && g.home_score! > g.away_score!;
            const awayWon = isFinal && g.away_score! > g.home_score!;

            return (
              <ChatCardLink
                key={g.id}
                href={`/${sport}/games/${g.id}?year=${year}&date=${selectedDate}`}
                sport={sport as "nfl" | "nba" | "mlb"}
                homeTeam={g.home_team ?? ""}
                awayTeam={g.away_team ?? ""}
                date={g.date ?? null}
                context={buildGameContext(sport as "mlb" | "nba" | "nfl", g)}
                hideChat={(g.status ?? "").toLowerCase() === "final"}
                className="block text-center border border-white/10 rounded-xl p-3"
              >
                {/* Matchup header: [logo] AWAY [score] @ [score] HOME [logo] */}
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && <TeamLogo abbr={g.away_team} sport="mlb" size={20} />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && <TeamLogo abbr={g.home_team} sport="mlb" size={20} />}
                </div>

                {/* Status/time */}
                <div className="mt-1.5">
                  <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>{badge.label}</span>
                  {isFinal && g.actual_innings && g.actual_innings > 9 && (
                    <span className="ml-2 text-[10px] text-gray-500">{g.actual_innings} inn</span>
                  )}
                  {isFinal && g.duration_minutes && (
                    <span className="ml-2 text-[10px] text-gray-600">{Math.floor(g.duration_minutes / 60)}:{String(g.duration_minutes % 60).padStart(2, "0")}</span>
                  )}
                  {!isFinal && !isLive ? (
                    <div className="text-xs text-gray-500 mt-1">{formatTime(g.date)}</div>
                  ) : (
                    <div className="h-4 mt-1" aria-hidden="true" />
                  )}
                </div>

                {/* Betting lines: spread (favored/Pick'em), moneyline, over/under */}
                {(g.spread != null || g.over_under != null) && (
                  <div className="mt-3 pt-3 pb-1 border-t border-white/10 text-xs text-center">
                    <div className="text-gray-400">
                      <span className="text-earl-300">{favoredSpread(g.spread, g.home_team, g.away_team)}</span>
                      <span className="mx-2 text-gray-700">|</span>
                      <span>{formatMoneyline(g.home_moneyline)}/{formatMoneyline(g.away_moneyline)}</span>
                      {g.over_under != null && <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>}
                    </div>
                  </div>
                )}

                {/* Premium picks (self-gated) */}
                {hasPicks({ spread: g.pick_spread, ou: g.pick_over_under, ml: g.pick_moneyline }) && (
                  <div className="mt-2">
                    <EarlsPicksPanel
                      compact
                      items={buildPickItems({
                        spreadPick: g.pick_spread,
                        overUnder: g.pick_over_under,
                        mlPick: g.pick_moneyline,
                        atsEv: g.pick_ats_ev,
                        ouEv: g.pick_ou_ev,
                        mlEv: g.pick_ml_ev,
spreadResult: g.result_spread,
ouResult: g.result_over_under,
mlResult: g.result_moneyline,
                        home: g.home_team,
                        away: g.away_team,
                        spreadLabel: "Run Line",
                      })}
                    />
                  </div>
                )}
              </ChatCardLink>
            );
          })}
        </div>
      )}
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// NFL Schedule (week-based)
// ════════════════════════════════════════════════════════════════════
function NFLSchedule({ sport }: { sport: string }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const weekCarouselRef = useRef<HTMLDivElement>(null);
  const [games, setGames] = useState<Game[]>([]);
  const [week, setWeek] = useState(() => {
    const wp = searchParams.get('week');
    return wp ? parseInt(wp) : 1;
  });
  const [seasonYear, setSeasonYear] = useState(() => {
    const yp = searchParams.get('year');
    return yp ? parseInt(yp) : CURRENT_YEAR;
  });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('year', String(seasonYear));
    params.set('week', String(week));
    router.replace(`/${sport}/schedule?${params.toString()}`, { scroll: false });
  }, [seasonYear, week, sport]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setLoading(true);
    api.games
      .list({ season_year: seasonYear, week })
      .then(data => setGames(data))
      .finally(() => setLoading(false));
  }, [week, seasonYear]);

  // Auto-poll schedule + pick results every 30s for the current NFL season year
  // (any week) so completed-game pick color coding updates automatically.
  useEffect(() => {
    if (seasonYear !== CURRENT_YEAR) return;
    const interval = setInterval(() => {
      api.games
        .list({ season_year: seasonYear, week })
        .then(data => setGames(data))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [seasonYear, week]);

  function formatDate(iso: string) {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
      timeZone: "America/New_York",
    }) + " ET";
  }

  function scrollWeeks(dir: "left" | "right") {
    const el = weekCarouselRef.current;
    if (!el) return;
    const amount = dir === "left" ? -200 : 200;
    el.scrollBy({ left: amount, behavior: "smooth" });
  }

  return (
    <>
      <h1 className="font-display text-4xl font-bold">NFL Schedule</h1>

      <div className="flex items-center gap-3">
        <select value={seasonYear} onChange={e => setSeasonYear(Number(e.target.value))}
          className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-gray-300">
          {[CURRENT_YEAR, 2025, 2024, 2023, 2022, 2021, 2020].map(y => (
            <option key={y} value={y} className="bg-gray-900">{y}</option>
          ))}
        </select>
      </div>

      <div className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold mb-1.5">Weeks</div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => scrollWeeks("left")}
          aria-label="Previous weeks"
          className="shrink-0 w-7 h-7 rounded-lg bg-white/5 border border-white/10 text-gray-300 hover:bg-white/10 hover:text-white transition flex items-center justify-center"
        >
          ‹
        </button>
        <div
          ref={weekCarouselRef}
          className="flex gap-1 overflow-x-auto pb-1 snap-x snap-mandatory [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {ALL_WEEKS.map((w) => (
            <button
              key={w}
              onClick={() => setWeek(w)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap snap-start transition shrink-0 ${
                week === w
                  ? "bg-earl-600 text-white"
                  : "bg-white/5 text-gray-400 hover:bg-white/10"
              }`}
            >
              {weekLabel(w)}
            </button>
          ))}
        </div>
        <button
          onClick={() => scrollWeeks("right")}
          aria-label="Next weeks"
          className="shrink-0 w-7 h-7 rounded-lg bg-white/5 border border-white/10 text-gray-300 hover:bg-white/10 hover:text-white transition flex items-center justify-center"
        >
          ›
        </button>
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      ) : games.length === 0 ? (
        <div className="text-center py-12 text-gray-500">No games found for this week.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {games.map((g) => {
            const badge = statusBadge(g.status);
            const isFinal = g.status.toLowerCase() === "final";
            const isLive = g.status.toLowerCase() === "in_progress";
            const homeWon = isFinal && g.home_score! > g.away_score!;
            const awayWon = isFinal && g.away_score! > g.home_score!;

            return (
              <ChatCardLink
                key={g.id}
                href={`/${sport}/games/${g.id}?year=${seasonYear}&week=${week}`}
                sport={sport as "nfl" | "nba" | "mlb"}
                homeTeam={g.home_team ?? ""}
                awayTeam={g.away_team ?? ""}
                date={g.date ?? null}
                context={buildGameContext(sport as "mlb" | "nba" | "nfl", g)}
                hideChat={(g.status ?? "").toLowerCase() === "final"}
                className="block text-center border border-white/10 rounded-xl p-3"
              >
                {/* Matchup header: [logo] AWAY [score] @ [score] HOME [logo] */}
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && <TeamLogo abbr={g.away_team} sport="nfl" size={20} />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && <TeamLogo abbr={g.home_team} sport="nfl" size={20} />}
                </div>

                {/* Status/time */}
                <div className="mt-1.5">
                  <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>{badge.label}</span>
                  {!isFinal && !isLive ? (
                    <div className="text-xs text-gray-500 mt-1">{formatDate(g.date)}</div>
                  ) : (
                    <div className="h-4 mt-1" aria-hidden="true" />
                  )}
                </div>

                {/* Betting lines: spread (favored/Pick'em), moneyline, over/under */}
                {(g.spread != null || g.over_under != null) && (
                  <div className="mt-3 pt-3 pb-1 border-t border-white/10 text-xs text-center">
                    <div className="text-gray-400">
                      <span className="text-earl-300">{favoredSpread(g.spread, g.home_team, g.away_team)}</span>
                      <span className="mx-2 text-gray-700">|</span>
                      <span>{formatMoneyline(g.home_moneyline)}/{formatMoneyline(g.away_moneyline)}</span>
                      {g.over_under != null && <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>}
                    </div>
                  </div>
                )}

                {/* Premium picks (self-gated) */}
                {hasPicks({ spread: g.pick_spread, ou: g.pick_over_under, ml: g.pick_moneyline }) && (
                  <div className="mt-2">
                    <EarlsPicksPanel
                      compact
                      items={buildPickItems({
                        spreadPick: g.pick_spread,
                        overUnder: g.pick_over_under,
                        mlPick: g.pick_moneyline,
                        atsEv: g.pick_ats_ev,
                        ouEv: g.pick_ou_ev,
                        mlEv: g.pick_ml_ev,
spreadResult: g.result_spread,
ouResult: g.result_over_under,
mlResult: g.result_moneyline,
                        home: g.home_team,
                        away: g.away_team,
                        spreadLabel: "Spread",
                      })}
                    />
                  </div>
                )}
              </ChatCardLink>
            );
          })}
        </div>
      )}
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// Main Page
// ════════════════════════════════════════════════════════════════════
export default function SchedulePage() {
  const params = useParams<{ sport: string }>();
  const sport = params?.sport || "nfl";

  const sportLabel = sport === "nfl" ? "NFL" : sport === "nba" ? "NBA" : sport === "mlb" ? "MLB" : sport.toUpperCase();
  useSeo({
    title: `${sportLabel} Schedule, Odds & Lines | Earl Knows Ball`,
    description: `View the ${sportLabel} schedule, spreads, and betting lines on Earl Knows Ball.`,
    keywords: `${sport}, ${sportLabel}, schedule, odds, spreads, lines, betting, Earl Knows Ball`,
  });

  return (
    <>
      {sport === "mlb" ? (
        <MLBSchedule sport={sport} />
      ) : sport === "nba" ? (
        <NBASchedule sport={sport} />
      ) : (
        <NFLSchedule sport={sport} />
      )}
    </>
  );
}
