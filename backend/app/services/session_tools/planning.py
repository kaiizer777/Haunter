"""
Planning and Clarification Tools — Cloud Agentic Live Session Phase 6.

Enables interactive agent autonomy:
  1. update_plan: updates and validates a live multi-step task checklist.
  2. ask_user_clarification: requests user decision on architectural trade-offs
     and pauses agent execution until user responds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentSession
from app.services.session_streamer import SseQueue

logger = logging.getLogger(__name__)

_VALID_TASK_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})


async def tool_update_plan(
    tasks: list[dict[str, Any]],
    session: AgentSession,
    queue: SseQueue,
    db: AsyncSession,
) -> str:
    """
    Validate and update the session's live execution plan checklist.

    Args:
        tasks: List of task dicts, each with 'id', 'title', and 'status'.
        session: Active AgentSession ORM object.
        queue: SseQueue for emitting live plan_update SSE events.
        db: Async database session.

    Returns:
        Status message string for the LLM.
    """
    if not isinstance(tasks, list):
        return "Error: tasks must be a list of task objects."

    validated_tasks: list[dict[str, Any]] = []
    for idx, t in enumerate(tasks):
        if not isinstance(t, dict):
            return f"Error: task at index {idx} must be a dictionary."
        task_id = str(t.get("id", "")).strip()
        title = str(t.get("title", "")).strip()
        status = str(t.get("status", "")).strip()

        if not task_id:
            return f"Error: task at index {idx} has an empty 'id'."
        if not title:
            return f"Error: task at index {idx} has an empty 'title'."
        if status not in _VALID_TASK_STATUSES:
            return (
                f"Error: task '{task_id}' has invalid status {status!r}. "
                f"Allowed: {sorted(_VALID_TASK_STATUSES)}"
            )

        validated_tasks.append({
            "id": task_id,
            "title": title,
            "status": status,
        })

    session.plan = validated_tasks
    await queue.put_plan_update(validated_tasks)
    return f"Plan updated with {len(validated_tasks)} tasks."


async def tool_ask_user_clarification(
    question: str,
    options: list[str],
    session: AgentSession,
    queue: SseQueue,
    db: AsyncSession,
) -> str:
    """
    Pause the agent turn to request user clarification on trade-offs.

    Args:
        question: Non-empty question string.
        options: Non-empty list of option strings.
        session: Active AgentSession ORM object.
        queue: SseQueue for emitting clarification_requested SSE events.
        db: Async database session.

    Returns:
        Status message string for the LLM.
    """
    if not isinstance(question, str) or not question.strip():
        return "Error: question must be a non-empty string."

    if not isinstance(options, list) or not options:
        return "Error: options must be a non-empty list of strings."

    cleaned_options: list[str] = [str(opt).strip() for opt in options if str(opt).strip()]
    if not cleaned_options:
        return "Error: options must contain at least one non-empty string choice."

    session.status = "awaiting_clarification"
    session.waiting_input = {
        "question": question.strip(),
        "options": cleaned_options,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    await queue.put_clarification_requested(question.strip(), cleaned_options)
    return "Clarification requested from user. Execution paused."
