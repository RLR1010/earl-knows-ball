"""Chat tool definitions and executors for all sports.

Each sport module exports:
    TOOL_DEFINITIONS: list of OpenAI function-calling schemas.
    execute_<sport>_tool: async dispatcher function.
"""

from app.chat_tools.base import ToolChatEngine
from app.chat_tools.mlb import TOOL_DEFINITIONS as MLB_TOOL_DEFINITIONS
from app.chat_tools.mlb import execute_mlb_tool
from app.chat_tools.nfl import TOOL_DEFINITIONS as NFL_TOOL_DEFINITIONS
from app.chat_tools.nfl import execute_nfl_tool
from app.chat_tools.nba import TOOL_DEFINITIONS as NBA_TOOL_DEFINITIONS
from app.chat_tools.nba import execute_nba_tool

__all__ = [
    "ToolChatEngine",
    "MLB_TOOL_DEFINITIONS",
    "execute_mlb_tool",
    "NFL_TOOL_DEFINITIONS",
    "execute_nfl_tool",
    "NBA_TOOL_DEFINITIONS",
    "execute_nba_tool",
]
