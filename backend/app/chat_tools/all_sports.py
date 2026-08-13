"""Cross-sport tool definitions + dispatcher for "All" (site-wide) editorial articles.

Gives a single ToolChatEngine access to every research tool across NFL, MLB,
and NBA by prefixing each tool name with its sport, e.g. ``mlb_get_team_stats``,
``nfl_get_team_stats``, ``nba_get_team_stats``. The names would otherwise
collide (all three sports define e.g. ``get_team_info``, ``get_standings``,
etc.), which would make it impossible for the model to address a specific
sport's tool.

The dispatcher strips the ``<sport>_`` prefix and routes to the matching
sport's own ``execute_*_tool``.
"""

from app.chat_tools import (
    MLB_TOOL_DEFINITIONS,
    NBA_TOOL_DEFINITIONS,
    NFL_TOOL_DEFINITIONS,
    execute_mlb_tool,
    execute_nba_tool,
    execute_nfl_tool,
)

PREFIXES = ("mlb", "nfl", "nba")


def _prefixed(tools, prefix: str) -> list[dict]:
    out = []
    for t in tools:
        clone = {"type": t.get("type", "function")}
        fn = dict(t.get("function", {}))
        fn["name"] = f"{prefix}_{fn['name']}"
        clone["function"] = fn
        out.append(clone)
    return out


PREFIXED_TOOL_DEFINITIONS = (
    _prefixed(MLB_TOOL_DEFINITIONS, "mlb")
    + _prefixed(NFL_TOOL_DEFINITIONS, "nfl")
    + _prefixed(NBA_TOOL_DEFINITIONS, "nba")
)

# Tool names -> (prefix, sport executor)
_EXECUTORS = {
    "mlb": execute_mlb_tool,
    "nfl": execute_nfl_tool,
    "nba": execute_nba_tool,
}


async def execute_all_sports_tool(db, tool_call):
    """Dispatch a prefixed tool name to the right sport's executor.

    Accepts the ``tool_call`` object (or a name/args dict-like) that the sport
    executors consume. The name is expected as ``<sport>_<tool_name>``.
    """
    raw_name = getattr(tool_call, "function", None)
    name = None
    if raw_name is not None:
        name = getattr(raw_name, "name", None) or raw_name.get("name")
    else:
        # allow a dict with a "name" or function-dict
        name = tool_call.get("name") or (tool_call.get("function") or {}).get("name")

    if not name:
        return '{"error": "no tool name"}'

    for prefix in PREFIXES:
        if name.startswith(f"{prefix}_"):
            renamed = name[len(prefix) + 1 :]
            real = _make_renamed(tool_call, renamed)
            return await _EXECUTORS[prefix](db, real)

    return f'{{"error": "Unknown cross-sport tool: {name}"}}'


def _make_renamed(tool_call, new_name):
    """Return a shallow copy of tool_call with its function name replaced."""
    fn = getattr(tool_call, "function", None)
    if fn is not None and hasattr(fn, "name"):
        import copy

        clone = copy.copy(tool_call)
        clone_fn = copy.copy(fn)
        clone_fn.name = new_name
        clone.function = clone_fn
        return clone
    if isinstance(tool_call, dict):
        import copy

        clone = copy.deepcopy(tool_call)
        clone.setdefault("function", {})["name"] = new_name
        return clone
    return tool_call
