"""Keyword-scored agent router for the CC-AutoDevOps swarm.

Maps task descriptions to the best-fit agent using domain keyword scoring,
ported from master_control's orchestrator classifyTask() algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Agent domain keywords — 25+ per agent, matching master_control definitions.
AGENT_KEYWORDS: dict[str, list[str]] = {
    "#silly": [
        "gis", "architecture", "cloud", "infrastructure", "coordination",
        "pmo", "design", "system", "plan", "orchestrate", "verify",
        "validate", "review", "approve", "coordinate", "strategy",
        "objective", "decision", "scope", "tradeoff", "constraint",
        "milestone", "dependency", "risk", "governance", "audit",
    ],
    "#billy": [
        "ml", "ai", "analysis", "feature", "model", "training",
        "data", "science", "inference", "bedrock", "embedding",
        "classification", "prediction", "nlp", "rag", "vector",
        "prompt", "llm", "fine-tune", "dataset", "evaluation",
        "metric", "benchmark", "accuracy", "recall", "precision",
    ],
    "#chilly": [
        "backend", "qa", "testing", "deployment", "api", "devops",
        "database", "migration", "endpoint", "server", "lambda",
        "dynamodb", "s3", "sqs", "iam", "ci", "cd", "pipeline",
        "docker", "container", "health", "monitoring", "pytest",
        "integration", "unit", "regression",
    ],
    "#missy": [
        "frontend", "ux", "dashboard", "visualization", "marketing",
        "ui", "css", "react", "component", "layout", "chart",
        "graph", "display", "render", "style", "theme", "animation",
        "responsive", "accessibility", "wcag", "page", "panel",
        "widget", "icon", "color",
    ],
}

# Role-to-phase mapping: which phases each agent role can execute.
AGENT_PHASES: dict[str, list[str]] = {
    "#silly": ["PLAN", "VERIFY"],
    "#billy": ["ACT"],
    "#chilly": ["ACT"],
    "#missy": ["ACT"],
}

# Model preference per agent.
AGENT_MODEL: dict[str, str] = {
    "#silly": "opus",
    "#billy": "sonnet",
    "#chilly": "sonnet",
    "#missy": "sonnet",
}


@dataclass
class AgentAssignment:
    """Result of routing a task to an agent."""

    agent_id: str
    confidence: float
    rationale: str
    allowed_phases: list[str]
    model_preference: str
    keyword_hits: dict[str, int]


def classify_task(description: str) -> AgentAssignment:
    """Score a task description against agent domain keywords.

    Algorithm:
    1. Lowercase the description.
    2. Count keyword hits per agent.
    3. Sort by score descending.
    4. Confidence = best_score / total_score (capped at 1.0).
    5. If no keywords match: default to #silly with confidence 0.25.
    """
    desc_lower = description.lower()
    scores: dict[str, int] = {}

    for agent_id, keywords in AGENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in desc_lower)
        scores[agent_id] = hits

    total_score = sum(scores.values())
    if total_score == 0:
        return AgentAssignment(
            agent_id="#silly",
            confidence=0.25,
            rationale="No keyword match — defaulting to #silly (architect/coordinator)",
            allowed_phases=AGENT_PHASES["#silly"],
            model_preference=AGENT_MODEL["#silly"],
            keyword_hits=scores,
        )

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_agent, best_score = ranked[0]
    confidence = min(best_score / total_score, 1.0)

    # Build rationale showing top 3 keyword matches.
    matched_keywords = [
        kw for kw in AGENT_KEYWORDS[best_agent] if kw in desc_lower
    ][:5]

    return AgentAssignment(
        agent_id=best_agent,
        confidence=round(confidence, 3),
        rationale=f"Matched {best_score}/{total_score} keywords: {', '.join(matched_keywords)}",
        allowed_phases=AGENT_PHASES[best_agent],
        model_preference=AGENT_MODEL[best_agent],
        keyword_hits=scores,
    )


def route_to_workers(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Route a batch of tasks to agents. Returns tasks annotated with assignments."""
    results = []
    for task in tasks:
        title = str(task.get("title", ""))
        desc = str(task.get("description", ""))
        combined = f"{title} {desc}"
        assignment = classify_task(combined)
        results.append({
            **task,
            "assigned_agent": assignment.agent_id,
            "confidence": assignment.confidence,
            "rationale": assignment.rationale,
            "allowed_phases": assignment.allowed_phases,
            "model_preference": assignment.model_preference,
        })
    return results
