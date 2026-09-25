// power-rankings.ts — server-side helpers for the Power Rankings pages.
//
// Talks to the backend public API (/power-rankings/...) via backendBaseForPath,
// which routes to the api machine (non-compute prefix). All fetches are cached
// with ISR so the weekly snapshot is cheap to serve.

import { backendBaseForPath } from "@/lib/backend-url";

export type PRInjury = {
  player_id: number;
  name: string;
  position: string;
  report_status: string | null;
  practice_status: string | null;
  weight: number;
  value: number;
};

export type PRTeam = {
  rank: number;
  prev_rank: number | null;
  rank_delta: number | null;
  rank_movement: number | null;
  wins: number | null;
  losses: number | null;
  ties: number | null;
  record: string | null;
  injury_adj: number | null;
  injuries: PRInjury[];
  market_rating: number | null;
  market_delta: number | null;
  rating: number;
  rating_delta: number | null;
  sos: number;
  games_played: number;
  prior_rating: number | null;
  team: { id: number; abbr: string; name: string };
  blurb: string | null;
  components?: Record<string, unknown>;
};

export type PRWeek = {
  ok: boolean;
  sport: string;
  season: number;
  week: number;
  as_of_date: string | null;
  model_version: string;
  generated_at: string | null;
  teams: PRTeam[];
};

export type PRWeeks = { season: number; week: number; as_of_date: string | null }[];

export type PRTeamHistory = {
  ok: boolean;
  sport: string;
  team: { abbr: string; name: string };
  current: {
    season: number;
    week: number;
    rank: number;
    rating: number;
    rating_delta: number | null;
    injury_adj: number | null;
    injuries: PRInjury[];
    market_delta: number | null;
    blurb: string | null;
  };
  history: {
    season: number;
    week: number;
    as_of_date: string | null;
    rank: number;
    rating: number;
    rating_delta: number | null;
    sos: number;
    injury_adj: number | null;
    market_delta: number | null;
    record: string | null;
  }[];
};

const REVALIDATE = 600; // 10 min

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const base = backendBaseForPath(path);
    const res = await fetch(`${base}${path}`, { next: { revalidate: REVALIDATE } });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export const getCurrent = (sport: string) =>
  getJson<PRWeek>(`/power-rankings/${sport}/current`);

export const getWeeks = (sport: string) =>
  getJson<{ ok: boolean; sport: string; weeks: PRWeeks }>(`/power-rankings/${sport}/weeks`);

export const getWeek = (sport: string, week: number, season?: number) =>
  getJson<PRWeek>(
    `/power-rankings/${sport}/weeks/${week}${season ? `?season=${season}` : ""}`
  );

export const getTeam = (sport: string, abbr: string) =>
  getJson<PRTeamHistory>(`/power-rankings/${sport}/team/${encodeURIComponent(abbr)}`);

export const getMeta = (sport: string) =>
  getJson<{ ok: boolean; sport: string; methodology: string | null; season: number | null; week: number | null }>(
    `/power-rankings/${sport}/meta`
  );

export type PRArticle = {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  content: string;
  slug: string;
  published_at: string | null;
  teams: { team: string; name: string | null; rank: number; rating: number }[] | null;
};

// Latest weekly power-rankings column (published original_articles row,
// section='power-ranking'). The list endpoint is a compute-prefix route.
export const getLatestArticle = async (sport: string): Promise<PRArticle | null> => {
  const data = await getJson<{ ok?: boolean; sport: string; articles: PRArticle[] }>(
    `/original-articles/${sport}?section=power-ranking&limit=1`
  );
  return data?.articles?.[0] ?? null;
};
