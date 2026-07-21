"use client";

import { useMemo, useState, useRef, useEffect } from "react";

interface GameCalendarProps {
  /** ISO date strings for days that have games in this season */
  gameDates: string[];
  /** Currently selected ISO date string */
  selectedDate: string;
  /** Called when user picks a date */
  onSelect: (date: string) => void;
  /** Called when the calendar should close */
  onClose: () => void;
  /** Minimum selectable date (season opening day) — ISO string */
  minDate?: string;
  /** Maximum selectable date (season last day) — ISO string */
  maxDate?: string;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Build a Set of date strings for O(1) lookup. Normalizes to YYYY-MM-DD. */
function dateSet(dates: string[]): Set<string> {
  const s = new Set<string>();
  for (const d of dates) {
    const parts = d.split("T")[0].split("-");
    if (parts.length === 3) s.add(d.slice(0, 10));
  }
  return s;
}

export default function GameCalendar({
  gameDates,
  selectedDate,
  onSelect,
  onClose,
  minDate,
  maxDate,
}: GameCalendarProps) {
  const gameDateSet = useMemo(() => dateSet(gameDates), [gameDates]);
  const initialDate = selectedDate ? new Date(selectedDate + "T12:00:00") : new Date();
  const [viewYear, setViewYear] = useState(initialDate.getFullYear());
  const [viewMonth, setViewMonth] = useState(initialDate.getMonth()); // 0-indexed
  const calendarRef = useRef<HTMLDivElement>(null);

  // Close on click outside
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (calendarRef.current && !calendarRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [onClose]);

  // Close on Escape
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  const monthNames = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];

  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const firstDayOfWeek = new Date(viewYear, viewMonth, 1).getDay(); // 0=Sun
  const todayStr = new Date().toISOString().slice(0, 10);

  function prevMonth() {
    if (viewMonth === 0) {
      setViewYear(viewYear - 1);
      setViewMonth(11);
    } else {
      setViewMonth(viewMonth - 1);
    }
  }

  function nextMonth() {
    if (viewMonth === 11) {
      setViewYear(viewYear + 1);
      setViewMonth(0);
    } else {
      setViewMonth(viewMonth + 1);
    }
  }

  function handleDateClick(day: number) {
    const y = String(viewYear).padStart(4, "0");
    const m = String(viewMonth + 1).padStart(2, "0");
    const d = String(day).padStart(2, "0");
    const dateStr = `${y}-${m}-${d}`;
    if (!gameDateSet.has(dateStr)) return;
    if (minDate && dateStr < minDate) return;
    if (maxDate && dateStr > maxDate) return;
    onSelect(dateStr);
    onClose();
  }

  // Build day cells
  const cells: React.ReactNode[] = [];
  // Empty cells for leading days
  for (let i = 0; i < firstDayOfWeek; i++) {
    cells.push(<div key={`empty-${i}`} className="h-9 w-9" />);
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const y = String(viewYear).padStart(4, "0");
    const m = String(viewMonth + 1).padStart(2, "0");
    const d = String(day).padStart(2, "0");
    const dateStr = `${y}-${m}-${d}`;

    const hasGame = gameDateSet.has(dateStr);
    const isSelected = dateStr === selectedDate;
    const isToday = dateStr === todayStr;
    const isDisabled =
      !hasGame ||
      (minDate !== undefined && dateStr < minDate) ||
      (maxDate !== undefined && dateStr > maxDate);

    let cls = "h-9 w-9 rounded text-sm flex items-center justify-center";
    if (isSelected) {
      cls += " bg-green-600 text-white font-bold";
    } else if (isDisabled) {
      cls += " text-gray-500 cursor-default";
    } else if (isToday) {
      cls += " text-green-400 font-semibold cursor-pointer hover:bg-gray-700";
    } else {
      cls += " text-gray-200 cursor-pointer hover:bg-gray-700";
    }

    cells.push(
      <div
        key={dateStr}
        className={cls}
        onClick={() => handleDateClick(day)}
        title={hasGame ? dateStr : `${dateStr} (no games)`}
      >
        {day}
      </div>
    );
  }

  return (
    <div
      ref={calendarRef}
      className="absolute top-full left-0 mt-1 z-50 bg-gray-800 border border-gray-600 rounded-lg shadow-xl p-3 w-[280px]"
    >
      {/* Month/Year header */}
      <div className="flex items-center justify-between mb-2">
        <button
          onClick={prevMonth}
          className="px-2 py-1 text-gray-300 hover:text-white hover:bg-gray-700 rounded"
          aria-label="Previous month"
        >
          ◀
        </button>
        <span className="text-white font-semibold text-sm">
          {monthNames[viewMonth]} {viewYear}
        </span>
        <button
          onClick={nextMonth}
          className="px-2 py-1 text-gray-300 hover:text-white hover:bg-gray-700 rounded"
          aria-label="Next month"
        >
          ▶
        </button>
      </div>

      {/* Day-of-week header */}
      <div className="grid grid-cols-7 gap-0 mb-1">
        {DAYS.map((d) => (
          <div key={d} className="h-7 w-9 text-center text-xs text-gray-400 font-medium">
            {d.charAt(0)}
          </div>
        ))}
      </div>

      {/* Day grid */}
      <div className="grid grid-cols-7 gap-0">{cells}</div>
    </div>
  );
}
