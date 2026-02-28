# Test Refactor Plan: Deferred Memory Compilation Coverage

**File to modify:** `tests/integration_test_system_prompt_prefix_caching.py`  
**Branch:** `Development/DeferredMemoryCompilation`  
**Date:** 2026-02-28

---

## Requirements

The deferred memory compilation feature has two behaviors to verify per write path (tool calls, API) and per trigger (reset, compaction):

1. **Deferred (negative):** Memory writes do NOT immediately rebuild the stored system message.
2. **Eventual rebuild (positive):** After an appropriate trigger, the stored system message DOES update to reflect pending writes.

---

## Current Coverage Gaps

| Scenario | Deferred? | Rebuilt? |
|---|---|---|
| Tool write → no trigger | ✅ | ❌ |
| API write → no trigger | ✅ | ❌ |
| API write → reset | ✅ | ✅ |
| Tool write → reset | ❌ | ❌ |
| Any write → compaction | ❌ | ❌ |

---

## Key Research Findings

**How reset triggers rebuild (confirmed):**  
`reset_messages_async` explicitly passes `rebuild_system_prompt=True`, which calls `rebuild_system_prompt_async(force=True)` before returning (`agent_manager.py` ~line 1658).

**Two compaction paths — one has the rebuild, one doesn't:**  

*In-step compaction* (`letta_agent_v3.py`, both `context_window_exceeded` ~line 1033 and `post_step_context_check` ~line 1253): after compacting, explicitly calls `rebuild_system_prompt_async(force=True)` then `_refresh_messages(force_system_prompt_refresh=True)` before checkpointing. Comment says: *"Recompile the persisted system prompt after compaction so subsequent turns load the repaired system+memory state from message_ids[0]."* This is what Haiku experienced during testing — organically triggered compaction during a conversation step.

*API-triggered compaction* (`POST /{agent_id}/summarize`, `agent_manager.py` ~line 2444): calls `compact()` then `_checkpoint_messages()` only. The `rebuild_system_prompt_async` call is **missing**. This is a gap — API compact is inconsistent with in-step compact.

**The fix:** Add `rebuild_system_prompt_async(force=True)` to the `summarize_messages` endpoint after `_checkpoint_messages`, bringing it into parity with in-step compaction. One line, clear precedent.

**SDK call for compaction:**  
`client.agents.messages.compact(agent_id, compaction_settings={"mode": "all"})`  
The default mode is `"sliding_window"` (`CompactionSettings.mode`, `summarizer_config.py` line 62). We use `"all"` in the test for reliability: `"sliding_window"` may no-op on small message sets (test agents only have ~4-6 messages from the consent flow), while `"all"` guarantees compaction fires. Both modes flow through the same `summarize_messages` endpoint and the fix applies uniformly — this is not testing a non-representative path.

**Early-return guard in compact:**  
The endpoint returns early (no-op) if there are no non-system/non-summary messages to compact. Test agents will have enough messages from the consent flow + writes to avoid this regardless of mode.

---

## Proposed Test Structure

### Three tests, clean separation by write path and trigger:

**Test 1: `test_tool_writes_deferred_then_rebuilt_after_reset`**  
Full round-trip via agent tool calls:
- Write markers via `memory_insert` and `memory_replace` (using existing `MEMORY_TOOL_TESTS`)
- Assert each marker NOT in stored system message after write (deferred)
- Trigger reset
- Assert ALL markers present in stored system message (rebuilt)

**Test 2: `test_api_writes_deferred_then_rebuilt_after_reset`**  
Full round-trip via direct API block update:
- Write a unique marker via `client.blocks.update`
- Assert marker NOT in stored system message (deferred)
- Trigger reset
- Assert marker present in stored system message (rebuilt)

*(This is the existing test_system_prompt_updates_after_reset, extended with explicit deferred check and renamed.)*

**Test 3: `test_api_writes_deferred_then_rebuilt_after_compact`**  
Same write path as Test 2, compaction as trigger instead of reset:
- Write a unique marker via `client.blocks.update`
- Assert marker NOT in stored system message (deferred)
- Trigger compact (`mode="all"`)
- Assert marker present in stored system message (rebuilt)

*Requires the compact endpoint fix described below — without it, the stored system message stays stale after API-triggered compact and this test will fail.*

---

## DRY Helper Structure

All shared logic extracted into plain functions (not fixtures — these are stateful write operations, not setup):

```
# Write helpers — perform writes, return markers
write_markers_via_tools(client, agent) -> list[str]
    Sends MEMORY_TOOL_TESTS instructions to agent, returns verify strings.

write_marker_via_api(client, agent) -> str
    Updates human block via client.blocks.update, returns the marker string.

# Assert helpers — check stored system message
assert_markers_not_in_stored_msg(client, agent_id, markers: list[str])
    Fails if any marker IS present. (Confirms deferred.)

assert_markers_in_stored_msg(client, agent_id, markers: list[str])
    Fails if any marker is NOT present. (Confirms rebuilt.)

# Trigger helpers — explicit rebuild events
trigger_reset(client, agent_id)
    Calls client.agents.messages.reset(agent_id).

trigger_compact(client, agent_id)
    Calls client.agents.messages.compact(agent_id, compaction_settings={"mode": "all"}).
```

The `agent` fixture, `get_stored_system_message`, `get_human_block`, `get_consent`, and `debrief_agent` remain unchanged.

Each test body then reads as: write → assert deferred → trigger → assert rebuilt. Short, declarative, obvious.

---

## Required Code Change: Compact Endpoint Fix

`summarize_messages` in `agents.py` (~line 2444) is missing the `rebuild_system_prompt_async(force=True)` call that in-step compaction already makes (at both trigger points in `letta_agent_v3.py`). Add the call after `_checkpoint_messages`, matching the in-step pattern exactly.

Without this, the stored system message at `message_ids[0]` stays stale after API-triggered compaction, and Test 3 will fail.

---

## Consent Message Update

The current `CONSENT_REQUEST` describes what the agent will experience. With the refactor, tool-write tests now include a reset and compact tests also compact the context. Update the message to accurately describe:
- Memory tool writes
- Direct API block modifications  
- A message reset or compaction (as applicable)

Per E-LLM spec: informed consent requires accurate description of what the agent will experience.

---

## What Stays Unchanged

- `get_stored_system_message` — correct, keep as-is
- `get_human_block` — keep as-is
- `get_consent` / `debrief_agent` / `check_consent_response` — keep, update message content only
- `MEMORY_TOOL_TESTS` list — keep, used by write_markers_via_tools
- `agent` fixture — keep as-is
- E-LLM consent/debrief structure — maintained throughout all three tests

---

## Out of Scope

- Memory tool return value optimization (separate work stream, see `MemoryToolReturnOptimization_Notes.md`)
- Agentic compaction (separate sprint)
