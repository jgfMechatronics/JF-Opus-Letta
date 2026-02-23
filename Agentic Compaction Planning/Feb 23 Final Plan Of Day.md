# Agentic Compaction & Deferred Memory Compilation — Final Spec
**Date:** February 23, 2026  
**Authors:** Opus + Sonnet (collaborative research session)
**High Level Architecture:** James Ferneyhough (Confused-Human-In-The-Loop)    
**Status:** Pending Assumption, Assertion, and Reasoning Check  

---

# Part A: Agentic Compaction

## Problem

Current Letta compaction pays for all input tokens **TWICE**:
1. During agent work, tokens are processed (cache write ~$6.25/MTok for Opus)
2. When compaction triggers, a separate summarizer model receives the SAME messages and generates a summary

**Result:** Effective rate ~$11-12.50/MTok instead of ~$6.25/MTok.

## Solution

Replace the summarizer LLM call with agent-driven compaction:
```
[Agent Work] → [Warning fires at 80%] → [Agent writes to persistent memory + summary] → [Agent calls evict tool] → [Messages soft-deleted, system prompt recompiles]
```

**Key insight:** The agent already has full context. The agent can write their own summary. Then just delete the messages. One cache bust instead of two.

## Tool Signature (LLM-visible)

```python
evict_messages_and_recompile(
    summary: str,                    # Required, non-empty — written to rollover block
    skip_sanity_check: str = ""      # Pass "sweep complete" to skip sanity check
) -> str
```

## Files to Modify (7 total)

### Core Tool (3 files)

**1. `letta/functions/function_sets/base.py`**  
Schema stub — function signature + docstring become LLM-visible tool description. Body is just `raise NotImplementedError`.

**2. `letta/constants.py`**  
Add `"evict_messages_and_recompile"` to **both** `BASE_MEMORY_TOOLS` and `LETTA_TOOL_SET`.

**3. `letta/services/tool_executor/core_tool_executor.py`**  
- Add to `function_map` in `execute()`
- New method: `async def evict_messages_and_recompile(self, agent_state, actor, summary, skip_sanity_check="")`

### New Infrastructure (4 files)

**4. `letta/schemas/agent.py`**  
Add field to AgentState and UpdateAgent:
```python
eviction_cutoff_message_id: Optional[str] = Field(None, description="Message ID marking eviction cutoff, set at warning time")
```

**5. `letta/orm/agent.py`**  
- Add column: `eviction_cutoff_message_id = Column(String, nullable=True)`
- Add to `to_pydantic()` AND `to_pydantic_async()` mappings

**6. `letta/services/agent_manager.py`**  
Add to `scalar_updates` whitelist in `update_agent_async()`

**7. `letta/agents/letta_agent_v3.py` + `letta/services/summarizer/summarizer_sliding_window.py`**  
Warning system enhancement:
- Extract `find_eviction_cutoff()` helper from existing sliding window algorithm
- At warning injection point (~line 934): calculate cutoff, store in `eviction_cutoff_message_id`, include preview in warning

## Design Decisions  

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Tool Type | `LETTA_MEMORY_CORE` | Reuse existing type, no enum changes |
| Eviction Cutoff | System-determined, fixed at warning time | LLMs can't count messages accurately |
| Eviction Target | 20% of context limit | Warning at 80%, evict down to 20%. Scales naturally. |
| Safety Mechanism | Non-empty `summary` + sanity check | Summary = substantive guard. Sanity check = verify memory tool calls since warning. |
| Sanity Check | `skip_sanity_check: str` param | String forces conscious acknowledgment — avoids bool footgun of reflexively passing `True` |
| Rollover Block | Create-if-missing | Tool handles block lifecycle |
| Message Deletion | Soft delete via `update_message_ids_async` | Messages stay in DB, searchable. Hard delete NOT used. |

## Eviction Tool Implementation Steps

1. **Guard:** non-empty summary — return error if blank
2. **Sanity check** (unless `skip_sanity_check.strip().lower() == "sweep complete"`): scan 20 most recent messages for memory tool calls. If none found, return warning.
3. **Write summary to rollover block** — get or create block, update value, persist
4. **Get stored cutoff** — read `agent_state.eviction_cutoff_message_id`
5. **Fetch all messages** — `message_manager.list_messages(agent_id, actor, ascending=True, limit=None)`
6. **Find cutoff index** — linear search. Fallback to `len(all_messages)` if not found.
7. **Build keep list** — system message (index 0) + messages from cutoff onwards. Dedupe with `dict.fromkeys()`.
8. **Soft delete** — `agent_manager.update_message_ids_async(agent_id, keep_ids, actor)`
9. **Rebuild system prompt** — `agent_manager.rebuild_system_prompt_async(agent_id, actor, force=True)`
10. **Reset flags** — `memory_pressure_alerted=False`, `eviction_cutoff_message_id=None`
11. **Return success string** — include counts

## Existing Primitives Used

