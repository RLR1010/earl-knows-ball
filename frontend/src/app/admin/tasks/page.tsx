"use client";

import { useEffect, useState, useCallback } from "react";

const token = () => localStorage.getItem("earl_token");

interface Task {
  id: number;
  name: string;
  description: string | null;
  task_type: string;
  cron_expr: string;
  timezone: string;
  enabled: boolean;
  created_at: string | null;
  last_status: string | null;
  last_run: string | null;
  last_duration: number | null;
  last_error: string | null;
  next_run: string | null;
}

interface TaskRun {
  id: number;
  task_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  error_message: string | null;
  details: Record<string, unknown> | null;
  created_at: string | null;
}

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-xs text-gray-500">—</span>;
  const colors: Record<string, string> = {
    success: "bg-green-900/40 text-green-400 border-green-700/40",
    failed: "bg-red-900/40 text-red-400 border-red-700/40",
    running: "bg-blue-900/40 text-blue-400 border-blue-700/40 animate-pulse",
    timeout: "bg-orange-900/40 text-orange-400 border-orange-700/40",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${colors[status] || "bg-gray-800 text-gray-400 border-gray-700"}`}>
      {status}
    </span>
  );
}

function Duration({ ms }: { ms: number | null }) {
  if (!ms) return <span className="text-gray-500">—</span>;
  if (ms < 1000) return <span className="text-gray-400">{ms}ms</span>;
  if (ms < 60000) return <span className="text-gray-400">{(ms / 1000).toFixed(1)}s</span>;
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return <span className="text-gray-400">{m}m {s}s</span>;
}

function Time({ t }: { t: string | null }) {
  if (!t) return <span className="text-gray-500">—</span>;
  const d = new Date(t);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffHrs = diffMs / 3600000;

  let relative: string;
  if (diffHrs < 1) {
    const mins = Math.round(diffMs / 60000);
    relative = `${mins}m ago`;
  } else if (diffHrs < 24) {
    relative = `${Math.round(diffHrs)}h ago`;
  } else {
    relative = `${Math.round(diffHrs / 24)}d ago`;
  }

  const localTime = d.toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });

  return (
    <span className="text-gray-400" title={d.toLocaleString()}>
      {localTime} <span className="text-gray-500 text-xs">({relative})</span>
    </span>
  );
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [triggering, setTriggering] = useState<string | null>(null);

  const fetchTasks = useCallback(async () => {
    try {
      const res = await fetch("/api/admin/tasks", {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setTasks(await res.json());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchRuns = useCallback(async (name: string) => {
    setRunsLoading(true);
    try {
      const res = await fetch(`/api/admin/tasks/${name}/runs?limit=20`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRuns(await res.json());
    } catch (e: any) {
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  const handleTrigger = async (name: string) => {
    setTriggering(name);
    try {
      await fetch(`/api/admin/tasks/${name}/trigger`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token()}` },
      });
      // Wait a moment then refresh
      setTimeout(() => fetchTasks(), 2000);
    } catch (e: any) {
      alert(`Failed to trigger: ${e.message}`);
    } finally {
      setTriggering(null);
    }
  };

  const handleSelect = (name: string) => {
    if (selectedTask === name) {
      setSelectedTask(null);
      setRuns([]);
    } else {
      setSelectedTask(name);
      fetchRuns(name);
    }
  };

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Task Runner</h1>
          <p className="text-sm text-gray-500 mt-1">
            Automated processes — RSS scrapes, lines refresh, embeddings, pick cards
          </p>
        </div>
        <button
          onClick={fetchTasks}
          className="px-4 py-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-sm text-gray-300 transition"
        >
          ↻ Refresh
        </button>
      </div>

      {loading && <div className="text-gray-400">Loading tasks...</div>}

      {error && (
        <div className="bg-red-900/20 border border-red-800/30 rounded-xl p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Tasks Table */}
      {!loading && (
        <div className="bg-white/[0.02] border border-white/5 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10 bg-white/[0.03]">
                  <th className="text-left py-3 px-4 text-gray-400 font-medium">Task</th>
                  <th className="text-left py-3 px-4 text-gray-400 font-medium">Schedule</th>
                  <th className="text-center py-3 px-4 text-gray-400 font-medium">Status</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Last Run</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Duration</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Next Run</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Action</th>
                </tr>
              </thead>
              <tbody>
                {tasks.length === 0 && (
                  <tr>
                    <td colSpan={7} className="py-8 text-center text-gray-500">
                      No tasks configured
                    </td>
                  </tr>
                )}
                {tasks.map((task) => (
                  <tr
                    key={task.name}
                    onClick={() => handleSelect(task.name)}
                    className={`border-b border-white/5 hover:bg-white/[0.03] cursor-pointer transition ${
                      selectedTask === task.name ? "bg-earl-600/10" : ""
                    }`}
                  >
                    <td className="py-3 px-4">
                      <div className="text-white font-medium">{task.name}</div>
                      {task.description && (
                        <div className="text-xs text-gray-500 mt-0.5 max-w-xs truncate">{task.description}</div>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className="font-mono text-xs bg-white/5 px-2 py-1 rounded text-gray-300">{task.cron_expr}</span>
                    </td>
                    <td className="py-3 px-4 text-center">
                      <StatusBadge status={task.last_status} />
                    </td>
                    <td className="py-3 px-4 text-right">
                      <Time t={task.last_run} />
                    </td>
                    <td className="py-3 px-4 text-right">
                      <Duration ms={task.last_duration} />
                    </td>
                    <td className="py-3 px-4 text-right text-gray-300">
                      {task.next_run || "—"}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleTrigger(task.name); }}
                        disabled={triggering === task.name}
                        className="px-3 py-1.5 bg-earl-600/20 hover:bg-earl-600/30 text-earl-400 border border-earl-600/30 rounded-lg text-xs font-medium transition disabled:opacity-50"
                      >
                        {triggering === task.name ? "..." : "▶ Run"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Run History for Selected Task */}
      {selectedTask && (
        <div>
          <h3 className="text-lg font-semibold text-white mb-3">
            Recent runs: <span className="text-earl-400">{selectedTask}</span>
          </h3>
          <div className="bg-white/[0.02] border border-white/5 rounded-xl overflow-hidden">
            {runsLoading ? (
              <div className="p-6 text-gray-500 text-sm">Loading runs...</div>
            ) : runs.length === 0 ? (
              <div className="p-6 text-gray-500 text-sm">No runs recorded yet</div>
            ) : (
              <div className="overflow-x-auto max-h-80 overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-[#0a0a0f]">
                    <tr className="border-b border-white/10">
                      <th className="text-left py-2 px-4 text-gray-400 font-medium">#</th>
                      <th className="text-left py-2 px-4 text-gray-400 font-medium">Started</th>
                      <th className="text-center py-2 px-4 text-gray-400 font-medium">Status</th>
                      <th className="text-right py-2 px-4 text-gray-400 font-medium">Duration</th>
                      <th className="py-2 px-4 text-gray-400 font-medium">Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr key={run.id} className="border-b border-white/5 hover:bg-white/[0.02]">
                        <td className="py-2 px-4 text-gray-500 text-xs">{run.id}</td>
                        <td className="py-2 px-4">
                          <Time t={run.started_at} />
                        </td>
                        <td className="py-2 px-4 text-center">
                          <StatusBadge status={run.status} />
                        </td>
                        <td className="py-2 px-4 text-right">
                          <Duration ms={run.duration_ms} />
                        </td>
                        <td className="py-2 px-4 max-w-xs">
                          {run.error_message ? (
                            <span className="text-red-400 text-xs truncate block" title={run.error_message}>
                              {run.error_message}
                            </span>
                          ) : (
                            <span className="text-gray-600">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
