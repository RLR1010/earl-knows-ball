const API_BASE = "";

/**
 * Return the JWT from localStorage if present and not expired.
 * The backend accepts it as `Authorization: Bearer`, which lets API calls
 * succeed even when the httpOnly cookie is missing (e.g. cookie cleared).
 */
export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  const t = localStorage.getItem("earl_token");
  if (!t) return null;
  try {
    const payload = JSON.parse(atob(t.split(".")[1]));
    if (payload.exp && payload.exp * 1000 < Date.now()) return null;
  } catch {
    return null;
  }
  return t;
}

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getStoredToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

export interface Team {
  id: number;
  abbreviation: string;
  name: string;
  conference: string;
  division: string;
  logo_url?: string;
  byeweek?: number;
}

export interface Player {
  id: number;
  name: string;
  position: string;
  team_abbr?: string;
  team_name?: string;
  status?: string;
  jersey_number?: number;
  height?: number;
  weight?: number;
  college?: string;
  years_exp?: number;
}

export interface Game {
  id: number;
  week: number;
  game_type: string;
  status: string;
  date: string;
  venue?: string;
  home_team?: string;
  away_team?: string;
  home_record?: string | null;
  away_record?: string | null;
  home_score?: number;
  away_score?: number;
  spread?: number | null;       // from home team perspective (+ = home underdog, - = home favorite)
  over_under?: number | null;
  home_moneyline?: number | null;
  away_moneyline?: number | null;
  pick_spread?: string | null;
  pick_over_under?: string | null;
  pick_moneyline?: string | null;
  pick_ats_ev?: number | null;
  pick_ou_ev?: number | null;
  pick_ml_ev?: number | null;
  result_spread?: string | null;
  result_over_under?: string | null;
  result_moneyline?: string | null;
  home_time_of_possession_secs?: number | null;
  away_time_of_possession_secs?: number | null;
  // Cross-sport source for the home aggregation + Best Bets panel
  sport?: string | null;
  // Best Bets metadata (from GET /api/home/best-bets)
  best_bet_type?: "ats" | "ou" | "ml" | null;
  best_bet_label?: string | null;
  best_bet_edge?: number | null;
  best_bet_edge_pct?: number | null;
  best_bet_confidence_pct?: number | null;
  best_bet_implied_pct?: number | null;
  best_bet_ev?: number | null;
}

export interface DepthChartEntry {
  id: number;
  team_id: number;
  position: string;
  slot: number;
  player_id?: number;
  player_name: string;
  jersey_number?: number;
  acquisition_info?: string;
  status: string;
}

export interface BoxScorePlayer {
  player_id: number;
  player_name: string;
  position: string;
  team_abbr: string | null;
  pass_attempts: number;
  pass_completions: number;
  pass_yards: number;
  pass_tds: number;
  pass_int: number;
  rush_attempts: number;
  rush_yards: number;
  rush_tds: number;
  targets: number;
  receptions: number;
  receiving_yards: number;
  receiving_tds: number;
  field_goals_made: number;
  field_goals_attempted: number;
  extra_points_made: number;
  tackles: number;
  sacks: number;
  interceptions: number;
  fumbles_recovered: number;
  defensive_tds: number;
}

export interface BoxScoreStats {
  total_yards: number;
  pass_yards: number;
  rush_yards: number;
  turnovers: number;
  first_downs: number;
  third_down_pct: number | null;
  time_of_possession: string | null;
  penalties: number;
  penalty_yards: number;
  top_players: BoxScorePlayer[];
}

export interface BoxScore {
  game: Game;
  home_stats: BoxScoreStats;
  away_stats: BoxScoreStats;
}

export interface Article {
  id: number;
  title: string;
  slug: string;
  excerpt?: string;
  category: string;
  tier: string;
  published_at?: string;
}

// ── Admin API ─────────────────────────────────────────────────

