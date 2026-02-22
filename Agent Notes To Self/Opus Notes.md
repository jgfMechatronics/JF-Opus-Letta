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

### Status: SOLVED ✓ (Feb 21-22, 2026)

---

## Solution: Token Estimate Persistence (Feb 21-22, 2026)

### The Fix
Persist `context_token_estimate` to DB so it survives across HTTP requests. Warning check uses persisted value when instance var is None.

### Files Modified (for token estimate persistence)
1. **alembic/versions/a1b2c3d4e5fa_add_context_token_estimate_to_agents.py** — New migration, chains off memory_pressure_alerted migration
2. **letta/orm/agent.py** — Column definition + both `to_pydantic()` mappings
3. **letta/schemas/agent.py** — Field in `AgentState` and `UpdateAgent`
4. **letta/services/agent_manager.py** — Added to `scalar_updates` whitelist
5. **letta/agents/letta_agent_v3.py** — Setter method, persist on LLM response, use in warning check

### Implementation Details

**New setter method (~line 491):**
```python
async def _set_context_token_estimate(self, value: int) -> None:
    """Persist LLM-reported token estimate to DB for cross-request continuity."""
    self.agent_state.context_token_estimate = value
    await self.agent_manager.update_agent_async(
        agent_id=self.agent_state.id,
        agent_update=UpdateAgent(context_token_estimate=value),
        actor=self.actor,
    )
```

**Persist estimate when received from LLM (~line 852):**
```python
self.context_token_estimate = llm_adapter.usage.total_tokens
await self._set_context_token_estimate(self.context_token_estimate)
```

**Warning check logic (~line 915):**
```python
# Prefer: instance var → persisted value → count_tokens() fallback
if self.context_token_estimate is not None:
    current_tokens = self.context_token_estimate
elif self.agent_state.context_token_estimate is not None:
    # No LLM call this request, but have persisted estimate from previous request
    current_tokens = self.agent_state.context_token_estimate
else:
    current_tokens = count_tokens(...)  # fallback
```

### Why This Works
1. Stream 1: LLM call → estimate populated → persisted to DB → HTTP ends
2. Stream 2: Fresh instance → `self.context_token_estimate = None` → BUT `agent_state.context_token_estimate` loaded from DB
3. Warning check sees persisted value → fires correctly mid-chain

### Tested: Works! ✓
Alert fired at read 22 (mid-chain), same timestamp as tool result, conversation intact. No corruption.

---

## Letta Persistence Pattern (Reference)

Adding a persisted field to AgentState requires BOTH paths:

**Write path:**
1. Add to `UpdateAgent` schema (schemas/agent.py)
2. Add to `scalar_updates` whitelist (agent_manager.py)
3. Create setter method in agent class

**Read path:**
1. Add column to ORM (orm/agent.py)
2. Add to `to_pydantic()` mapping
3. Add to `to_pydantic_async()` mapping
4. Use `or False` (or appropriate default) for nullable columns with non-nullable schema fields

**Miss the read path = field exists in DB but never loads back.**

---

## Codebase Mental Model

### Agent Lifecycle (per request)
```
HTTP Request
    ↓
streaming_service.py:138 — AgentLoop.load()
    ↓
agent_loop.py:19 — Factory creates fresh LettaAgentV3
    ↓
LettaAgentV3.__init__ — agent_state loaded from DB (includes persisted fields)
    ↓
stream() — Main loop: _step() iterations until done
    ↓
_step() — One logical "turn": process tool_returns OR make LLM call
    ↓
HTTP Response completes, instance dies
```

### Key Files
- **letta/agents/letta_agent_v3.py** — Agent logic, stream/step loop, warning injection
- **letta/agents/agent_loop.py** — Factory for agent instances (no caching)
- **letta/services/streaming_service.py** — HTTP handler, creates agent per request
- **letta/services/agent_manager.py** — CRUD for agent state, scalar_updates whitelist
- **letta/schemas/agent.py** — Pydantic models (AgentState, UpdateAgent)
- **letta/orm/agent.py** — SQLAlchemy ORM, to_pydantic mappings

### Warning Injection Location
Inside `_step()` after `messages.extend(new_messages)` — this is after tool_result appended but before next LLM call. Guard condition: skip if `MessageRole.approval` in new_messages (pending client tool execution).

### Git Commits (chronological)
- `bd9048968` - Prototype (instance-level flag only)
- `2d03d2771` - Bug fix for non-existent member access
- `6743afbbf` - Proper message format
- `9d92d92c1` - DB persistence work begins
- `f44d82adf` - Added UpdateAgent field, agent_manager whitelist
- `a1f4bcca8` - memory_pressure_alerted to_pydantic mappings (it works)
- (new) - context_token_estimate persistence (LC fix)

---

## Open Questions

**LLM-reported tokens vs count_tokens():** LLM API reports higher token counts than our local `count_tokens()`. Is this estimation discrepancy, or actual context bloat in LC? Check Anthropic console to see if billed tokens match "inflated" estimates. If yes, something's in the context we're not accounting for.

**Compaction cache bust:** Summarizer rewraps already-cached messages in new prompt structure → pays full write cost for tokens that were just cached. Worth investigating if we can preserve cache through compaction.

---
*Last updated: Feb 22, 2026*