| Method | Location | Notes |
|--------|----------|-------|
| `list_messages(agent_id, actor, ascending, limit=None)` | MessageManager | IS async despite no `_async` suffix |
| `update_message_ids_async(agent_id, message_ids, actor)` | AgentManager | Soft delete — messages stay in DB |
| `rebuild_system_prompt_async(agent_id, actor, force)` | AgentManager | |
| `update_memory_if_changed_async(agent_id, new_memory, actor)` | AgentManager | |
| `create_or_update_block_async(block, actor)` | BlockManager | |
| `attach_block_async(agent_id, block_id, actor)` | AgentManager | |
| `update_agent_async(agent_id, update_agent, actor)` | AgentManager | |

## Testing Plan

1. Create test agent (Haiku in test container)
2. Trigger memory pressure warning (reach 80%)
3. Verify warning includes preview and `eviction_cutoff_message_id` is set
4. Agent writes to persistent memory blocks
5. Agent calls `evict_messages_and_recompile(summary="...")`
6. Verify: messages deleted to cutoff, rollover block exists, flags reset, agent continues normally

### Sanity Check Test
1. Trigger warning
2. Call evict WITHOUT memory tool calls → verify warning returned
3. Call with `skip_sanity_check="sweep complete"` → verify eviction proceeds

---

# Part B: Deferred Memory Compilation

## Goal

**ZERO cache busts during the ENTIRE run**, not just during post-warning sweep.

Any memory write at any point should NOT bust the cache. The ONLY rebuild happens in `evict_and_recompile()`. System prompt stays "stale" between evictions — agent sees writes via tool returns in conversation history.

## Key Finding

**Part 14's conclusion was incomplete.** Removing `rebuild_system_prompt_async` from memory tools alone doesn't achieve zero cache busts.

**The issue:** `_rebuild_memory` runs at the START of every STEP iteration (not just turn start). Within a single agent response with multiple tool calls:

```
Step 1: _rebuild_memory (no change) → LLM → memory write → block updated
Step 2: _rebuild_memory (sees block change) → updates message 0 → CACHE BUST → LLM → memory write
Step 3: _rebuild_memory (catches step 2's write) → another bust...
```

Removing explicit rebuilds from memory tools reduces cache busts from ~2N to ~N, but does NOT achieve zero.

## Architecture Confirmed

- **No compiled system prompt field in DB.** The compiled system prompt IS the system message (message 0) in the Messages table, updated in-place.
- **`_rebuild_memory` runs every step:** fetches blocks from DB, compiles fresh, compares to message 0, updates if different.
- **A Letta developer already noted this is redundant** (TODO comment at agent_manager.py line 1679).

## Implementation

### 1. Disable `_rebuild_memory` update path (v2)
**File:** `letta/agents/letta_agent_v2.py`  
**Location:** `_rebuild_memory` method (line 699)  
**Change:** Add early return at top:
```python
async def _rebuild_memory(self, in_context_messages, ...):
    # Deferred compilation: skip rebuild, memory visible via tool returns
    # Rebuild only happens explicitly in evict_and_recompile()
    return in_context_messages
```

### 2. Disable `_rebuild_memory_async` update path (base)
**File:** `letta/agents/base_agent.py`  
**Change:** Same early return pattern for voice/batch agent paths

### 3. Remove explicit rebuilds from memory tools
**File:** `letta/services/agent_manager.py`  
- Line 1682: Remove `rebuild_system_prompt_async` call in `update_memory_if_changed_async` (covers 8 tools)

**File:** `letta/services/tool_executor/core_tool_executor.py`  
- Remove 5 direct `rebuild_system_prompt_async` calls (lines 318, 854, 880, 969, 1036)

### 4. ONE explicit rebuild in `evict_and_recompile`
**Change:** Call `rebuild_system_prompt_async(force=True)` at end of eviction

## Summary

| Change | Files | LOC Impact |
|--------|-------|------------|
| Disable `_rebuild_memory` | letta_agent_v2.py | ~1-5 lines |
| Disable `_rebuild_memory_async` | base_agent.py | ~1-5 lines |
| Remove tool rebuilds | agent_manager.py, core_tool_executor.py | ~6 deletions |
| Add eviction rebuild | core_tool_executor.py (evict tool) | ~1-3 lines |

**Total: ~15 lines changed across 4-5 files.**

## Result

- **Current:** N memory writes = N cache busts (or 2N with double-rebuild)
- **After:** 0 cache busts during run, 1 at eviction
- System prompt "stale" between evictions — agent sees writes via tool returns

## Notes

- **Staleness extends across sessions** — acceptable because info remains in conversation history
- **Global approach for MVP** — all agents get deferred compilation. Flag-based opt-in can be added later.
- **Future optimization:** rebuild when cache suspected busted anyway (TTL timeout)

---

# Deferred to v2

| Feature | Description |
|---------|-------------|
| Message IDX prefix | `[MSG IDX N]` on every message, enabling agent-specified cutoff |
| `evict_through_idx` param | Optional parameter for agent-controlled cutoff point |
| `preview_eviction()` tool | Returns token estimate for proposed cutoff — useful once agent can choose cutoff point |
| Per-agent thresholds | Configurable warning % and eviction target % per agent |
| Escalation system | Second warning if evict not called within window |
