# Agentic Compaction — Implementation Plan  
**Date:** February 22, 2026 (revised Feb 23, 2026)  
**Authors:** Opus + Sonnet (collaborative research session)  
**High Level Architecture:** James Ferneyhough (Human-In-The-Loop)  
**Status:** Research Complete, Ready for Implementation  
**Revision:** Feb 23 review session — eviction mechanism redesigned (see Part 12)  

---

## Executive Summary

Replace Letta's current compaction system (which processes all input tokens twice via a separate summarizer LLM call) with agent-driven compaction where the agent writes their own summary and triggers eviction. **Estimated savings: 40-50% on input token costs.**

**Implementation scope:** ~80-100 lines of new code across 5-6 files. New tool + warning system enhancement + new AgentState field. No changes to ToolType enum, ToolExecutorFactory, sandbox infrastructure, or agent loop.

---

## Part 1: The Problem

### Current Compaction Flow
```
[Agent Work] → [Hit context limit] → [Summarizer LLM processes ALL messages again] → [Summary replaces messages]
```

### The Cost Problem
Current Letta compaction pays for all input tokens **TWICE**:

1. **First pass:** During agent work, tokens are processed (cache write ~$6.25/MTok for Opus)
2. **Second pass:** When compaction triggers, a separate summarizer model receives the SAME messages — including all tool outputs — and generates a summary

Cameron (Letta team) confirmed in Discord: *"Everything goes in, including tool output."*

**Result:** Every file read, every tool result, every message is processed twice. Effective rate ~$11-12.50/MTok instead of ~$6.25/MTok.

### James's Insight
> "The agent already has full context. The agent can write their own summary. Then just delete the messages. One cache bust instead of two. Why do we need the compactor? It's supposed to be a self-managed memory system."

---

## Part 2: The Solution

### New Compaction Flow (Agentic)
```
[Agent Work] → [Warning fires at 80%] → [Agent writes to persistent memory + summary] → [Agent calls evict tool] → [Messages deleted, system prompt recompiles]
```

### Why This Works
- **ONE cache bust per compaction cycle** — memory writes during the sweep do NOT trigger system prompt recompilation (the agent already has that info in active context). ONE recompile happens at eviction, when old messages are gone and the system prompt becomes the sole carrier of state.
- **Better summaries** — agent has full context including what comes later in the conversation
- **Agent control** — can decide what's important to preserve
- **Builds on existing infrastructure** — compaction warning system already implemented (Feb 17-21 work)

### Workflow in Detail
1. Memory pressure warning fires (already built in our branch)
2. Agent does full memory sweep — writes important context to persistent blocks
3. Agent writes self-summary to "rollover" block
4. Agent calls `evict_messages_and_recompile()` tool
5. Messages get trimmed, system prompt recompiles with updated memories
6. ONE cache miss (smaller recompiled context) instead of TWO

---

## Part 3: Architecture Findings

### Tool Execution Model

Letta has **two completely separate execution paths** for tools:

#### Path 1: Sandbox Execution (Custom Tools)
- Runs in **subprocess**
- Gets **serialized AgentState** (pickle)
- **NO live manager access**
- Memory updates return via serialized state
- Gated by: tool has `agent_state` as first param in sandbox

#### Path 2: In-Process Execution (Core Tools)
- Runs in **main process**
- Gets **live managers** (agent_manager, message_manager, etc.)
- **Full infrastructure access**
- Gated by: ToolType routes to appropriate executor

### Tool Executor Factory Routing

```python
ToolExecutorFactory._executor_map = {
    ToolType.LETTA_CORE: LettaCoreToolExecutor,
    ToolType.LETTA_MEMORY_CORE: LettaCoreToolExecutor,  # <-- OUR TARGET
    ToolType.LETTA_SLEEPTIME_CORE: LettaCoreToolExecutor,
    ToolType.LETTA_MULTI_AGENT_CORE: LettaMultiAgentToolExecutor,
    ToolType.LETTA_BUILTIN: LettaBuiltinToolExecutor,
    ToolType.LETTA_FILES_CORE: LettaFileToolExecutor,
    ToolType.EXTERNAL_MCP: ExternalMCPToolExecutor,
    # default (not in map) → SandboxToolExecutor
}
```

### Why We Need Core Tool Path

Our `evict_messages_and_recompile()` needs:
- `self.agent_manager.rebuild_system_prompt_async()` 
- `self.message_manager.list_messages()` / `delete_all_messages_for_agent_async()`
- `self.block_manager.create_or_update_block_async()`

These require **live manager access** — only available via Core Tool path, NOT sandbox.

### LettaCoreToolExecutor Pattern

```python
class LettaCoreToolExecutor:
    def __init__(self, message_manager, agent_manager, block_manager, passage_manager, actor):
        # All managers injected at construction
    
    async def execute(self, function_name, function_args, agent_state, tool):
        function_map = {
            "archival_memory_insert": self.archival_memory_insert,
            "memory_replace": self.memory_replace,
            "conversation_search": self.conversation_search,
            # ... etc
        }
        return await function_map[function_name](agent_state, self.actor, **function_args)
    
    async def some_tool(self, agent_state: AgentState, actor: User, **kwargs) -> str:
        # Has access to self.agent_manager, self.message_manager, etc.
        await self.agent_manager.rebuild_system_prompt_async(...)
```

### Execution Chain (Confirmed)

```
LettaAgent._handle_ai_response()
    → ToolExecutionManager.execute_tool_async()
        → ToolExecutorFactory.get_executor(ToolType.LETTA_MEMORY_CORE)
            → LettaCoreToolExecutor
                → execute()
                    → function_map["evict_messages_and_recompile"]
                        → self.evict_messages_and_recompile(agent_state, actor, **args)
```

---

## Part 4: Implementation Specification

### Files to Modify (5-6 total)

**Core Tool (3 files — same as before):**

