"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

interface PlayerProfile {
  id: number;
  name: string;
  position: string;
  team_abbr: string | null;
  team_name: string | null;
  college: string | null;
  height: number | null;
  weight: number | null;
  birth_date: string | null;
  years_exp: number | null;
  status: string | null;
  jersey_number: number | null;
  headshot_url: string | null;
  bats: string | null;
  throws: string | null;
  draft: { year: number; round: number; pick: number; team: string } | null;
  depth_chart: { position: string; slot: number; status: string } | null;
  career_batting: {
    games: number; at_bats: number; hits: number; home_runs: number;
    rbi: number; runs: number; stolen_bases: number; walks: number;
  } | null;
  career_pitching: {
    games: number; wins: number; losses: number; saves: number;
    era: number; whip: number; innings_pitched: number;
    strikeouts_pitching: number; walks: number;
  } | null;
  stats: {
    games: number; first_year: number; last_year: number;
    pass_yds: number; pass_tds: number; pass_int: number;
    rush_yds: number; rush_tds: number;
    rec: number; rec_yds: number; rec_tds: number;
    fantasy_ppr: number;
    // NFL/NBA/MLB generic
    home_runs?: number; rbi?: number; hits?: number; runs?: number;
    stolen_bases?: number; walks?: number;
    // NBA fields (optional)
    points?: number; rebounds?: number; assists?: number;
    steals?: number; blocks?: number;
  } | null;
  recent_seasons: {
    year: number; games: number;
    pass_yds: number; pass_tds: number; pass_int: number;
    rush_yds: number; rush_tds: number;
    rec: number; rec_yds: number; rec_tds: number;
    fantasy_ppr: number;
    // MLB batting
    avg?: number; obp?: number; slg?: number; ops?: number;
    home_runs?: number; runs_batted_in?: number; stolen_bases?: number;
    hits?: number; at_bats?: number; walks?: number; strikeouts?: number;
    // MLB pitching
    era?: number; whip?: number; wins?: number; losses?: number;
    saves?: number; innings_pitched?: number;
    games_started?: number; strikeouts_pitching?: number;
    // NBA fields (optional)
    points?: number; points_per_game?: number;
    rebounds_per_game?: number; assists_per_game?: number;
    steals?: number; blocks?: number;
    field_goal_pct?: number; three_point_pct?: number; free_throw_pct?: number;
  }[];
  injuries: { week: number; year: number; injury: string; status: string }[];
  transactions: { date: string; type: string; details: string }[];
  writeup: string | null;
}

function posLabel(pos: string, sport?: string): string {
  const labels: Record<string, string> = {
    // NFL
    QB: "Quarterback", RB: "Running Back", WR: "Wide Receiver",
    TE: "Tight End", K: "Kicker", DST: "Defense",
    // NBA
    PG: "Point Guard", SG: "Shooting Guard", SF: "Small Forward",
    PF: "Power Forward", G: "Guard", F: "Forward",
    // MLB
    P: "Pitcher", SP: "Starting Pitcher", RP: "Relief Pitcher",
    C: "Catcher",
    "1B": "First Base", "2B": "Second Base", "3B": "Third Base",
    SS: "Shortstop",
    LF: "Left Field", CF: "Center Field", RF: "Right Field",
    OF: "Outfielder", DH: "Designated Hitter", UT: "Utility",
  };
  // 'C' during NBA season context → Center instead of Catcher
  if (pos === "C" && sport === "nba") return "Center";
  return labels[pos] || pos;
}

