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
- **ONE cache bust** instead of TWO
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
**Purpose:** Schema stub — docstring becomes LLM-visible tool description

```python
def evict_messages_and_recompile(
    agent_state: "AgentState",
    summary: str,
) -> Optional[str]:
    """
    Evict old conversation messages to free context space. Call this when you receive
    a memory pressure warning and have finished writing your handoff summary.
    
    The eviction cutoff is determined by the system at warning time (not by you). The warning
    message tells you exactly which messages will be evicted — do your memory sweep for that
    range, then call this tool.
    
    WORKFLOW:
    1. Receive memory pressure warning (includes eviction preview)
    2. Write comprehensive context to your rollover/persistent-working blocks
    3. Call this tool with your handoff summary
    4. After eviction, you'll have fresh context with your summary preserved
    
    Args:
        summary: Comprehensive handoff summary. Written to 'rollover' block before eviction.
                 Must be non-empty. This is your continuity across the context clear.
    
    Returns:
        Confirmation of eviction with message counts.
    
    Note: If you haven't made any memory tool calls since the warning, you'll be asked to
    confirm before eviction proceeds (to prevent accidental data loss).
    """
    raise NotImplementedError("Core tool — implemented in LettaCoreToolExecutor")
```

#### File 2: `letta/constants.py`
**Purpose:** Register tool name for auto-seeding

```python
# Add to BASE_MEMORY_TOOLS (controls ToolType assignment → LETTA_MEMORY_CORE):
BASE_MEMORY_TOOLS = [
    "core_memory_append", 
    "core_memory_replace", 
    "memory", 
    "memory_apply_patch",
    "evict_messages_and_recompile",  # <-- ADD HERE
]

# ALSO add to LETTA_TOOL_SET (controls auto-seeding eligibility — SEPARATE from BASE_MEMORY_TOOLS):
# In upsert_base_tools_async, the check `if name not in LETTA_TOOL_SET: continue` runs BEFORE
# the BASE_MEMORY_TOOLS check. Must be in BOTH or tool will NOT be seeded.
LETTA_TOOL_SET = {
    # ... existing entries ...
    "evict_messages_and_recompile",  # <-- ADD HERE TOO
}
```

#### File 3: `letta/services/tool_executor/core_tool_executor.py`
**Purpose:** Actual implementation (NOTE: explored during session — confirmed this is the correct path, NOT `letta/agents/`)

**Add to function_map in `execute()` method:**
```python
function_map = {
    # ... existing entries ...
    "evict_messages_and_recompile": self.evict_messages_and_recompile,
}
```

**Add method to class:**
```python
async def evict_messages_and_recompile(
    self,
    agent_state: AgentState,
    actor: User,
    summary: str,
    confirmed: bool = False,
) -> str:
    """Evict old messages and recompile context. Cutoff determined at warning time."""
    
    # Safety: require non-empty summary
    if not summary or not summary.strip():
        return "Error: summary must be non-empty. Write your handoff summary before evicting."
    
    # Sanity check: verify agent made memory tool calls since warning
    # (Implementation note: scan all_messages for tool_calls with names in BASE_MEMORY_TOOLS
    #  between warning timestamp and now. If none found and not confirmed, return warning.)
    if not confirmed:
        # TODO: Implement sanity check — scan for memory tool calls since warning
        # If no memory tool calls found:
        #   return ("Warning: No memory tool calls detected since the compaction warning. "
        #           "Are you sure you've saved everything important? "
        #           "Call again with confirmed=True to proceed anyway.")
        pass  # For MVP, just log and continue; full check in v1.1
    
    # 1. Write summary to rollover block (create if missing)
    rollover_label = "rollover"
    try:
        agent_state.memory.get_block(rollover_label)
        agent_state.memory.update_block_value(label=rollover_label, value=summary)
    except KeyError:
        from letta.schemas.block import Block
        new_block = Block(
            label=rollover_label,
            value=summary,
            limit=10000,
            description="Handoff summary from most recent compaction cycle."
        )
        persisted = await self.block_manager.create_or_update_block_async(new_block, actor)
        await self.agent_manager.attach_block_async(
            agent_id=agent_state.id, block_id=persisted.id, actor=actor
        )
        agent_state.memory.set_block(persisted)
    
    await self.agent_manager.update_memory_if_changed_async(
        agent_id=agent_state.id, new_memory=agent_state.memory, actor=actor
    )
    
    # 2. Get eviction cutoff (fixed at warning time)
    # The cutoff_message_id was stored when the warning fired — retrieve it
    # (Implementation note: need to add eviction_cutoff_message_id field to AgentState,
    #  set by the warning system when it fires)
    cutoff_message_id = agent_state.eviction_cutoff_message_id  # TODO: Add this field
    
    # 3. Get all in-context messages
    all_messages = await self.message_manager.list_messages_async(
        agent_id=agent_state.id, actor=actor, ascending=True
    )
    
    if not all_messages:
        return "Error: No messages found in context. Nothing to evict."
    
    # 4. Identify messages to keep: system message + everything AFTER cutoff
    # Find cutoff index
    cutoff_idx = next(
        (i for i, m in enumerate(all_messages) if m.id == cutoff_message_id),
        len(all_messages)  # fallback: keep all if cutoff not found
    )
    
    # Keep: system message (idx 0) + messages from cutoff onwards
    keep_ids = list(dict.fromkeys(
        [all_messages[0].id] + [m.id for m in all_messages[cutoff_idx:]]
    ))
    
    # 5. SOFT DELETE — trim from context window only
    await self.agent_manager.update_message_ids_async(
        agent_id=agent_state.id, message_ids=keep_ids, actor=actor
    )
    deleted = len(all_messages) - len(keep_ids)
    
    # 6. Rebuild system prompt
    await self.agent_manager.rebuild_system_prompt_async(
        agent_id=agent_state.id, actor=actor, force=True
    )
    
    # 7. Reset memory pressure flag AND clear eviction cutoff
    from letta.schemas.agent import UpdateAgent
    await self.agent_manager.update_agent_async(
        agent_id=agent_state.id,
        update_agent=UpdateAgent(
            memory_pressure_alerted=False,
            eviction_cutoff_message_id=None,  # Clear after use
        ),
        actor=actor
    )
    
    return (
        f"Eviction complete. Trimmed {deleted} messages from context "
        f"(kept {len(keep_ids)} of {len(all_messages)}). "
        f"Messages remain in recall memory and are searchable. "
        f"Rollover summary written to '{rollover_label}' block. "
        f"System prompt recompiled."
    )
```

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

