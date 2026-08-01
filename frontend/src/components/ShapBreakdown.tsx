"use client";

/**
 * SHAP Feature Attribution breakdown — admin-only.
 * Renders the shap_json column from game_predictions for all sports.
 * Data shape (per target, e.g. ats/ou):
 *   { expected_value, predicted_value, contributions: [{name, display_name,
 *      description, value, contribution, direction}], story }
 */

interface ShapContribution {
  name?: string;
  display_name?: string;
  description?: string;
  value?: number | string | null;
  contribution?: number | null;
  direction?: string; // "up" | "down"
}

interface ShapTarget {
  expected_value?: number | null;
  predicted_value?: number | null;
  contributions?: ShapContribution[];
  story?: string | null;
}

export interface ShapData {
  ats?: ShapTarget;
  ou?: ShapTarget;
}

const TARGET_META: Record<string, { title: string; blurb: string }> = {
  ats: { title: "ATS", blurb: "Against the spread" },
  ou: { title: "Over/Under", blurb: "Total points" },
};

function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  // Model outputs are regressions (margin in points/runs, total in points/runs),
  // NOT probabilities — never format as a percentage.
  return v.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

// Feature values are mixed: some are rates (SLG, OPS, win%) where % reads better,
// others are counts (ERA, runs, temperature). Rates live in (0, 1].
function fmtFeatureValue(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (Math.abs(v) <= 1.0001 && Math.abs(v) > 0) {
    return `${(v * 100).toFixed(1)}%`;
  }
  return v.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

function fmtContrib(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(4)}`;
}

export default function ShapBreakdown({ data }: { data: ShapData }) {
  if (!data) return null;
  const targets = Object.keys(TARGET_META).filter((k) => data[k as keyof ShapData]);

  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h4 className="text-sm font-bold text-amber-300">SHAP Feature Attribution</h4>
        <span className="rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-300">
          Admin
        </span>
      </div>
      <p className="mb-4 text-[11px] leading-relaxed text-gray-400">
        Model explainability — how each feature pushed the raw model output, before calibration.
        Positive = pushes the prediction up; negative = pushes it down.
      </p>

      {targets.length === 0 && (
        <p className="text-xs text-gray-500">No SHAP attribution stored for this game.</p>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {targets.map((key) => {
          const t = data[key as keyof ShapData] as ShapTarget | undefined;
          if (!t) return null;
          const meta = TARGET_META[key];
          const contribs = [...(t.contributions ?? [])].sort(
            (a, b) => Math.abs(b.contribution ?? 0) - Math.abs(a.contribution ?? 0)
          );
          return (
            <div key={key} className="rounded-lg border border-gray-700/60 bg-gray-900/40 p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-bold text-white">{meta.title}</span>
                <span className="text-[10px] text-gray-500">{meta.blurb}</span>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2 text-[11px]">
                <div className="rounded-md bg-gray-800/60 px-2 py-1.5">
                  <div className="text-gray-500">Baseline (expected)</div>
                  <div className="font-semibold text-gray-200">{fmtNum(t.expected_value)}</div>
                </div>
                <div className="rounded-md bg-gray-800/60 px-2 py-1.5">
                  <div className="text-gray-500">Raw model output</div>
                  <div className="font-semibold text-emerald-300">{fmtNum(t.predicted_value)}</div>
                </div>
              </div>

              <ul className="space-y-1">
                {contribs.slice(0, 12).map((c, i) => {
                  const up = c.direction === "up";
                  const pos = (c.contribution ?? 0) >= 0;
                  return (
                    <li
                      key={`${c.name ?? "contrib"}-${i}`}
                      className="flex items-center justify-between gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-gray-800/40"
                      title={c.description || c.display_name || c.name}
                    >
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span
                          className={`text-[9px] font-bold ${
                            up ? "text-emerald-400" : "text-rose-400"
                          }`}
                        >
                          {up ? "▲" : "▼"}
                        </span>
                        <span className="truncate text-gray-300">
                          {c.display_name || c.name || "—"}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        <span className="text-gray-500">{fmtFeatureValue(c.value as number)}</span>
                        <span
                          className={`w-16 text-right font-mono font-semibold ${
                            pos ? "text-emerald-400" : "text-rose-400"
                          }`}
                        >
                          {fmtContrib(c.contribution)}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>

              {t.story && (
                <p className="mt-2 border-t border-gray-700/60 pt-2 text-[10px] italic leading-relaxed text-gray-500">
                  {t.story}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
