"""
agents/action_schema.py
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

KNOWN_ACTION_TYPES = {
    "create_issue",
    "update_issue",
    "delete_issue",
    "bulk_update_issues",
    "create_project",
    "add_file_to_issue",
    "search_and_update",
    "unsupported",
    "needs_clarification",
}


@dataclass
class Action:
    type: str  # str instead of Literal — runtime doesn't enforce Literal anyway
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class ActionPlan:
    actions: list[Action]
    preamble: str = ""
    requires_confirmation: bool = False
    confirmation_prompt: str = ""


def parse_action_plan(raw: dict) -> ActionPlan:
    """
    Convert the LLM's JSON output into a typed ActionPlan.
    Never raises — unknown or malformed actions are coerced to 'unsupported'.
    """
    if not isinstance(raw, dict):
        logger.error(f"[SCHEMA] parse_action_plan got non-dict: {type(raw)}")
        return _fallback_plan("I received an unexpected response format. Please try again.")

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or len(raw_actions) == 0:
        logger.warning("[SCHEMA] No actions in plan — treating as unsupported")
        return _fallback_plan("I wasn't sure how to handle that request. Could you rephrase it?")

    actions = []
    for i, a in enumerate(raw_actions):
        if not isinstance(a, dict):
            logger.warning(f"[SCHEMA] Action[{i}] is not a dict: {a}")
            continue

        action_type = a.get("type")

        if not action_type or not isinstance(action_type, str):
            logger.warning(f"[SCHEMA] Action[{i}] missing or invalid 'type': {a}")
            actions.append(Action(
                type="unsupported",
                params={"reason": "I couldn't determine what action to take. Please rephrase your request."},
                description="Missing action type",
            ))
            continue

        if action_type not in KNOWN_ACTION_TYPES:
            logger.warning(f"[SCHEMA] Action[{i}] unknown type '{action_type}' — coercing to unsupported")
            actions.append(Action(
                type="unsupported",
                params={"reason": f"The action '{action_type}' isn't something I can do right now."},
                description=a.get("description", ""),
            ))
            continue

        actions.append(Action(
            type=action_type,
            params=a.get("params") if isinstance(a.get("params"), dict) else {},
            description=a.get("description", ""),
        ))

    if not actions:
        return _fallback_plan("I wasn't able to plan any actions for that request.")

    return ActionPlan(
        actions=actions,
        preamble=raw.get("preamble", "") if isinstance(raw.get("preamble"), str) else "",
        requires_confirmation=bool(raw.get("requires_confirmation", False)),
        confirmation_prompt=raw.get("confirmation_prompt", "") if isinstance(
            raw.get("confirmation_prompt"), str) else "",
    )


def _fallback_plan(reason: str) -> ActionPlan:
    """Returns a safe unsupported plan — used when parsing fails entirely."""
    return ActionPlan(
        actions=[Action(
            type="unsupported",
            params={"reason": reason},
            description="Fallback due to parse failure",
        )]
    )