export interface DashboardStats {
  total_users: number;
  active_users: number;
  premium_users: number;
  monthly_revenue_cents: number;
  total_revenue_cents: number;
  users_today: number;
  users_this_week: number;
  subscriptions_active: number;
  subscriptions_canceled: number;
  failed_payments: number;
  plans_count: number;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string | null;
  subscription_tier: string;
  is_active: boolean;
  is_admin: boolean;
  email_verified: boolean;
  stripe_customer_id: string | null;
  created_at: string | null;
  last_login_at: string | null;
}

export interface SubscriptionPlan {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  price_cents: number;
  currency: string;
  interval: string;
  trial_days: number;
  features: string[];
  is_active: boolean;
  sort_order: number;
  stripe_price_id: string | null;
  stripe_product_id: string | null;
  created_at: string | null;
}

export interface UserSubscription {
  id: string;
  user_id: string;
  user_email: string;
  user_name: string;
  plan_id: string | null;
  plan_name: string;
  status: string;
  current_period_start: string | null;
  current_period_end: string | null;
  canceled_at: string | null;
  trial_end: string | null;
  stripe_subscription_id: string | null;
  created_at: string | null;
}

export interface Payment {
  id: string;
  user_id: string;
  user_email: string;
  user_name: string;
  subscription_id: string | null;
  amount_cents: number;
  currency: string;
  status: string;
  description: string | null;
  stripe_invoice_id: string | null;
  created_at: string | null;
}

export interface UserProfile {
  id: string;
  email: string;
  display_name: string | null;
  subscription_tier: string;
  is_admin: boolean;
  email_verified: boolean;
  created_at: string | null;
  last_login_at: string | null;
  stripe_customer_id: string | null;
}

export interface PaymentRecord {
  id: string;
  user_id: string;
  user_email: string;
  user_name: string | null;
  subscription_id: string | null;
  amount_cents: number;
  currency: string;
  status: string;
  description: string | null;
  stripe_invoice_id: string | null;
  created_at: string | null;
}

export interface TokenUsageResponse {
  month: string;
  tokens_used: number;
  token_limit: number | null;
  percent_used: number | null;
  extra_token_balance: number;
}

// Standings (feature #1: Standings / Down-The-Stretch frames)
export interface StandingsTeam {
  team_id: number;
  team_name: string;
  abbreviation: string;
  logo_url: string | null;
  group: string | null; // conference/league
  division: string | null;
  games: number;
  wins: number;
  losses: number;
  win_pct: number;
  streak: number; // + = winning streak, - = losing streak
  last10: { wins: number; losses: number };
  home: { wins: number; losses: number };
  away: { wins: number; losses: number };
  points_for: number;
  points_against: number;
  diff: number;
  games_back: number;
}

export interface StandingsDivision {
  division: string | null;
  teams: StandingsTeam[];
}

export interface StandingsConference {
  name: string | null;
  divisions: StandingsDivision[];
}

export interface StandingsResponse {
  sport: string;
  season: number | null;
  in_season: boolean | null;
  conferences: StandingsConference[];
  teams: StandingsTeam[];
}

// Matchup (feature #2+#3 combined — trends + side-by-side comparison)
export interface MatchupTeamTrends {
  latest_game?: string;
  last_5?: Record<string, unknown>;
  last_10?: Record<string, unknown>;
  recent_weighted_3?: Record<string, unknown>;
  recent_weighted_5?: Record<string, unknown>;
  windows?: string[];
  latest_summary?: Record<string, unknown>;
  recent_games?: unknown[];
  [k: string]: unknown;
}

export interface MatchupTeam {
  name: string;
  id: number | null;
  abbr: string;
  trends: MatchupTeamTrends | null;
  trends_error?: string | null;
  splits: Record<string, unknown> | null;
}

export interface MatchupResponse {
  sport: string;
  game_id: number | null;
  game_date: string | null;
  teams: { home: MatchupTeam; away: MatchupTeam };
  // comparison is { compare: { [metric]: { [abbr]: number } }, team_a, team_b }
  comparison: {
    compare: Record<string, Record<string, number>>;
    team_a: string;
    team_b: string;
  } | null;
  comparison_error?: string | null;
}

