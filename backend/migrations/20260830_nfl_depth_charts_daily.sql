-- ---------------------------------------------------------------------------
-- 20260830_nfl_depth_charts_daily.sql
-- Daily NFL depth-chart refresh task.
--
-- Registers a daily (6:30 AM CT) `subprocess` scheduler task that scrapes
-- current Ourlads depth charts for all 32 teams into nfl.depth_charts via
-- backend/app/scripts/ingress/run_nfl_depth_charts.py.
--
-- The runner is idempotent: each team's chart is a full-replace (old entries
-- deleted, new inserted), committed per team. Runs in roughly ~2-4 minutes
-- (32 sequential network fetches). Idempotent against re-running this file.
-- ---------------------------------------------------------------------------

INSERT INTO task_config (name, task_type, config, cron_expr, timezone, enabled, max_retries, description)
SELECT
    'nfl-depth-charts-refresh',
    'subprocess',
    '{"command": "cd /home/rich/.openclaw/workspace/earl-knows-football/backend && PYTHONPATH=$PWD /home/rich/.openclaw/workspace/earl-knows-football/venv/bin/python app/scripts/ingress/run_nfl_depth_charts.py", "timeout": 1800}'::jsonb,
    '30 6 * * *',
    'America/Chicago',
    true,
    2,
    'NFL depth chart refresh (subprocess): scrapes current Ourlads depth charts for all 32 teams into nfl.depth_charts'
WHERE NOT EXISTS (
    SELECT 1 FROM task_config WHERE name = 'nfl-depth-charts-refresh'
);