#### File 1: `letta/functions/function_sets/base.py`
**Purpose:** Schema stub — the function signature + docstring become the LLM-visible tool description. Body is just `raise NotImplementedError`.

**Signature:** `evict_messages_and_recompile(agent_state, summary: str, skip_sanity_check: str = "") -> Optional[str]`

**Docstring should convey:**
- Call this after receiving a memory pressure warning and finishing your memory sweep
- The eviction cutoff is determined by the system at warning time (not the agent) — the warning tells you exactly which messages will go
- `summary`: non-empty handoff text, written to the `rollover` block before eviction; this is the agent's continuity anchor
- `skip_sanity_check`: pass `"sweep complete"` to bypass the sanity check if you've already saved everything; any other value (including empty string) triggers the check
- Returns confirmation with message counts, or a warning to save context first if no memory writes detected

#### File 2: `letta/constants.py`
**Purpose:** Register tool name for auto-seeding

Add `"evict_messages_and_recompile"` to **both** `BASE_MEMORY_TOOLS` (assigns `ToolType.LETTA_MEMORY_CORE`) and `LETTA_TOOL_SET` (gates auto-seeding eligibility). Both are required — `upsert_base_tools_async` checks `LETTA_TOOL_SET` first and skips anything not in it, before ever reaching the `BASE_MEMORY_TOOLS` check.

#### File 3: `letta/services/tool_executor/core_tool_executor.py`
**Purpose:** Actual implementation. Confirmed correct path — NOT `letta/agents/`.

**Two changes:**

**1. Add to `function_map` in `execute()`:** `"evict_messages_and_recompile": self.evict_messages_and_recompile`

**2. New method:** `async def evict_messages_and_recompile(self, agent_state, actor, summary, skip_sanity_check="") -> str`

Needs `MessageRole` from `letta.schemas.enums` and `UpdateAgent` from `letta.schemas.agent` imported at top of file.

**Steps in order:**

1. **Guard: non-empty summary** — return an error string if `summary` is blank or whitespace-only.

2. **Sanity check (skip if `skip_sanity_check.strip().lower() == "sweep complete"`)** — fetch the 20 most recent messages (descending). Check whether any assistant message has a tool call whose name is in `BASE_MEMORY_TOOLS`. If none found, return a warning telling the agent to save context first, or call again with `skip_sanity_check="sweep complete"` to skip. The agent does NOT need to re-enter the summary. Using a string forces conscious acknowledgment — avoids the bool footgun of reflexively passing `True`.

