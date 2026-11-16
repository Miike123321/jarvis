from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

PROMPT_TEMPLATE_FIELDS = {
    "diagnostics": {
        "child.age_months",
        "child.gender",
        "child.language",
        "child.interests",
        "child.skills",
        "child.skill_gaps",
        "child.diagnostic_results",
        "diagnostics.excluded_skill_ids",
        "diagnostics.excluded_questions",
    },
    "daily_plan_task1": {
        "child.age_months",
        "child.gender",
        "child.language",
        "child.interests",
        "child.skills",
        "child.skill_gaps",
        "child.diagnostic_results",
        "plan.previous_difficulty",
        "plan.recent_components",
    },
    "daily_plan_task2": {
        "child.age_months",
        "child.gender",
        "child.language",
        "child.interests",
        "child.skills",
        "child.skill_gaps",
        "child.diagnostic_results",
        "plan.previous_difficulty",
        "plan.recent_components",
    },
    "daily_plan_advice": {
        "child.age_months",
        "child.gender",
        "child.language",
        "child.interests",
        "child.skills",
        "child.skill_gaps",
        "child.diagnostic_results",
        "plan.previous_difficulty",
        "plan.recent_components",
    },
}


def render_prompt_template(template: str, context: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise ValueError(f"Unknown prompt placeholder: {name}")
        value = context[name]
        if value is None or value == "":
            return "не задано"
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return _PLACEHOLDER.sub(replace, template)


def validate_prompt_template(template: str, prompt_name: str) -> None:
    allowed = PROMPT_TEMPLATE_FIELDS.get(prompt_name)
    if allowed is None:
        return
    for match in _PLACEHOLDER.finditer(template):
        if match.group(1) not in allowed:
            raise ValueError(f"Unknown prompt placeholder: {match.group(1)}")
