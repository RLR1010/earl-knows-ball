"use client";

import { useState } from "react";

// ── Data Definitions ──────────────────────────────────────────────

interface Section {
  id: string;
  title: string;
  emoji: string;
  content: () => React.ReactNode;
}

interface File {
  path: string;
  purpose: string;
  sub?: File[];
}

interface ArticleCount {
  nfl: number;
  nba: number;
  mlb: number;
}

// ── Main Component ──

export default function SiteStructurePage() {
  const [expanded, setExpanded] = useState<string[]>([]);

  const toggle = (id: string) => {
    setExpanded(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]
    );
  };

  const isOpen = (id: string) => expanded.includes(id);

  const sections: Section[] = [
    {
      id: "overview",
      title: "Architecture Overview",
      emoji: "🏗️",
      content: () => <ArchitectureOverview />,
    },
    {
      id: "backend",
      title: "Backend Structure",
      emoji: "⚙️",
      content: () => <BackendStructure />,
    },
    {
      id: "models",
      title: "Database Models (SQLAlchemy)",
      emoji: "🗄️",
      content: () => <DatabaseModels />,
    },
    {
      id: "routers",
      title: "API Routers & Endpoints",
      emoji: "🔌",
      content: () => <ApiRouters />,
    },
    {
      id: "ingestion",
      title: "Ingestion Pipeline",
      emoji: "📥",
      content: () => <IngestionPipeline />,
    },
    {
      id: "handicapping",
      title: "Handicapping System",
      emoji: "🧠",
      content: () => <HandicappingSystem />,
    },
    {
      id: "pickcard",
      title: "MLB Pick Card — End-to-End Flow",
      emoji: "🃏",
      content: () => <PickCardFlow />,
    },
    {
      id: "frontend",
      title: "Frontend Structure",
      emoji: "🖥️",
      content: () => <FrontendStructure />,
    },
    {
      id: "admin",
      title: "Admin Panel",
      emoji: "🔐",
      content: () => <AdminPanel />,
    },
    {
      id: "infrastructure",
      title: "Infrastructure & Deployment",
      emoji: "🚢",
      content: () => <Infrastructure />,
    },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">🏗️ Site Structure</h1>
        <p className="text-gray-400 text-sm mt-1">
          Complete reference for the Earl Knows Ball codebase — architecture, data flow,
          file purposes, and key patterns. Use this page to understand how everything fits together.
        </p>
        <p className="text-gray-500 text-xs mt-2">
          Last updated: June 8, 2026
        </p>
      </div>

      {/* Search / Quick Jump */}
      <div className="flex flex-wrap gap-2 mb-6">
        {sections.map(s => (
          <button
            key={s.id}
            onClick={() => {
              document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth" });
              if (!isOpen(s.id)) toggle(s.id);
            }}
            className="px-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-xs text-gray-400 hover:text-white hover:bg-white/10 transition"
          >
            {s.emoji} {s.title}
          </button>
        ))}
      </div>

      {/* Sections */}
      {sections.map(s => (
        <section
          key={s.id}
          id={s.id}
          className="border border-white/10 rounded-2xl overflow-hidden bg-white/[0.02]"
        >
          <button
            onClick={() => toggle(s.id)}
            className="w-full flex items-center gap-3 px-6 py-4 bg-white/5 hover:bg-white/10 transition text-left"
          >
            <span className="text-xl">{s.emoji}</span>
            <span className="text-lg font-semibold text-white">{s.title}</span>
            <span className="ml-auto text-gray-500 text-sm">
              {isOpen(s.id) ? "▲" : "▼"}
            </span>
          </button>

          {isOpen(s.id) && (
            <div className="px-6 py-6">
              {s.content()}
            </div>
          )}
        </section>
      ))}

      {/* Footer */}
      <div className="text-center text-xs text-gray-600 py-8 border-t border-white/5">
        Earl Knows Ball — Site Structure Reference &middot; Rusty 🦀
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════
// SECTION COMPONENTS
// ═══════════════════════════════════════════════════════════════════

function ArchitectureOverview() {
  return (
    <div className="space-y-6">
      <p className="text-gray-300 leading-relaxed">
        <strong className="text-white">Earl Knows Ball</strong> is a multi-sport sports analytics platform
        with integrated AI chat. It ingests articles, game data, betting lines, DFS salaries, and
        player stats for <strong className="text-white">NFL</strong> (football),{" "}
        <strong className="text-white">NBA</strong> (basketball), and{" "}
        <strong className="text-white">MLB</strong> (baseball).
      </p>

      <div className="bg-white/5 border border-white/10 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">📐 Architecture: Schema-Per-Sport</h3>
        <p className="text-sm text-gray-400 leading-relaxed mb-3">
          Every sport has <strong className="text-white">its own Postgres schema</strong> with identical table
          structures. This is the single most important architectural rule:
        </p>
        <div className="grid grid-cols-3 gap-3 text-sm">
          {[
            { schema: "nfl", color: "text-green-400", data: "Football" },
            { schema: "nba", color: "text-orange-400", data: "Basketball" },
            { schema: "mlb", color: "text-red-400", data: "Baseball" },
          ].map(s => (
            <div key={s.schema} className="bg-white/5 rounded-lg p-3 border border-white/10">
              <div className={`font-bold text-lg ${s.color} font-mono`}>{s.schema}</div>
              <div className="text-xs text-gray-500">{s.data}</div>
              <div className="text-xs text-gray-400 mt-1">articles, games, teams,</div>
              <div className="text-xs text-gray-400">players, seasons,</div>
              <div className="text-xs text-gray-400">betting_lines, game_predictions,</div>
              <div className="text-xs text-gray-400">article_embeddings</div>
            </div>
          ))}
        </div>
        <p className="text-xs text-gray-500 mt-3">
          Chat history is shared in <code className="text-earl-400">public.chat_history</code> scoped by a <code className="text-earl-400">sport</code> column.
          Auth/users are also in the <code className="text-earl-400">public</code> schema.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">📦 Tech Stack</h3>
          <ul className="text-xs text-gray-400 space-y-1">
            <li><strong className="text-gray-300">Backend:</strong> Python / FastAPI (port 8001)</li>
            <li><strong className="text-gray-300">Frontend:</strong> Next.js / React / TypeScript / Tailwind (port 3000)</li>
            <li><strong className="text-gray-300">Database:</strong> PostgreSQL 16 + pgvector</li>
            <li><strong className="text-gray-300">Vector DB:</strong> pgvector (nomic-embed-text, 768d)</li>
            <li><strong className="text-gray-300">AI Chat:</strong> DeepSeek V4 Flash</li>
            <li><strong className="text-gray-300">ML:</strong> XGBoost (rolling year-by-year training)</li>
            <li><strong className="text-gray-300">GPU:</strong> 2x GTX 1080 Ti (Ollama + model training)</li>
            <li><strong className="text-gray-300">Deployment:</strong> Docker Compose on home workstation</li>
          </ul>
        </div>

        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">🌐 Top-Level URL Routes</h3>
          <ul className="text-xs text-gray-400 space-y-1">
            <li><code className="text-earl-400">/{'{sport}'}</code> - Sport home page</li>
            <li><code className="text-earl-400">/{'{sport}'}/teams</code> - Team list</li>
            <li><code className="text-earl-400">/{'{sport}'}/teams/[abbreviation]</code> - Team detail</li>
            <li><code className="text-earl-400">/{'{sport}'}/players</code> - Player list</li>
            <li><code className="text-earl-400">/{'{sport}'}/players/[id]</code> - Player detail</li>
            <li><code className="text-earl-400">/{'{sport}'}/schedule</code> - Schedule & games</li>
            <li><code className="text-earl-400">/{'{sport}'}/games/[id]</code> - Game detail / box score</li>
            <li><code className="text-earl-400">/{'{sport}'}/stats</code> - League stats</li>
            <li><code className="text-earl-400">/chat</code> - AI chat</li>
            <li><code className="text-earl-400">/login</code> / <code className="text-earl-400">/register</code> - Auth</li>
            <li><code className="text-earl-400">/admin</code> - Admin panel</li>
          </ul>
        </div>
      </div>
    </div>
  );
}

function BackendStructure() {
  const dirs = [
    {
      path: "backend/app/",
      items: [
        "main.py — FastAPI app entry, lifespan (init_db + scheduler startup), router registration",
        "database.py — SQLAlchemy engine + session config (async_sessionmaker)",
        "context_processor.py — Chat context builder (articles → DeepSeek prompts)",
        "task_scheduler.py — APScheduler-based task runner for periodic ingestion",
        "seed_admin.py — CLI for creating initial admin user",
      ],
    },
    {
      path: "backend/app/core/",
      items: [
        "config.py — Pydantic Settings: DB URLs, JWT secret, CORS origins, app name",
        "security.py — JWT token creation/verification, password hashing (passlib bcrypt)",
      ],
    },
    {
      path: "backend/app/routers/",
      items: [
        "auth.py — POST /auth/register, /auth/login, /auth/me, /auth/me (PATCH)",
        "teams.py — GET /api/teams (with sport filter), GET /api/teams/&#123;abbr&#125;",
        "players.py — GET /api/players (with sport filter), GET /api/players/&#123;id&#125;",
        "games.py — GET /api/games (with sport/schedule filter), GET /api/games/&#123;id&#125;/box-score",
        "chat.py — POST /api/chat (NFL), streaming DeepSeek + pgvector RAG",
        "chat_nba.py — POST /api/chat/nba (NBA chat, same pattern)",
        "chat_mlb.py — POST /api/chat/mlb (MLB chat, same pattern)",
        "ingest.py — POST /api/ingest/* (RSS, SB Nation, betting lines, DFS, etc.)",
        "handicap.py — NFL handicapping endpoints (/api/handicapping/*)",
        "handicap_mlb.py — MLB handicapping endpoints (/api/handicapping/mlb/*)",
        "stats.py — NFL stats endpoints",
        "mlb_stats.py — MLB stats (/mlb/players, /mlb/stats/, /mlb/games/, /mlb/games/&#123;id&#125;/boxscore)",
        "nba_stats.py — NBA stats endpoints",
        "admin.py — Admin API (/api/admin/*): dashboard, users, plans, subscriptions, models, tasks",
        "subscriptions.py — Stripe subscription management",
        "articles.py — GET /api/articles (public article access)",
      ],
    },
    {
      path: "backend/app/ingestion/",
      items: [
        "articles.py — NFL RSS feeds & SB Nation archive scraper (37 NFL feeds)",
        "articles_nba.py — NBA RSS feeds & SB Nation archives (39 NBA feeds)",
        "articles_mlb.py — MLB RSS feeds & SB Nation archives (41 MLB feeds)",
        "articles_fangraphs.py — FanGraphs archive scraper (MLB)",
        "articles_hoopsrumors.py — HoopsRumors NBA news scraper",
        "sbnation_archives.py — Multi-blog archive scraper (all sports)",
        "pft_archives.py — Pro Football Talk historical archives",
        "national_archives.py — National NFL media scrapers",
        "rss_feeds.py — Feed registry: all RSS feeds indexed by sport/team",
        "betting_lines.py — NFL betting lines (The Odds API)",
        "nfl_opening_lines.py — NFL opening lines pipeline (The Odds API snapshot)",
        "mlb_betting_lines.py — MLB betting lines (The Odds API + historical datasets)",
        "mlb_odds_consolidated.py — MLB consolidated odds table builder",
        "mlb_lineups.py — MLB lineups fetcher",
        "nba_betting_lines.py — NBA betting lines loader",
        "dfs_salaries.py — NFL DFS salaries (DraftKings)",
        "nba_dfs_salaries.py — NBA DFS salaries (DraftKings)",
        "historical.py — NFL historical game data",
        "mlb_stats.py — MLB player stats (batting/pitching) from MLB API",
        "nba_stats.py — NBA player stats ingestion",
        "mlb_pitcher_stats.py — MLB pitcher game logs",
        "player_profiles.py — Player headshot + profile ingestion",
        "match_players.py — Player matching across data sources",
        "historical_games.py — Historical game data backfill",
        "nflverse.py — nflverse data pipeline",
        "nflverse_data.py — Additional nflverse datasets",
        "nfl_pace.py — NFL pace/stats aggregation",
        "depth_charts.py — NFL depth charts (Ourlads)",
        "ourlads_archive.py — Ourlads historical depth charts",
        "import_sbr_lines.py — SBR lines historical import",
        "historical_lines_backfill.py — Historical betting lines backfill",
        "rebuild_game_lines.py — Game lines rebuild utility",
        "fill_missing_mlb_opening.py — Fix missing MLB opening lines",
        "compute_team_rankings.py — Team ranking computation",
        "per_game_backfill.py — Per-game prediction backfill",
        "pipeline.py — Orchestration pipeline",
        "pgvector_search.py — pgvector search: multi-sport (NFL/NBA/MLB) via SPORT_CONFIG",
      ],
    },
    {
      path: "backend/app/handicapping/",
      items: [
        "engine.py — NFL handicapping engine (MatchupAnalysis → NFLHandicapper)",
        "mlb_engine.py — MLB handicapping engine (MLBPickCard → MLBHandicapper)",
        "mlb_backtest.py — MLB backtest runner (ATS model, mlb_backtest_results.json)",
        "mlb_backtest_ou.py — MLB O/U backtest (mlb_ou_backtest_results.json)",
        "mlb_backtest_ml.py — MLB ML backtest (mlb_ml_backtest_results.json)",
        "mlb_situational.py — MLB situational analysis (rest, travel, weather)",
        "mlb_splits.py — MLB betting splits (line movement, implied public %)",
        "ml_model.py — NFL ML model training",
        "train_rolling*.py — NFL rolling ATS/OU/ML training scripts",
        "xgb_predict*.py — NFL XGBoost prediction scripts",
        "ml_confidence_calibration.json / confidence_calibration.json — Calibration curves",
      ],
    },
  ];

  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed mb-4">
        The backend is a Python FastAPI application at <code className="text-earl-400">backend/app/</code>.
        It uses SQLAlchemy async with PostgreSQL, APScheduler for cron tasks, and XGBoost for predictions.
      </p>
      {dirs.map(dir => (
        <div key={dir.path} className="mb-4">
          <h3 className="text-xs font-bold text-earl-400 uppercase tracking-wider mb-2 font-mono">
            {dir.path}
          </h3>
          <ul className="text-xs text-gray-400 space-y-1">
            {dir.items.map((item, i) => (
              <li key={i} className="hover:text-gray-300 transition">
                <code className="text-earl-300">{item.split(" — ")[0]}</code>
                <span className="text-gray-500"> — </span>
                {item.split(" — ").slice(1).join(" — ")}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function DatabaseModels() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        Each sport has its own set of models in a dedicated folder. NFL models live at the top level in{" "}
        <code className="text-earl-400">backend/app/models/</code>, NBA in{" "}
        <code className="text-earl-400">backend/app/models/nba/</code>, and MLB in{" "}
        <code className="text-earl-400">backend/app/models/mlb/</code>. All sports use{" "}
        <code className="text-earl-400">__table_args__ = {"{"}"schema": "nfl"{"}"}</code> (or nba/mlb)
        to route to the correct schema.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ModelTable
          title="NFL Models (no subdirectory)"
          models={[
            ["Article", "nfl.articles + article_embeddings (pgvector)"],
            ["Game", "nfl.games"],
            ["Team", "nfl.teams"],
            ["Player", "nfl.players"],
            ["Season", "nfl.seasons"],
            ["BettingLine", "nfl.betting_lines"],
            ["GameLine", "nfl.game_lines"],
            ["DFSsalary", "nfl.dfs_salaries"],
            ["Injury", "nfl.injuries"],
            ["DepthChart", "nfl.depth_charts"],
            ["GamePrediction", "nfl.game_predictions"],
            ["Transaction", "nfl.transactions"],
            ["TeamPaceStats", "nfl.team_pace_stats"],
            ["PlayerWeeklyStats", "nfl.player_weekly_stats"],
            ["DepthChartArchive", "nfl.depth_chart_archive"],
            ["ChatHistory", "public.chat_history (sport='nfl')"],
          ]}
        />

        <div className="space-y-4">
          <ModelTable
            title="MLB Models (models/mlb/)"
            models={[
              ["MLBArticle", "mlb.articles + article_embeddings (pgvector)"],
              ["MLBGame", "mlb.games"],
              ["MLBTeam", "mlb.teams"],
              ["MLBPlayer", "mlb.players"],
              ["MLBSeason", "mlb.seasons"],
              ["MLBBettingLine", "mlb.betting_lines"],
              ["MLBBettingLinesConsolidated", "mlb.betting_lines_consolidated"],
              ["MLBGamePrediction", "mlb.game_predictions"],
              ["MLBLineup", "mlb.lineups"],
              ["MLBBattingStats", "mlb.batting_stats"],
              ["MLBPitchingStats", "mlb.pitching_stats"],
            ]}
          />

          <ModelTable
            title="NBA Models (models/nba/)"
            models={[
              ["NBAArticle", "nba.articles + article_embeddings (pgvector)"],
              ["NBAGame", "nba.games"],
              ["NBATeam", "nba.teams"],
              ["NBAPlayer", "nba.players"],
              ["NBASeason", "nba.seasons"],
              ["NBABettingLine", "nba.betting_lines"],
              ["NBADfsSalary", "nba.dfs_salaries"],
              ["NBAPlayerStats", "nba.player_stats"],
            ]}
          />

          <ModelTable
            title="Shared Models (public schema)"
            models={[
              ["User", "public.users"],
              ["ChatHistory", "public.chat_history (sport column)"],
              ["SubscriptionPlan", "public.subscription_plans"],
              ["UserSubscription", "public.user_subscriptions"],
              ["Payment", "public.payments"],
            ]}
          />
        </div>
      </div>
    </div>
  );
}

function ModelTable({ title, models }: { title: string; models: [string, string][] }) {
  return (
    <div className="bg-white/5 border border-white/10 rounded-xl overflow-hidden">
      <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-earl-400">{title}</div>
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-white/10">
            <th className="text-left py-1.5 px-3 text-gray-500 font-medium">Model</th>
            <th className="text-left py-1.5 px-3 text-gray-500 font-medium">Table</th>
          </tr>
        </thead>
        <tbody>
          {models.map(([model, table]) => (
            <tr key={model} className="border-b border-white/5 hover:bg-white/[0.02]">
              <td className="py-1.5 px-3 font-mono text-gray-300">{model}</td>
              <td className="py-1.5 px-3 text-gray-400">{table}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ApiRouters() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        FastAPI includes all routers from <code className="text-earl-400">backend/app/main.py</code>.
        Each router is a separate file in <code className="text-earl-400">routers/</code>.
      </p>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-white/10">
              <th className="text-left py-2 px-3 text-gray-400 font-medium">Prefix</th>
              <th className="text-left py-2 px-3 text-gray-400 font-medium">File</th>
              <th className="text-left py-2 px-3 text-gray-400 font-medium">Key Endpoints</th>
            </tr>
          </thead>
          <tbody>
            {[
              ["/auth", "auth.py", "POST register, login · GET me"],
              ["/api", "teams.py", "GET teams (sport filter) · GET teams/&#123;abbr&#125;"],
              ["/api", "players.py", "GET players (sport filter) · GET players/&#123;id&#125;"],
              ["/api", "games.py", "GET games (sport/schedule) · GET games/&#123;id&#125;/box-score"],
              ["/api/chat", "chat.py", "POST /api/chat (NFL streaming)"],
              ["/api/chat/nba", "chat_nba.py", "POST /api/chat/nba (NBA streaming)"],
              ["/api/chat/mlb", "chat_mlb.py", "POST /api/chat/mlb (MLB streaming)"],
              ["/api/ingest", "ingest.py", "All ingestion endpoints (RSS, SB Nation, betting lines, DFS)"],
              ["/api/handicapping", "handicap.py", "NFL: GET predictions, team-stats, process, date picks"],
              ["/api/handicapping/mlb", "handicap_mlb.py", "MLB: GET date picks, matchup, team-stats, standings, situational, splits"],
              ["/api", "stats.py", "GET stats (NFL)"],
              ["/api", "mlb_stats.py", "GET mlb/players, mlb/stats/batting, mlb/stats/pitching, mlb/games, mlb/games/&#123;id&#125;/boxscore"],
              ["/api", "nba_stats.py", "GET nba stats endpoints"],
              ["", "admin.py", "GET/POST/PATCH/DELETE admin dashboard, users, plans, subs, payments, models, tasks, articles"],
              ["", "subscriptions.py", "Stripe webhook + subscription management"],
              ["", "articles.py", "GET /api/articles (public article access)"],
            ].map(([prefix, file, endpoints]) => (
              <tr key={file} className="border-b border-white/5 hover:bg-white/[0.02]">
                <td className="py-1.5 px-3">
                  <code className="text-earl-400">{prefix}</code>
                </td>
                <td className="py-1.5 px-3 font-mono text-gray-300">{file}</td>
                <td className="py-1.5 px-3 text-gray-400 text-[11px]">{endpoints}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function IngestionPipeline() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        Ingestion runs through two systems: <strong className="text-white">daily cron jobs</strong> (RSS feeds
        via OpenClaw cron) and <strong className="text-white">standalone container scripts</strong> (archive
        scrapers, backfills). The embedding pipeline runs as a separate Docker container.
      </p>

      {/* Daily RSS schedule */}
      <div className="bg-white/5 border border-white/10 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">📅 Daily RSS Scraping Schedule</h3>
        <div className="grid grid-cols-3 gap-3 text-xs">
          {[
            { sport: "NFL", time: "8:00 AM CT", feeds: "37 feeds", file: "articles.py" },
            { sport: "NBA", time: "8:00 AM CT", feeds: "39 feeds", file: "articles_nba.py" },
            { sport: "MLB", time: "8:30 AM CT", feeds: "41 feeds", file: "articles_mlb.py" },
          ].map(s => (
            <div key={s.sport} className="bg-white/5 rounded-lg p-3 border border-white/10">
              <div className="font-bold text-white">{s.sport}</div>
              <div className="text-gray-400 mt-1">{s.time}</div>
              <div className="text-gray-500">{s.feeds}</div>
              <div className="text-gray-600 mt-1">
                <code className="text-earl-400">ingestion/{s.file}</code>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Standalone scraper containers */}
      <div className="bg-amber-900/10 border border-amber-800/20 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-amber-400 mb-3">📦 Standalone Scraper Containers</h3>
        <p className="text-xs text-amber-300/80 mb-3">
          These run as <code className="text-amber-400">docker run</code> (NOT docker compose) and survive
          <code className="text-amber-400">docker compose down</code>.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
          {[
            { file: "backfill.sh", label: "NFL historical articles" },
            { file: "national_scraper.sh", label: "National NFL archives" },
            { file: "nba_scraper.sh", label: "NBA + Backfill archives" },
            { file: "mlb_scraper.sh", label: "MLB + FanGraphs archives" },
            { file: "embed_all.sh", label: "Embeddings (all sports)" },
          ].map(s => (
            <div key={s.file} className="bg-amber-900/20 rounded-lg p-2 border border-amber-800/20">
              <code className="text-amber-400">{s.file}</code>
              <div className="text-gray-500 mt-0.5">{s.label}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Embedding pipeline */}
      <div className="bg-purple-900/10 border border-purple-800/20 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-purple-400 mb-2">🧠 Embedding Pipeline (pgvector)</h3>
        <div className="text-xs text-gray-400 space-y-1">
          <p><strong className="text-gray-300">Script:</strong> <code className="text-purple-300">backend/run_embed_pgvector_all.py</code></p>
          <p><strong className="text-gray-300">Container:</strong> <code className="text-purple-300">earl-embed-pgvector</code> (standalone docker run)</p>
          <p><strong className="text-gray-300">Model:</strong> Ollama <code className="text-purple-300">nomic-embed-text</code> at localhost:11434 (768d vectors)</p>
          <p><strong className="text-gray-300">Rate:</strong> ~2.2 articles/sec (bottleneck: Ollama on 2x GTX 1080 Ti)</p>
          <p><strong className="text-gray-300">Index:</strong> IVFFlat, cosine similarity, lists=100</p>
          <p><strong className="text-gray-300">Cycle:</strong> Cycles NFL → NBA → MLB per loop, finds articles where <code className="text-purple-300">embedded_at IS NULL</code></p>
          <p><strong className="text-gray-300">Runner:</strong> <code className="text-purple-300">./embed_all.sh {"{start|status|logs|stop}"}</code></p>
        </div>
      </div>

      {/* Data sources */}
      <div className="bg-blue-900/10 border border-blue-800/20 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-blue-400 mb-2">📡 Data Sources by Sport</h3>
        <div className="grid grid-cols-3 gap-3 text-xs">
          <div>
            <h4 className="font-bold text-green-400 mb-2">NFL</h4>
            <ul className="space-y-0.5 text-gray-400">
              <li>ESPN, Yahoo, The Athletic</li>
              <li>SB Nation (main + 32 team blogs)</li>
              <li>Pro Football Talk (archives)</li>
              <li>Sportsnaut, NFL Spin Zone</li>
              <li>Last Word on Sports</li>
              <li>The Odds API (betting lines)</li>
              <li>nflverse (historical data)</li>
              <li>Ourlads (depth charts)</li>
              <li>DraftKings (DFS salaries)</li>
            </ul>
          </div>
          <div>
            <h4 className="font-bold text-orange-400 mb-2">NBA</h4>
            <ul className="space-y-0.5 text-gray-400">
              <li>ESPN, Yahoo, CBS, NBC Sports</li>
              <li>The Athletic</li>
              <li>ClutchPoints</li>
              <li>BasketballNews.com</li>
              <li>SB Nation (main + 30 team blogs)</li>
              <li>HoopsRumors</li>
              <li>The Odds API (betting lines)</li>
              <li>DraftKings (DFS salaries)</li>
            </ul>
          </div>
          <div>
            <h4 className="font-bold text-red-400 mb-2">MLB</h4>
            <ul className="space-y-0.5 text-gray-400">
              <li>ESPN, Yahoo, CBS</li>
              <li>MLB.com, The Athletic</li>
              <li>FanGraphs (articles + archives)</li>
              <li>Baseball Prospectus</li>
              <li>Pitcher List</li>
              <li>MLB Trade Rumors</li>
              <li>SB Nation (main + 30 team blogs)</li>
              <li>The Odds API (betting lines)</li>
              <li>SBR / GitHub (historical lines)</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

function HandicappingSystem() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        The handicapping system uses three specialized <strong className="text-white">XGBoost</strong> models
        per sport, each optimized for a different betting market: ATS (spread/run line), O/U (total), and
        ML (moneyline). Models are trained in a <strong className="text-white">rolling year-by-year</strong> fashion
        to prevent look-ahead bias.
      </p>

      <div className="grid grid-cols-3 gap-3">
        {[
          {
            name: "ATS Model",
            color: "text-blue-400",
            border: "border-blue-800/30",
            bg: "bg-blue-900/10",
            sport: "NFL",
            features: "20 (opponent-adjusted scoring, market, form, identity, situational)",
            algo: "XGBoost Regressor, n_estimators=500, max_depth=6",
            file: "train_rolling_ats.py",
          },
          {
            name: "O/U Model",
            color: "text-yellow-400",
            border: "border-yellow-800/30",
            bg: "bg-yellow-900/10",
            sport: "NFL",
            features: "18 (scoring, pace, market, form, weather)",
            algo: "XGBoost Regressor, n_estimators=200, max_depth=4",
            file: "train_rolling_ou.py",
          },
          {
            name: "ML Model",
            color: "text-red-400",
            border: "border-red-800/30",
            bg: "bg-red-900/10",
            sport: "NFL",
            features: "27 (full set + Platt calibration)",
            algo: "XGBoost Classifier, n_estimators=300, max_depth=4",
            file: "train_rolling_ml.py",
          },
        ].map(m => (
          <div key={m.name} className={`${m.bg} border ${m.border} rounded-xl p-4`}>
            <h3 className={`text-sm font-bold ${m.color}`}>{m.name}</h3>
            <div className="text-xs text-gray-400 mt-2 space-y-1">
              <p><strong className="text-gray-300">Sport:</strong> {m.sport}</p>
              <p><strong className="text-gray-300">Features:</strong> {m.features}</p>
              <p><strong className="text-gray-300">Algorithm:</strong> {m.algo}</p>
              <p><strong className="text-gray-300">Training:</strong> <code className="text-earl-400">{m.file}</code></p>
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-3 gap-3">
        {[
          {
            name: "MLB ATS Model",
            color: "text-blue-400",
            border: "border-blue-800/30",
            bg: "bg-blue-900/10",
            features: "32 (rolling runs, splits, rest, market, situational)",
            file: "mlb_backtest.py",
            trained: "Yes",
            backtest_file: "mlb_backtest_results.json",
          },
          {
            name: "MLB O/U Model",
            color: "text-yellow-400",
            border: "border-yellow-800/30",
            bg: "bg-yellow-900/10",
            features: "9 (implied total, movement, pitcher, bullpen, park)",
            file: "mlb_backtest_ou.py",
            trained: "Yes",
            backtest_file: "mlb_ou_backtest_results.json",
          },
          {
            name: "MLB ML Model",
            color: "text-red-400",
            border: "border-red-800/30",
            bg: "bg-red-900/10",
            features: "23 (rolling, pitching, market, form)",
            file: "mlb_backtest_ml.py",
            trained: "Yes",
            backtest_file: "mlb_ml_backtest_results.json",
          },
        ].map(m => (
          <div key={m.name} className={`${m.bg} border ${m.border} rounded-xl p-4`}>
            <h3 className={`text-sm font-bold ${m.color}`}>{m.name}</h3>
            <div className="text-xs text-gray-400 mt-2 space-y-1">
              <p><strong className="text-gray-300">Features:</strong> {m.features}</p>
              <p><strong className="text-gray-300">Trained:</strong> {m.trained}</p>
              <p><strong className="text-gray-300">Code:</strong> <code className="text-earl-400">{m.file}</code></p>
              <p><strong className="text-gray-300">Results:</strong> <code className="text-earl-400">{m.backtest_file}</code></p>
            </div>
          </div>
        ))}
      </div>

      <div className="bg-white/5 border border-white/10 rounded-xl p-4 text-xs text-gray-400">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">🧬 Key Handicapping Files</h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <h4 className="font-bold text-gray-300 mb-1">NFL</h4>
            <ul className="space-y-0.5">
              <li><code className="text-earl-400">engine.py</code> — NFLHandicapper + MatchupAnalysis</li>
              <li><code className="text-earl-400">situational.py</code> — NFL situational factors</li>
              <li><code className="text-earl-400">splits.py</code> — NFL betting splits</li>
              <li><code className="text-earl-400">pace.py</code> — NFL pace analysis</li>
              <li><code className="text-earl-400">calibrate_confidence.py</code> — Confidence calibration</li>
              <li><code className="text-earl-400">confidence_calibration.json</code> — Curve data</li>
            </ul>
          </div>
          <div>
            <h4 className="font-bold text-gray-300 mb-1">MLB</h4>
            <ul className="space-y-0.5">
              <li><code className="text-earl-400">mlb_engine.py</code> — MLBHandicapper + MLBPickCard</li>
              <li><code className="text-earl-400">mlb_situational.py</code> — MLB situational factors</li>
              <li><code className="text-earl-400">mlb_splits.py</code> — MLB betting splits</li>
              <li><code className="text-earl-400">mlb_backtest.py</code> — MLB ATS backtest</li>
              <li><code className="text-earl-400">mlb_backtest_ou.py</code> — MLB O/U backtest</li>
              <li><code className="text-earl-400">mlb_backtest_ml.py</code> — MLB ML backtest</li>
              <li><code className="text-earl-400">mlb_confidence_calibration.json</code> — Calibration curve</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

function PickCardFlow() {
  return (
    <div className="space-y-6">
      <div className="bg-yellow-900/10 border border-yellow-800/20 rounded-xl p-4 mb-4">
        <h3 className="text-sm font-semibold text-yellow-400 mb-2">⚠️ The MLB Pick Card Problem</h3>
        <div className="text-xs text-gray-400 space-y-2">
          <p>
            The MLB pick card was recently running into issues. Here is the complete data flow to understand
            what can go wrong:
          </p>
        </div>
      </div>

      {/* End-to-end flow diagram */}
      <div className="bg-white/5 border border-white/10 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-earl-400 mb-4">📊 End-to-End Flow: API → Pick Card</h3>

        <div className="relative text-xs">
          {/* Step 1 */}
          <Step num={1} label="User visits MLB game detail page">
            Frontend: <code className="text-earl-400">/{'{sport}'}/games/[id]/page.tsx</code>
            <br />
            The <code className="text-earl-300">GameDetailPage</code> detects <code className="text-earl-300">sport === "mlb"</code> and renders {"<"}MLBClassicPage {"/"}＞
          </Step>

          <Arrow />

          {/* Step 2 */}
          <Step num={2} label="MLBClassicPage fetches boxscore">
            <code className="text-earl-400">GET /api/mlb/games/{"{game_id}"}/boxscore</code>
            <br />
            Handled by <code className="text-earl-300">routers/mlb_stats.py</code> — function <code className="text-earl-300">mlb_game_boxscore()</code>
          </Step>

          <Arrow />

          {/* Step 3 */}
          <Step num={3} label="Backend loads game + live data">
            <ul className="space-y-1 mt-1">
              <li>1. Queries <code className="text-earl-400">mlb.games</code> for game info (teams, date, venue)</li>
              <li>2. Fetches <strong className="text-white">MLB Stats API</strong> (<code className="text-earl-400">/api/v1.1/game/&#123;gamePk&#125;/feed/live</code> + <code className="text-earl-400">/api/v1/game/&#123;gamePk&#125;/boxscore</code>) for live scores + player stats</li>
              <li>3. Syncs DB status/scores if changed (live data overlay)</li>
              <li>4. Queries <code className="text-earl-400">mlb.betting_lines_consolidated</code> for lines (spread, OU, ML)</li>
            </ul>
          </Step>

          <Arrow />

          {/* Step 4 */}
          <Step num={4} label="Pick card assembly — Two Paths">
            <div className="grid grid-cols-2 gap-3 mt-2">
              <div className="bg-blue-900/20 border border-blue-800/30 rounded-lg p-3">
                <h4 className="text-blue-300 font-semibold mb-1">✅ Cached Prediction</h4>
                <p className="text-xs text-gray-400">
                  Queries <code className="text-earl-400">mlb.game_predictions</code> WHERE source='api' AND game_id=...
                  <br />
                  If found → return pick_card directly. This is the <strong className="text-green-400">fast path</strong>.
                </p>
                <p className="text-xs text-gray-500 mt-2">
                  Contains: predicted_home_runs, predicted_away_runs, margin, rl_conf, ou_conf, ml_conf, picks, results
                </p>
              </div>
              <div className="bg-red-900/20 border border-red-800/30 rounded-lg p-3">
                <h4 className="text-red-300 font-semibold mb-1">⚠️ Inline Generation</h4>
                <p className="text-xs text-gray-400">
                  If no cached prediction AND game is scheduled: calls{" "}
                  <code className="text-earl-400">MLBHandicapper.handicap_date()</code> inline.
                  <br />
                  This is the <strong className="text-red-400">slow path</strong> — can timeout (25s limit).
                </p>
                <p className="text-xs text-gray-500 mt-2">
                  Requires trained model files on disk, DB connection, asyncpg for feature building.
                </p>
              </div>
            </div>
          </Step>

          <Arrow />

          {/* Step 5 */}
          <Step num={5} label="MLBHandicapper.generate_pick_cards()">
            <p>For each game on the date:</p>
            <ul className="space-y-1 mt-1">
              <li>1. Load market lines (spread, OU, ML odds) from <code className="text-earl-400">mlb.betting_lines_consolidated</code></li>
              <li>2. Load trained ATS model: <code className="text-amber-400">/app/data/mlb_margin_model_prod.pkl</code></li>
              <li>3. Load trained OU model: <code className="text-amber-400">/app/data/mlb_ou_model_prod.pkl</code></li>
              <li>4. Load trained ML model: <code className="text-amber-400">/app/data/mlb_ml_model_prod.pkl</code></li>
              <li>5. For each model, build features via asyncpg (real-time DB queries for rolling stats, pitcher ERA, travel, etc.)</li>
              <li>6. Run XGBoost prediction → get margin / total / win prob</li>
            </ul>
          </Step>

          <Arrow />

          {/* Step 6 */}
          <Step num={6} label="MLBPickCard.predict() — Score Synthesis">
            <p>Combines three model outputs into unified pick card:</p>
            <ul className="space-y-1 mt-1">
              <li><strong className="text-blue-300">Total =</strong> OU prediction || (season_avg_runs * 2)</li>
              <li><strong className="text-blue-300">Margin =</strong> ATS prediction</li>
              <li><strong className="text-blue-300">Scores:</strong> home_runs = (total + margin) / 2, away_runs = (total - margin) / 2</li>
              <li><strong className="text-blue-300">RL pick:</strong> margin vs run_line spread</li>
              <li><strong className="text-blue-300">ML pick:</strong> home_win_prob vs market implied</li>
              <li><strong className="text-blue-300">OU pick:</strong> predicted total vs market total</li>
              <li><strong className="text-blue-300">Conflict detection:</strong> ATS vs ML agreement check → confidence adjustment</li>
            </ul>
          </Step>

          <Arrow />

          {/* Step 7 */}
          <Step num={7} label="Save to DB + Cache">
            <p>
              <code className="text-earl-400">_save_prediction()</code> writes to <code className="text-earl-400">mlb.game_predictions</code> with
              source='api'. Does NOT overwrite if game has already started.
            </p>
          </Step>

          <Arrow />

          {/* Step 8 */}
          <Step num={8} label="Frontend renders pick card">
            <p>MLBClassicPage renders the pick card section if <code className="text-earl-400">data.pick_card</code> is present:</p>
            <ul className="space-y-1 mt-1">
              <li>• Predicted score card (if predictions exist)</li>
              <li>• Actual score (if game is final)</li>
              <li>• Three result panels: Run Line (blue), Over/Under (yellow), Moneyline (cyan)</li>
              <li>• Each shows the pick, confidence bar, and market line</li>
            </ul>
          </Step>
        </div>
      </div>

      {/* Error sources */}
      <div className="bg-red-900/10 border border-red-800/20 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-red-400 mb-3">
          🔍 Common Failure Modes (Pick Cards)
        </h3>
        <div className="text-xs space-y-2">
          {[
            ["No trained model file", "mlb_margin_model_prod.pkl (or ou/ml) not present at /app/data/. Check that backtest has been run and model pickled."],
            ["asyncpg connection failure", "_ats_predict / _ou_predict / _ml_predict functions open direct asyncpg connections using DSN. If DB is unreachable, feature building fails silently → fallback to stat-based estimate."],
            ["Handicapper timeout", "handicap_date() has a 25s timeout in the inline generation fallback. Each game needs 3 DB-heavy queries + ML inference → can time out on busy dates."],
            ["Missing betting lines", "No betting_lines_consolidated row for game → no market data → no OU/ML picks. Verify mlb.odds_consolidated_pipeline() has run."],
            ["Zero articles / stats", "TeamStatsBuilder returns empty if season has no games. For future games on opening day, no rolling stats → margin falls back to 0."],
            ["Confidence calibration file missing", "calibrate_confidence() imports mlb_confidence_calibration.json. If absent or malformed, raw confidence values are used uncalibrated."],
            ["SQLAlchemy vs asyncpg model mismatch", "Engine uses SQLAlchemy for base queries but asyncpg for feature building (both need to agree on DSN). Mismatched DB URLs = silent fallback."],
            ["Cache not being invalidated", "Inline generation only happens on cache miss. If a prediction was saved before models updated, stale data serves until deleted."],
          ].map(([issue, detail]) => (
            <div key={issue} className="flex gap-2">
              <span className="text-red-400 shrink-0 mt-0.5">•</span>
              <div>
                <span className="font-semibold text-red-300">{issue}:</span>
                <span className="text-gray-400 ml-1">{detail}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Data flow diagram */}
      <div className="bg-white/5 border border-white/10 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">📡 MLB Boxscore Response Shape</h3>
        <div className="text-xs text-gray-400">
          <p className="mb-2">
            The <code className="text-earl-400">/api/mlb/games/{"{id}"}/boxscore</code> endpoint returns a JSON object
            with these top-level keys:
          </p>
          <pre className="bg-black/40 rounded-lg p-4 text-gray-300 overflow-x-auto">
{`{
  "game": {           // From mlb.games table
    id, mlb_game_id, home_team, away_team,
    home_score, away_score, status, date,
    venue, attendance, duration_minutes
  },
  "boxscore": {       // MLB Stats API proxy (player stats)
    teams: { away: { players: {...}, batters: [...], pitchers: [...] }, home: { ... } }
  },
  "linescore": {      // Live inning-by-inning data
    currentInning, innings: [...], teams: { away: { runs, hits, errors }, home: {...} }
  },
  "betting_lines": [{ // From betting_lines_consolidated
    spread, over_under, home_moneyline, away_moneyline,
    opening_spread, opening_total,
    home_implied_probability, away_implied_probability
  }],
  "pick_card": {      // From mlb.game_predictions (or inline)
    predictions: { home_runs, away_runs, total, margin },
    actual: { home_runs, away_runs, total, margin },
    results: { run_line, over_under, moneyline },
    confidence: { rl, ou, ml, margin, raw: { rl, ou, ml } },
    picks: { run_line, over_under, moneyline },
    models: { ats_margin, ou_total, home_win_prob, ml_edge },
    lines: { home_moneyline, away_moneyline, run_line, over_under }
  },
  "splits": {         // From MLBSplitAnalyzer
    line_movement, implied_public_pct, current_line
  }
}`}
          </pre>
        </div>
      </div>
    </div>
  );
}

function FrontendStructure() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        The frontend is a Next.js app at <code className="text-earl-400">frontend/</code>. It uses
        React with TypeScript and Tailwind CSS. The app uses the App Router with a sports layout pattern.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">📁 Pages</h3>
          <div className="text-xs space-y-0.5 text-gray-400">
            <FileLine path="frontend/src/app/layout.tsx" desc="Root layout (dark theme, fonts)" />
            <FileLine path="frontend/src/app/page.tsx" desc="Landing page (sport selection)" />
            <FileLine path="frontend/src/app/chat/page.tsx" desc="AI chat page (tabbed NFL/NBA/MLB)" />
            <FileLine path="frontend/src/app/login/page.tsx" desc="Login form" />
            <FileLine path="frontend/src/app/register/page.tsx" desc="Registration form" />
            <FileLine path="frontend/src/app/(sports)/[sport]/page.tsx" desc="Sport home page" />
            <FileLine path="frontend/src/app/(sports)/[sport]/teams/page.tsx" desc="Teams list (sport-scoped)" />
            <FileLine path="frontend/src/app/(sports)/[sport]/teams/[abbreviation]/page.tsx" desc="Team detail" />
            <FileLine path="frontend/src/app/(sports)/[sport]/players/page.tsx" desc="Players list" />
            <FileLine path="frontend/src/app/(sports)/[sport]/players/[id]/page.tsx" desc="Player detail" />
            <FileLine path="frontend/src/app/(sports)/[sport]/schedule/page.tsx" desc="Season schedule + games" />
            <FileLine path="frontend/src/app/(sports)/[sport]/games/[id]/page.tsx" desc="Game detail (NFL boxscore + MLB pick card)" />
            <FileLine path="frontend/src/app/(sports)/[sport]/stats/page.tsx" desc="League stats tables" />
            <FileLine path="frontend/src/app/teams/page.tsx" desc="(Legacy) All teams" />
            <FileLine path="frontend/src/app/teams/[abbreviation]/page.tsx" desc="(Legacy) Team detail" />
          </div>
        </div>

        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">⚙️ Key Libraries</h3>
          <div className="text-xs space-y-0.5 text-gray-400">
            <FileLine path="frontend/src/lib/api.ts" desc="API client: fetch wrappers, auth headers, typed responses" />
            <FileLine path="frontend/src/lib/team_logos.ts" desc="Team logo URL builder" />
          </div>

          <h3 className="text-sm font-semibold text-earl-400 mb-2 mt-4">🎨 Styling</h3>
          <div className="text-xs space-y-0.5 text-gray-400">
            <p>• Tailwind CSS with custom <code className="text-earl-400">earl-*</code> color palette</p>
            <p>• Dark theme (<code className="text-gray-400">bg-[#0a0a0f]</code>)</p>
            <p>• Team logos: 32+ PNG files at <code className="text-earl-400">frontend/public/logos/{'{ABBR}'}.png</code></p>
            <p>• Referenced as <code className="text-earl-400">/logos/{'{ABBR}'}.png</code></p>
          </div>

          <h3 className="text-sm font-semibold text-earl-400 mb-2 mt-4">📐 Sports Layout Pattern</h3>
          <div className="text-xs text-gray-400 space-y-1">
            <p>
              Pages under <code className="text-earl-400">(sports)/[sport]/</code> use a dynamic route where
              the <code className="text-earl-300">sport</code> param is "nfl", "nba", or "mlb". Each page adjusts
              its data fetching based on the sport parameter.
            </p>
            <p className="mt-1">
              On the game detail page (<code className="text-earl-400">{'[sport]/games/[id]'}</code>):</p>
            <ul className="ml-4 space-y-0.5 list-disc">
              <li>If <code className="text-earl-300">sport === "nfl"</code> → NFLBoxScore + NFLPickCard</li>
              <li>If <code className="text-earl-300">sport === "mlb"</code> → MLBClassicPage (inline component)</li>
              <li>NBA falls through to a "not found" state (no NBA boxscore yet)</li>
            </ul>
          </div>
        </div>
      </div>

      <ArchitectureDiagram />
    </div>
  );
}

function FileLine({ path, desc }: { path: string; desc: string }) {
  return (
    <p>
      <code className="text-earl-400">{path.replace("frontend/", "").replace("src/app/", "")}</code>
      <span className="text-gray-600"> — </span>
      <span>{desc}</span>
    </p>
  );
}

function ArchitectureDiagram() {
  return (
    <div className="bg-white/5 border border-white/10 rounded-xl p-5">
      <h3 className="text-sm font-semibold text-earl-400 mb-3">📐 Request Flow Architecture</h3>
      <div className="bg-black/40 rounded-lg p-4 text-xs font-mono text-gray-300 leading-relaxed">
        <pre>{`┌─────────────────────────────────────────────────────────────────┐
│                      User's Browser                              │
│  Next.js (port 3000)   React / Tailwind CSS                      │
│  frontend/src/app/(sports)/[sport]/games/[id]/page.tsx           │
└─────────────────┬───────────────────────────────────────────────┘
                  │ fetch("/api/mlb/games/&#123;id&#125;/boxscore")
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                  FastAPI Backend (port 8001)                     │
│  router: mlb_stats.py :: mlb_game_boxscore()                    │
│                                                                 │
│  1. Query mlb.games table (Postgres)                            │
│  2. Proxy MLB Stats API (statsapi.mlb.com) for live data        │
│  3. Sync DB status/scores from live feed                        │
│  4. Query mlb.betting_lines_consolidated for market lines       │
│  5. Query mlb.game_predictions for cached pick card             │
│  6. If no cache + game scheduled: MLBHandicapper inline         │
│  7. Query MLBSplitAnalyzer for betting splits                   │
└─────────────────┬───────────────────────────────────────────────┘
                  │ SQLAlchemy queries
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PostgreSQL 16 + pgvector                    │
│  Database: earl_knows_football                                  │
│  Schemas: public, nfl, nba, mlb                                 │
│  Key tables: mlb.games, mlb.game_predictions,                  │
│              mlb.betting_lines_consolidated                     │
│  Vector: mlb.article_embeddings (nomic-embed-text, 768d)       │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     XGBoost Models (on disk)                     │
│  /app/data/                                                     │
│  ├── mlb_margin_model_prod.pkl       (ATS - Run Diff)           │
│  ├── mlb_ou_model_prod.pkl           (O/U - Total)              │
│  ├── mlb_ml_model_prod.pkl           (ML - Win Prob)            │
│  ├── mlb_confidence_calibration.json (Confidence curves)        │
│  └── backtest results JSON files                                │
└─────────────────────────────────────────────────────────────────┘`}</pre>
      </div>
    </div>
  );
}

function AdminPanel() {
  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-400 leading-relaxed">
        The admin panel lives at <code className="text-earl-400">/admin</code> and requires a user with
        <code className="text-earl-400">is_admin = true</code>. Auth is JWT-based (stored in localStorage
        as <code className="text-earl-400">earl_token</code>).
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">📋 Admin Pages</h3>
          <div className="text-xs space-y-0.5 text-gray-400">
            <p><code className="text-earl-400">/admin</code> — Dashboard (stats cards: users, revenue, subs)</p>
            <p><code className="text-earl-400">/admin/users</code> — User management (list, search, edit, delete)</p>
            <p><code className="text-earl-400">/admin/subscriptions</code> — Subscription list + management</p>
            <p><code className="text-earl-400">/admin/plans</code> — Plan CRUD (create, edit, delete)</p>
            <p><code className="text-earl-400">/admin/models</code> — Model detail (features, backtest results, confidence)</p>
            <p><code className="text-earl-400">/admin/articles</code> — Article browser + stats by sport</p>
            <p><code className="text-earl-400">/admin/articles/rss</code> — RSS feed configuration explorer</p>
            <p><code className="text-earl-400">/admin/predictions</code> — Prediction stats by year + confidence breakdown</p>
            <p><code className="text-earl-400">/admin/tasks</code> — Task scheduler management</p>
            <p><strong className="text-yellow-400">/admin/structure</strong> — This page 🏗️</p>
          </div>
        </div>

        <div className="bg-white/5 border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-earl-400 mb-2">🔌 Backend Admin Endpoints</h3>
          <div className="text-xs space-y-0.5 text-gray-400">
            <p><code className="text-earl-400">GET /api/admin/stats</code> — Dashboard stats</p>
            <p><code className="text-earl-400">GET/POST/PATCH/DELETE /api/admin/users</code> — User CRUD</p>
            <p><code className="text-earl-400">GET/POST/PATCH/DELETE /api/admin/plans</code> — Plans CRUD</p>
            <p><code className="text-earl-400">GET/PATCH /api/admin/subscriptions</code> — Subscription management</p>
            <p><code className="text-earl-400">GET /api/admin/payments</code> — Payment history</p>
            <p><code className="text-earl-400">GET /api/admin/models/{'{sport}'}</code> — Model detail</p>
            <p><code className="text-earl-400">GET /api/admin/articles/{'{sport}'}/stats</code> — Article stats by sport</p>
            <p><code className="text-earl-400">GET /api/admin/articles/{'{sport}'}</code> — Article list</p>
            <p><code className="text-earl-400">GET /api/admin/articles/{'{sport}'}/rss-feeds</code> — RSS feed config</p>
            <p><code className="text-earl-400">GET /api/admin/prediction-stats/{'{sport}'}</code> — Per-year prediction results</p>
            <p><code className="text-earl-400">GET/POST /api/admin/tasks</code> — Task management</p>
          </div>
        </div>
      </div>

      <div className="bg-green-900/10 border border-green-800/20 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-green-400 mb-2">🔐 Admin Auth Pattern</h3>
        <div className="text-xs text-gray-400 space-y-1">
          <p>1. User logs in → JWT stored in <code className="text-earl-400">localStorage.earl_token</code></p>
          <p>2. Every admin page checks <code className="text-earl-400">/auth/me</code> with <code className="text-earl-400">Authorization: Bearer &#123;token&#125;</code></p>
          <p>3. Backend <code className="text-earl-400">get_admin_user()</code> dependency decodes JWT, checks <code className="text-earl-400">user.is_admin</code></p>
          <p>4. Non-admin users are redirected to <code className="text-earl-400">/login</code></p>
          <p className="text-yellow-400 mt-2">⚠️ CRITICAL: The test user is <code className="text-yellow-300">test@earl.com</code> / <code className="text-yellow-300">test1234</code>. Never modify the rich@ljart.com account.</p>
        </div>
      </div>
    </div>
  );
}

function Infrastructure() {
  return (
    <div className="space-y-6">
      <div className="bg-white/5 border border-white/10 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">🐳 Docker Services</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-white/10">
                <th className="text-left py-1.5 px-3 text-gray-400 font-medium">Service</th>
                <th className="text-left py-1.5 px-3 text-gray-400 font-medium">Image</th>
                <th className="text-left py-1.5 px-3 text-gray-400 font-medium">Port</th>
                <th className="text-left py-1.5 px-3 text-gray-400 font-medium">Notes</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["Frontend", "earl-knows-football-frontend", "3000", "Next.js SSR"],
                ["API", "earl-knows-football-api", "8001", "FastAPI (auto-reload in dev)"],
                ["Database", "postgres:16", "5432", "With pgvector extension"],
                ["Embedder *", "earl-embed-pgvector", "—", "Standalone docker run"],
                ["Ollama *", "systemd service", "11434", "nomic-embed-text, llama3.2"],
              ].map(([name, image, port, notes]) => (
                <tr key={name} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="py-1.5 px-3 font-medium text-gray-300">{name}</td>
                  <td className="py-1.5 px-3 font-mono text-earl-400">{image}</td>
                  <td className="py-1.5 px-3 text-gray-400">{port}</td>
                  <td className="py-1.5 px-3 text-gray-400">{notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-gray-500 mt-2">
          * Standalone services not managed by docker compose. Embedder survives rebuilds.
        </p>
      </div>

      <div className="bg-white/5 border border-white/10 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">📜 Key Shell Scripts</h3>
        <div className="grid grid-cols-2 gap-3 text-xs">
          {[
            ["docker compose up -d", "Start all compose services"],
            ["docker compose build frontend", "Rebuild frontend (run after code changes)"],
            ["docker compose build", "Rebuild everything"],
            ["docker compose logs -f", "Tail all logs"],
            ['docker logs earl-knows-football-frontend-1', "Frontend logs"],
            ['docker logs earl-knows-football-api-1', "API logs"],
            ["./backfill.sh start", "NFL article backfill"],
            ["./national_scraper.sh start", "National NFL archives"],
            ["./nba_scraper.sh start", "NBA article backfill"],
            ["./mlb_scraper.sh start deep", "MLB deep backfill"],
            ["./embed_all.sh start", "Start embedding pipeline"],
            ["./embed_all.sh logs", "View embedder logs"],
            ["./embed_all.sh status", "Check embedder status"],
            ["fuser -k 3000/tcp", "Kill process on port 3000 (if Docker blocked)"],
          ].map(([cmd, desc]) => (
            <div key={cmd} className="bg-white/5 rounded-lg p-2 border border-white/10">
              <code className="text-earl-400">{cmd}</code>
              <div className="text-gray-500 mt-0.5">{desc}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white/5 border border-white/10 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-earl-400 mb-3">📦 Database Connection</h3>
        <div className="text-xs text-gray-400 space-y-1">
          <p><strong className="text-gray-300">Host:</strong> localhost:5432</p>
          <p><strong className="text-gray-300">Database:</strong> earl_knows_football</p>
          <p><strong className="text-gray-300">User:</strong> earl</p>
          <p><strong className="text-gray-300">Password:</strong> earl_dev_pass</p>
          <p><strong className="text-gray-300">Async DSN:</strong> <code className="text-earl-400">postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football</code></p>
          <p><strong className="text-gray-300">Sync DSN:</strong> <code className="text-earl-400">postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football</code></p>
        </div>
      </div>
    </div>
  );
}

// ── UI Helpers ─────────────────────────────────────────────────────

function Step({ num, label, children }: { num: number; label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 mb-2">
      <div className="flex-shrink-0 w-6 h-6 bg-earl-600 rounded-full flex items-center justify-center text-xs font-bold text-white">
        {num}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-white mb-1">{label}</div>
        <div className="text-xs text-gray-400">{children}</div>
      </div>
    </div>
  );
}

function Arrow() {
  return (
    <div className="flex justify-center py-1 text-gray-600">
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
      </svg>
    </div>
  );
}
