"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X, RefreshCw, AlertTriangle } from "lucide-react";
import TeamLogo from "./TeamLogo";
import { MatchupTrendsGrid, MatchupComparisonTable } from "./MatchupView";
import { api, MatchupResponse } from "../lib/api";

type Sport = "nfl" | "nba" | "mlb";

const SPORT_META: Record<Sport, { label: string; color: string }> = {
  nfl: { label: "NFL", color: "text-emerald-400" },
  nba: { label: "NBA", color: "text-orange-400" },
  mlb: { label: "MLB", color: "text-red-400" },
};

export default function MatchupModal({
  sport,
  gameId,
  homeAbbr,
  awayAbbr,
  homeName,
  awayName,
  open,
  onClose,
}: {
  sport: Sport;
  gameId?: number;
  homeAbbr: string;
  awayAbbr: string;
  homeName?: string;
  awayName?: string;
  open: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<MatchupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!open) return;
    setError(null);
    setData(null);
    setLoading(true);
    try {
      const res = await api.matchup.get({
        sport,
        gameId,
        home: homeName ?? homeAbbr,
        away: awayName ?? awayAbbr,
      });
      setData(res ?? null);
    } catch (e: any) {
      setError(e?.message ?? "Couldn't load matchup");
    } finally {
      setLoading(false);
    }
  }, [open, sport, gameId, homeName, awayName, homeAbbr, awayAbbr]);

  useEffect(() => {
    load();
  }, [load]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Swallow events outside the modal so the game card beneath can't navigate.
  useEffect(() => {
    if (!open) return;
    const swallow = (e: Event) => {
      const t = e.target as HTMLElement | null;
      if (t && t.closest("[data-matchup-modal]")) return;
      e.stopPropagation();
      if (e.type === "mousedown") e.preventDefault();
    };
    for (const evt of ["pointerdown", "mousedown", "click"]) {
      document.addEventListener(evt, swallow, true);
    }
    return () => {
      for (const evt of ["pointerdown", "mousedown", "click"]) {
        document.removeEventListener(evt, swallow, true);
      }
    };
  }, [open]);

  if (!open) return null;

  const meta = SPORT_META[sport] ?? SPORT_META.nba;
  const home = data?.teams?.home;
  const away = data?.teams?.away;

  const panel = (
    <div className="fixed inset-x-0 bottom-0 sm:inset-0 sm:flex sm:items-center sm:justify-center sm:p-6 z-[95] pointer-events-none">
      <div
        data-matchup-modal
        className="pointer-events-auto relative w-full max-w-3xl max-h-[90vh] overflow-hidden rounded-t-2xl sm:rounded-2xl border border-gray-800 bg-gray-950 shadow-2xl"
      >
        {/* Header */}
        <div className="relative border-b border-gray-800 bg-gradient-to-br from-gray-900 to-gray-950 px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            className="absolute right-4 top-4 z-10 rounded-lg p-1.5 text-gray-400 transition hover:bg-gray-800 hover:text-white"
            aria-label="Close matchup"
          >
            <X className="h-5 w-5" />
          </button>

          <div className="flex items-center justify-between gap-3 pr-8">
            <HeaderTeam abbr={home?.abbr ?? homeAbbr} name={home?.name ?? homeName} sport={sport} align="left" />
            <div className="flex flex-col items-center px-2">
              <span className={`flex h-11 w-11 items-center justify-center rounded-full border border-gray-700 text-sm font-black uppercase ${meta.color}`}>
                vs
              </span>
              <span className="mt-1 text-[10px] uppercase tracking-wider text-gray-500">{meta.label}</span>
            </div>
            <HeaderTeam abbr={away?.abbr ?? awayAbbr} name={away?.name ?? awayName} sport={sport} align="right" />
          </div>

          {data?.game_date && (
            <p className="mt-2 text-center text-xs text-gray-500">{formatDate(data.game_date)}</p>
          )}
        </div>

        {/* Body */}
        <div className="max-h-[calc(90vh-110px)] overflow-y-auto px-5 py-5">
          {loading && (
            <div className="flex flex-col items-center justify-center py-14 text-gray-400">
              <RefreshCw className="mb-3 h-6 w-6 animate-spin text-gray-500" />
              <p className="text-sm">Loading matchup data...</p>
            </div>
          )}

          {error && !loading && (
            <div className="flex items-center gap-2 rounded-xl border border-red-900/50 bg-red-950/30 px-4 py-3 text-sm text-red-300">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {error}
            </div>
          )}

          {!loading && !error && data && home && away && (
            <div className="space-y-6">
              <section>
                <SectionTitle>How they&apos;re playing</SectionTitle>
                <MatchupTrendsGrid teams={data.teams} sport={sport} />
              </section>

              {data.comparison && (
                <section>
                  <SectionTitle>Head-to-head numbers</SectionTitle>
                  <MatchupComparisonTable
                    compare={data.comparison.compare}
                    teamA={data.comparison.team_a}
                    teamB={data.comparison.team_b}
                    homeAbbr={home.abbr}
                    awayAbbr={away.abbr}
                  />
                </section>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return createPortal(
    <>
      <div
        className="fixed inset-0 z-[90] bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden
      />
      {panel}
    </>,
    document.body
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className="text-xs font-bold uppercase tracking-widest text-gray-400">{children}</span>
      <span className="h-px flex-1 bg-gray-800" />
    </div>
  );
}

function HeaderTeam({
  abbr,
  name,
  sport,
  align,
}: {
  abbr: string;
  name?: string;
  sport: Sport;
  align: "left" | "right";
}) {
  return (
    <div className={`flex min-w-0 items-center gap-2 ${align === "left" ? "flex-row text-left" : "flex-row-reverse text-right"}`}>
      <TeamLogo abbr={abbr} sport={sport} name={name} size={36} />
      <div className="min-w-0">
        <div className="truncate text-sm font-bold text-white sm:text-base">{abbr}</div>
        <div className="hidden max-w-[140px] truncate text-[11px] text-gray-500 sm:block">{name}</div>
      </div>
    </div>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
