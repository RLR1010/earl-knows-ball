"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";
import { X, Info, ChevronDown, ChevronRight } from "lucide-react";

/* ─── Types ──────────────────────────────────────────────── */

interface FeatureDef {
  slug: string;
  display_name: string;
  description: string;
}

interface StatsJson {
  home_stats: Record<string, unknown> | null;
  away_stats: Record<string, unknown> | null;
  situational: Record<string, unknown> | null;
  splits: Record<string, unknown> | null;
  features: Record<string, unknown> | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  homeTeam: string;
  awayTeam: string;
  statsJson: StatsJson;
}

/* ─── Loading feature definitions (cached at module level) ── */

let featureDefsCache: FeatureDef[] | null = null;
let featureDefsPromise: Promise<FeatureDef[]> | null = null;

async function loadFeatureDefs(): Promise<FeatureDef[]> {
  if (featureDefsCache) return featureDefsCache;
  if (featureDefsPromise) return featureDefsPromise;
  featureDefsPromise = (async () => {
    try {
      const res = await fetch("/api/mlb/feature-definitions");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: FeatureDef[] = await res.json();
      featureDefsCache = data;
      return data;
    } catch {
      featureDefsCache = [];
      return [];
    }
  })();
  return featureDefsPromise;
}

function useFeatureDefs() {
  const [defs, setDefs] = useState<FeatureDef[]>(featureDefsCache ?? []);
  useEffect(() => {
    if (featureDefsCache) {
      setDefs(featureDefsCache);
    } else {
      loadFeatureDefs().then(setDefs);
    }
  }, []);
  return defs;
}

/* ─── Popover ───────────────────────────────────────────── */

function PopoverInfo({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!show) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setShow(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [show]);

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        type="button"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        onClick={() => setShow((v) => !v)}
        className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-earl-700/50 text-earl-300 hover:bg-earl-600/50 hover:text-earl-100 transition-colors ml-1 flex-shrink-0"
      >
        <Info className="w-3 h-3" />
      </button>
      {show && (
        <div className="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2 w-72 p-3 rounded-lg bg-gray-800 border border-gray-700 shadow-xl text-xs text-gray-200 leading-relaxed">
          {text}
          <div className="absolute top-full left-1/2 -translate-x-1/2 w-3 h-3 rotate-45 bg-gray-800 border-r border-b border-gray-700 -mt-[5px]" />
        </div>
      )}
    </div>
  );
}

/* ─── Value formatting ──────────────────────────────────── */

function formatVal(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    if (Math.abs(v) > 10) return v.toFixed(1);
    return v.toFixed(2);
  }
  return String(v);
}

/* ─── Recursive stat tree renderer ───────────────────────── */