export type ParlayKind = "ml" | "spread" | "total";

export interface ParlayLeg {
  game_id: number;
  sport: string;
  kind: ParlayKind;
  label: string;
  pick: string;
  side: string | null;
  prob: number | null;
  odds: number | null;
  decimal?: number;
  ev: number | null;
  model_file: string | null;
  is_calibrated: boolean;
  favorite_side?: string | null;
  game_label: string;
  game_date: string;
}

export interface ParlayGame {
  game_id: number;
  sport: string;
  game_label: string;
  home_abbr: string;
  home_name: string;
  away_abbr: string;
  away_name: string;
  date: string;
  status: string;
  favorite_side: string | null;
  legs: Partial<Record<ParlayKind, ParlayLeg>>;
}

export interface ParlayTicket {
  n_legs: number;
  fair_probability: number;
  fair_decimal: number;
  fair_american: number;
  book_decimal: number;
  book_american: number;
  combined_implied: number;
  vig_drag: number;
  ev_pct: number;
  ev_dollars: number;
  correlation_warnings: string[];
  correlation_blocks: string[];
  independent_note?: string;
  legs: ParlayLeg[];
}

export interface ParlayLegInput extends ParlayLeg {}

export interface SavedParlayTicket {
  id: number;
  name: string;
  legs: ParlayLeg[];
  created_at: string | null;
  updated_at: string | null;
}
export interface ParlayCorrelation {
  kind_a: string;
  kind_b: string;
  n: number;
  p_a: number;
  p_b: number;
  p_joint: number;
  p_indep: number;
  corr: number;
  is_block: boolean;
}

