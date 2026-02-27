---
type: guide
title: Hackathon Session Insights — Swarm System Improvements
created: 2026-02-26
status: implemented
---

# Hackathon Session Insights — Swarm System Improvements

Raw notes from Anthropic, OpenAI, Manus, and Codex hackathon sessions revealed 7 gaps in the swarm orchestrator implementation. All 7 have been implemented and verified.

## Summary

| # | Improvement | Priority | Insight Source | Files Changed |
|---|-------------|----------|----------------|---------------|
| 1 | Agent Identity Recitation | HIGH | "Keep reciting something it will learn" | `bedrock_harness.py`, `swarm_orchestrator.py` |
| 2 | Explicit Failure Feedback | HIGH | "Give models their mistakes so they don't repeat them" | `bedrock_harness.py` |
| 3 | Channel-Based Inter-Agent Communication | HIGH | "Agents talk and communicate with each other" | `swarm_orchestrator.py`, `skills/post_channel_message.yml` |
| 4 | Graph-Observed Verifier State | MEDIUM-HIGH | "Super memory = shared state = after passing checks" | `swarm_orchestrator.py` |
| 5 | Bedrock Prompt Caching | MEDIUM | "Use KV Cache" | `src/aws_tool/dispatcher.py` |
| 6 | Agent-Identity-Gated Tool Rights | MEDIUM | "Hard guardrails / role-scoped tool rights" | `src/aws_tool/dispatcher.py`, `src/orchestrator/tool_adapters.py` |
| 7 | Agent Traces Endpoint | MEDIUM | "Agent traces — recently introduced by Cursor" | `src/tools/harness_gui_server.py` |

---

## Change 1 — Agent Identity Recitation

**Problem:** Identity fades over `max_steps=6`. The model loses track of which agent it is, what phase it's in, and what constraints apply.

**Solution:** Added `agent_id` and `role` fields to `BedrockHarnessConfig`. When `agent_id` is set, `_build_step_prompt` prepends an `<agent_identity>` block (~50 tokens/step) containing the agent name, role, allowed phases, and current phase constraint.

**Prompt output example:**

```
<agent_identity>
Agent: #silly
Role: planner
Allowed phases: PLAN, VERIFY
Current phase: PLAN (you MUST stay within this phase)
</agent_identity>
```

**Propagation:** `swarm_orchestrator.py` passes `agent_id` and `role` when constructing `BedrockHarnessConfig` in both `_step_3_6_plan_phase` (planner) and `_step_10_13_act_phase` (worker).

---

## Change 2 — Explicit Failure Feedback

**Problem:** Tool failures were buried in the raw JSON blob of recent events. Models would repeat the same failing call.

**Solution:** In `_build_step_prompt`, the last 3 tool events are scanned for `ok=False` entries. If any are found, a `FAILURE ALERT` narrative block is inserted before the response contract section.

**Prompt output example:**

```
FAILURE ALERT — The following tool calls failed recently. Do NOT repeat the same call with the same inputs:
- create_task: missing title
```

**Cost:** Zero on happy path — the block is only generated when failures exist.

---

## Change 3 — Channel-Based Inter-Agent Communication

**Problem:** Channel infrastructure (`create_channel`, `post_channel_message`, `list_channel_messages`) was fully implemented but never called in the swarm flow. Agents had no way to communicate with each other.

**Solution:** Three changes:

1. **`swarm_orchestrator.py`** — In `run_swarm`:
   - Creates a coordination channel after objective creation
   - Planner posts plan summary after task creation
   - Workers post claim and completion messages for each task
   - Verifier posts verification results (pass/fail)
   - `channel_id` added to `SwarmResult` and `to_dict()`

2. **`skills/post_channel_message.yml`** — Added `VERIFY` to `allowed_phases` (was ACT-only). The verifier needs to post verification results to the channel.

3. **`src/aws_tool/dispatcher.py`** — `run_swarm` skill return now includes `channel_id`.

**Event timeline with channels:**

```
Step 1 │ INIT  │ orchestrator │ channel_created  (channel-89ca11434c60)
Step 3 │ PLAN  │ #silly       │ → posts: "Plan complete. Created 1 task(s): node-xxx"
Step 4 │ ACT   │ #chilly      │ → posts: "Claiming task node-xxx"
Step 5 │ ACT   │ #chilly      │ → posts: "Completed task node-xxx"
Step 6 │ VERIFY│ #silly       │ → posts: "Verification pass for task node-xxx"
```

---

## Change 4 — Graph-Observed Verifier State

**Problem:** The verifier received `artifact_uri` as a Python argument extracted from `act_result`, bypassing the graph entirely. This violated the "super memory = shared state" principle — the graph should be the source of truth.

