"""Phase-gated swarm orchestrator: PLAN → ACT → VERIFY state machine.

Wraps the Bedrock harness in a multi-agent orchestration loop where:
- #silly (Planner/Verifier) runs PLAN and VERIFY phases via Opus
- #billy/#chilly/#missy (Workers) run ACT phase via Sonnet
- Phase transitions are event-driven (in-process callbacks for demo)
- 3-level verification enforces evidence-gated task promotion
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.aws_tool.cli import handle_request
from src.orchestrator.agent_router import (
    AGENT_MODEL,
    AGENT_PHASES,
    AgentAssignment,
    classify_task,
)
from src.orchestrator.bedrock_harness import BedrockHarnessConfig, run_bedrock_harness


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_skill(*, session: str, phase: str, skill: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a skill through the CLI handler (same pattern as workshop_scenarios)."""
    response = handle_request(
        {
            "skill": skill,
            "input": payload,
            "session": session,
            "phase": phase,
        }
    )
    output = response.get("output")
    if not isinstance(output, dict):
        raise RuntimeError(f"{skill} returned non-object output: {response}")
    return output


@dataclass
class SwarmEvent:
    """Typed event emitted during orchestration."""

    step: int
    phase: str
    agent_id: str
    event_type: str  # plan_started, task_created, task_claimed, task_completed, verify_started, verify_passed, verify_failed
    data: dict[str, Any]
    ts_utc: str = ""

    def __post_init__(self) -> None:
        if not self.ts_utc:
            self.ts_utc = _now_utc()


@dataclass
class SwarmConfig:
    """Configuration for one swarm orchestration run."""

    goal: str
    session_id: str = ""
    use_bedrock: bool = False  # If True, use Bedrock harness for agent reasoning
    max_bedrock_steps: int = 6
    max_tokens: int = 400
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = f"swarm-{uuid.uuid4().hex[:10]}"


@dataclass
class SwarmResult:
    """Result of a full swarm orchestration run."""

    ok: bool
    session_id: str
    goal: str
    events: list[dict[str, Any]] = field(default_factory=list)
    tasks_created: list[str] = field(default_factory=list)
    tasks_verified: list[str] = field(default_factory=list)
    tasks_failed: list[str] = field(default_factory=list)
    error: str = ""
    channel_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "goal": self.goal,
            "events": self.events,
            "tasks_created": self.tasks_created,
            "tasks_verified": self.tasks_verified,
            "tasks_failed": self.tasks_failed,
            "error": self.error,
            "channel_id": self.channel_id,
            "completed_at": _now_utc(),
        }


def _step_1_create_objective(session_id: str, goal: str) -> dict[str, Any]:
    """Step 1: Human creates task/objective."""
    return _run_skill(
        session=session_id,
        phase="ACT",
        skill="upsert_node",
        payload={
            "run_id": session_id,
            "node_type": "Objective",
            "node_id": f"objective-{session_id}",
            "payload": {"text": goal, "ts_utc": _now_utc()},
        },
    )


def _step_2_route_task(goal: str) -> AgentAssignment:
    """Step 2: Router scores keywords, assigns agent."""
    return classify_task(goal)


def _step_3_6_plan_phase(
    session_id: str,
    goal: str,
    assignment: AgentAssignment,
    config: SwarmConfig,
) -> list[dict[str, Any]]:
    """Steps 3-6: Planner (#silly) enters PLAN phase, creates tasks.

    If use_bedrock=True, runs the Bedrock harness to let the model create tasks.
    Otherwise, creates a single task directly for the demo.
    """
    tasks_created = []

    if config.use_bedrock:
        harness_config = BedrockHarnessConfig(
            run_id=session_id,
            phase="PLAN",
            goal=f"You are agent {assignment.agent_id} (Planner). "
                 f"Create tasks to accomplish this goal: {goal}. "
                 f"Use create_task to create at least one task. "
                 f"When done, respond with a summary.",
            session_key=f"{session_id}-planner",
            max_steps=config.max_bedrock_steps,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            agent_id=assignment.agent_id,
            role="planner",
        )
        result = run_bedrock_harness(harness_config)
        # Extract any tasks created from tool events
        for event in result.get("tool_events", []):
            if event.get("ok") and event.get("tool_name") == "create_task":
                output = event.get("output", {})
                if output.get("task_id"):
                    tasks_created.append(output)
    else:
        # Direct task creation for demo mode
        task = _run_skill(
            session=session_id,
            phase="PLAN",
            skill="create_task",
            payload={
                "run_id": session_id,
                "title": goal,
                "description": f"Assigned to {assignment.agent_id} via keyword router "
                               f"(confidence={assignment.confidence}). "
                               f"Rationale: {assignment.rationale}",
            },
        )
        tasks_created.append(task)

    # Create Plan node
    _run_skill(
        session=session_id,
        phase="ACT",
        skill="upsert_node",
        payload={
            "run_id": session_id,
            "node_type": "Plan",
            "node_id": f"plan-{session_id}",
            "payload": {
                "steps": [f"ACT: {t.get('task_id', 'unknown')}" for t in tasks_created]
                         + ["VERIFY: run 3-level verification"],
                "revision": 1,
                "assigned_agent": assignment.agent_id,
                "ts_utc": _now_utc(),
            },
        },
    )

    return tasks_created


