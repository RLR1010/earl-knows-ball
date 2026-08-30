import io

path = "app/(sports)/[sport]/schedule/ScheduleClient.tsx"
with io.open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Replace lines 456..545 (1-indexed inclusive) with the ScheduleGameCard map.
start, end = 456, 545
assert "{games.map((g) => {" in lines[start-1], lines[start-1]
assert "})}" in lines[end-1], lines[end-1]

replacement = '''          {games.map((g) => (
            <ScheduleGameCard
              key={g.id}
              game={g}
              sport={sport as CardSport}
              href={`/${sport}/games/${g.id}?year=${year}&date=${selectedDate}`}
            />
          ))}'''

new_lines = lines[: start-1] + [replacement + "\n"] + lines[end:]
with io.open(path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Replaced lines", start, "-", end, "with ScheduleGameCard map")