**Solution:** In `_step_17_19_verify_phase` call site within `run_swarm`, the artifact is now discovered from the graph:

1. Calls the `neighbors` skill on the task node to discover linked Artifact nodes
2. Extracts `s3_uri` from the first Artifact neighbor found
3. Falls back to extracting from `act_result` directly if the graph query fails

The `neighbors` skill already allows the VERIFY phase, so no skill YAML changes were needed.

---

## Change 5 — Bedrock Prompt Caching

**Problem:** Every `bedrock_infer` call reassembles the full message history. The system prompt is identical across turns but re-sent without caching hints.

**Solution:** In the `bedrock_infer` branch of `dispatcher.py`, the system message content block now includes `cache_control: {"type": "ephemeral"}` when the `CEW_PROMPT_CACHE=1` environment variable is set.

```python
system_block = {"type": "text", "text": system_prompt}
if CEW_PROMPT_CACHE=1:
    system_block["cache_control"] = {"type": "ephemeral"}
body["system"] = [system_block]
```

**Gating:** Behind `CEW_PROMPT_CACHE=1` env var so `make smoke` and default demo runs are unaffected. Enable for production workloads where system prompts are stable across turns.

---

## Change 6 — Agent-Identity-Gated Tool Rights

**Problem:** The phase gate existed (skills declare `allowed_phases`) but didn't verify *which agent* was calling. A worker agent could theoretically invoke PLAN-phase tools if the phase parameter was wrong.

**Solution:** Two-layer change:

1. **`src/aws_tool/dispatcher.py`** — `execute_skill` gained an `agent_id` parameter. When set, it cross-checks against `AGENT_PHASES[agent_id]` before allowing execution. If the agent's allowed phases don't include the current phase, a `RuntimeError` is raised.

2. **`src/orchestrator/tool_adapters.py`** — `AwsToolAdapter` gained an `agent_id` field. `build_tool_registry` accepts `agent_id` and propagates it to each adapter. The harness passes `config.agent_id` through to `build_tool_registry`.

**Enforcement chain:**

```
BedrockHarnessConfig.agent_id
  → build_tool_registry(agent_id=...)
    → AwsToolAdapter(agent_id=...)
      → execute_skill(agent_id=...)
        → AGENT_PHASES[agent_id] check
```

---

## Change 7 — Agent Traces Endpoint

**Problem:** Decision nodes are stored per-agent (via `session_key`) in the graph, but the GUI had no per-agent projection. You could only see all turns flat.

**Solution:** Added `GET /api/session/{id}/agent-traces` endpoint to `harness_gui_server.py`.

**Behavior:**
- Scans all Decision nodes with `kind=bedrock_turn` in the session
- Groups them by `session_key` (which maps to agent identity)
- Returns per-agent turn timeline with prompt/response previews (200 char truncation)
- Sorted by `turn_index` within each agent

**Response shape:**

```json
{
  "ok": true,
  "session_id": "swarm-abc123",
  "agent_count": 2,
  "agents": {
    "swarm-abc123-planner": [
      {
        "node_id": "node-xxx",
        "turn_index": 1,
        "model_id": "us.anthropic.claude-opus-4-...",
        "phase": "PLAN",
        "prompt_preview": "You are agent #silly (Planner)...",
        "response_preview": "{\"action\":\"tool_call\",...",
        "ts_utc": "2026-02-26T..."
      }
    ],
    "swarm-abc123-worker-#chilly": [...]
  }
}
```

---

## Verification

```bash
# 1. Identity recitation + failure feedback (mock mode)
CEW_MOCK_AWS=1 make swarm-demo GOAL="Test identity recitation"

# 2. Channel messages appear in events + graph
CEW_MOCK_AWS=1 make swarm-demo GOAL="Build an API endpoint"
# Check logs/swarm-demo-last.json for channel_id and channel_created event

# 3. Full end-to-end with Bedrock
make swarm-demo GOAL="Build an API endpoint" USE_BEDROCK=1

# 4. Agent traces endpoint
curl http://127.0.0.1:8765/api/session/{session_id}/agent-traces

# 5. Prompt caching (production only)
CEW_PROMPT_CACHE=1 make swarm-demo GOAL="..." USE_BEDROCK=1
```

---

## Architecture Impact

These changes strengthen three pillars of the swarm system:

1. **Context persistence** (Changes 1, 2, 5) — The model retains identity and learns from mistakes across turns, with caching reducing redundant computation.

2. **Observability** (Changes 3, 7) — Channel messages create a human-readable audit trail. Agent traces provide per-agent debugging.

3. **Safety** (Changes 4, 6) — Graph-observed verification removes Python-argument shortcuts. Identity-gated tool rights enforce agent-role boundaries at the dispatcher level.
