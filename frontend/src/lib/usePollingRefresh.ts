"use client";

import { useEffect, useRef } from "react";

/**
 * Periodically re-runs `refresh` while the tab is visible.
 *
 * Mirrors the schedule page's auto-refresh: a 30s interval that re-fetches
 * data so scores/odds/picks update without a manual reload. Pauses while the
 * tab is hidden (no point polling a background tab) and fires an immediate
 * catch-up refresh when it becomes visible again.
 *
 * @param refresh  Callback that re-fetches + updates state. Kept in a ref so
 *                 the interval is not torn down/recreated on every render.
 * @param enabled  When false, polling is disabled (e.g. viewing a historical
 *                 year/date that never changes).
 * @param intervalMs Poll interval. Defaults to 30s to match the schedule page.
 */
export function usePollingRefresh(
  refresh: () => void | Promise<void>,
  enabled: boolean = true,
  intervalMs: number = 30_000,
) {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!enabled || typeof document === "undefined") return;

    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = () => {
      if (document.hidden) return;
      void refreshRef.current();
    };

    const start = () => {
      if (timer !== null) return;
      timer = setInterval(tick, intervalMs);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        // Catch up immediately on return, then resume the interval.
        void refreshRef.current();
        start();
      }
    };

    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, intervalMs]);
}
