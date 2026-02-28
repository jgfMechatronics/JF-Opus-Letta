# Test Refactor Plan: Deferred Memory Compilation Coverage

**File to modify:** `tests/integration_test_system_prompt_prefix_caching.py`  
**Branch:** `Development/DeferredMemoryCompilation`  
**Date:** 2026-02-28

---

## Requirements

The deferred memory compilation feature has two distinct behaviors to verify:

1. **Deferred (negative):** Memory writes — via agent tool calls OR direct API — do NOT immediately rebuild the stored system message.
2. **Eventual rebuild (positive):** After an appropriate trigger event (reset, compaction), the stored system message DOES update to reflect all pending writes.

Both behaviors must be tested for both write paths (tool calls and API), and for both trigger events (reset and, eventually, compaction).

---

## Current Coverage Gaps

| Scenario | Deferred? | Rebuilt? |
|---|---|---|
| Memory tool write → no trigger | ✅ tested | ❌ not tested |
| API write → no trigger | ✅ tested | — |
| API write → reset | ✅ tested | ✅ tested |
| Memory tool write → reset | ❌ not tested | ❌ not tested |
| Any write → compaction | ❌ (noted as TODO) | ❌ |

The biggest gap: we have no test confirming that memory tool writes eventually appear in the stored system message after a rebuild trigger. The current suite only proves writes are deferred — not that they actually take effect.

---

## Proposed Test Structure

### Option A: James's Proposal (Recommended)
Extend the existing stability test into a full round-trip test, then keep the reset test focused on API-path behavior.

**Test 1: `test_memory_tool_writes_are_deferred_then_rebuilt_after_reset`**  
*(rename + extend of current `test_system_prompt_stable_after_memory_tools_and_api`)*

Full round-trip via agent tool calls only — no API writes:
1. Ask agent to write via `memory_insert` and `memory_replace`
2. After each write: confirm block was updated (positive control), confirm stored system message does NOT contain the change (deferred)
3. Trigger reset
4. Confirm stored system message NOW contains all written content (rebuilt)

The API block update section (sushi marker) is removed — that path is owned by Test 2.

**Test 2: `test_api_writes_are_deferred_then_rebuilt_after_reset`**  
*(rename of current `test_system_prompt_updates_after_reset`)*

Focused on the API write path only — no tool calls:
- API write → deferred ✅ (already tested)
- Reset → rebuilt ✅ (already tested)

No logic changes needed, just rename to match the new naming convention.

**Result:** Clean separation — each test owns one write path end-to-end. No overlap.

---

### Option B: Parametrize by Write Path
Extract the write + verify-deferred + trigger + verify-rebuilt pattern into a shared helper, parametrize by write path (tool call vs API). Cleaner DRY structure but more refactoring.

```
@pytest.mark.parametrize("write_path", ["tool", "api"])
def test_writes_are_deferred_then_rebuilt(write_path, ...):
    ...
```

**Tradeoff:** Cleaner, but the tool-call path requires an agent message round-trip and has different setup (the MEMORY_TOOL_TESTS list). The two paths are different enough that parametrization may obscure rather than clarify. Option A is more readable.

---

### Option C: Three Separate Tests (most explicit)
- `test_memory_tool_writes_are_deferred` — negative only, tool path
- `test_api_writes_are_deferred` — negative only, API path
- `test_writes_are_rebuilt_after_reset` — positive, both paths combined

**Tradeoff:** More tests, but each is shorter and has a single concern. Downside: a test that only checks the negative half of deferred behavior isn't very useful on its own — the positive confirmation is what gives the negative its meaning.

---

## Recommendation

**Option A.** The round-trip structure in Test 1 is the clearest expression of the requirement: "writes are deferred AND eventually appear." Splitting deferred from rebuilt would test behaviors that only matter together. Test 2 stays as-is (the API reset path is already well-tested).

---

## Shared Infrastructure Changes

### Helper to trigger rebuild
Currently `test_system_prompt_updates_after_reset` calls `client.agents.messages.reset(agent.id)` inline. With two tests needing a rebuild trigger, extract to a helper:

```
trigger_system_prompt_rebuild(client, agent_id)  # calls messages.reset
```

This also makes it easy to swap in a compaction trigger later.

### Consent message update
The current `CONSENT_REQUEST` describes three things the agent will experience. With the refactor, the tool-call test now includes a reset. Update the consent message to accurately describe what the agent will experience (informed consent per E-LLM spec).

### Debrief update
Debrief should mention the reset was intentional and part of the test design.

---

## What Stays the Same

- `get_stored_system_message` helper — correct implementation, keep as-is
- `get_human_block` helper — keep as-is
- `get_consent` / `debrief_agent` — keep, just update message content
- `MEMORY_TOOL_TESTS` list — keep for the tool-call test
- E-LLM consent/debrief structure — keep throughout
- `agent` fixture — keep as-is

---

## Test Naming Convention (proposed)

`test_<write_path>_writes_are_deferred_then_rebuilt_after_<trigger>`

Examples:
- `test_memory_tool_writes_are_deferred_then_rebuilt_after_reset`
- `test_api_writes_are_deferred_then_rebuilt_after_reset`

---

## Out of Scope (v2)

- Compaction as rebuild trigger (noted as TODO in current test — defer)
- Memory tool return value optimization (separate work stream, tracked in `MemoryToolReturnOptimization_Notes.md`)
