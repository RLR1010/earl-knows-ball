const API_BASE = "";

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
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
  home_score?: number;
  away_score?: number;
  spread?: number | null;       // from home team perspective (+ = home underdog, - = home favorite)
  over_under?: number | null;
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
}

export const api = {
  // Teams
  teams: {
    list: () => fetchAPI<Team[]>("/teams"),
    get: (id: number) => fetchAPI<Team>(`/teams/${id}`),
    getByAbbr: (abbr: string) => fetchAPI<Team>(`/teams/by-abbr/${abbr}`),
    depthChart: (teamId: number) => fetchAPI<DepthChartEntry[]>(`/teams/${teamId}/depth-chart`),
  },

  // Players
  players: {
    list: (params?: { position?: string; team_id?: number }) => {
      const q = new URLSearchParams();
      if (params?.position) q.set("position", params.position);
      if (params?.team_id) q.set("team_id", String(params.team_id));
      const qs = q.toString();
      return fetchAPI<Player[]>(`/players${qs ? `?${qs}` : ""}`);
    },
    get: (id: number) => fetchAPI<Player>(`/players/${id}`),
    search: (name: string) => fetchAPI<Player[]>(`/players/search/${name}`),
  },

  // Seasons
  seasons: {
    list: () => fetchAPI<number[]>("/seasons"),
  },

  // Games
  games: {
    list: (params?: { season_year?: number; week?: number; team_id?: number }) => {
      const q = new URLSearchParams();
      if (params?.season_year) q.set("season_year", String(params.season_year));
      if (params?.week) q.set("week", String(params.week));
      if (params?.team_id) q.set("team_id", String(params.team_id));
      const qs = q.toString();
      return fetchAPI<Game[]>(`/games${qs ? `?${qs}` : ""}`);
    },
    get: (id: number) => fetchAPI<Game>(`/games/${id}`),
    boxScore: (id: number) => fetchAPI<BoxScore | null>(`/games/${id}/box-score`),
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
          `/admin/features/${sport}`,
          {} 
        ),
    },
    training: {
      trigger: (sport: string, modelType: string, features: string[]) =>
        fetchAPI<{ status: string; features_updated: number; training_pid: number; message: string }>(
          `/admin/train-new/${sport}/${modelType}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ features }),
          }
        ),
      getRuns: (sport: string, modelType: string) =>
        fetchAPI<any[]>(
          `/admin/training-runs/${sport}/${modelType}`,
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
    payments: (params?: { limit?: number; offset?: number }) => {
      const q = new URLSearchParams();
      if (params?.limit) q.set("limit", String(params.limit));
      if (params?.offset) q.set("offset", String(params.offset));
      return fetchAPI<PaymentRecord[]>(
        `/api/subscriptions/payments?${q}`,
        {} 
      );
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