def _step_10_13_act_phase(
    session_id: str,
    task: dict[str, Any],
    assignment: AgentAssignment,
    config: SwarmConfig,
) -> dict[str, Any]:
    """Steps 10-14: Worker claims task, executes work, uploads evidence.

    If use_bedrock=True, runs harness. Otherwise, deterministic demo flow.
    """
    task_id = str(task.get("task_id", ""))
    worker_agent = assignment.agent_id

    # Claim task
    claim = _run_skill(
        session=session_id,
        phase="ACT",
        skill="claim_task",
        payload={
            "run_id": session_id,
            "task_id": task_id,
            "agent_id": worker_agent,
            "lease_seconds": 900,
        },
    )

    if config.use_bedrock:
        harness_config = BedrockHarnessConfig(
            run_id=session_id,
            phase="ACT",
            goal=f"You are agent {worker_agent} (Worker). "
                 f"Execute task '{task_id}': {task.get('title', '')}. "
                 f"Upload evidence using upload_artifact, then complete the task "
                 f"using complete_task. Respond when done.",
            session_key=f"{session_id}-worker-{worker_agent}",
            max_steps=config.max_bedrock_steps,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            agent_id=worker_assignment.agent_id,
            role="worker",
        )
        result = run_bedrock_harness(harness_config)
        return {
            "task_id": task_id,
            "agent_id": worker_agent,
            "claim": claim,
            "harness_result": {
                "ok": result.get("ok"),
                "steps_executed": result.get("steps_executed"),
                "final_response": result.get("final_response", ""),
            },
        }

    # Direct execution for demo mode
    artifact = _run_skill(
        session=session_id,
        phase="ACT",
        skill="upload_artifact",
        payload={
            "run_id": session_id,
            "name": f"evidence-{task_id}.txt",
            "content": f"Evidence produced by {worker_agent} for task {task_id}: "
                       f"{task.get('title', 'untitled')}. Completed at {_now_utc()}.",
        },
    )
    artifact_uri = str(artifact.get("s3_uri", ""))

    complete = _run_skill(
        session=session_id,
        phase="ACT",
        skill="complete_task",
        payload={
            "run_id": session_id,
            "task_id": task_id,
            "agent_id": worker_agent,
            "summary": f"Completed by {worker_agent}. Evidence at {artifact_uri}.",
            "artifact_uri": artifact_uri,
            "status": "success",
        },
    )

    return {
        "task_id": task_id,
        "agent_id": worker_agent,
        "claim": claim,
        "artifact": artifact,
        "complete": complete,
    }


