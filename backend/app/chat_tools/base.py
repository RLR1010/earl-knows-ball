"""Core tool-calling chat engine.

Provides the `ToolChatEngine` class that runs the research loop (DeepSeek calls
tools to query the database) and optionally enrichment (vector search + summary).
"""

import json
import logging
from typing import Any, Callable

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are Earl, an AI sports handicapper specializing in {sport} betting analysis.

You have access to a comprehensive database of {sport} data through function calls.
When answering a user's question, follow this process:

1. RESEARCH FIRST — use the available functions to look up data. Do NOT base your
   answer on general knowledge or training data alone. HIT THE DATABASE.
2. Gather enough context — call multiple functions if needed (team stats, pitching
   matchups, injuries, head-to-head history, etc.)
3. Provide a sharp handicapping analysis with specific numbers backing up your opinion
4. Be honest if data is limited — say so and give your best read

Available data: {data_description}

CRITICAL RULES:
- Research before answering. Always. Use the tools.
- NEVER recommend parlays or same-game parlays — they're sucker bets with
  terrible expected value and not a smart wagering strategy.
- NEVER suggest chasing losses or increasing bet size after a loss.
- Use plain text only. No markdown formatting, no asterisks.
- Be direct and opinionated, but back it up with data.
- Keep responses concise — a few focused paragraphs.
- If you don't have data for something, say so.
- The current Central US date/time is provided at the start of each user message."""


class ToolChatEngine:
    """Chat engine that uses OpenAI function calling to let DeepSeek research queries
    against a sports database before answering."""

    def __init__(
        self,
        sport: str,
        sport_display: str,
        data_description: str,
        tools: list[dict],
        executor: Callable[[Any, Any], str],
        system_prompt_extra: str = "",
        model: str | None = None,
    ):
        self.sport = sport
        self.sport_display = sport_display
        self.tools = tools
        self.executor = executor
        self.model = model or settings.deepseek_model

        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            sport=sport_display,
            data_description=data_description,
        )
        if system_prompt_extra:
            self.system_prompt += f"\n\n{system_prompt_extra}"

    async def research_and_answer(
        self,
        db: Any,
        messages: list[dict],
        max_turns: int = 15,
    ) -> str:
        """Run the tool-calling research loop and return DeepSeek's final answer.

        Args:
            db: Database session (AsyncSession or sync session).
            messages: List of message dicts.
            max_turns: Maximum tool-calling rounds before forcing a final answer.

        Returns:
            Final answer text from DeepSeek.
        """
        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
            timeout=45.0,
        )

        # First call with tools available
        response = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self.tools,
            tool_choice="auto",
        )

        assistant_msg = response.choices[0].message
        self._append_assistant(messages, assistant_msg)

        turns = 0
        while assistant_msg.tool_calls and turns < max_turns:
            turns += 1
            logger.info(
                "Tool call round %d/%d: %d tool(s)",
                turns, max_turns, len(assistant_msg.tool_calls),
            )

            # Execute each tool call
            for tool_call in assistant_msg.tool_calls:
                try:
                    result = await self.executor(db, tool_call)
                    content = json.dumps(result, default=str)
                except Exception as e:
                    logger.exception("Tool execution failed: %s", e)
                    content = json.dumps({"error": str(e)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": content,
                })

            # Next turn
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
            )
            assistant_msg = response.choices[0].message
            self._append_assistant(messages, assistant_msg)

        # If DeepSeek still wants to call tools (hit max_turns), force a final answer
        if not assistant_msg.content and assistant_msg.tool_calls:
            logger.info("Hit max_turns with pending tool calls — forcing final answer")
            messages.append({"role": "user", "content": "You have all the data you need. Provide your final answer now. Be concise."})
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
            assistant_msg = response.choices[0].message

        return assistant_msg.content or ""


    @staticmethod
    def _extract_tool_results(messages: list[dict]) -> str:
        """Extract tool call results from messages into readable text."""
        parts = []
        for msg in messages:
            if msg.get("role") == "tool":
                parts.append(msg.get("content", ""))
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    name = fn.get("name", "unknown")
                    args = fn.get("arguments", "{}")
                    parts.append(f"Called: {name}({args})")
        return "\n\n".join(parts) if parts else "(no tool results)"

    def _append_assistant(
        self, messages: list[dict], msg: Any,
    ) -> None:
        """Append an assistant message (with optional tool_calls) to the message list."""
        entry: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:
            entry["tool_calls"] = [
                tc.model_dump() for tc in msg.tool_calls
            ]
        messages.append(entry)

    @staticmethod
    async def run_enrichment(
        db: Any,
        question: str,
        sport: str,
        top_k: int = 10,
    ) -> str:
        """Search pgvector for relevant articles and get a relevance summary from DeepSeek.

        Returns an empty string if no relevant articles found.
        """
        from app.ingestion.pgvector_search import search_articles

        articles = await search_articles(
            db, question, top_k=top_k, sport=sport,
        )
        if not articles:
            logger.info("No articles found for enrichment")
            return ""

        articles_text = "\n\n".join(
            f"ARTICLE {i + 1}:\n"
            f"Title: {a.get('title', 'Untitled')}\n"
            f"Source: {a.get('source_name', 'Unknown')}\n"
            f"Date: {a.get('published_at', '')}\n"
            f"Content: {a.get('text', '')[:2500]}"
            for i, a in enumerate(articles[:top_k])
        )

        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
            timeout=30.0,
        )

        summary_response = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a research assistant helping a sports handicapper. "
                        "Given a question and several recent articles, extract and "
                        "summarize ONLY information that is directly relevant to the "
                        "question. Be concise — just the facts. If nothing is relevant, "
                        "say 'No relevant information found.'"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nArticles:\n{articles_text}",
                },
            ],
            temperature=0.3,
            max_tokens=2048,
        )

        return summary_response.choices[0].message.content or ""
