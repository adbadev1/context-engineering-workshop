"""Full 19-step CC-AutoDevOps swarm demo runner.

Executes the complete PLAN → ACT → VERIFY flow:
  1. Human creates objective
  2. Router scores keywords, assigns agent
  3-6. Planner (#silly) creates tasks via PLAN phase
  7. State written to DynamoDB graph
  8-9. Event triggers worker dispatch
  10. Worker picks up task
  11-13. Worker ACTs (via Bedrock or deterministic)
  14. Evidence uploaded to S3
  15-16. Completion triggers VERIFY
  17-18. Verifier (#silly) runs 3-level verification
  19. Task promoted to CLOSED

Usage:
    make swarm-demo GOAL="Build an API endpoint for user auth"
    make swarm-demo GOAL="Create ML training pipeline" USE_BEDROCK=1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from src.orchestrator.swarm_orchestrator import SwarmConfig, SwarmResult, run_swarm


_BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║          CC-AutoDevOps × Context Engineering Swarm Demo         ║
║                  AWS Generative AI Hackathon                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

_PHASE_ICONS = {
    "INIT": "🎯",
    "ROUTE": "🔀",
    "PLAN": "📋",
    "DISPATCH": "📨",
    "ACT": "⚡",
    "VERIFY": "✅",
}


def _print_event(event: dict[str, Any]) -> None:
    """Pretty-print a swarm event."""
    phase = str(event.get("phase", ""))
    icon = _PHASE_ICONS.get(phase, "•")
    agent = str(event.get("agent_id", ""))
    etype = str(event.get("event_type", ""))
    step = event.get("step", "?")

    print(f"  {icon} Step {step:>2} │ {phase:<8} │ {agent:<10} │ {etype}")

    data = event.get("data", {})
    # Show key details inline
    if etype == "agent_assigned":
        print(f"          │ Agent: {data.get('assigned_agent')} "
              f"(confidence={data.get('confidence')})")
        print(f"          │ Rationale: {data.get('rationale')}")
    elif etype == "plan_completed":
        tasks = data.get("tasks_created", [])
        print(f"          │ Tasks created: {len(tasks)}")
        for tid in tasks:
            print(f"          │   → {tid}")
    elif etype == "worker_dispatched":
        print(f"          │ Task {data.get('task_id')} → {data.get('worker')} "
              f"(confidence={data.get('confidence')})")
    elif etype == "task_completed":
        artifact = data.get("artifact", {})
        if isinstance(artifact, dict) and artifact.get("s3_uri"):
            print(f"          │ Artifact: {artifact['s3_uri']}")
    elif etype == "verification_complete":
        status = data.get("overall_status", "unknown")
        levels = data.get("levels", {})
        l1 = levels.get("level_1_contract", {}).get("status", "?")
        l2 = levels.get("level_2_runtime", {}).get("status", "?")
        l3 = levels.get("level_3_objective", {}).get("status", "?")
        print(f"          │ Result: {status.upper()} "
              f"(L1:{l1} L2:{l2} L3:{l3})")


def _print_summary(result: SwarmResult) -> None:
    """Print final summary."""
    status = "PASS ✓" if result.ok else "FAIL ✗"
    print(f"\n{'─' * 66}")
    print(f"  Result: {status}")
    print(f"  Session: {result.session_id}")
    print(f"  Tasks created: {len(result.tasks_created)}")
    print(f"  Tasks verified: {len(result.tasks_verified)}")
    print(f"  Tasks failed: {len(result.tasks_failed)}")
    if result.error:
        print(f"  Error: {result.error}")
    print(f"{'─' * 66}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the full 19-step CC-AutoDevOps swarm demo."
    )
    parser.add_argument(
        "--goal", required=True,
        help="The goal/task description for the swarm to accomplish.",
    )
    parser.add_argument(
        "--session", default="",
        help="Session ID (auto-generated if not provided).",
    )
    parser.add_argument(
        "--use-bedrock", action="store_true", default=False,
        help="Use Bedrock harness for agent reasoning (slower, uses API).",
    )
    parser.add_argument(
        "--max-steps", type=int, default=6,
        help="Max Bedrock harness steps per phase (default: 6).",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=400,
        help="Max tokens per Bedrock response (default: 400).",
    )
    parser.add_argument(
        "--output", default="",
        help="Write JSON result to this file path.",
    )
    args = parser.parse_args()

    print(_BANNER)
    print(f"  Goal: {args.goal}")
    print(f"  Bedrock: {'ON' if args.use_bedrock else 'OFF (deterministic demo)'}")
    print(f"{'─' * 66}")
    print()

    config = SwarmConfig(
        goal=args.goal,
        session_id=args.session or "",
        use_bedrock=args.use_bedrock,
        max_bedrock_steps=args.max_steps,
        max_tokens=args.max_tokens,
    )

    result = run_swarm(config)

    # Print event timeline
    print("  Event Timeline:")
    print(f"  {'─' * 60}")
    for event in result.events:
        _print_event(event)
    print()

    _print_summary(result)

    # Write JSON output
    output_path = args.output or "logs/swarm-demo-last.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    print(f"\n  Full output: {output_path}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