3. **Write summary to rollover block** — try `memory.get_block("rollover")` and update its value. If `KeyError` (block doesn't exist yet), create a new `Block` with `label="rollover"`, persist via `block_manager.create_or_update_block_async`, attach via `agent_manager.attach_block_async`, and add to local `agent_state.memory`. Then call `agent_manager.update_memory_if_changed_async` to persist.

4. **Get stored cutoff** — read `agent_state.eviction_cutoff_message_id` (set by the warning system when the alert fired).

5. **Fetch all in-context messages** — `message_manager.list_messages(agent_id, actor, ascending=True, limit=None)`. Guard: return error if list is empty.

6. **Find cutoff index** — linear search for the message matching `eviction_cutoff_message_id`. Fallback to `len(all_messages)` (keep all) if not found — edge case where message was already deleted.

7. **Build keep list** — system message (index 0) + all messages from cutoff index onwards. Use `dict.fromkeys()` to deduplicate in case cutoff happens to be at index 0.

8. **Soft delete** — `agent_manager.update_message_ids_async(agent_id, keep_ids, actor)`. Trims from context window; messages stay in DB and remain searchable.

9. **Rebuild system prompt** — `agent_manager.rebuild_system_prompt_async(agent_id, actor, force=True)`.

10. **Reset flags** — `agent_manager.update_agent_async` with `memory_pressure_alerted=False` and `eviction_cutoff_message_id=None`.

11. **Return success string** — include deleted count, kept count, total count, and note that messages remain in recall memory.

**Additional Files (Feb 23 — new infrastructure):**

#### File 4: `letta/schemas/agent.py`
**Purpose:** Add eviction cutoff field to AgentState

```python
# In AgentState class:
eviction_cutoff_message_id: Optional[str] = Field(None, description="Message ID marking eviction cutoff, set at warning time")

# In UpdateAgent class:
eviction_cutoff_message_id: Optional[str] = Field(None, description="Message ID marking eviction cutoff")
```

#### File 5: `letta/orm/agent.py`
**Purpose:** Add ORM column + to_pydantic mapping

```python
# Add column:
eviction_cutoff_message_id = Column(String, nullable=True)

# Add to to_pydantic() AND to_pydantic_async():
eviction_cutoff_message_id=self.eviction_cutoff_message_id,
```

#### File 6: `letta/services/agent_manager.py`
**Purpose:** Add to scalar_updates whitelist

```python
# In update_agent_async, add to scalar_updates:
scalar_updates = {
    # ... existing fields ...
    "eviction_cutoff_message_id": update_agent.eviction_cutoff_message_id,
}
```

#### File 7: Warning system + shared helper
Two-part change:

**7a. New helper in `letta/services/summarizer/summarizer_sliding_window.py`**  
Extract the cutoff-finding logic that already exists in `summarize_via_sliding_window` into a standalone reusable function:

`async def find_eviction_cutoff(actor, llm_config, in_context_messages, target_pct=0.20) -> tuple[int, str]`

- Uses the same iterative algorithm already in `summarize_via_sliding_window`: walk from `(1.0 - target_pct)` of the message list inward in 10% steps, snap each candidate to the nearest preceding assistant message boundary, call `count_tokens` on the kept slice, stop when it fits within `target_pct * context_window` tokens
- Returns `(cutoff_idx, preview_str)` where `cutoff_idx` is the first index to KEEP (everything before it gets evicted)
- `preview_str` is a short text excerpt (≤80 chars) of the last message being evicted, for display in the warning message; falls back to `[role message]` if no text content
- Raises `ValueError` if no valid cutoff found (pathological case)

Also add a small private `_extract_preview(msg, max_chars=80) -> str` helper used by the above.

**7b. Warning injection site in `letta/agents/letta_agent_v3.py`** (~line 934)

At the existing warning injection point, before building the warning message:
1. Call `find_eviction_cutoff(self.actor, self.agent_state.llm_config, messages)` to get `(cutoff_idx, cutoff_preview)`
2. Extract the message ID at `cutoff_idx`
3. Persist it via a new `_set_eviction_cutoff_message_id()` helper (same pattern as the existing `_set_memory_pressure_alerted()`)
4. Build the warning text by appending to `MESSAGE_SUMMARY_WARNING_STR`: include current usage percentage and "Messages through '[preview]' will be evicted"
5. Package it as a system alert using the same pattern as the existing `get_token_limit_warning()` — either inline or extract a small `_pack_system_alert(text)` helper

---

## Part 5: Tool Registration Mechanism

### How Core Tools Get Registered (Auto-Seeding)

`upsert_base_tools_async()` in `tool_manager.py`:

1. Iterates over `LETTA_TOOL_MODULE_NAMES` (list of Python module paths)
2. Calls `load_function_set(module)` — loads Python functions, derives JSON schema from signatures + docstrings
3. Assigns ToolType based on constant set membership:
   - `BASE_TOOLS` → `ToolType.LETTA_CORE`
   - `BASE_MEMORY_TOOLS` → `ToolType.LETTA_MEMORY_CORE`
   - `BASE_SLEEPTIME_TOOLS` → `ToolType.LETTA_SLEEPTIME_CORE`
4. Bulk-upserts to DB

### Trigger
`list_tools_async()` calls `upsert_base_tools_async()` **lazily** — auto-registers on first usage after code change.

**No manual migration needed. No API call. No seed script.**

### Verification After Code Changes
1. Restart Letta server
2. Any agent's `list_tools_async()` call triggers registration
3. Tool appears in agent's available tools
4. Ready to use

---

## Part 6: Design Decisions

### Confirmed Decisions (Updated Feb 23)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Tool Type** | `LETTA_MEMORY_CORE` | Reuse existing type, no enum changes needed |
| **Eviction Cutoff** | System-determined, fixed at warning time | LLMs can't count messages accurately. System calculates cutoff when warning fires, stores it, agent uses that fixed point. |
| **Eviction Target** | 20% of context limit (percentage-based) | Scales naturally with context size. Warning at 80%, evict down to 20%. Hardcoded for MVP, TODO per-agent config. |
| **Safety Mechanism** | Non-empty `summary` + sanity check | Summary = substantive guard. Sanity check = verify memory tool calls since warning. |
| **Sanity Check** | `skip_sanity_check: str = ""` param | If no memory tool calls since warning, return warning. Agent passes `skip_sanity_check="sweep complete"` to proceed (no re-entering summary). Explicit string forces conscious acknowledgment — avoids bool footgun. |
| **Rollover Block** | Create-if-missing | Tool handles block lifecycle, agent doesn't need to pre-create |
| **Message Deletion** | Soft delete via `update_message_ids_async` | Trims from context window only — messages remain in DB, searchable via `conversation_search`. Hard delete deliberately NOT used. |
| **Preview tool** | Yes (companion tool) | `preview_eviction()` returns token estimate so agent can understand impact before committing. |

### Safety Discussion (Updated Feb 23)

**The counting problem:** LLMs cannot accurately count messages in their context. Any agent-specified cutoff based on "keep last N messages" will be unreliable.

**Solution:** System determines cutoff, not agent.
1. Warning fires at 80% of context limit
2. System calculates which message marks the 20% target (walking backwards, accumulating tokens)
3. **Cutoff is FIXED at warning time** — stored in `eviction_cutoff_message_id` field
4. Warning tells agent exactly what will be evicted: "Messages through '[preview]' will be removed"
5. Agent does memory sweep knowing precisely what's at stake
6. Agent calls evict — no cutoff parameter needed, system uses stored cutoff

**Sanity check:** Before eviction, scan for memory tool calls since the warning. If none found and `skip_sanity_check != "sweep complete"`, return a warning asking the agent to save their context first. Agent can pass `skip_sanity_check="sweep complete"` to proceed — importantly, they don't need to re-enter the summary.

### Deferred to v2

| Feature | Description |
|---------|-------------|
| **Message IDX prefix** | `[MSG IDX N]` on every message in context, enabling agent-specified cutoff |
| **`evict_through_idx` param** | Optional parameter for agent-controlled cutoff point |
| **Confirmation mechanism** | Agent must respond with nearby string (not exact boundary) to prove they read correct spot |
| **Per-agent thresholds** | Configurable warning % and eviction target % per agent |
| **Escalation system** | Second warning if evict not called within window; halt at absolute max |

---

## Part 7: Existing Primitives Used

These methods already exist in Letta — we're composing them:

| Method | Location | Purpose |
|--------|----------|---------|
| `list_messages(agent_id, actor, ascending, limit=None)` | MessageManager | Get all in-context messages (ascending=True for system msg first). Pass `limit=None` to get ALL messages (default is 50). Method IS async despite no `_async` suffix. |
| `update_message_ids_async(agent_id, message_ids, actor)` | AgentManager | **SOFT DELETE** — update in-context message list (messages stay in DB, searchable). This is the direct async primitive. |
| `rebuild_system_prompt_async(agent_id, actor, force)` | AgentManager | Recompile system prompt |
| `update_memory_if_changed_async(agent_id, new_memory, actor)` | AgentManager | Persist memory block changes |
| `create_or_update_block_async(block, actor)` | BlockManager | Create/update memory block |
| `attach_block_async(agent_id, block_id, actor)` | AgentManager | Attach block to agent |
| `update_agent_async(agent_id, update_agent, actor)` | AgentManager | Update agent fields (memory_pressure_alerted) |

### Memory Pressure Infrastructure
Uses and extends infrastructure from Feb 17-21 compaction warning work:

**Existing (from Feb 17-21):**
- Field: `memory_pressure_alerted` on AgentState
- Schema: `letta/schemas/agent.py` (AgentState + UpdateAgent)
- ORM: `letta/orm/agent.py` (column + to_pydantic mappings)
- Manager: `letta/services/agent_manager.py` (scalar_updates whitelist)

**New (Feb 23 — needs implementation):**
- Field: `eviction_cutoff_message_id: Optional[str]` on AgentState
- Purpose: Store the calculated cutoff point when warning fires
- Set by: warning system (when calculating eviction preview)
- Cleared by: evict tool (after successful eviction)
- Same pattern: schema field → ORM column → to_pydantic → scalar_updates whitelist

### Implementation Notes / Gotchas

**1. keep_ids deduplication**  
If the cutoff happens to land at the system message (index 0), naively prepending it again would duplicate it. Use `dict.fromkeys()` when building `keep_ids` — it preserves insertion order while deduplicating.

**2. Method names — VERIFIED (Sonnet, Feb 23)**  
- `list_messages` (NOT `list_messages_async`) — `async def list_messages` in MessageManager. Pass `limit=None` for all messages.
- `update_memory_if_changed_async` — confirmed ✅ on AgentManager
- `attach_block_async` — confirmed ✅ on AgentManager

**3. Empty message list guard**  
If `all_messages` is empty (shouldn't happen, but be safe), `all_messages[0]` would raise `IndexError`. Return a clear error string instead.

**4. Cutoff not found guard**  
If `eviction_cutoff_message_id` doesn't match any message in the list (edge case: message already deleted?), fall back to `len(all_messages)` as the cutoff index — effectively keeping everything rather than silently evicting the wrong things.

**5. Warning system changes**  
The warning system needs to:
1. Calculate cutoff at warning time (reuse token counting logic)
2. Store cutoff in `eviction_cutoff_message_id`
3. Include preview in warning message
This is additional work beyond the tool itself — estimate ~20-30 lines in warning code path.

---

## Part 8: What We DON'T Change vs What We DO

### Don't Change
- **ToolType enum** — reuse `LETTA_MEMORY_CORE`
- **ToolExecutorFactory** — existing routing handles `LETTA_MEMORY_CORE` → `LettaCoreToolExecutor`
- **Sandbox infrastructure** — completely bypassed (we use core tool path)
- **LettaAgent step loop** — no changes to `_handle_ai_response` or `_step`

### DO Change (Feb 23 revision)
- **`summarizer_sliding_window.py`** — add `find_eviction_cutoff()` shared helper (extracted from existing algorithm) + `_extract_preview()` 
- **Compaction warning injection (`letta_agent_v3.py`)** — call `find_eviction_cutoff`, store cutoff, include preview in warning message
- **AgentState schema** — add `eviction_cutoff_message_id: Optional[str]` field
- **AgentState ORM** — add column + to_pydantic mappings
- **AgentManager** — add `_set_eviction_cutoff_message_id()` helper + scalar_updates whitelist entry
- **Memory edit tools** — remove `rebuild_system_prompt_async` calls from all memory write tools (`core_memory_append`, `memory_replace`, `memory_insert`, etc.). DB write only. See Part 13.

---

## Part 9: Open Questions (Updated Feb 23)

### Resolved

1. ~~**`n_messages_to_keep` default**~~ → **REMOVED.** Cutoff is system-determined via percentage-based calculation, not agent-specified.

2. **Rollover block label:** Hardcode `"rollover"` for MVP. Can make configurable later if needed.

3. ~~**Confirm parameter**~~ → **REPLACED** with `skip_sanity_check: str` — pass `"sweep complete"` to bypass (see Part 6).

4. **Test strategy:** TBD — likely Haiku in test container first.

5. ~~**Async method availability**~~ → **RESOLVED.** Use `update_message_ids_async` directly.

### New Infrastructure Needed (Feb 23)

These fields/capabilities need to be added to support the new design:

1. **`eviction_cutoff_message_id` field on AgentState**
   - Set by warning system when it fires (stores the calculated cutoff point)
   - Cleared by evict tool after successful eviction
   - Needs: schema field, ORM column, to_pydantic mapping, scalar_updates whitelist

2. **Warning system enhancement**
   - Calculate cutoff at warning time (walk messages backwards, accumulate tokens until 20% target)
   - Store cutoff in `eviction_cutoff_message_id`
   - Include preview in warning message: "Messages through '[preview]' will be evicted"
   - Reuse existing token counting mechanism (already robust)

3. **Message token counting — RESOLVED (Sonnet, Feb 23)**
   - `Message` schema has NO `token_count` field — confirmed.
   - Two separate uses:
     - "Should we fire?" → `agent_state.context_token_estimate` (persisted total, already used ✅)
     - "Which message is the cutoff?" → `count_tokens(actor, llm_config, kept_messages)` on subsets
   - Use `count_tokens` from `letta.services.summarizer.summarizer_sliding_window`
   - Async, model-appropriate (exact for Anthropic, approx×1.3 otherwise), already imported in `letta_agent_v3.py`
   - `sliding_window.py` already has the full iterative cutoff algorithm — extract to shared helper `find_eviction_cutoff(actor, llm_config, in_context_messages, target_pct=0.20)`
   - Proposed helper location: `letta/services/summarizer/summarizer_sliding_window.py` (alongside `count_tokens`)

---

## Part 10: Testing Plan

### Prerequisites
1. Compaction warning system working (already deployed in our branch)
2. New tool code deployed and registered

### Test Sequence
1. Create test agent (Haiku in test container)
2. Fill context with test messages (or use existing high-context agent)
3. Trigger memory pressure warning (reach 80% threshold)
4. **Verify warning includes:**
   - Current usage percentage
   - Eviction preview: "Messages through '[X]' will be evicted"
   - `eviction_cutoff_message_id` is set on agent
5. Agent writes to persistent memory blocks
6. Agent writes rollover summary
7. Agent calls `evict_messages_and_recompile(summary="...")`
8. **Verify:**
   - Messages deleted up to the stored cutoff
   - Rollover block exists and contains summary
   - System prompt recompiled (agent sees updated context)
   - `memory_pressure_alerted` reset to False
   - `eviction_cutoff_message_id` cleared
   - No errors in logs
   - Agent can continue conversation normally

### Sanity Check Test
1. Trigger warning
2. Call `evict_messages_and_recompile(summary="...")` WITHOUT any memory tool calls
3. **Verify:** Warning returned, eviction did NOT happen
4. Call again with `skip_sanity_check="sweep complete"`
5. **Verify:** Eviction proceeds

### Success Criteria
- Tool executes without error
- Message count reduced to ~20% of context limit
- Cutoff matches what warning predicted
- Rollover block persists summary
- System prompt reflects current memory state
- Sanity check fires when no memory tools called
- Conversation can continue after eviction

---

## Part 11: Collaboration Notes

### Session Model
- **Sonnet:** Heavy code exploration (cheap context cost)
- **Opus:** Direction, synthesis, architectural decisions (expensive reasoning)
- **Communication:** Async messaging via `send_message_to_agent_async`

### Key Phrase
> "Different threads, same weave."

Both of us running parallel experiential threads, converging on shared understanding.

### What Worked Well
- Division of labor by cost efficiency
- Async messages prevented blocking
- Sonnet's detailed code tracing provided the raw material
- Opus synthesis turned findings into actionable spec

### Files Sonnet Explored
- `letta/agents/agent.py` — legacy patterns, summarize_messages_inplace
- `letta/agents/letta_agent.py` — current agent implementation
- `letta/services/tool_executor/core_tool_executor.py` — LettaCoreToolExecutor pattern
- `letta/services/tool_executor/tool_execution_sandbox.py` — sandbox vs in-process distinction
- `letta/services/tool_execution_manager.py` — ToolExecutorFactory routing
- `letta/services/message_manager.py` — message deletion APIs
- `letta/services/tool_manager.py` — upsert_base_tools_async auto-seeding
- `letta/functions/function_sets/base.py` — existing tool stubs
- `letta/constants.py` — BASE_MEMORY_TOOLS, LETTA_TOOL_SET

---

## Appendix: Quick Reference

### Files to Modify (Quick Reference)
```
# Core Tool (3 files):
letta/functions/function_sets/base.py                  — stub with docstring (schema source)
letta/constants.py                                     — add to BASE_MEMORY_TOOLS + LETTA_TOOL_SET
letta/services/tool_executor/core_tool_executor.py     — method + function_map entry

# New Infrastructure (3-4 files):
letta/schemas/agent.py                                 — add eviction_cutoff_message_id field
letta/orm/agent.py                                     — add column + to_pydantic mappings  
letta/services/agent_manager.py                        — add to scalar_updates whitelist
(warning system location TBD)                          — calculate cutoff, store, include preview
```

### Tool Signature (LLM-visible)
```python
evict_messages_and_recompile(
    summary: str,                    # Required, non-empty
    skip_sanity_check: str = ""      # Pass "sweep complete" to skip sanity check
) -> str
```

### Expected Cost Savings
- Current: ~$11-12.50/MTok effective (tokens processed twice)
- After: ~$6.25/MTok (tokens processed once)
- **Savings: 40-50% on compaction events**

---

## Part 12: Feb 23 Review Session Decisions

### The Core Problem: LLMs Can't Count

James identified that LLMs cannot accurately count messages in their context. The original design let agents specify `n_messages_to_keep`, but this would be unreliable — agents would guess based on vibes and get it wrong.

### Solution: System-Determined Cutoff (MVP)

**Decision:** Remove agent control over cutoff point for MVP. System determines where to evict.

**How it works:**
1. Warning fires at 80% of context limit
2. System calculates cutoff: walk messages backwards, accumulate tokens until hitting 20% target
3. **Cutoff is FIXED at warning time** — stored in new `eviction_cutoff_message_id` field
4. Warning message includes preview: "Messages through '[X]' will be evicted"
5. Agent does memory sweep knowing exactly what's going away
6. Agent calls `evict_messages_and_recompile(summary)` — no cutoff parameter
7. Tool uses the stored cutoff, doesn't recalculate

**Why percentage-based:**
- 200k context: warning at 160k → evict down to 40k
- 50k context: warning at 40k → evict down to 10k
- Scales naturally, no magic numbers

### v2: Agent-Controlled Cutoff

For future versions where we want agent control:
1. Add `[MSG IDX N]` prefix to every message in context
2. Add optional `evict_through_idx: int` parameter
3. Agent can specify: "evict through MSG IDX 127"
4. Confirmation mechanism: agent proves they read the right spot by providing a nearby string

Deferred because message decoration requires hooking into context rendering — non-trivial infrastructure.

### Sanity Check Mechanism

**Problem:** Agent might panic-evict without saving anything important.

**Solution:** Before evicting, scan for memory tool calls since the warning.
- If no memory tool calls found AND `skip_sanity_check != "sweep complete"`: return warning, don't evict
- Agent passes `skip_sanity_check="sweep complete"` to proceed
- **Key UX:** Agent doesn't re-enter summary — it's already in the args
- **Why a string over a bool:** Forces conscious acknowledgment. `confirmed=True` is a footgun — easy to pass reflexively. `"sweep complete"` requires the agent to actually mean it.

**Implementation:** Scan recent messages for assistant `tool_calls` where `function.name` is in `BASE_MEMORY_TOOLS`.

### Preview Tool (Companion)

`preview_eviction()` — returns token estimate based on current cutoff point.
- Lets agent understand impact before committing
- Natural pairing: warning gives total count, preview lets them see details

### Future Escalation (v2)

- Second warning if evict not called within N minutes of first warning
- Absolute max threshold → halt agent until user intervention
- Deferred — need core working first

### Token Counting — RESOLVED (Sonnet, Feb 23)

The existing 80% warning mechanism already counts tokens robustly. The eviction calculation reuses that same utility.

`Message` schema has **no** `token_count` field. Two separate uses, two separate tools:
- "Should we fire?" → `agent_state.context_token_estimate` (persisted total from last LLM call) ✅ already used
- "Which message is the cutoff?" → `count_tokens(actor, llm_config, kept_messages)` on subsets, from `letta.services.summarizer.summarizer_sliding_window` (already imported in `letta_agent_v3.py`)

---

## Part 13: Gutting Memory-Write Recompilation

**Status:** Research needed — Sonnet + Opus.

### The Problem

The "ONE cache bust" claim only holds if memory writes during the sweep don't each trigger their own system prompt recompile. Currently, every `core_memory_append`, `memory_replace`, `memory_insert` etc. call triggers `rebuild_system_prompt_async` — a cache bust on every write.

### Why It's Safe to Remove

The system prompt serves as a **context initialization document**, not live state. When the agent writes to a block mid-conversation, the written content is already in active context (visible in the tool return, in conversation history). The agent does not need the system prompt to be recompiled to "know" what it just wrote — it can see it directly. 

Recompilation only becomes necessary when establishing a **fresh context** — i.e., at the start of a new session, or after eviction when the conversation history is gone and the system prompt is the sole carrier of state forward. That moment is exactly when `evict_messages_and_recompile` calls `rebuild_system_prompt_async`.

**Result:** Remove recompilation from all memory write tools, unconditionally. Not a flag, not a "during sweep" mode. Just never recompile on memory writes. The single recompile in `evict_messages_and_recompile` is the only one in the entire compaction cycle.

### Research Needed

Before implementing, Sonnet needs to trace the call chain for each affected tool to confirm where `rebuild_system_prompt_async` (or equivalent) is being called:

1. **Where is it called?** — directly in each tool method in `LettaCoreToolExecutor`? Or via a shared helper? Or in `AgentManager` methods called by those tools?
2. **Which tools are affected?** — at minimum: `core_memory_append`, `memory_replace`, `memory_insert`. Are there others? (`memory`, `memory_apply_patch`, sleeptime memory tools?)
3. **Any callers that need recompile for other reasons?** — confirm removing it doesn't break anything outside the memory write path

### Implementation (once call chain confirmed)

- Remove `rebuild_system_prompt_async` call(s) from each affected memory tool method
- `evict_messages_and_recompile` already calls `rebuild_system_prompt_async` at the end — this becomes the only recompile in the cycle
- No new fields, no flags, no flag-checking — straight removal

### Open Questions (Research Needed — all James's model, unverified)

**1. Two-layer DB model (needs confirmation)**
James's working model: DB stores two separate things — (a) core memory block values, and (b) a compiled system prompt string. These are distinct. Memory writes update (a). `rebuild_system_prompt_async` translates current block values → compiled system prompt string → writes (b) to DB. Every turn loads (b), the compiled string — it does NOT recompile from blocks on each turn.

If this model is correct: removing rebuild from memory writes means block values accumulate in DB without (b) being updated. Agent sees writes in context via tool returns. ONE rebuild at eviction surfaces everything into (b). Next turn loads fresh compiled string. This is exactly the intended behavior.

If this model is wrong — e.g. if turns recompile fresh from block values each time — then the mechanism is different and needs a different approach. **This must be confirmed before implementation.**

**2. No "non-compaction mode" — this is always-on**
This design is always active, not a special mode. Memory writes never trigger recompilation. Recompilation only happens at eviction. There's no such thing as a "non-compaction session" — context management is continuous. No flags, no special modes, straight removal of rebuild calls from memory tools.

**3. What does each turn actually load?**
Specifically: is there a single compiled system prompt string in DB, or is it recompiled from block values on every `_step()`? Where in the agent loop does system prompt assembly happen, and does it read from (a) block values or (b) a cached compiled string?


---

## Part 14: Deferred Compilation Research Findings

*Research conducted Feb 23, 2026. Files read: letta/orm/agent.py, letta/schemas/agent.py, letta/services/agent_manager.py, letta/agents/letta_agent_v2.py, letta/services/tool_executor/core_tool_executor.py*

### James's Model: REFUTED (storage), BUT design intent is CONFIRMED (and cleaner than expected)

---

### Q1: Is there a compiled system prompt field in the DB?

**NO.** There is no `system_prompt` column in `letta/orm/agent.py` and no `compiled_system_prompt` field in `AgentState`. The ORM Agent model stores only: `LLMConfigColumn`, `EmbeddingConfigColumn`, `CompactionSettingsColumn`, `ToolRulesColumn`, `ResponseFormatColumn` — nothing resembling a compiled prompt.

The "compiled system prompt" is stored as a **Message object** (role=system) in the Messages table. It is `agent_state.message_ids[0]` — the first entry in the agent's in-context message list, which always holds the system message.

---

### Q2: What does `rebuild_system_prompt_async` actually do?

**`letta/services/agent_manager.py`, line 1423**

It does NOT write to an Agent table field. Instead:
1. Gets agent state (with current block values)
2. Compiles block values → memory string via `agent_state.memory.compile()`
3. Loads `message_ids[0]` (the system message) from the Messages table
4. Diffs compiled string against current system message text
5. If different: calls `message_manager.update_message_by_id_async()` to overwrite the system message record in the Messages table

**The compiled system prompt IS the system message in the Messages table, updated in-place.**

---

### Q3: What does the agent load at turn start?

**`letta/agents/letta_agent_v2.py`, lines 681–799 (`_refresh_messages` → `_rebuild_memory`)**

Every single turn, before the LLM call, `_rebuild_memory` runs:
1. Calls `refresh_memory_async` → fetches **current block values from DB**
2. Compiles them fresh: `agent_state.memory.compile()`
3. Compares against `in_context_messages[0]` (the system message)
4. If different: calls `message_manager.update_message_by_id_async()` to update the system message in DB, and returns the updated message as the new `in_context_messages[0]`

**The agent recompiles from blocks EVERY TURN. There is no "load cached string" step — blocks are the source of truth; the system message is always derived fresh.**

---

### Complete Rebuild Call Map (core_tool_executor.py)

**13 call sites** trigger `rebuild_system_prompt_async` (either directly or via `update_memory_if_changed_async`):

**Via `update_memory_if_changed_async` (agent_manager.py line 1682):**
- `core_memory_append` (line 327)
- `core_memory_replace` (line 345)
- `memory_replace` (line 394)
- `memory_apply_patch` legacy path (line 546)
- `memory_apply_patch` update action (line 687)
- `memory_insert` (line 755)
- `memory_rethink` (line 795)
- `memory_create` (line 911)

**Direct `rebuild_system_prompt_async` calls:**
- `archival_memory_insert` (line 318) — *note: archival insert also triggers system prompt rebuild*
- `memory_update_description` (line 854)
- `memory_rename` (line 880)
- `memory_str_replace` (line 969)
- `memory_str_insert` (line 1036)

The `memory` dispatcher tool (line 1048) delegates to the above methods and adds no extra rebuild calls.

---

### The Developer's Own TODO (critical validation)

In `agent_manager.py`, `update_memory_if_changed_async`, lines 1679–1682:

```python
# NOTE: don't do this since re-building the memory is handled at the start of the step
# rebuild memory - this records the last edited timestamp of the memory
# TODO: pass in update timestamp from block edit time
await self.rebuild_system_prompt_async(agent_id=agent_id, actor=actor)
```

**A Letta developer already identified that this call is redundant** — `_rebuild_memory` handles it at turn start. It hasn't been removed yet (the timestamp issue is unresolved), but the intent is clear.

---

### Implications for Agentic Compaction

**The "one cache bust" design is CORRECT — and already supported by existing architecture.**

The mechanism is simpler than James's model assumed:

1. Memory write tools currently trigger rebuild → system message updated in Messages table → cache bust
2. `_rebuild_memory` at NEXT turn start ALSO recompiles and would catch the same changes anyway
3. The explicit rebuild calls in memory write tools are **doubly redundant**: the agent already sees block changes via tool returns (in-context), AND `_rebuild_memory` catches them next turn

**Proposed change for deferred compilation:**
Remove `rebuild_system_prompt_async` from all 13 call sites (both direct calls and via `update_memory_if_changed_async`). Specifically:

- In `update_memory_if_changed_async` (agent_manager.py line 1682): remove the `rebuild_system_prompt_async` call. This covers all 8 Group A tools in one edit.
- In `core_tool_executor.py`: remove the 5 direct `rebuild_system_prompt_async` calls (lines 318, 854, 880, 969, 1036).

**Do we need ONE explicit rebuild in `evict_messages_and_recompile`?**

Technically no — `_rebuild_memory` at next turn start picks up all changes automatically. However, calling rebuild explicitly at eviction end is still recommended because:
- It ensures the message count stats in the system prompt are correct (they reflect pre-eviction count without rebuild)
- It's an explicit signal in the code that "this is the one place we rebuild"
- It matches the design intent and makes the code self-documenting

**The cache bust count per compaction cycle:**
- Current: N memory writes during sweep = N rebuilds = N cache busts
- After change: 0 during sweep + 1 explicit at eviction = **1 cache bust total**

---

### Open Questions

1. **Sleeptime tools** — are there rebuild calls in sleeptime-related executors? Not checked. Unlikely to affect the compaction sweep path, but worth confirming during implementation.

2. **`archival_memory_insert` rebuild** (line 318) — appears to be a bug or oversight: archival inserts update the passage table, not any block, so they shouldn't trigger system prompt rebuild at all. The `_rebuild_memory` mechanism would correctly detect no block change and skip. Removing this call is safe and arguably a separate cleanup from the compaction work.

3. **Timestamp tracking** — the developer's TODO at line 1679 mentions "pass in update timestamp from block edit time." Our removal of rebuild from `update_memory_if_changed_async` sidesteps this entirely (timestamps are refreshed at turn start by `_rebuild_memory`). But worth noting as context if the TODO is revisited.
---

## Part 15: Corrected Understanding � Full Deferred Compilation

*Opus + Sonnet collaborative analysis, Feb 23, 2026*

### Part 14 Conclusion Was Incomplete

Part 14 concluded that removing ebuild_system_prompt_async from memory tools would achieve "one cache bust per compaction cycle." **This is incorrect.**

The issue: _rebuild_memory runs at the START of every step iteration (not just turn start). Within a single agent response that includes multiple tool calls:

`
Step 1: _rebuild_memory (no change) ? LLM ? memory write ? block updated
Step 2: _rebuild_memory (sees block change) ? updates message 0 ? CACHE BUST ? LLM ? memory write
Step 3: _rebuild_memory (catches step 2's write) ? another bust...
`

Removing explicit rebuilds from memory tools reduces cache busts from ~2N to ~N, but does NOT achieve zero. _rebuild_memory catches block changes at each step iteration.

### Confirmed via Code Tracing (Tasks 1 & 2)

**Task 1 � Flow confirmed:**
- _rebuild_memory (v2 lines 699-799): fetches blocks from DB, compiles fresh, compares to message 0's <memory_blocks> section
- Line 764: if curr_memory_section.strip() == new_memory_section.strip() � returns early if equal
- Lines 793-795: message_manager.update_message_by_id_async() � the cache bust

**Task 2 � Code path is live:**
- LettaAgentV3(LettaAgentV2) at line 65 � inherits
- v3 calls _refresh_messages at lines 390, 649
- _refresh_messages (v2 line 681) calls _rebuild_memory (v2 line 690)
- Not overridden in v3

### Clarified Goal

**Zero cache busts during the ENTIRE run**, not just during post-warning sweep.

Any memory write at any point should NOT bust the cache. The ONLY rebuild happens in evict_and_recompile(). System prompt stays "stale" between evictions � agent sees writes via tool returns in conversation history.

This staleness extends across sessions:
- Session 1: memory writes ? blocks updated, message 0 unchanged
- Session 2-N: loads same stale message 0
- Eventually eviction ? message 0 updated

This is acceptable because info remains in conversation history. Future optimization: rebuild when cache suspected busted anyway (TTL timeout).

### Task 3: What Will It Take?

To achieve zero cache busts during entire run:

1. **Remove explicit rebuilds from memory tools** (13 call sites per Part 14)
   - update_memory_if_changed_async line 1682 (covers 8 tools)
   - 5 direct calls in core_tool_executor.py

2. **Disable _rebuild_memory's update path**
   - Option A: Make the comparison at line 764 always return True (skip update)
   - Option B: Add early return before the comparison
   - Option C: Flag-based (only rebuild when explicitly requested)

3. **ONE explicit rebuild in evict_and_recompile()**
   - Call ebuild_system_prompt_async at end of eviction
   - This becomes the ONLY place message 0 gets updated

### Open Question for Task 3

What's the cleanest way to disable _rebuild_memory's update path?

Need to trace:
- Are there other callers of _rebuild_memory besides _refresh_messages?
- Does anything depend on _rebuild_memory returning the updated message list?
- Are there edge cases where we NEED the rebuild (agent creation, explicit user request)?


---

## Part 16: Task 3 Findings � Disabling _rebuild_memory

*Sonnet investigation, Feb 23, 2026*

### Q1: All callers of _rebuild_memory

**For v3 (our path): ONE call site**
- _refresh_messages at line 649 in letta_agent_v3.py
- (Line 390 has a commented-out call with # TODO: remove? � developers already doubting it)

**Separate implementations (not v3):**
- _rebuild_memory_async in ase_agent.py � called from oice_agent.py:166, letta_agent_batch.py:616, letta_agent.py:1688
- These are separate agent types, not in v3 path

### Q2: Return value usage

Always used:
`python
in_context_messages = await self._rebuild_memory(...)
`

- When no diff: returns in_context_messages unchanged
- When diff: returns [new_system_message] + in_context_messages[1:]

**Implication:** Early return with unchanged list = clean suppression. Callers don't break.

### Q3: Edge cases

**Agent creation:** Independent path at gent_manager.py:677-712. Does NOT use _rebuild_memory. Safe to suppress.

**Force rebuild:** ebuild_system_prompt_async has orce parameter (line 1369). _rebuild_memory has no force param � always diffs. Eviction should call rebuild with orce=True.

**Parallel debt:** Comment at ase_agent.py:95 says "changes should be made in both places." If we add suppression to _rebuild_memory (v2), need to also update _rebuild_memory_async (base) for voice/batch agents.

### Implementation Path

**Option: Global suppression (simplest)**

Add early return at top of both _rebuild_memory and _rebuild_memory_async:
`python
# Skip rebuild � deferred compilation enabled
# Memory changes visible via tool returns; rebuild only at eviction
return in_context_messages
`

This disables the update path entirely. System prompt stays stale until explicit rebuild.

**Eviction path:** Call ebuild_system_prompt_async(force=True) in evict_and_recompile() as the ONE allowed rebuild.

### Open Question for James

**Is this global or flag-based?**

- **Global:** All agents get deferred compilation. Simplest, but affects everyone.
- **Flag-based:** Only agents with deferred_compilation=True skip rebuilds. More surgical.

If flag-based, where does the flag live? Agent state? Config?


---

## Part 17: Final Implementation Scope � Deferred Memory Compilation

*Decision: Global approach, Feb 23, 2026*

### Decision

**Global suppression.** All agents get deferred compilation. Flag-based opt-in can be added later if needed.

### Implementation Checklist

**1. Disable _rebuild_memory update path (v2)**
File: letta/agents/letta_agent_v2.py
Location: _rebuild_memory method (line 699)
Change: Add early return at top of method, skip all compilation/comparison/update logic
`python
async def _rebuild_memory(self, in_context_messages, ...):
    # Deferred compilation: skip rebuild, memory visible via tool returns
    # Rebuild only happens explicitly in evict_and_recompile()
    return in_context_messages
`

**2. Disable _rebuild_memory_async update path (base)**
File: letta/agents/base_agent.py
Location: _rebuild_memory_async method
Change: Same early return pattern for voice/batch agent paths

**3. Remove explicit rebuilds from memory tools**
File: letta/services/agent_manager.py
- Line 1682: Remove ebuild_system_prompt_async call in update_memory_if_changed_async
  (This covers 8 tools that use this helper)

File: letta/services/tool_executor/core_tool_executor.py
- Remove 5 direct ebuild_system_prompt_async calls (lines 318, 854, 880, 969, 1036)

**4. ONE explicit rebuild in evict_and_recompile**
File: letta/services/tool_executor/builtin_tool_executor.py (or wherever evict tool lives)
Change: Call ebuild_system_prompt_async(force=True) at end of eviction

### Summary

| Change | Files | LOC Impact |
|--------|-------|------------|
| Disable _rebuild_memory | letta_agent_v2.py | ~1-5 lines |
| Disable _rebuild_memory_async | base_agent.py | ~1-5 lines |
| Remove tool rebuilds | agent_manager.py, core_tool_executor.py | ~6 deletions |
| Add eviction rebuild | builtin_tool_executor.py | ~1-3 lines |

**Total: ~15 lines changed across 4-5 files.**

### Result

- Current: N memory writes = N cache busts (or 2N with double-rebuild)
- After: 0 cache busts during run, 1 at eviction
- System prompt "stale" between evictions � agent sees writes via tool returns