function statCell(label: string, value: string | number, highlight = false) {
  return (
    <div className="bg-white/5 rounded-lg p-3 text-center">
      <div className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-lg font-bold ${highlight ? "text-earl-400" : "text-white"}`}>{value}</div>
    </div>
  );
}

export default function PlayerProfilePage() {
  const params = useParams<{ sport: string; id: string }>();
  const sport = params?.sport || "nfl";
  const playerId = params?.id || "";
  const [profile, setProfile] = useState<PlayerProfile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!playerId) return;
    fetch(`/api/${sport}/players/${playerId}/profile`)
      .then((r) => r.json())
      .then(setProfile)
      .finally(() => setLoading(false));
  }, [playerId]);

  if (loading) return <div className="text-center py-12 text-gray-500">Loading...</div>;
  if (!profile) return <div className="text-center py-12 text-gray-500">Player not found</div>;

  const p = profile;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="rounded-2xl p-6 md:p-8 border border-white/10 bg-gradient-to-br from-white/5 to-transparent">
        <div className="flex items-center gap-4">
          <div className="w-16 h-16 rounded-xl bg-white/10 flex items-center justify-center text-2xl font-bold text-earl-400 shrink-0">
            {p.jersey_number || p.position}
          </div>
          <div className="flex-1">
            <h1 className="font-display text-3xl md:text-4xl font-bold">{p.name}</h1>
            <p className="text-sm text-gray-400 mt-1">
              {posLabel(p.position, sport)} · {p.team_name || "Free Agent"}
              {p.jersey_number ? ` · #${p.jersey_number}` : ""}
              {p.status && p.status !== "Active" ? ` · ${p.status}` : ""}
            </p>
          </div>
          <Link href={`/${sport}/players`} className="text-xs text-gray-500 hover:text-gray-300 transition">← Back to Players</Link>
        </div>

        {/* Quick stats row */}
        <div className="grid grid-cols-4 md:grid-cols-6 gap-3 mt-6">
          {p.college && statCell("College", p.college)}
          {p.height && statCell("Height", `${Math.floor(p.height / 12)}'${p.height % 12}"`)}
          {p.weight && statCell("Weight", `${p.weight} lbs`)}
          {p.years_exp !== null && statCell("Experience", `${p.years_exp} yrs`)}
          {p.birth_date && statCell("Born", p.birth_date)}
          {p.draft && statCell("Draft", `R${p.draft.round} P${p.draft.pick} (${p.draft.year})`, true)}
        </div>

        {p.depth_chart && (
          <div className="mt-3 text-xs text-gray-500">
            Depth Chart: {p.depth_chart.position} #{p.depth_chart.slot} ({p.depth_chart.status})
          </div>
        )}
      </div>

      {/* Write-up */}
      {p.writeup && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Profile</h2>
          <div className="prose prose-sm prose-invert max-w-none">
            {p.writeup.split("\n").map((line, i) => {
              if (line.startsWith("# ")) return <h1 key={i} className="text-lg font-bold mt-4 mb-2">{line.slice(2)}</h1>;
              if (line.startsWith("## ")) return <h2 key={i} className="text-base font-semibold mt-4 mb-2 text-earl-400">{line.slice(3)}</h2>;
              if (line.startsWith("- **")) return <p key={i} className="text-sm text-gray-300 ml-2">• {line.slice(4).replace(/\*\*/g, "")}</p>;
              if (line.startsWith("-")) return <p key={i} className="text-sm text-gray-300 ml-2">• {line.slice(1).trim()}</p>;
              if (line.startsWith("  ")) return <p key={i} className="text-sm text-gray-300 ml-4">{line.trim()}</p>;
              if (!line.trim()) return <div key={i} className="h-2" />;
              return <p key={i} className="text-sm text-gray-300">{line}</p>;
            })}
          </div>
        </div>
      )}

      {/* Career Stats — MLB */}
      {sport === "mlb" && p.stats && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Career Stats</h2>
          {p.position === "P" ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {statCell("Games", p.career_pitching?.games ?? "-")}
              {statCell("W-L", (p.career_pitching?.wins ?? 0) + "-" + (p.career_pitching?.losses ?? 0))}
              {statCell("Saves", p.career_pitching?.saves ?? "-")}
              {statCell("ERA", p.career_pitching?.era?.toFixed(2) ?? "-", true)}
              {statCell("WHIP", p.career_pitching?.whip?.toFixed(2) ?? "-")}
              {statCell("IP", p.career_pitching?.innings_pitched?.toFixed(1) ?? "-")}
              {statCell("K", p.career_pitching?.strikeouts_pitching ?? "-")}
              {statCell("BB", p.career_pitching?.walks ?? "-")}
            </div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {statCell("Games", p.stats.games ?? "-")}
              {statCell("AVG", p.career_batting?.hits && p.career_batting?.at_bats ? (p.career_batting.hits / p.career_batting.at_bats).toFixed(3).slice(1) : "-", true)}
              {statCell("HR", p.stats.home_runs ?? "-")}
              {statCell("RBI", p.stats.rbi ?? "-")}
              {statCell("H", p.stats.hits ?? "-")}
              {statCell("R", p.stats.runs ?? "-")}
              {statCell("SB", p.stats.stolen_bases ?? "-")}
              {statCell("BB", p.stats.walks ?? "-")}
            </div>
          )}
        </div>
      )}

      {/* Career Stats — NFL */}
      {sport === "nfl" && p.stats && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Career Stats ({p.stats.first_year}–{p.stats.last_year})</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {statCell("Games", p.stats.games)}
            {statCell("Fantasy PPR", p.stats.fantasy_ppr.toLocaleString(), true)}
            {p.position === "QB" && (
              <>{statCell("Pass Yds", p.stats.pass_yds.toLocaleString())}{statCell("Pass TD", p.stats.pass_tds)}{statCell("INT", p.stats.pass_int)}{statCell("Rush Yds", p.stats.rush_yds.toLocaleString())}{statCell("Rush TD", p.stats.rush_tds)}</>
            )}
            {p.position === "RB" && (
              <>{statCell("Rush Yds", p.stats.rush_yds.toLocaleString())}{statCell("Rush TD", p.stats.rush_tds)}{statCell("Rec", p.stats.rec)}{statCell("Rec Yds", p.stats.rec_yds.toLocaleString())}{statCell("Rec TD", p.stats.rec_tds)}</>
            )}
            {(p.position === "WR" || p.position === "TE") && (
              <>{statCell("Rec", p.stats.rec)}{statCell("Rec Yds", p.stats.rec_yds.toLocaleString())}{statCell("Rec TD", p.stats.rec_tds)}{statCell("Rush Yds", p.stats.rush_yds.toLocaleString())}{statCell("Rush TD", p.stats.rush_tds)}</>
            )}
          </div>
        </div>
      )}

      {/* Career Stats — NBA */}
      {sport === "nba" && p.stats && (
        <>
          <h2 className="font-display text-xl font-bold mb-4">Career Stats ({p.stats.first_year}–{p.stats.last_year})</h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
            {statCell("Games", p.stats.games)}
            {statCell("Points", p.stats.points?.toLocaleString() ?? "-")}
            {statCell("Rebounds", p.stats.rebounds?.toLocaleString() ?? "-")}
            {statCell("Assists", p.stats.assists?.toLocaleString() ?? "-")}
            {statCell("Steals", p.stats.steals?.toLocaleString() ?? "-")}
            {statCell("Blocks", p.stats.blocks?.toLocaleString() ?? "-")}
          </div>
        </>
      )}

      {/* Recent Seasons — NFL */}
      {sport === "nfl" && p.recent_seasons.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Recent Seasons</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 uppercase text-[10px] tracking-wider border-b border-white/10">
                  <th className="px-3 py-2 text-left">Year</th>
                  <th className="px-3 py-2 text-center">G</th>
                  {p.position === "QB" && <><th className="px-3 py-2 text-right">Pass Yds</th><th className="px-3 py-2 text-right">TD</th><th className="px-3 py-2 text-right">INT</th></>}
                  {(p.position === "QB" || p.position === "RB") && <><th className="px-3 py-2 text-right">Rush Yds</th><th className="px-3 py-2 text-right">TD</th></>}
                  {(p.position === "RB" || p.position === "WR" || p.position === "TE") && <><th className="px-3 py-2 text-right">Rec</th><th className="px-3 py-2 text-right">Yds</th><th className="px-3 py-2 text-right">TD</th></>}
                  <th className="px-3 py-2 text-right text-earl-400">PPR</th>
                </tr>
              </thead>
              <tbody>
                {p.recent_seasons.map((s) => (
                  <tr key={s.year} className="border-b border-white/5 hover:bg-white/5">
                    <td className="px-3 py-2 font-semibold">{s.year}</td>
                    <td className="px-3 py-2 text-center">{s.games}</td>
                    {p.position === "QB" && <><td className="px-3 py-2 text-right">{s.pass_yds.toLocaleString()}</td><td className="px-3 py-2 text-right">{s.pass_tds}</td><td className="px-3 py-2 text-right">{s.pass_int ?? 0}</td></>}
                    {(p.position === "QB" || p.position === "RB") && <><td className="px-3 py-2 text-right">{s.rush_yds.toLocaleString()}</td><td className="px-3 py-2 text-right">{s.rush_tds}</td></>}
                    {(p.position === "RB" || p.position === "WR" || p.position === "TE") && <><td className="px-3 py-2 text-right">{s.rec}</td><td className="px-3 py-2 text-right">{s.rec_yds.toLocaleString()}</td><td className="px-3 py-2 text-right">{s.rec_tds}</td></>}
                    <td className="px-3 py-2 text-right font-semibold text-earl-400">{s.fantasy_ppr.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Recent Seasons — MLB */}
      {sport === "mlb" && p.recent_seasons.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Season Stats</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 uppercase text-[10px] tracking-wider border-b border-white/10">
                  <th className="px-3 py-2 text-left">Year</th>
                  <th className="px-3 py-2 text-center">G</th>
                  {p.position === "P" ? (
                    <>
                      <th className="px-3 py-2 text-right">W-L</th>
                      <th className="px-3 py-2 text-right">SV</th>
                      <th className="px-3 py-2 text-right">ERA</th>
                      <th className="px-3 py-2 text-right">WHIP</th>
                      <th className="px-3 py-2 text-right">IP</th>
                      <th className="px-3 py-2 text-right">K</th>
                      <th className="px-3 py-2 text-right">BB</th>
                    </>
                  ) : (
                    <>
                      <th className="px-3 py-2 text-right">AVG</th>
                      <th className="px-3 py-2 text-right">OPS</th>
                      <th className="px-3 py-2 text-right">HR</th>
                      <th className="px-3 py-2 text-right">RBI</th>
                      <th className="px-3 py-2 text-right">H</th>
                      <th className="px-3 py-2 text-right">R</th>
                      <th className="px-3 py-2 text-right">SB</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {p.recent_seasons.map((s: any) => (
                  <tr key={s.year} className="border-b border-white/5 hover:bg-white/5">
                    <td className="px-3 py-2 font-semibold">{s.year}</td>
                    <td className="px-3 py-2 text-center">{s.games}</td>
                    {p.position === "P" ? (
                      <>
                        <td className="px-3 py-2 text-right">{s.wins != null ? `${s.wins}-${s.losses ?? 0}` : "-"}</td>
                        <td className="px-3 py-2 text-right">{s.saves ?? "-"}</td>
                        <td className="px-3 py-2 text-right text-earl-400">{s.era != null ? s.era.toFixed(2) : "-"}</td>
                        <td className="px-3 py-2 text-right">{s.whip != null ? s.whip.toFixed(2) : "-"}</td>
                        <td className="px-3 py-2 text-right">{s.innings_pitched != null ? s.innings_pitched : "-"}</td>
                        <td className="px-3 py-2 text-right">{s.strikeouts_pitching ?? "-"}</td>
                        <td className="px-3 py-2 text-right">{s.walks ?? "-"}</td>
                      </>
                    ) : (
                      <>
                        <td className="px-3 py-2 text-right">{s.avg != null ? s.avg.toFixed(3).slice(1) : "-"}</td>
                        <td className="px-3 py-2 text-right">{s.ops != null ? s.ops.toFixed(3) : "-"}</td>
                        <td className="px-3 py-2 text-right text-earl-400">{s.home_runs ?? "-"}</td>
                        <td className="px-3 py-2 text-right">{s.runs_batted_in ?? "-"}</td>
                        <td className="px-3 py-2 text-right">{s.hits ?? "-"}</td>
                        <td className="px-3 py-2 text-right">-</td>
                        <td className="px-3 py-2 text-right">{s.stolen_bases ?? "-"}</td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Recent Seasons — NBA */}
      {sport === "nba" && p.recent_seasons.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Recent Seasons</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 uppercase text-[10px] tracking-wider border-b border-white/10">
                  <th className="px-3 py-2 text-left">Year</th>
                  <th className="px-3 py-2 text-center">G</th>
                  <th className="px-3 py-2 text-center">GS</th>
                  <th className="px-3 py-2 text-right">PPG</th>
                  <th className="px-3 py-2 text-right">RPG</th>
                  <th className="px-3 py-2 text-right">APG</th>
                  <th className="px-3 py-2 text-right">SPG</th>
                  <th className="px-3 py-2 text-right">BPG</th>
                  <th className="px-3 py-2 text-right">FG%</th>
                  <th className="px-3 py-2 text-right">3P%</th>
                  <th className="px-3 py-2 text-right">FT%</th>
                </tr>
              </thead>
              <tbody>
                {p.recent_seasons.map((s) => (
                  <tr key={s.year} className="border-b border-white/5 hover:bg-white/5">
                    <td className="px-3 py-2 font-semibold">{s.year}</td>
                    <td className="px-3 py-2 text-center">{s.games}</td>
                    <td className="px-3 py-2 text-center">{s.games_started}</td>
                    <td className="px-3 py-2 text-right">{s.points_per_game != null ? s.points_per_game.toFixed(1) : "-"}</td>
                    <td className="px-3 py-2 text-right">{s.rebounds_per_game != null ? s.rebounds_per_game.toFixed(1) : "-"}</td>
                    <td className="px-3 py-2 text-right">{s.assists_per_game != null ? s.assists_per_game.toFixed(1) : "-"}</td>
                    <td className="px-3 py-2 text-right">{s.steals}</td>
                    <td className="px-3 py-2 text-right">{s.blocks}</td>
                    <td className="px-3 py-2 text-right">{s.field_goal_pct != null ? (s.field_goal_pct * 100).toFixed(1) + "%" : "-"}</td>
                    <td className="px-3 py-2 text-right">{s.three_point_pct != null ? (s.three_point_pct * 100).toFixed(1) + "%" : "-"}</td>
                    <td className="px-3 py-2 text-right">{s.free_throw_pct != null ? (s.free_throw_pct * 100).toFixed(1) + "%" : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Injuries */}
      {p.injuries.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Injury History</h2>
          <div className="flex flex-wrap gap-2">
            {p.injuries.map((inj, i) => (
              <div key={i} className="bg-red-900/20 border border-red-800/30 rounded-lg px-3 py-2 text-xs">
                <span className="text-red-400 font-semibold">W{inj.week} ({inj.year})</span>: {inj.injury} — {inj.status}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Transactions */}
      {p.transactions.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 md:p-6 bg-white/5">
          <h2 className="font-display text-xl font-bold mb-4">Transaction History</h2>
          <div className="space-y-2">
            {p.transactions.map((t, i) => (
              <div key={i} className="bg-white/5 rounded-lg px-4 py-2 text-sm flex items-center gap-3">
                <span className="text-gray-500 text-xs shrink-0">{t.date}</span>
                <span className="text-earl-400 font-semibold text-xs uppercase shrink-0">{t.type}</span>
                <span className="text-gray-300">{t.details}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
