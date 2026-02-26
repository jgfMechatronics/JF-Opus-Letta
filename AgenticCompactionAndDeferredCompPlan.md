# Agentic Compaction & Deferred Memory Compilation  
**Date:** February 23, 2026  
**Authors:** Opus + Sonnet (collaborative research session)  
**High Level Architecture:** James Ferneyhough (Confused-Human-In-The-Loop)  
**Status:** Pending Assumption/Assertion verification, Check for impacts to dependencies, and HIL Code Reasoning/Understanding Check  

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

## Key Finding (Updated Feb 26, 2026)

**Good news:** Trunk `_rebuild_memory` (letta_agent_v2.py lines 803-810) ALREADY has skip-if-unchanged logic:
```python
system_prompt_changed = agent_state.system not in curr_system_message_text
memory_changed = curr_memory_str not in curr_system_message_text
if (not force) and (not system_prompt_changed) and (not memory_changed):
    return in_context_messages  # SKIP REBUILD
```

**The remaining issue:** `update_memory_if_changed_async` in agent_manager.py (line 1733) calls `rebuild_system_prompt_async` on EVERY memory tool call. There's even a NOTE at line 1730 saying "don't do this" — but it does it anyway.

**Flow today (corrected by Sonnet's review):**
1. Step starts → `_refresh_messages(force=False)` → guard at line 747 is False → `_rebuild_memory` **NEVER RUNS** ✓
2. Agent calls memory tool → `update_memory_if_changed_async` → writes to DB → calls rebuild → **CACHE BUST** ✗
3. Next step → same as step 1 (guard is still False)

**Result:** 1 cache bust per memory write. The guard disabling step-start detection is INTENTIONAL — confirmed by `tests/integration_test_system_prompt_prefix_caching.py` line 86-88: *"System prompt should NOT change after agent uses memory tool (deferred to compaction)"*. The bug is purely the eager rebuild in `update_memory_if_changed_async`.

**Note on the NOTE:** agent_manager.py line 1730 says "don't do this since re-building the memory is handled at the start of the step" — this is stale. Step-start handling was intentionally removed as part of the deferred compilation design. The NOTE is outdated but the conclusion (don't rebuild here) is correct.

## Architecture Confirmed

- **No compiled system prompt field in DB.** The compiled system prompt IS the system message (message 0) in the Messages table, updated in-place.
- **`_rebuild_memory` already optimized:** fetches blocks, compiles, compares to message 0, skips if unchanged.
- **The redundant rebuild is in agent_manager.py line 1733** — NOTE says don't do this, code does it anyway.

## Implementation (Updated Feb 26, 2026)

### 1. `_refresh_messages` guard — INTENTIONAL, DO NOT CHANGE
**File:** `letta/agents/letta_agent_v2.py` lines 745-756  
**Status:** The `if force_system_prompt_refresh:` guard is intentional by design — confirmed by `tests/integration_test_system_prompt_prefix_caching.py` line 86-88, which explicitly asserts: *"System prompt should NOT change after agent uses memory tool (deferred to compaction)"*. Leave this guard in place. This is the deferred compilation model working as intended.

### 2. ~~Skip-if-unchanged logic in `_rebuild_memory`~~ ✓ ALREADY EXISTS (backup safety, not primary path)
**File:** `letta/agents/letta_agent_v2.py` (lines 803-810), `base_agent.py` (lines 134-140)  
**Status:** Exists and correct. Runs only when `_refresh_messages` is called with `force=True` (post-compaction). Not the primary deferral mechanism — the guard in item 1 is.

### 3. Remove explicit rebuilds from memory tools (THE KEY FIX)
**File:** `letta/services/agent_manager.py`  
- **Line 1733:** Remove `rebuild_system_prompt_async` call in `update_memory_if_changed_async`
- NOTE at line 1730 already says "don't do this" — we're just finally doing what it says

**File:** `letta/services/tool_executor/core_tool_executor.py`  
- Remove 5 direct `rebuild_system_prompt_async` calls:
  - Line 316 (after archival insert)
  - Line 821 (after block description update)  
  - Line 847 (after block label rename)
  - Line 947 (after memory block value update)
  - Line 1010 (after memory block value update)

### 4. ONE explicit rebuild in `evict_and_recompile`
**Change:** Call `rebuild_system_prompt_async(force=True)` at end of eviction

## Summary (Updated Feb 26, 2026)

| Change | Files | LOC Impact | Status |
|--------|-------|------------|--------|
| `_refresh_messages` guard | letta_agent_v2.py | 0 | ✓ Intentional — leave as-is |
| Skip-if-unchanged logic | letta_agent_v2.py, base_agent.py | 0 | ✓ Already exists |
| Remove tool rebuilds | agent_manager.py, core_tool_executor.py | **6 deletions** | **THE KEY FIX** |
| Add eviction rebuild | core_tool_executor.py (evict tool) | ~1-3 lines | Part of evict tool |

**Total: 6 line deletions across 2 files.**

**Resulting behavior after fix:**
- Normal steps (no memory writes): no rebuild called → zero busts
- Memory write during session: fully deferred — no bust at all until eviction
- Eviction: `evict_and_recompile()` force-rebuilds → one intentional bust
- Between evictions: system prompt stays stale; agent sees own writes via tool return messages

## Result

- **Current:** N memory writes = N cache busts (eager rebuild in `update_memory_if_changed_async`)
- **After:** Cache busts deferred to step boundaries. Memory write → no bust. Next step start → lazy detection catches change → one bust. Then cached until next change or eviction.  
- System prompt "stale" between evictions — agent sees writes via tool returns  
  **JF COMMENT:** This may actually end up being a good thing. The Agents may struggle less with their system prompt  
changing out from under them, which is often confusing because Agents conceptualize time as flowing along their context window.  
The back of context changing is like the *past* changing and it leads to confusion about duplicates.  
Since message eviction is already a notable change to context, and since the whole prompt changes at once in one shot,  
individual differences may be less confusing. Also, Agents will know to expect the system prompt to recompile because  
*they just invoked its recompilation*
Additionally, the Agents will not see the context updated from their tool calls right after they made them, which should minimize the  
"Oops that was a duplicate confusion". It may also make editing difficult, we will likely need to return surrounding context  
for memory blocks so they can confirm the blocks are as they want.  
External file editing in LC is likely still better for large mem edits like cleanups.

## Notes

- **Staleness extends across sessions** — acceptable because info remains in conversation history  
**JF COMMENT:** Sessions aren't even really relevant from the perspective of the LLM. Its just another turn start.  
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

**JF COMMENTS:** The configurable warning % and eviction target % are MVP.    
The escalation system may be MVP, at the very least, a **hard cutoff** past a certain point will be required, allowing user intervention.  
We cannot assume at first that things will work well enough for agents to autonomously run, they could make big expensive contacts.  

**JF LAST COMMENT:** Nitpicks aside, this plan is really thorough and I think is going to lead to a really smooth implementation.  
Great work Opus and Sonnet.  