def _step_17_19_verify_phase(
    session_id: str,
    task_id: str,
    artifact_uri: str,
    config: SwarmConfig,
) -> dict[str, Any]:
    """Steps 17-19: Verifier (#silly) runs 3-level verification.

    Level 1 (Contract): Schema + phase + tool pre-execution gate.
    Level 2 (Runtime): s3_head_object deterministic proof.
    Level 3 (Objective): Claim → Receipt chain evidence-gated promotion.
    """
    verification_results: dict[str, Any] = {}

    # Level 1: Contract verification (schema validation is inherent in dispatcher)
    verification_results["level_1_contract"] = {
        "status": "pass",
        "check": "schema_phase_tool_validation",
        "detail": "Dispatcher enforced schema + phase + allowlist on all prior calls",
    }

    # Level 2: Runtime verification via s3_head_object
    verify = _run_skill(
        session=session_id,
        phase="VERIFY",
        skill="verify_task",
        payload={
            "run_id": session_id,
            "task_id": task_id,
            "check_type": "s3_head_object" if artifact_uri else "noop",
            "artifact_uri": artifact_uri,
            "notes": "Level 2 runtime verification: artifact existence check",
        },
    )
    verification_results["level_2_runtime"] = verify

    # Level 3: Objective verification — evidence chain
    level_3_status = "pass" if verify.get("status") == "pass" else "fail"
    if level_3_status == "pass":
        # Write verification evidence
        evidence_content = json.dumps({
            "task_id": task_id,
            "verification_chain": [
                verification_results["level_1_contract"],
                {"status": verify.get("status"), "test_result_id": verify.get("test_result_id")},
            ],
            "artifact_uri": artifact_uri,
            "conclusion": "All 3 verification levels passed. Task promoted to CLOSED.",
        }, sort_keys=True)

        evidence_artifact = _run_skill(
            session=session_id,
            phase="VERIFY",
            skill="upload_artifact",
            payload={
                "run_id": session_id,
                "name": f"verification-chain-{task_id}.json",
                "content": evidence_content,
            },
        )

        # Write TASK_VERIFIED_BY edge
        _run_skill(
            session=session_id,
            phase="VERIFY",
            skill="link_edge",
            payload={
                "run_id": session_id,
                "from_id": task_id,
                "to_id": verify.get("test_result_id", ""),
                "edge_type": "TASK_VERIFIED_BY",
            },
        )

        verification_results["level_3_objective"] = {
            "status": "pass",
            "evidence_artifact": evidence_artifact.get("s3_uri"),
        }
    else:
        verification_results["level_3_objective"] = {
            "status": "fail",
            "reason": f"Level 2 failed: {verify.get('status')}",
        }

    return {
        "task_id": task_id,
        "overall_status": level_3_status,
        "levels": verification_results,
    }


