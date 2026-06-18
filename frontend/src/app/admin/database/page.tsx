"use client";

import { useEffect, useState, useCallback } from "react";

// ── Types ──────────────────────────────────────────────────────────

interface ColumnInfo {
  column_name: string;
  data_type: string;
  nullable: boolean;
  is_pk: boolean;
  default: string | null;
}

interface TableData {
  schema_name: string;
  table_name: string;
  columns: ColumnInfo[];
  rows: Record<string, unknown>[];
  total_count: number;
  offset: number;
  limit: number;
  has_more: boolean;
}

// ── Token helper ───────────────────────────────────────────────────

const token = () => localStorage.getItem("earl_token");

// ── Main Component ─────────────────────────────────────────────────

const SCHEMAS = ["public", "nfl", "nba", "mlb"];

export default function DatabaseExplorer() {
  const [schemas] = useState<string[]>(SCHEMAS);
  const [selectedSchema, setSelectedSchema] = useState<string>("public");
  const [tables, setTables] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [tableData, setTableData] = useState<TableData | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingTables, setLoadingTables] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pageInput, setPageInput] = useState("");

  // ── Fetch tables when schema changes ─────────────────────────────

  useEffect(() => {
    setSelectedTable(null);
    setTableData(null);
    setError(null);

    const fetchTables = async () => {
      setLoadingTables(true);
      try {
        const res = await fetch(
          `/api/admin/db/schemas/${selectedSchema}/tables`,
          { headers: { Authorization: `Bearer ${token()}` } }
        );
        if (!res.ok) throw new Error(`Failed to load tables: ${res.statusText}`);
        const data = await res.json();
        setTables(data.map((t: { table_name: string }) => t.table_name));
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Unknown error";
        setError(msg);
      } finally {
        setLoadingTables(false);
      }
    };

    fetchTables();
  }, [selectedSchema]);

  // ── Fetch table data ─────────────────────────────────────────────

  const fetchTableData = useCallback(
    async (tableName: string, offset = 0) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(
          `/api/admin/db/schemas/${selectedSchema}/tables/${tableName}?offset=${offset}&limit=50`,
          { headers: { Authorization: `Bearer ${token()}` } }
        );
        if (!res.ok) throw new Error(`Failed to load data: ${res.statusText}`);
        const data: TableData = await res.json();
        setTableData(data);
        setSelectedTable(tableName);
        setPageInput("");
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Unknown error";
        setError(msg);
      } finally {
        setLoading(false);
      }
    },
    [selectedSchema]
  );

  const handleNextPage = () => {
    if (!tableData || !selectedTable) return;
    fetchTableData(selectedTable, tableData.offset + tableData.limit);
  };

  const handlePrevPage = () => {
    if (!tableData || !selectedTable) return;
    const newOffset = Math.max(0, tableData.offset - tableData.limit);
    fetchTableData(selectedTable, newOffset);
  };

  // ── Render ───────────────────────────────────────────────────────

  const currentPage = tableData
    ? Math.floor(tableData.offset / tableData.limit) + 1
    : 0;
  const totalPages = tableData
    ? Math.ceil(tableData.total_count / tableData.limit)
    : 0;

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">🗄️ Database Explorer</h1>
        <p className="text-gray-400 text-sm mt-1">
          Browse tables and data across all schemas. Read-only view with 50-row
          pagination.
        </p>
      </div>

      {/* Schema & Table Selection */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        {/* Schema selector */}
        <div className="md:col-span-1">
          <label className="block text-xs text-gray-500 uppercase tracking-wider font-semibold mb-2">
            Schema
          </label>
          <div className="space-y-1">
            {schemas.map((s) => (
              <button
                key={s}
                onClick={() => setSelectedSchema(s)}
                className={`w-full text-left px-4 py-2.5 rounded-lg text-sm transition ${
                  selectedSchema === s
                    ? "bg-earl-600 text-white font-semibold"
                    : "bg-white/5 text-gray-300 hover:bg-white/10 hover:text-white border border-white/10"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Table list */}
        <div className="md:col-span-1">
          <label className="block text-xs text-gray-500 uppercase tracking-wider font-semibold mb-2">
            Tables
            {loadingTables && (
              <span className="ml-2 inline-block w-3 h-3 border-2 border-gray-500 border-t-transparent rounded-full animate-spin" />
            )}
          </label>
          <div className="space-y-1 max-h-[calc(100vh-320px)] overflow-y-auto">
            {tables.length === 0 && !loadingTables && (
              <p className="text-gray-600 text-sm italic px-4 py-2">
                No tables found
              </p>
            )}
            {tables.map((t) => (
              <button
                key={t}
                onClick={() => fetchTableData(t)}
                className={`w-full text-left px-4 py-2 rounded-lg text-sm transition ${
                  selectedTable === t
                    ? "bg-white/15 text-white font-semibold border border-white/20"
                    : "bg-white/[0.03] text-gray-400 hover:bg-white/10 hover:text-gray-200 border border-transparent"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
        </div>

        {/* Column info & data */}
        <div className="md:col-span-2">
          {error && (
            <div className="bg-red-900/30 border border-red-700/50 rounded-lg p-4 text-sm text-red-300 mb-4">
              {error}
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-earl-500 border-t-transparent rounded-full animate-spin" />
              <span className="ml-3 text-gray-400 text-sm">Loading data...</span>
            </div>
          )}

          {tableData && !loading && (
            <div className="space-y-4">
              {/* Table info bar */}
              <div className="flex items-center justify-between bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                <div>
                  <span className="text-white font-semibold text-sm">
                    {tableData.schema_name}.{tableData.table_name}
                  </span>
                  <span className="text-gray-500 text-xs ml-3">
                    {tableData.total_count.toLocaleString()} rows total
                  </span>
                  <span className="text-gray-600 text-xs ml-2">
                    · {tableData.columns.length} columns
                  </span>
                </div>
              </div>

              {/* Column schema */}
              <details className="bg-white/[0.03] border border-white/10 rounded-lg">
                <summary className="px-4 py-2.5 text-xs text-gray-400 cursor-pointer hover:text-gray-300 select-none font-semibold uppercase tracking-wider">
                  Column Definitions
                </summary>
                <div className="px-4 pb-3 overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-500 border-b border-white/10">
                        <th className="text-left py-2 pr-4 font-semibold">Column</th>
                        <th className="text-left py-2 pr-4 font-semibold">Type</th>
                        <th className="text-left py-2 pr-4 font-semibold">Nullable</th>
                        <th className="text-left py-2 pr-4 font-semibold">PK</th>
                        <th className="text-left py-2 font-semibold">Default</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tableData.columns.map((col) => (
                        <tr
                          key={col.column_name}
                          className="border-b border-white/5 text-gray-400 hover:text-gray-300"
                        >
                          <td className="py-1.5 pr-4 font-mono">
                            {col.is_pk && (
                              <span className="text-yellow-500 mr-1" title="Primary Key">
                                🔑
                              </span>
                            )}
                            {col.column_name}
                          </td>
                          <td className="py-1.5 pr-4 font-mono text-gray-500">
                            {col.data_type}
                          </td>
                          <td className="py-1.5 pr-4">
                            {col.nullable ? (
                              <span className="text-gray-600">YES</span>
                            ) : (
                              <span className="text-gray-400">NO</span>
                            )}
                          </td>
                          <td className="py-1.5 pr-4">
                            {col.is_pk ? (
                              <span className="text-yellow-500">YES</span>
                            ) : (
                              <span className="text-gray-600">—</span>
                            )}
                          </td>
                          <td className="py-1.5 font-mono text-gray-500 max-w-[200px] truncate">
                            {col.default || (
                              <span className="text-gray-700">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>

              {/* Data rows */}
              <div className="bg-white/[0.03] border border-white/10 rounded-lg overflow-hidden">
                <div className="overflow-x-auto max-h-[calc(100vh-480px)] overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-gray-900 z-10">
                      <tr className="text-gray-400 border-b border-white/10">
                        <th className="text-left py-2.5 px-3 font-semibold whitespace-nowrap text-gray-500 w-10">
                          #
                        </th>
                        {tableData.columns.map((col) => (
                          <th
                            key={col.column_name}
                            className="text-left py-2.5 px-3 font-semibold whitespace-nowrap font-mono"
                            title={`${col.data_type}${col.is_pk ? " [PK]" : ""}`}
                          >
                            {col.is_pk && <span className="text-yellow-500 mr-0.5">🔑</span>}
                            {col.column_name}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {tableData.rows.length === 0 && (
                        <tr>
                          <td
                            colSpan={tableData.columns.length + 1}
                            className="text-center py-12 text-gray-600 italic"
                          >
                            No data
                          </td>
                        </tr>
                      )}
                      {tableData.rows.map((row, rowIdx) => (
                        <tr
                          key={rowIdx}
                          className="border-b border-white/5 text-gray-300 hover:bg-white/[0.04] transition"
                        >
                          <td className="py-1.5 px-3 text-gray-600 font-mono whitespace-nowrap">
                            {tableData.offset + rowIdx + 1}
                          </td>
                          {tableData.columns.map((col) => {
                            const val = row[col.column_name];
                            const formatted = formatCellValue(val);
                            return (
                              <td
                                key={col.column_name}
                                className={
                                  "py-1.5 px-3 font-mono whitespace-nowrap max-w-[300px] truncate" +
                                  (val === null || val === undefined
                                    ? " text-gray-700 italic"
                                    : "")
                                }
                                title={val === null || val === undefined ? "NULL" : String(val)}
                              >
                                {formatted}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Pagination */}
              <div className="flex items-center justify-between bg-white/[0.03] border border-white/10 rounded-lg px-4 py-3">
                <div className="text-xs text-gray-500">
                  Showing rows{" "}
                  <span className="text-gray-300 font-semibold">
                    {tableData.offset + 1}–{tableData.offset + tableData.rows.length}
                  </span>{" "}
                  of{" "}
                  <span className="text-gray-300 font-semibold">
                    {tableData.total_count.toLocaleString()}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handlePrevPage}
                    disabled={tableData.offset === 0}
                    className="px-3 py-1.5 text-xs font-medium rounded-lg border transition disabled:opacity-30 disabled:cursor-not-allowed bg-white/5 border-white/10 text-gray-300 hover:bg-white/10 hover:text-white"
                  >
                    ← Prev
                  </button>
                  <div className="flex items-center gap-1.5 text-xs text-gray-500">
                    <span>Page</span>
                    <input
                      type="number"
                      min={1}
                      max={totalPages}
                      value={pageInput || currentPage}
                      onChange={(e) => setPageInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          const p = parseInt(pageInput, 10);
                          if (
                            !isNaN(p) &&
                            p >= 1 &&
                            p <= totalPages &&
                            selectedTable
                          ) {
                            const newOffset = (p - 1) * tableData.limit;
                            setPageInput("");
                            fetchTableData(selectedTable, newOffset);
                          }
                        }
                      }}
                      onBlur={() => setPageInput("")}
                      className="w-14 text-center bg-white/5 border border-white/10 rounded-md py-1 px-1.5 text-white font-semibold outline-none focus:border-earl-500 focus:ring-1 focus:ring-earl-500/30 transition"
                    />
                    <span>of {totalPages}</span>
                  </div>
                  <button
                    onClick={handleNextPage}
                    disabled={!tableData.has_more}
                    className="px-3 py-1.5 text-xs font-medium rounded-lg border transition disabled:opacity-30 disabled:cursor-not-allowed bg-white/5 border-white/10 text-gray-300 hover:bg-white/10 hover:text-white"
                  >
                    Next →
                  </button>
                </div>
              </div>
            </div>
          )}

          {!tableData && !loading && !error && (
            <div className="flex items-center justify-center py-20 text-gray-600">
              <div className="text-center">
                <div className="text-4xl mb-3">🗄️</div>
                <p className="text-sm">Select a schema and table to browse data</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────

function formatCellValue(val: unknown): string {
  if (val === null || val === undefined) return "NULL";
  if (typeof val === "boolean") return val ? "true" : "false";
  if (typeof val === "object") {
    try {
      return JSON.stringify(val);
    } catch {
      return String(val);
    }
  }
  return String(val);
}
