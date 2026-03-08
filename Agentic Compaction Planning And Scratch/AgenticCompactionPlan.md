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
[Agent Work] → [Warning fires at 80%] → [Agent writes to persistent memory + summary] → [Agent calls evict tool] → [Messages soft-deleted, system prompt recompiles, summary added somewhere]
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

## Key Open Questions
1. Can agents effectively scan a chunk of context identified by starting and ending strings?
2. Can agents effectively pull key information for recording out of such a section?

## TODOs
- [] Testing on open questions with Sonnet and Opus
- [] Pull Deferred memory compilation onto trunk
- [] Finish testing on compaction warning, move to trunk and run on prod. This will help further dev and serve as basis for the injected agentic compaction warning
- [] Rebase this branch on trunk with those features pulled in
- [] Interrogate this plan and iron out more detailed requirements
- [] Start working in chunks, identify riskiest parts first. Develop test plan then implement against tests
- [] Commonize the consent based live LLM integration testing from the Deferred memory compilation

## Redundant memory writes at warning time + Soln (To be implemented after basic function complete and tested)
### Problem
Agent is still expected to be writing memories as they go, if they only write memories after the warning they will inevitably miss important stuff.  
Issue this creates is that once the warning fires and the agent has to scan the block under eviction, it will be very difficult for them to tell what they have already written (memory writes are many needles in a haystack).  
Since core memory is not recompiled until AFTER the agent calls eviction, they won't easily be able to tell what they have already recorded from the block under eviction.  
### Solution
At warning time, gather pending memory writes and present to agent with the warning. They can avoid rewriting redundant information by using the much easier reference of all the pending writes at the front of context (which they will easily attend to).  
This will chew *some* tokens and represent *some* reprocessing, but it:  
- Still beats the hell out of reprocessing *everything*
- Is injected at the end of context and therefore doesn't bust the cache
- Excludes archival writes and file writes. Core memory writes already have to be fairly tight as they will persist in context, so it represents minimal reprocessing.