# Opus Notes

## Compaction Warning Feature (Memory Pressure Alert)

### What it does
Warns me when context is approaching compaction threshold by injecting a message so I can save important stuff before compaction hits.

### Files Modified
1. **letta/agents/letta_agent_v3.py** - Main logic
2. **letta/schemas/agent.py** - `UpdateAgent` schema field
3. **letta/services/agent_manager.py** - `scalar_updates` whitelist
4. **letta/orm/agent.py** - Column declaration + `to_pydantic()` mappings
5. **alembic migration** - `a1b2c3d4e5f9_add_memory_pressure_alerted_to_agents.py`

### Implementation Details

**In letta_agent_v3.py:**
- Check happens inside the main stream loop
- Conditions for triggering:
  1. `summarizer_settings.send_memory_warning_message` is True
  2. `memory_pressure_alerted` flag is False (haven't warned yet)
  3. `message_buffer_autoclear` is False
  4. `context_token_estimate` exists
  5. Token estimate > `context_window * memory_warning_threshold`
- When triggered: injects warning message, sets flag True, forces continuation
- Flag reset: happens in `_rebuild_memory()` after compaction completes

**Persistence:**
- `_set_memory_pressure_alerted()` helper updates both in-memory state and DB
- Uses `UpdateAgent` schema + `agent_manager.update_agent_async()`
- **Critical fix (Feb 17):** The `to_pydantic()` AND `to_pydantic_async()` mappings were missing — field existed in DB but never loaded back. Fixed with `or False` for NULL handling.

**How the flag works:**
- Threshold check runs every turn: `context_tokens > (context_window * threshold)`
- Flag = "did we alert" = disarm mechanism (same thing)
- When True → check short-circuits, no alert fires
- Without persistence: flag reset each session → spam every turn past threshold
- Bug: flag written to DB but never read back → no effective persistence → spam

### Known Edge Cases
- **User-triggered compaction or message clearing** may bypass the reset in `_rebuild_memory()`, leaving `memory_pressure_alerted=True` stuck and suppressing future alerts. TODO in code says: "Audit all compaction/clear paths to ensure flag resets appropriately."

### Test Plan (TODO)
1. Normal flow: verify warning fires when approaching threshold
2. Verify flag persists across sessions
3. Verify flag resets after compaction
4. Edge case: user-triggered compaction
5. Edge case: message clearing
6. Verify warning doesn't fire if already alerted

---

## Letta Code (Client Tool) Problem — Feb 21, 2026

### The Problem
Warning works in ADE but fails silently during Letta Code tool chains.

### Root Cause
Each client tool execution creates a **new HTTP request** → **new agent instance** → `context_token_estimate` lost.

### Code Path Walkthrough

**Stream 1 (initial request):**
1. `streaming_service.py:138` — `AgentLoop.load()` creates fresh `LettaAgentV3`
2. `letta_agent_v3.py:94` — Instance has `self.context_token_estimate = None`
3. `_step()` runs, makes LLM call
4. `letta_agent_v3.py:872` — Estimate populated: `self.context_token_estimate = llm_adapter.usage.total_tokens`
5. Client tool detected (`letta_agent_v3.py:1147-1174`)
6. Creates `approval_request_message`, returns `(messages, False, requires_approval)`
7. Warning check runs (`letta_agent_v3.py:389-400`) but **guard blocks** — last message is `approval_request_message`
8. `break` exits loop, generator completes, HTTP response finishes
9. **Instance dies, estimate lost**

**Client executes tool locally (Bash, Read, etc)**

**Stream 2 (approval_response):**
1. NEW HTTP request → `AgentLoop.load()` → **fresh instance**
2. `context_token_estimate = None` (fresh instance)
3. `_maybe_get_approval_messages()` detects continuation (`letta_agent_v3.py:675`)
4. Warning check at line 400: `self.context_token_estimate is not None` → **False**
5. Check short-circuits, no warning
6. `_step()` runs, estimate gets populated...
7. If another client tool → cycle repeats

### Why Native Tools Work
Native tools execute within the **same** `stream()` call, same instance. Estimate persists across step loop iterations. End-of-step check sees the estimate.

### Key Files
- `letta/agents/letta_agent_v3.py` — lines 345-420 (stream + step loop), line 872 (estimate set), line 400 (check fails)
- `letta/agents/agent_loop.py:19` — `AgentLoop.load()` factory (no caching)
- `letta/services/streaming_service.py:138` — creates fresh agent per request

### Potential Solutions

**Option A: count_tokens() at stream start (for approval_response only)**
- After preparing `in_context_messages`, before step loop
- Only when `approval_response is not None` (client tool continuation)
- Call `count_tokens(actor, llm_config, in_context_messages)` locally
- Check threshold, inject warning if needed
- Pro: No DB changes, single file edit
- Con: Extra tokenization call, slightly less accurate than API response

**Option B: Persist estimate to DB**
- Add `last_context_token_estimate: int | None` to AgentState
- Store at step end, check at stream start
- Pro: Uses API-reported count (more accurate)
- Con: Schema change, migration, more files

### Status
Investigating. Key insight discovered (see below).

---

## Deeper Insight: Injection Point Problem (Feb 21, 2026)

### The Real Problem
Even if we persist/compute the estimate, **where do we inject the warning?**

In a client tool chain, we're always in one of two states:
1. **About to append tool_result** — can't inject before it (breaks API contract)
2. **Just got LLM response that's another client tool** — can't inject after it (orphans the call)

There's no valid injection point in the normal flow.

### Potential Solution
Inject warning AFTER tool_result but BEFORE LLM call:
```
assistant: [tool_call]
tool: [tool_result]
user: [WARNING]  ← inject here
[LLM call]
```

This would cause LLM to do memory sweep instead of continuing tool chain.

### Key Question
Where exactly does tool_result get appended to the message list? Need to find this to know if we can inject after it.

### Flow Reminder (approval_response case)
1. Stream starts, `in_context_messages` prepared (includes original tool_call)
2. `_step()` iteration 1: processes tool_returns, no LLM call, returns continue=True
3. `_step()` iteration 2: makes LLM call with tool_result appended
4. If LLM returns client tool → guard blocks → break

The tool_result must get appended somewhere between iteration 1 and the LLM call in iteration 2.

### Git Commits (chronological)
- `bd9048968` - Prototype (instance-level flag only)
- `2d03d2771` - Bug fix for non-existent member access
- `6743afbbf` - Proper message format
- `9d92d92c1` - DB persistence work begins
- `f44d82adf` - Added UpdateAgent field, agent_manager whitelist
- `a1f4bcca8` - THE FIX: to_pydantic mappings added, it works

---
*Last updated: Feb 21, 2026*