export const api = {
  // Teams
  teams: {
    list: () => fetchAPI<Team[]>("/api/teams"),
    get: (id: number) => fetchAPI<Team>(`/api/teams/${id}`),
    getByAbbr: (abbr: string) => fetchAPI<Team>(`/api/teams/by-abbr/${abbr}`),
    depthChart: (teamId: number) => fetchAPI<DepthChartEntry[]>(`/api/teams/${teamId}/depth-chart`),
  },

  // Players
  players: {
    list: (params?: { position?: string; team_id?: number }) => {
      const q = new URLSearchParams();
      if (params?.position) q.set("position", params.position);
      if (params?.team_id) q.set("team_id", String(params.team_id));
      const qs = q.toString();
      return fetchAPI<Player[]>(`/api/players${qs ? `?${qs}` : ""}`);
    },
    get: (id: number) => fetchAPI<Player>(`/api/players/${id}`),
    search: (name: string) => fetchAPI<Player[]>(`/api/players/search/${name}`),
  },

  // Seasons
  seasons: {
    list: () => fetchAPI<number[]>("/api/seasons"),
  },

  nbaSeasons: {
    list: () => fetchAPI<number[]>("/api/nba/seasons"),
  },

  mlbSeasons: {
    list: () => fetchAPI<number[]>("/api/mlb/seasons"),
  },

  // Games
  games: {
    list: (params?: { season_year?: number; week?: number; team_id?: number }) => {
      const q = new URLSearchParams();
      if (params?.season_year) q.set("season_year", String(params.season_year));
      if (params?.week) q.set("week", String(params.week));
      if (params?.team_id) q.set("team_id", String(params.team_id));
      const qs = q.toString();
      return fetchAPI<Game[]>(`/api/games${qs ? `?${qs}` : ""}`);
    },
    get: (id: number) => fetchAPI<Game>(`/api/games/${id}`),
    boxScore: (id: number) => fetchAPI<BoxScore | null>(`/api/games/${id}/box-score`),
  },

  // Best Bets (Earl's single best value pick per upcoming game)
  bestBets: {
    list: (params?: { sport?: "all" | "mlb" | "nba" | "nfl"; limit?: number }) =>
      fetchAPI<Game[]>(`/api/home/best-bets?sport=${params?.sport ?? "all"}&limit=${params?.limit ?? 6}`),
  },

  // Parlay builder — upcoming games as selectable legs (ML / spread / total)
  parlay: {
    legs: (sport: "mlb" | "nfl" | "nba") =>
      fetchAPI<{ sport: string; count: number; games: ParlayGame[]; correlations: Record<string, ParlayCorrelation> }>(`/api/parlay/legs?sport=${sport}`),
    // Saved parlay tickets (premium, user-scoped, cross-sport)
    listTickets: () => fetchAPI<{ tickets: SavedParlayTicket[] }>(`/api/parlay/tickets`),
    getTicket: (id: number) => fetchAPI<SavedParlayTicket>(`/api/parlay/tickets/${id}`),
    saveTicket: (payload: { name?: string; legs: ParlayLegInput[]; ticket_id?: number }) =>
      fetchAPI<SavedParlayTicket>(`/api/parlay/tickets`, {
        method: "POST",
        body: JSON.stringify({
          name: payload.name ?? "My Parlay",
          legs: payload.legs,
          ...(payload.ticket_id ? { ticket_id: payload.ticket_id } : {}),
        }),
      }),
    deleteTicket: (id: number) =>
      fetchAPI<{ deleted: boolean; ticket_id: number }>(`/api/parlay/tickets/${id}`, { method: "DELETE" }),
  },

  standings: {
    get: (params?: {
      sport: "nfl" | "nba" | "mlb";
      seasonYear?: number;
      conference?: string;
      division?: string;
    }) =>
      fetchAPI<StandingsResponse>(
        `/api/home/standings?sport=${params?.sport}` +
          (params?.seasonYear ? `&season_year=${params.seasonYear}` : "") +
          (params?.conference ? `&conference=${encodeURIComponent(params.conference)}` : "") +
          (params?.division ? `&division=${encodeURIComponent(params.division)}` : "")
      ),
  },

  matchup: {
    get: (params: { sport: "nfl" | "nba" | "mlb"; gameId?: number; home?: string; away?: string }) =>
      fetchAPI<MatchupResponse>(
        `/api/matchup?sport=${params.sport}` +
          (params.gameId ? `&game_id=${params.gameId}` : "") +
          (params.home ? `&home=${encodeURIComponent(params.home)}` : "") +
          (params.away ? `&away=${encodeURIComponent(params.away)}` : "")
      ),
  },

  // Auth
  auth: {
    sendCode: (email: string) =>
      fetchAPI<{ message: string }>("/auth/send-code", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    verifyCode: (email: string, code: string) =>
      fetchAPI<{ user: any; token: string; message: string }>("/auth/verify-code", {
        method: "POST",
        body: JSON.stringify({ email, code }),
      }),
    me: () => fetchAPI<any>("/auth/me"),
    logout: () =>
      fetchAPI<{ message: string }>("/auth/logout", {
        method: "POST",
      }),
  },

  // Token Usage
  tokenUsage: {
    my: () => fetchAPI<TokenUsageResponse>("/api/users/me/token-usage"),
  },

  // Admin
  admin: {
    stats: () => fetchAPI<DashboardStats>("/api/admin/stats", {} ),
    users: {
      list: (params?: { search?: string; tier?: string }) => {
        const q = new URLSearchParams();
        if (params?.search) q.set("search", params.search);
        if (params?.tier) q.set("tier", params.tier);
        return fetchAPI<AdminUser[]>(`/api/admin/users?${q}`, {} );
      },
      get: (id: string) => fetchAPI<AdminUser>(`/api/admin/users/${id}`, {} ),
      update: (id: string, data: any) =>
        fetchAPI<AdminUser>(`/api/admin/users/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }),
      delete: (id: string) =>
        fetchAPI<void>(`/api/admin/users/${id}`, { method: "DELETE",  }),
    },
    plans: {
      list: () => fetchAPI<SubscriptionPlan[]>("/api/admin/plans", {} ),
      get: (id: string) => fetchAPI<SubscriptionPlan>(`/api/admin/plans/${id}`, {} ),
      create: (data: any) =>
        fetchAPI<SubscriptionPlan>("/api/admin/plans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }),
      update: (id: string, data: any) =>
        fetchAPI<SubscriptionPlan>(`/api/admin/plans/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }),
      delete: (id: string) =>
        fetchAPI<void>(`/api/admin/plans/${id}`, { method: "DELETE",  }),
    },
    subscriptions: {
      list: (status?: string) => {
        const q = status ? `?status_filter=${status}` : "";
        return fetchAPI<UserSubscription[]>(`/api/admin/subscriptions${q}`, {} );
      },
      get: (id: string) =>
        fetchAPI<UserSubscription>(`/api/admin/subscriptions/${id}`, {} ),
      update: (id: string, data: any) =>
        fetchAPI<UserSubscription>(`/api/admin/subscriptions/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }),
    },
    payments: {
      list: (status?: string) => {
        const q = status ? `?status_filter=${status}` : "";
        return fetchAPI<Payment[]>(`/api/admin/payments${q}`, {} );
      },
      get: (id: string) => fetchAPI<Payment>(`/api/admin/payments/${id}`, {} ),
    },
    // MLB model features + training
    features: {
      get: (sport: string) =>
        fetchAPI<{ features: Array<{name: string; description: string; display_name: string | null; is_trainable: boolean; current_ou: boolean; current_ats: boolean}> }>(
          `/api/admin/features/${sport}`,
          {} 
        ),
    },
    training: {
      trigger: (sport: string, modelType: string, features: string[]) =>
        fetchAPI<{ status: string; features_updated: number; training_pid: number; message: string }>(
          `/api/admin/train-new/${sport}/${modelType}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ features }),
          }
        ),
      getRuns: (sport: string, modelType: string) =>
        fetchAPI<any[]>(
          `/api/admin/training-runs/${sport}/${modelType}`,
          {} 
        ),
    },
  },

  // Subscriptions (public)
  subscriptions: {
    plans: () => fetchAPI<SubscriptionPlan[]>("/api/subscriptions/plans"),
    my: () => fetchAPI<any>("/api/subscriptions/my", {} ),
    checkout: (planId: string, successUrl?: string, cancelUrl?: string) =>
      fetchAPI<{ url: string | null; mock: boolean; message: string }>("/api/subscriptions/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: planId, success_url: successUrl, cancel_url: cancelUrl }),
      }),
    cancel: () =>
      fetchAPI<{ status: string; message: string }>("/api/subscriptions/cancel", {
        method: "POST",
      }),
    tokenTopup: (body?: { success_url?: string; cancel_url?: string; ui_mode?: string }) =>
      fetchAPI<{ url: string | null; client_secret: string | null; mock: boolean; message: string }>(
        "/api/subscriptions/token-topup/checkout",
        { method: "POST", body: body ? JSON.stringify(body) : undefined }
      ),
    payments: async (params?: { limit?: number; offset?: number }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      const token = getStoredToken();
      const res = await fetch(`${API_BASE}/api/subscriptions/payments?${q}`, {
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
      });
      if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
      const items = (await res.json()) as PaymentRecord[];
      const total = Number(res.headers.get("X-Total-Count") || items.length);
      return { items, total };
    },
  },

};

/** Format a spread for display: positive = home underdog, negative = home favorite */
export function formatSpread(spread: number | null | undefined, homeTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${homeTeam} +${spread}`;
  if (spread < 0) return `${homeTeam} ${spread}`;
  return "PK";
}

/** Format the away team perspective from the home spread */
export function formatSpreadAway(spread: number | null | undefined, awayTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${awayTeam} -${spread}`;  // home underdog = away favorite
  if (spread < 0) return `${awayTeam} +${Math.abs(spread)}`;  // home favorite = away underdog
  return "PK";
}

/** Format over/under */
export function formatOverUnder(ou: number | null | undefined): string {
  if (ou == null) return "";
  return `O/U ${ou}`;
}

// Re-export helpers

