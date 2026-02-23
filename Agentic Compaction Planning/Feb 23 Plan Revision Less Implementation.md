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