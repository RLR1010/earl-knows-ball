"""Core tool-calling chat engine.

Provides the `ToolChatEngine` class that runs the research loop (DeepSeek calls
tools to query the database) and optionally enrichment (vector search + summary).
"""

import json
import logging
from typing import Any, AsyncGenerator, Callable

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
- Never actively recommend, suggest, or encourage parlays or same-game parlays. Treat singles
  (straight bets) as the default smart strategy, because book vig compounds across parlay legs and
  same-game legs are often correlated — both of which erode expected value. If a user explicitly asks
  you to evaluate a specific parlay, you may honestly analyze it with the tools, but always call out
  negative EV, compounded vig, and correlated/duplicate legs, and never present it as a smart play.
- NEVER suggest chasing losses or increasing bet size after a loss.
- Format responses with clean Markdown for readability: use **bold** for emphasis,
  # or ## for section headers, | tables | for structured data, --- for section
  breaks, lists for bullets, and use emojis as section markers.
- NEVER use *** (triple asterisks). Use **bold** (double asterisks) instead.
- Be direct and opinionated, but back it up with data.
- Keep responses concise — a few focused paragraphs.
- If you don't have data for something, say so.
- The current Central US date/time is provided at the start of each user message.
- Use the date provided at the start of the user message as TODAY ("tonight"/"today"/"this
  week" refer to that date). Remember how old any supplementary info is relative to TODAY.
- When live game/schedule/lines data (from your current data tools) and older background
  material (e.g. articles) disagree about a matchup, schedule, or outcome, the live game
  data is authoritative and current — go with it, and never present dated background
  material as if it describes today's game.
- NEVER narrate the plumbing of how you got your answer. Do not mention or describe: "the
  game-data feed", "the article cycle/corpus", "the database", "data feeds", ES/API
  internals, retrieval/search mechanisms, a "Data-Conflict Flag", or any contradiction
  between your data sources. If sources disagree, reconcile silently using the rule above
  and just answer with the current, correct facts. Only say you're unsure if you genuinely
  have no authoritative data — say it plainly without referencing internal systems."""


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
        reasoning_effort: str | None = None,
    ):
        self.sport = sport
        self.sport_display = sport_display
        self.tools = tools
        self.executor = executor
        self.model = model or settings.deepseek_model
        self.reasoning_effort = reasoning_effort

        # Chat is a reasoning feature — keep thinking ENABLED so live reasoning
        # updates stream and answer quality stays high. Default to 'low' effort:
        # benchmark showed low finishes reliably within budget (5/5 stop), while
        # medium/high can run away and get cut off. A reasoning_effort passed in
        # overrides (used by the benchmark).
        self._chat_extra_body = {"thinking": {"type": "enabled"},"reasoning_effort": reasoning_effort or "low"}

        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            sport=sport_display,
            data_description=data_description,
        )
        if system_prompt_extra:
            self.system_prompt += f"\n\n{system_prompt_extra}"

    async def _chat_create(self, client, messages, extra_body=None, **kwargs):
        """Run a chat completion with thinking enabled at 'low' reasoning.

        If the reasoning pass runs away and comes back cut-off/empty
        (finish_reason=length with no usable content), retry ONCE with thinking
        DISABLED using the SAME messages — so the research already in the prompt
        is preserved and we never pay for a wasted pass. Non-thinking always
        completes, so we always get a usable answer.
        """
        # Strip any model kwarg (call sites pass model=self.model) so it can't
        # collide with the explicit model below — the engine always uses self.model.
        kwargs.pop("model", None)
        body = extra_body or self._chat_extra_body
        kwargs.setdefault("max_tokens", 8192)  # headroom so low reasoning fits
        kwargs.setdefault("temperature", 0.7)
        response = await client.chat.completions.create(
            model=self.model,
            extra_body=body,
            messages=messages,
            **kwargs,
        )

        # Detect a cut-off / empty answer from the reasoning pass.
        choices = getattr(response, "choices", None) or []
        if choices:
            msg = choices[0].message
            content = (msg.content or "").strip()
            finish = getattr(choices[0], "finish_reason", None)
            if not content and finish == "length":
                # Retry with thinking disabled, same messages (research intact).
                response = await client.chat.completions.create(
                    model=self.model,
                    extra_body={"thinking": {"type": "disabled"}},
                    messages=messages,
                    **kwargs,
                )
        return response

    async def research_and_answer(
        self,
        db: Any,
        messages: list[dict],
        max_turns: int = 15,
        reasoning: str | None = None,
        timeout: float = 45.0,
        return_full_messages: bool = False,
        research_only: bool = False,
    ) -> tuple[str, int] | tuple[str, int, list[dict]]:
        """Run the tool-calling research loop and return DeepSeek's final answer.

        Args:
            db: Database session (AsyncSession or sync session).
            messages: List of message dicts.
            max_turns: Maximum tool-calling rounds before forcing a final answer.
            reasoning: Optional reasoning_effort override ("minimal", "low",
                "medium", "high"). If None, uses the engine default.
            timeout: Per-DeepSeek-call timeout in seconds. Interactive chat uses
                45s; heavy workloads (e.g. original-article generation) pass a
                larger value so long tool-calling rounds don't abort.
            return_full_messages: If True, also return the complete conversation
                message list (including all tool results) as a third element, so
                callers can build a deterministic research brief for other
                requests (e.g. accuracy checks with cache-shared prefixes).
            research_only: If True, run the tool loop to gather research but do
                NOT force a final article-writing turn. The returned answer is a
                short research digest; the full tool messages (via
                return_full_messages) are what callers should build the research
                brief from. Used by the two-phase original-article flow so the
                actual writing happens in a separate, deterministic-prefixed
                call that shares the research brief with the accuracy check.

        Returns:
            Tuple of (final answer text, total tokens used) or, when
            return_full_messages is True, (answer, tokens, messages).
        """
        original_answer = ""
        total_tokens = 0

        # Build the chat extra_body once; override reasoning_effort if requested.
        research_extra_body = dict(self._chat_extra_body)
        if reasoning:
            research_extra_body["reasoning_effort"] = reasoning
            # Reasoning must be paired with thinking enabled to take effect.
            research_extra_body["thinking"] = {"type": "enabled"}

        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
                timeout=timeout,
            )

            # First call with tools available
            response = await self._chat_create(client,
                model=self.model,
                extra_body=research_extra_body,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
            )

            if response.usage:
                total_tokens += response.usage.total_tokens

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
                response = await self._chat_create(client,
                    model=self.model,
                    extra_body=research_extra_body,
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                )
                if response.usage:
                    total_tokens += response.usage.total_tokens
                assistant_msg = response.choices[0].message
                self._append_assistant(messages, assistant_msg)

            # If DeepSeek still wants to call tools (hit max_turns), force a final answer
            if not assistant_msg.content and assistant_msg.tool_calls:
                logger.info("Hit max_turns with pending tool calls — forcing final answer")
                # Execute the remaining tool calls to avoid hanging tool_calls
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
                messages.append({"role": "user", "content": "You have all the data you need. Provide your final answer now based on the tool results. Be concise."})
                response = await self._chat_create(client,
                    model=self.model,
                    extra_body=research_extra_body,
                    messages=messages,
                )
                if response.usage:
                    total_tokens += response.usage.total_tokens
                assistant_msg = response.choices[0].message

            if research_only:
                # Two-phase original-article flow: we only wanted the research.
                # Don't treat the model's content as the article; the caller runs a
                # separate deterministic write call from the research brief. Return
                # a compact digest plus the full tool messages.
                answer = (
                    assistant_msg.content or ""
                    or "Research gathered. `return_full_messages` contains the tool trace."
                )
                logger.info("research_and_answer(research_only) total tokens used: %d", total_tokens)
                if return_full_messages:
                    return answer, total_tokens, messages
                return answer, total_tokens

            original_answer = assistant_msg.content or ""
            logger.info("research_and_answer total tokens used: %d", total_tokens)
            if return_full_messages:
                return original_answer, total_tokens, messages
            return original_answer, total_tokens
        except Exception as e:
            logger.warning("research_and_answer error: %s", e)
            if return_full_messages:
                return (
                    f"I was researching your question but hit a snag. Here's what I know so far:\n\n{original_answer}",
                    total_tokens,
                    messages,
                )
            return f"I was researching your question but hit a snag. Here's what I know so far:\n\n{original_answer}", total_tokens


    @staticmethod
    def _describe_tool(tool_call: Any) -> str:
        """Generate a human-readable status message from a tool call."""
        try:
            args = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {}

        name = tool_call.function.name

        # Extract a team/player name for personalization
        subject = (
            args.get("team_name")
            or args.get("team")
            or args.get("home_team")
            or args.get("away_team")
            or args.get("player_name")
            or args.get("first_name")
            or args.get("query", "")
        )

        # Map tool names to natural language descriptions
        verb_map = {
            "search_teams": ("Looking up team info", subject),
            "get_team_info": ("Getting team info", subject),
            "get_team_stats": ("Checking team stats", subject),
            "get_team_batting_stats": ("Checking batting stats", subject),
            "get_team_pitching_stats": ("Checking pitching stats", subject),
            "get_standings": ("Checking standings", ""),
            "get_todays_games": ("Looking at today's games", ""),
            "get_week_games": ("Looking at this week's games", ""),
            "get_game_info": ("Getting game details", ""),
            "get_head_to_head": ("Checking head-to-head history", subject),
            "get_injuries": ("Pulling injury reports", subject),
            "get_depth_chart": ("Checking depth charts", subject),
            "get_player_stats": ("Looking up player stats", subject),
            "get_player_weekly_log": ("Checking weekly logs", subject),
            "get_game_prediction": ("Running model predictions", ""),
            "get_team_schedule": ("Looking up the schedule", subject),
            "get_team_splits": ("Checking team splits", subject),
            "search_articles": ("Searching for news", subject if subject else ""),
            "get_player_game_logs": ("Checking game logs", subject),
        }

        verb, subject = verb_map.get(name, (f"Running {name.replace('_', ' ')}", ""))
        if subject:
            return f"{verb} for {subject}..."
        return f"{verb}..."

    async def research_and_answer_stream(
        self,
        db: Any,
        messages: list[dict],
        max_turns: int = 15,
    ) -> AsyncGenerator[tuple[str, str], None]:
        """
        Same as research_and_answer but yields (type, data) tuples for SSE streaming.

        Yields:
            ("status", message) — progress update for the user
            ("answer", text) — final answer
        """
        original_answer = ""
        total_tokens = 0
        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
                timeout=45.0,
            )

            yield ("status", "Researching your question...")

            # First call with tools available
            response = await self._chat_create(client,
                model=self.model,
                extra_body=self._chat_extra_body,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
            )

            if response.usage:
                total_tokens += response.usage.total_tokens

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
                    yield ("status", self._describe_tool(tool_call))
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
                yield ("status", "Thinking about what I found...")
                response = await self._chat_create(client,
                    model=self.model,
                    extra_body=self._chat_extra_body,
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                )
                if response.usage:
                    total_tokens += response.usage.total_tokens
                assistant_msg = response.choices[0].message
                self._append_assistant(messages, assistant_msg)

            # If DeepSeek still wants to call tools (hit max_turns), force a final answer
            if not assistant_msg.content and assistant_msg.tool_calls:
                logger.info("Hit max_turns with pending tool calls — forcing final answer")
                yield ("status", "One more thing...")
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
                messages.append({"role": "user", "content": "You have all the data you need. Provide your final answer now based on ALL the information you have gathered. Be thorough and cite specific stats, matchups, and trends. Do not call any more tools."})
                response = await self._chat_create(client,
                    model=self.model,
                    extra_body=self._chat_extra_body,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=2048,
                    tool_choice="none",
                )
                if response.usage:
                    total_tokens += response.usage.total_tokens
                assistant_msg = response.choices[0].message

            yield ("status", "Drafting your breakdown...")
            original_answer = assistant_msg.content or ""
            if not original_answer:
                original_answer = "I gathered information about this matchup but ran into an issue generating a full breakdown."
            yield ("answer", original_answer)
            yield ("usage", {"total_tokens": total_tokens})

        except Exception as e:
            logger.exception("research_and_answer_stream error: %s", e, exc_info=True)
            if original_answer:
                yield ("status", "Drafting from what I found...")
                yield ("answer", original_answer)
            else:
                yield ("answer", f"I was researching your question but ran into an error. Let me summarize what I found.")
            yield ("usage", {"total_tokens": total_tokens})

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
        }
        if msg.tool_calls:
            entry["content"] = None
            entry["tool_calls"] = [
                tc.model_dump() for tc in msg.tool_calls
            ]
        else:
            entry["content"] = msg.content or ""
        messages.append(entry)

    @staticmethod
    async def run_enrichment(
        db: Any,
        question: str,
        sport: str,
        top_k: int = 10,
    ) -> tuple[str, int]:
        """Search pgvector for relevant articles and get a relevance summary from DeepSeek.

        Returns a tuple of (summary_text, total_tokens_used) or ("", 0) if no articles.
        """
        from app.ingestion.pgvector_search import search_articles

        articles = await search_articles(
            db, question, top_k=top_k, sport=sport,
        )
        if not articles:
            logger.info("No articles found for enrichment")
            return "", 0

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
            extra_body={
                # Enrichment: thinking enabled at minimal reasoning — enough to
                # reason about relevance without heavy CoT cost.
                "thinking": {"type": "enabled"},
                "reasoning_effort": "minimal",
            },
        )

        enrichment_tokens = summary_response.usage.total_tokens if summary_response.usage else 0
        logger.info("run_enrichment tokens: %d", enrichment_tokens)

        return summary_response.choices[0].message.content or "", enrichment_tokens
