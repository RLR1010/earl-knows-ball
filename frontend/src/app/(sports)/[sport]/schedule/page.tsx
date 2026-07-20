"use client";
import { useEffect, useState, useRef } from "react";
import * as React from "react";
import { useParams, useSearchParams, useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import { api, Game, formatSpread, formatSpreadAway, formatOverUnder } from "@/lib/api";
import { getTeamLogoUrl } from "@/lib/team_logos";

const WEEKS = Array.from({ length: 22 }, (_, i) => i + 1);

const PLAYOFF_LABELS: Record<number, string> = {
  19: "Wild Card",
  20: "Divisional",
  21: "Conf Champ",
  22: "Super Bowl",
};
function weekLabel(w: number): string {
  if (w >= 19) return PLAYOFF_LABELS[w] || `Week ${w}`;
  return `Week ${w}`;
}
const MLB_YEARS = Array.from({ length: 21 }, (_, i) => 2026 - i);
const NBA_YEARS = Array.from({ length: 21 }, (_, i) => 2026 - i);

function todayStr(): string {
  const d = new Date();
  const offset = d.getTimezoneOffset();
  const local = new Date(d.getTime() - offset * 60_000);
  return local.toISOString().slice(0, 10);
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
}

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/Chicago",
  });
}

function predBadge(label: string, result: string | null): React.ReactNode {
  if (!result) return null;
  const resultLower = result.toLowerCase();
  let cls: string;
  let letter: string;
  if (resultLower === "win") {
    cls = "bg-green-900/40 text-green-400 border border-green-500/30";
    letter = "W";
  } else if (resultLower === "push") {
    cls = "bg-gray-700/40 text-gray-400 border border-gray-600/30";
    letter = "P";
  } else {
    cls = "bg-red-900/40 text-red-400 border border-red-500/30";
    letter = "L";
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${cls}`}>
      {label} {letter}
    </span>
  );
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
  const [year, setYear] = useState(() => {
    const yp = searchParams.get('year');
    return yp ? parseInt(yp) : CURRENT_YEAR;
  });
  const [selectedDate, setSelectedDate] = useState(() => {
    return searchParams.get('date') || todayStr();
  });
  const [games, setGames] = useState<NBAGame[]>([]);
  const [loading, setLoading] = useState(true);

  const isCurrentYear = year === CURRENT_YEAR;
  const seasonFirst = nbaFirstGame(year);
  const seasonLast = nbaLastGame(year);

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

  // Auto-poll live scores for today
  useEffect(() => {
    if (!isCurrentYear || selectedDate !== todayStr()) return;
    const interval = setInterval(() => {
      const params = new URLSearchParams({ year: String(CURRENT_YEAR), date: selectedDate });
      fetch(`/api/nba/games?${params}`)
        .then(r => r.json())
        .then((data: NBAGame[]) => setGames(data.filter(g => g.game_type === "REG" || g.game_type === "POST")))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [isCurrentYear, selectedDate]);

  function goDay(delta: number) {
    autoSearchRef.current = 'done';
    cancelSearchRef.current = true;
    const d = new Date(selectedDate + "T12:00:00-06:00");
    d.setDate(d.getDate() + delta);
    const offset = d.getTimezoneOffset();
    const local = new Date(d.getTime() - offset * 60_000);
    setSelectedDate(local.toISOString().slice(0, 10));
  }

  const dateObj = selectedDate ? (() => {
    const d = new Date(selectedDate + "T12:00:00-06:00");
    return d;
  })() : new Date();
  
  const dateLabel = dateObj.toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
    timeZone: "America/Chicago",
  });

  return (
    <>
      <h1 className="font-display text-4xl font-bold">NBA Schedule</h1>

      <div className="flex items-center gap-3">
        <select
          value={year}
          onChange={e => setYear(Number(e.target.value))}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {NBA_YEARS.map(y => (
            <option key={y} value={y} className="text-black">{y}-{y + 1} Season</option>
          ))}
        </select>

        <button
          onClick={() => goDay(-1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >←</button>

        <input
          type="date"
          value={selectedDate}
          onChange={e => { cancelSearchRef.current = true; autoSearchRef.current = 'done'; setSelectedDate(e.target.value); }}
          min={seasonFirst}
          max={seasonLast}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 [color-scheme:dark]"
        />

        {isCurrentYear && (
          <button
            onClick={() => { autoSearchRef.current = 'idle'; setSelectedDate(todayStr()); }}
            className="px-3 py-1.5 rounded-lg bg-earl-600/20 border border-earl-500/30 text-xs text-earl-400 hover:bg-earl-600/30 transition"
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
              <Link
                key={g.id}
                href={`/${sport}/games/${g.id}?year=${year}&date=${selectedDate}`}
                className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
              >
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && getTeamLogoUrl(g.away_team, "nba") && <Image src={getTeamLogoUrl(g.away_team, "nba")!} alt={g.away_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && getTeamLogoUrl(g.home_team, "nba") && <Image src={getTeamLogoUrl(g.home_team, "nba")!} alt={g.home_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
                </div>

                <div className="mt-1.5">
                  <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>{badge.label}</span>
                  {!isFinal && !isLive && <div className="text-xs text-gray-500 mt-1">{formatTime(g.date)}</div>}
                </div>
              </Link>
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
  const [year, setYear] = useState(() => {
    const yp = searchParams.get('year');
    return yp ? parseInt(yp) : CURRENT_YEAR;
  });
  const [selectedDate, setSelectedDate] = useState(() => {
    return searchParams.get('date') || todayStr();
  });
  const [games, setGames] = useState<MLBGame[]>([]);
  const [loading, setLoading] = useState(true);

  const isCurrentYear = year === CURRENT_YEAR;
  const seasonFirst = mlbOpeningDay(year);
  const seasonLast = mlbLastDay(year);

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
    if (!isCurrentYear || selectedDate !== todayStr()) return;
    const interval = setInterval(() => {
      const params = new URLSearchParams({ year: String(CURRENT_YEAR), date: selectedDate });
      fetch(`/api/mlb/games?${params}`)
        .then(r => r.json())
        .then((data: MLBGame[]) => setGames(data))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [isCurrentYear, selectedDate]);

  function goDay(delta: number) {
    autoSearchRef.current = 'done';
    cancelSearchRef.current = true;
    const d = new Date(selectedDate + "T12:00:00-05:00");
    d.setDate(d.getDate() + delta);
    const offset = d.getTimezoneOffset();
    const local = new Date(d.getTime() - offset * 60_000);
    setSelectedDate(local.toISOString().slice(0, 10));
  }

  const dateObj = (() => {
    const d = new Date(selectedDate + "T12:00:00-05:00");
    return d;
  })();

  const dateLabel = dateObj.toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
    timeZone: "America/Chicago",
  });

  return (
    <>
      <h1 className="font-display text-4xl font-bold">MLB Schedule</h1>

      <div className="flex items-center gap-3">
        <select
          value={year}
          onChange={e => setYear(Number(e.target.value))}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {MLB_YEARS.map(y => (
            <option key={y} value={y} className="text-black">{y} Season</option>
          ))}
        </select>

        <button onClick={() => goDay(-1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
        >←</button>

        <input type="date" value={selectedDate} onChange={e => { cancelSearchRef.current = true; autoSearchRef.current = 'done'; setSelectedDate(e.target.value); }}
          min={seasonFirst} max={seasonLast}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 [color-scheme:dark]"
        />

        {isCurrentYear && (
          <button onClick={() => { autoSearchRef.current = 'idle'; setSelectedDate(todayStr()); }}
            className="px-3 py-1.5 rounded-lg bg-earl-600/20 border border-earl-500/30 text-xs text-earl-400 hover:bg-earl-600/30 transition"
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
              <Link key={g.id} href={`/${sport}/games/${g.id}?year=${year}&date=${selectedDate}`}
                className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
              >
                {/* Matchup header: [logo] AWAY [score] @ [score] HOME [logo] */}
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && getTeamLogoUrl(g.away_team, "mlb") && <Image src={getTeamLogoUrl(g.away_team, "mlb")!} alt={g.away_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && getTeamLogoUrl(g.home_team, "mlb") && <Image src={getTeamLogoUrl(g.home_team, "mlb")!} alt={g.home_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
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
                  {!isFinal && !isLive && <div className="text-xs text-gray-500 mt-1">{formatTime(g.date)}</div>}
                </div>

                {/* Venue / day-night */}
                {!isFinal && g.venue && <div className="text-[10px] text-gray-600 truncate mt-1 px-2">{g.venue}</div>}
                {g.day_night && !isFinal && <div className="text-[10px] text-gray-600">{g.day_night}</div>}

                {/* Betting line row */}
                {g.spread != null && g.over_under != null && (
                  <div className="mt-2 pt-2 border-t border-white/10 text-xs">
                    <span className="text-earl-300">{formatSpreadAway(g.spread, g.away_team || "")}</span>
                    <span className="mx-2 text-gray-700">|</span>
                    <span className="text-earl-400">{formatSpread(g.spread, g.home_team || "")}</span>
                    {g.over_under != null && <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>}
                  </div>
                )}

                {/* Prediction badges row */}
                {(g.pred_rl_result != null || g.pred_ou_result != null || g.pred_ml_result != null) && (
                  <div className="mt-1.5 flex flex-wrap items-center justify-center gap-1">
                    {predBadge("RL", g.pred_rl_result)}
                    {predBadge("OU", g.pred_ou_result)}
                    {predBadge("ML", g.pred_ml_result)}
                  </div>
                )}
              </Link>
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

  function formatDate(iso: string) {
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
      timeZone: "America/Chicago",
    });
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

      <div className="flex gap-1 flex-wrap">
        {WEEKS.map((w) => (
          <button
            key={w}
            onClick={() => setWeek(w)}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
              week === w
                ? "bg-earl-600 text-white"
                : "bg-white/5 text-gray-400 hover:bg-white/10"
            }`}
          >
            {weekLabel(w)}
          </button>
        ))}
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
              <Link key={g.id} href={`/${sport}/games/${g.id}?year=${seasonYear}&week=${week}`}
                className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
              >
                {/* Matchup header: [logo] AWAY [score] @ [score] HOME [logo] */}
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {g.away_team && getTeamLogoUrl(g.away_team, "nfl") && <Image src={getTeamLogoUrl(g.away_team, "nfl")!} alt={g.away_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{g.away_team ?? ""}</div>

                  {isFinal && <span className="font-bold text-white">{g.away_score}</span>}
                  {isLive && g.away_score !== null && <span className="font-bold text-red-400">{g.away_score}</span>}

                  <span className="text-gray-500 font-medium">@</span>

                  {isFinal && <span className="font-bold text-white">{g.home_score}</span>}
                  {isLive && g.home_score !== null && <span className="font-bold text-red-400">{g.home_score}</span>}

                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{g.home_team ?? ""}</div>
                  {g.home_team && getTeamLogoUrl(g.home_team, "nfl") && <Image src={getTeamLogoUrl(g.home_team, "nfl")!} alt={g.home_team} width={20} height={20} className="object-contain shrink-0" unoptimized />}
                </div>

                {/* Status/time */}
                <div className="mt-1.5">
                  <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>{badge.label}</span>
                  {!isFinal && !isLive && <div className="text-xs text-gray-500 mt-1">{formatTime(g.date)}</div>}
                </div>

                {/* Venue */}
                {!isFinal && g.venue && <div className="text-[10px] text-gray-600 truncate mt-1 px-2">{g.venue}</div>}

                {/* Betting line */}
                {g.spread != null && (
                  <div className="mt-2 pt-2 border-t border-white/10 text-xs">
                    <span className="text-earl-300">{formatSpreadAway(g.spread, g.away_team || "")}</span>
                    <span className="mx-2 text-gray-700">|</span>
                    <span className="text-earl-400">{formatSpread(g.spread, g.home_team || "")}</span>
                    {g.over_under != null && <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>}
                  </div>
                )}
              </Link>
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