function NestedObjectSection({
  label,
  entries,
  description,
  depth,
  defaultOpen,
}: {
  label: string;
  entries: [string, unknown][];
  description?: string;
  depth: number;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const paddingLeft = depth * 12;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-2 text-sm text-gray-300 hover:bg-gray-800/50 transition-colors"
        style={{ paddingLeft: `${16 + paddingLeft}px` }}
      >
        <span className="font-medium truncate mr-2">{label}</span>
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
        )}
      </button>
      {open && (
        <div className="divide-y divide-gray-700/30">
          {entries.map(([k, v]) => {
            const displayKey = k
              .replace(/_/g, " ")
              .replace(/\b\w/g, (c) => c.toUpperCase());
            return (
              <StatNode
                key={k}
                label={displayKey}
                value={v}
                description={description}
                depth={depth + 1}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function StatNode({
  label,
  value,
  description,
  depth = 0,
}: {
  label: string;
  value: unknown;
  description?: string;
  depth?: number;
}) {
  if (value === null || value === undefined) return null;

  const paddingLeft = depth * 12;

  if (typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== null && v !== undefined,
    );
    if (entries.length === 0) return null;
    return (
      <NestedObjectSection
        label={label}
        entries={entries}
        description={description}
        depth={depth}
        defaultOpen={depth < 2}
      />
    );
  }

  // Primitive value → leaf row
  return (
    <div
      className="flex items-start justify-between px-4 py-2 text-sm"
      style={{ paddingLeft: `${16 + paddingLeft}px` }}
    >
      <span className="text-gray-300 flex items-center gap-1 truncate mr-2">
        {label}
        {description && <PopoverInfo text={description} />}
      </span>
      <span className="text-gray-100 font-medium text-right whitespace-nowrap flex-shrink-0">
        {formatVal(value)}
      </span>
    </div>
  );
}

/* ─── Section card (collapsible, any depth) ─────────────── */

function SectionCard({
  title,
  data,
  defs,
  defaultOpen = true,
  slugPrefix = "",
}: {
  title: string;
  data: Record<string, unknown> | null | undefined;
  defs: FeatureDef[];
  defaultOpen?: boolean;
  slugPrefix?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const defMap = useRef(new Map<string, FeatureDef>());
  useEffect(() => {
    defMap.current = new Map(defs.map((d) => [d.slug, d]));
  }, [defs]);

  if (!data || Object.keys(data).length === 0) return null;

  const entries = Object.entries(data).filter(
    ([, v]) => v !== null && v !== undefined,
  );
  if (entries.length === 0) return null;

  return (
    <div className="rounded-lg border border-gray-700 overflow-hidden mb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 bg-gray-800/80 hover:bg-gray-700/80 transition-colors text-sm font-semibold text-gray-100"
      >
        <span>{title}</span>
        {open ? (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-400" />
        )}
      </button>
      {open && (
        <div className="divide-y divide-gray-700/30">
          {entries.map(([key, val]) => {
            const slug = slugPrefix ? `${slugPrefix}_${key}` : key;
            const def = defMap.current.get(key) ?? defMap.current.get(slug);
            const displayKey =
              def?.display_name ??
              key
                .replace(/_/g, " ")
                .replace(/\b\w/g, (c) => c.toUpperCase());
            return (
              <StatNode
                key={key}
                label={displayKey}
                value={val}
                description={def?.description}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ─── Tabs ──────────────────────────────────────────────── */

function TeamStatsTab({
  homeStats,
  awayStats,
  defs,
}: {
  homeStats: Record<string, unknown> | null;
  awayStats: Record<string, unknown> | null;
  defs: FeatureDef[];
}) {
  if (!homeStats && !awayStats) {
    return <p className="text-gray-500 text-sm text-center py-6">No team stats available.</p>;
  }
  return (
    <div>
      {awayStats && <SectionCard title="Away Team Stats" data={awayStats} defs={defs} slugPrefix="a" />}
      {homeStats && <SectionCard title="Home Team Stats" data={homeStats} defs={defs} slugPrefix="h" />}
    </div>
  );
}

function SituationalTab({
  data,
  defs,
}: {
  data: Record<string, unknown> | null | undefined;
  defs: FeatureDef[];
}) {
  if (!data) return <p className="text-gray-500 text-sm text-center py-6">No situational data available.</p>;
  return <SectionCard title="Situational Factors" data={data} defs={defs} />;
}

function SplitsTab({
  data,
  defs,
}: {
  data: Record<string, unknown> | null | undefined;
  defs: FeatureDef[];
}) {
  if (!data) return <p className="text-gray-500 text-sm text-center py-6">No splits data available.</p>;

  // Group top-level keys by category
  const homeKeys: Record<string, unknown> = {};
  const awayKeys: Record<string, unknown> = {};
  const otherKeys: Record<string, unknown> = {};

  for (const [key, val] of Object.entries(data)) {
    if (key.startsWith("home_")) homeKeys[key.replace("home_", "")] = val;
    else if (key.startsWith("away_")) awayKeys[key.replace("away_", "")] = val;
    else otherKeys[key] = val;
  }

  return (
    <div>
      {Object.keys(awayKeys).length > 0 && (
        <SectionCard title="Away Splits" data={awayKeys} defs={defs} slugPrefix="away" />
      )}
      {Object.keys(homeKeys).length > 0 && (
        <SectionCard title="Home Splits" data={homeKeys} defs={defs} slugPrefix="home" />
      )}
      {Object.keys(otherKeys).length > 0 && (
        <SectionCard title="Game Splits" data={otherKeys} defs={defs} />
      )}
    </div>
  );
}

function FeaturesTab({
  features,
  defs,
}: {
  features: Record<string, unknown> | null | undefined;
  defs: FeatureDef[];
}) {
  if (!features || Object.keys(features).length === 0) {
    return (
      <div className="text-center py-6">
        <p className="text-gray-500 text-sm mb-2">No model features available for this game.</p>
        <p className="text-gray-600 text-xs">
          Model feature data is populated during pipeline runs. Only games that have been
          processed by the prediction pipeline will have feature data.
        </p>
      </div>
    );
  }
  return <SectionCard title="Model Features" data={features} defs={defs} />;
}

/* ─── Main Modal ────────────────────────────────────────── */

type Tab = "team-stats" | "situational" | "splits" | "features";

export default function MLBPickCardStatsModal({ open, onClose, homeTeam, awayTeam, statsJson }: Props) {
  const defs = useFeatureDefs();
  const [activeTab, setActiveTab] = useState<Tab>("team-stats");
  const overlayRef = useRef<HTMLDivElement>(null);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  // Lock body scroll
  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  const handleOverlayClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === overlayRef.current) onClose();
    },
    [onClose],
  );

  if (!open) return null;

  const tabs: { id: Tab; label: string }[] = [
    { id: "team-stats", label: "Team Stats" },
    { id: "situational", label: "Situational" },
    { id: "splits", label: "Splits" },
    { id: "features", label: "Model Features" },
  ];

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={handleOverlayClick}
    >
      <div className="relative w-full max-w-2xl max-h-[85vh] flex flex-col rounded-xl border border-gray-700 bg-gray-900 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
          <div className="min-w-0">
            <h2 className="text-lg font-bold text-white truncate">
              {awayTeam} @ {homeTeam}
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">Full Prediction Stats</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex-shrink-0 p-1.5 rounded-lg text-gray-400 hover:text-white hover:bg-gray-700 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-700 overflow-x-auto px-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setActiveTab(t.id)}
              className={`px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
                activeTab === t.id
                  ? "text-earl-400 border-earl-400"
                  : "text-gray-400 border-transparent hover:text-gray-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4">
          {activeTab === "team-stats" && (
            <TeamStatsTab homeStats={statsJson.home_stats} awayStats={statsJson.away_stats} defs={defs} />
          )}
          {activeTab === "situational" && <SituationalTab data={statsJson.situational} defs={defs} />}
          {activeTab === "splits" && <SplitsTab data={statsJson.splits} defs={defs} />}
          {activeTab === "features" && <FeaturesTab features={statsJson.features} defs={defs} />}
        </div>
      </div>
    </div>
  );
}