#### File 7: Warning system (location TBD)
**Purpose:** Enhance compaction warning to calculate cutoff and include preview

```python
# Pseudocode — exact location depends on where warning fires
# (likely in letta_agent.py or agent.py)

# When warning fires:
context_limit = agent_state.llm_config.context_window
target_tokens = int(context_limit * 0.20)  # 20% target

# Walk messages backwards, accumulate tokens
# (Reuse existing token counting utility)
# Find cutoff message ID

# Store cutoff
await agent_manager.update_agent_async(
    agent_id=agent_state.id,
    update_agent=UpdateAgent(eviction_cutoff_message_id=cutoff_msg_id),
    actor=actor
)

# Include in warning message:
# f"Memory pressure at {pct}%. Messages through '{preview}' will be evicted."
```

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
| **Sanity Check** | `confirmed: bool = False` param | If no memory tool calls since warning, return warning. Agent adds `confirmed=True` to proceed (no re-entering summary). |
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

**Sanity check:** Before eviction, scan for memory tool calls since the warning. If none found and `confirmed=False`, return a warning asking the agent to confirm they really want to evict without saving anything. Agent can add `confirmed=True` to proceed — importantly, they don't need to re-enter the summary.

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
| `list_messages_async(agent_id, actor, ascending)` | MessageManager | Get all in-context messages (ascending=True for system msg first) |
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
If cutoff happens to be at the system message, deduplication needed. Implementation uses `dict.fromkeys()` to preserve order while deduplicating:
```python
keep_ids = list(dict.fromkeys([all_messages[0].id] + [m.id for m in all_messages[cutoff_idx:]]))
```

**2. Method name verification needed before coding**  
These names were confirmed from source exploration but should be verified against the actual branch:
- `list_messages_async` — source search found `list_messages` (sync). Async variant may be `list_messages_async` or may need to check actual signature.
- `update_memory_if_changed_async` — used in rollover block persistence; confirm this exists on AgentManager or find correct alternative.

**3. Empty message list guard**  
If `all_messages` is empty for some reason, `all_messages[0]` will raise IndexError. Add guard:
```python
if not all_messages:
    return "Error: No messages found in context. Nothing to evict."
```

**4. Cutoff not found guard**  
If `eviction_cutoff_message_id` doesn't match any message (edge case — message deleted?), need graceful fallback:
```python
cutoff_idx = next(
    (i for i, m in enumerate(all_messages) if m.id == cutoff_message_id),
    len(all_messages)  # fallback: keep all if cutoff not found
)
```

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
- **Compaction warning system** — enhance to calculate cutoff, store it, include preview in warning message
- **AgentState schema** — add `eviction_cutoff_message_id: Optional[str]` field
- **AgentState ORM** — add column + to_pydantic mappings
- **AgentManager** — add to scalar_updates whitelist

---

## Part 9: Open Questions (Updated Feb 23)

### Resolved

1. ~~**`n_messages_to_keep` default**~~ → **REMOVED.** Cutoff is system-determined via percentage-based calculation, not agent-specified.

2. **Rollover block label:** Hardcode `"rollover"` for MVP. Can make configurable later if needed.

3. ~~**Confirm parameter**~~ → **REPLACED** with `confirmed: bool` for sanity check bypass (see Part 6).

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

3. **Message token counting**
   - Verify: does `Message` schema have `token_count` field?
   - If not, need to either add it or use tokenizer at warning time
   - Existing 80% warning must count tokens somehow — find and reuse that utility

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
4. Call again with `confirmed=True`
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
    summary: str,              # Required, non-empty
    confirmed: bool = False    # Set True to skip sanity check
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
- If no memory tool calls found AND `confirmed=False`: return warning, don't evict
- Agent can add `confirmed=True` to proceed
- **Key UX:** Agent doesn't re-enter summary — it's already in the args

**Implementation:** Scan `all_messages` for `tool_call` messages where name is in `BASE_MEMORY_TOOLS`, between warning timestamp and now.

### Preview Tool (Companion)

`preview_eviction()` — returns token estimate based on current cutoff point.
- Lets agent understand impact before committing
- Natural pairing: warning gives total count, preview lets them see details

### Future Escalation (v2)

- Second warning if evict not called within N minutes of first warning
- Absolute max threshold → halt agent until user intervention
- Deferred — need core working first

### Token Counting

The existing 80% warning mechanism already counts tokens robustly. The eviction calculation should reuse that same utility rather than re-implementing.

**Open question:** Does `Message` schema have a `token_count` field? If yes, easy. If not, need tokenizer call or to add the field. Sonnet to verify.