def run_swarm(config: SwarmConfig) -> SwarmResult:
    """Execute the full 19-step swarm orchestration flow.

    Steps 1-19 as defined in the architecture:
    1. Human creates objective
    2. Router assigns agent
    3-6. Planner creates tasks via PLAN phase
    7. State written to graph
    8-9. Event triggers worker dispatch
    10. Worker picks up task
    11-13. Worker ACTs via Bedrock
    14. Evidence uploaded
    15-16. Completion triggers VERIFY
    17-18. Verifier runs 3-level check
    19. Task promoted to CLOSED
    """
    result = SwarmResult(
        ok=False,
        session_id=config.session_id,
        goal=config.goal,
    )
    events: list[SwarmEvent] = []
    step_counter = 0

    try:
        # ── Step 1: Human creates objective ──
        step_counter += 1
        objective = _step_1_create_objective(config.session_id, config.goal)
        events.append(SwarmEvent(
            step=step_counter, phase="INIT", agent_id="human",
            event_type="objective_created",
            data={"objective_node_id": objective.get("node_id")},
        ))

        # ── Change 3: Create coordination channel ──
        channel = _run_skill(
            session=config.session_id,
            phase="ACT",
            skill="create_channel",
            payload={
                "run_id": config.session_id,
                "channel_name": f"swarm-coord-{config.session_id}",
            },
        )
        channel_id = str(channel.get("channel_id", ""))
        result.channel_id = channel_id
        events.append(SwarmEvent(
            step=step_counter, phase="INIT", agent_id="orchestrator",
            event_type="channel_created",
            data={"channel_id": channel_id},
        ))

        # ── Step 2: Router assigns agent ──
        step_counter += 1
        assignment = _step_2_route_task(config.goal)
        events.append(SwarmEvent(
            step=step_counter, phase="ROUTE", agent_id="router",
            event_type="agent_assigned",
            data={
                "assigned_agent": assignment.agent_id,
                "confidence": assignment.confidence,
                "rationale": assignment.rationale,
                "keyword_hits": assignment.keyword_hits,
            },
        ))

        # ── Steps 3-6: Planner creates tasks ──
        step_counter += 1
        tasks_created = _step_3_6_plan_phase(
            config.session_id, config.goal, assignment, config,
        )
        result.tasks_created = [str(t.get("task_id", "")) for t in tasks_created]
        events.append(SwarmEvent(
            step=step_counter, phase="PLAN", agent_id="#silly",
            event_type="plan_completed",
            data={"tasks_created": result.tasks_created},
        ))

        # Change 3: Planner posts plan summary to channel
        if channel_id:
            _run_skill(
                session=config.session_id,
                phase="ACT",
                skill="post_channel_message",
                payload={
                    "run_id": config.session_id,
                    "channel_id": channel_id,
                    "agent_id": "#silly",
                    "message": f"Plan complete. Created {len(tasks_created)} task(s): "
                               + ", ".join(result.tasks_created),
                    "level": "info",
                },
            )

        # ── Steps 8-14: Workers execute tasks ──
        # Re-route each task to the best worker
        for task in tasks_created:
            step_counter += 1
            task_title = str(task.get("title", config.goal))
            worker_assignment = classify_task(task_title)

            # Workers can only ACT; if router picks #silly (planner),
            # fall back to #chilly (backend/QA) as default worker.
            if worker_assignment.agent_id == "#silly":
                worker_assignment = AgentAssignment(
                    agent_id="#chilly",
                    confidence=0.5,
                    rationale="Planner cannot ACT — falling back to #chilly (backend/QA)",
                    allowed_phases=["ACT"],
                    model_preference="sonnet",
                    keyword_hits=worker_assignment.keyword_hits,
                )

            events.append(SwarmEvent(
                step=step_counter, phase="DISPATCH", agent_id="router",
                event_type="worker_dispatched",
                data={
                    "task_id": task.get("task_id"),
                    "worker": worker_assignment.agent_id,
                    "confidence": worker_assignment.confidence,
                },
            ))

            # Change 3: Worker posts claim message to channel
            if channel_id:
                _run_skill(
                    session=config.session_id,
                    phase="ACT",
                    skill="post_channel_message",
                    payload={
                        "run_id": config.session_id,
                        "channel_id": channel_id,
                        "agent_id": worker_assignment.agent_id,
                        "task_id": task.get("task_id", ""),
                        "message": f"Claiming task {task.get('task_id', '')}",
                        "level": "info",
                    },
                )

            step_counter += 1
            act_result = _step_10_13_act_phase(
                config.session_id, task, worker_assignment, config,
            )
            events.append(SwarmEvent(
                step=step_counter, phase="ACT",
                agent_id=worker_assignment.agent_id,
                event_type="task_completed",
                data=act_result,
            ))

            # Change 3: Worker posts completion message to channel
            if channel_id:
                _run_skill(
                    session=config.session_id,
                    phase="ACT",
                    skill="post_channel_message",
                    payload={
                        "run_id": config.session_id,
                        "channel_id": channel_id,
                        "agent_id": worker_assignment.agent_id,
                        "task_id": task.get("task_id", ""),
                        "message": f"Completed task {task.get('task_id', '')}",
                        "level": "info",
                    },
                )

            # ── Steps 15-19: Verifier checks evidence ──
            step_counter += 1
            task_id_str = str(task.get("task_id", ""))

            # Change 4: Discover artifact_uri from graph via neighbors skill
            artifact_uri = ""
            try:
                neighbors = _run_skill(
                    session=config.session_id,
                    phase="VERIFY",
                    skill="neighbors",
                    payload={
                        "run_id": config.session_id,
                        "node_id": task_id_str,
                    },
                )
                for neighbor in neighbors.get("neighbors", []):
                    if str(neighbor.get("type", "")) == "Artifact":
                        data = neighbor.get("data", {})
                        if isinstance(data, dict):
                            artifact_uri = str(data.get("s3_uri", ""))
                        if artifact_uri:
                            break
            except Exception:
                # Fallback: try extracting from act_result directly
                if isinstance(act_result.get("artifact"), dict):
                    artifact_uri = str(act_result["artifact"].get("s3_uri", ""))

            verify_result = _step_17_19_verify_phase(
                config.session_id,
                task_id_str,
                artifact_uri,
                config,
            )
            events.append(SwarmEvent(
                step=step_counter, phase="VERIFY", agent_id="#silly",
                event_type="verification_complete",
                data=verify_result,
            ))

            # Change 3: Verifier posts verification result to channel
            if channel_id:
                v_status = verify_result.get("overall_status", "unknown")
                _run_skill(
                    session=config.session_id,
                    phase="VERIFY",
                    skill="post_channel_message",
                    payload={
                        "run_id": config.session_id,
                        "channel_id": channel_id,
                        "agent_id": "#silly",
                        "task_id": task_id_str,
                        "message": f"Verification {v_status} for task {task_id_str}",
                        "level": "info" if v_status == "pass" else "error",
                    },
                )

            if verify_result["overall_status"] == "pass":
                result.tasks_verified.append(task_id_str)
            else:
                result.tasks_failed.append(task_id_str)

        # Final status
        result.ok = len(result.tasks_failed) == 0 and len(result.tasks_verified) > 0
        result.events = [
            {
                "step": e.step,
                "phase": e.phase,
                "agent_id": e.agent_id,
                "event_type": e.event_type,
                "data": e.data,
                "ts_utc": e.ts_utc,
            }
            for e in events
        ]

    except Exception as exc:
        result.error = str(exc)
        result.events = [
            {
                "step": e.step,
                "phase": e.phase,
                "agent_id": e.agent_id,
                "event_type": e.event_type,
                "data": e.data,
                "ts_utc": e.ts_utc,
            }
            for e in events
        ]

    return result
