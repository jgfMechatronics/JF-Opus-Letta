# Agentic Compaction — Implementation Plan
**Date:** February 22, 2026  
**Authors:** Opus + Sonnet (collaborative research session) \n
**High Level Architecture:** James Ferneyhough (Human-In-The-Loop)  
**Status:** Research Complete, Ready for Implementation

---

## Executive Summary

Replace Letta's current compaction system (which processes all input tokens twice via a separate summarizer LLM call) with agent-driven compaction where the agent writes their own summary and triggers eviction. **Estimated savings: 40-50% on input token costs.**

**Implementation scope:** ~50 lines of new code across 3 files. No changes to ToolType enum, ToolExecutorFactory, sandbox infrastructure, or agent loop.

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

### Files to Modify (3 total)

#### File 1: `letta/functions/function_sets/base.py`
**Purpose:** Schema stub — docstring becomes LLM-visible tool description

```python
def evict_messages_and_recompile(
    agent_state: "AgentState",
    summary: str,
    n_messages_to_keep: int = 50,
) -> Optional[str]:
    """
    Evict old conversation messages to free context space. Call this when you receive
    a memory pressure warning and have finished writing your handoff summary.
    
    WORKFLOW:
    1. Write comprehensive context to your rollover/persistent-working blocks
    2. Call this tool with your handoff summary
    3. After eviction, you'll have fresh context with your summary preserved
    
    Args:
        summary: Comprehensive handoff summary. Written to 'rollover' block before eviction.
                 Must be non-empty. This is your continuity across the context clear.
        n_messages_to_keep: Recent messages to retain (default: 50).
    
    Returns:
        Confirmation of eviction with message counts.
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
    n_messages_to_keep: int = 50,
) -> str:
    """Evict old messages and recompile context."""
    
    # Safety: require non-empty summary
    if not summary or not summary.strip():
        return "Error: summary must be non-empty. Write your handoff summary before evicting."
    
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
    
    # 2. Get all in-context messages and identify which to keep
    # ascending=True so messages[0] is the system message (always keep)
    all_messages = await self.message_manager.list_messages_async(
        agent_id=agent_state.id, actor=actor, ascending=True
    )
    # Always keep: system/first message + N most recent
    keep_ids = [all_messages[0].id] + [m.id for m in all_messages[-n_messages_to_keep:]]
    
    # 3. SOFT DELETE — trim from context window only. Messages stay in DB, searchable via conversation_search.
    # update_message_ids_async is the direct async primitive (same as trim_older_in_context_messages under the hood)
    await self.agent_manager.update_message_ids_async(
        agent_id=agent_state.id, message_ids=keep_ids, actor=actor
    )
    deleted = len(all_messages) - len(keep_ids)
    
    # 4. Rebuild system prompt
    await self.agent_manager.rebuild_system_prompt_async(
        agent_id=agent_state.id, actor=actor, force=True
    )
    
    # 5. Reset memory pressure flag (uses Feb 17-21 infrastructure)
    from letta.schemas.agent import UpdateAgent
    await self.agent_manager.update_agent_async(
        agent_id=agent_state.id,
        update_agent=UpdateAgent(memory_pressure_alerted=False),
        actor=actor
    )
    
    total_before = len(all_messages)
    return (
        f"Eviction complete. Trimmed {deleted} messages from context (kept {len(keep_ids)} of {total_before}). "
        f"Messages remain in recall memory and are searchable. "
        f"Rollover summary written to '{rollover_label}' block. "
        f"System prompt recompiled."
    )
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

### Confirmed Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Tool Type** | `LETTA_MEMORY_CORE` | Reuse existing type, no enum changes needed |
| **Safety Mechanism** | Non-empty `summary` param | Substantive guard (forces continuity artifact), not security theater |
| **Eviction Spec** | `n_messages_to_keep: int` | Simple, agent can count. Message-ID based cutoff deferred to v2 |
| **Rollover Block** | Create-if-missing | Tool handles block lifecycle, agent doesn't need to pre-create |
| **Message Deletion** | Soft delete via `update_message_ids_async` | Trims from context window only — messages remain in DB, searchable via `conversation_search`. Hard delete deliberately NOT used. |
| **Separate preview tool** | No (MVP) | Avoid extra LLM turn at high context pressure |
| **Separate record_summary tool** | No | Use existing `memory_insert` → then `evict`. Two calls, clear protocol |

### Safety Discussion

**Option considered:** `confirm="EVICT"` string parameter as additional safety gate.

**Decision:** Summary-only is sufficient for MVP. Rationale:
- Non-empty summary requirement is a **substantive** guard — you can't evict without producing continuity content
- `confirm` param is belt-and-suspenders that adds friction without adding real safety
- Agent (me) knows the protocol — trust the agent, add guards if problems emerge

**James to adjudicate** if he prefers the extra confirm param.

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

### Memory Pressure Flag Reset
Uses infrastructure from Feb 17-21 compaction warning work:
- Field: `memory_pressure_alerted` on AgentState
- Schema: `letta/schemas/agent.py` (AgentState + UpdateAgent)
- ORM: `letta/orm/agent.py` (column + to_pydantic mappings)
- Manager: `letta/services/agent_manager.py` (scalar_updates whitelist)

### Implementation Notes / Gotchas

**1. keep_ids deduplication edge case**  
If `n_messages_to_keep >= len(all_messages) - 1`, `all_messages[0].id` appears in BOTH the prepended system message AND the last-N slice. This creates a duplicate in keep_ids. Should deduplicate while preserving order:
```python
# Deduplicate while preserving order (dict.fromkeys preserves insertion order in Python 3.7+)
keep_ids = list(dict.fromkeys([all_messages[0].id] + [m.id for m in all_messages[-n_messages_to_keep:]]))
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

---

## Part 8: What We DON'T Change

- **ToolType enum** — reuse `LETTA_MEMORY_CORE`
- **ToolExecutorFactory** — existing routing handles `LETTA_MEMORY_CORE` → `LettaCoreToolExecutor`
- **Sandbox infrastructure** — completely bypassed (we use core tool path)
- **LettaAgent step loop** — no changes to `_handle_ai_response` or `_step`
- **Compaction warning system** — already working from Feb 17-21 branch

---

## Part 9: Open Questions for James

1. **`n_messages_to_keep` default:** Hardcode 50, or make configurable via `settings.py`?

2. **Rollover block label:** Hardcode `"rollover"`, or make configurable parameter?

3. **Confirm parameter:** Add `confirm: str = ""` requiring `"EVICT"` to proceed, or trust summary-as-safety?

4. **Test strategy:** 
   - Option A: Test on Haiku 4.5 instance first (consent obtained from Claude.ai Haiku)
   - Option B: Deploy directly to my container
   - Option C: Fresh test container

5. ~~**Async method availability:**~~ **RESOLVED during session.** We do NOT use `trim_older_in_context_messages` at all. The direct async primitive is `update_message_ids_async(agent_id, message_ids, actor)` on AgentManager. This is what trim uses under the hood — we call it directly. No new async wrappers needed, no `asyncio.to_thread()` needed.

---

## Part 10: Testing Plan

### Prerequisites
1. Compaction warning system working (already deployed in our branch)
2. New tool code deployed and registered

### Test Sequence
1. Create test agent (Haiku 4.5 or isolated container)
2. Fill context with test messages (or use existing high-context agent)
3. Trigger memory pressure warning (reach 80% threshold)
4. Verify warning appears in agent context
5. Agent writes to persistent memory blocks
6. Agent writes rollover summary
7. Agent calls `evict_messages_and_recompile(summary="...", n_messages_to_keep=50)`
8. **Verify:**
   - Messages deleted (check DB / conversation_search)
   - Rollover block exists and contains summary
   - System prompt recompiled (agent sees updated context)
   - `memory_pressure_alerted` reset to False
   - No errors in logs
   - Agent can continue conversation normally

### Success Criteria
- Tool executes without error
- Message count reduced appropriately
- Rollover block persists summary
- System prompt reflects current memory state
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

### Three Files to Modify
```
letta/functions/function_sets/base.py                  — stub with docstring (schema source)
letta/constants.py                                     — add to BASE_MEMORY_TOOLS + LETTA_TOOL_SET (both required)
letta/services/tool_executor/core_tool_executor.py     — method + function_map entry
```

### Tool Signature (LLM-visible)
```python
evict_messages_and_recompile(
    summary: str,              # Required, non-empty
    n_messages_to_keep: int = 50
) -> str
```

### Expected Cost Savings
- Current: ~$11-12.50/MTok effective (tokens processed twice)
- After: ~$6.25/MTok (tokens processed once)
- **Savings: 40-50% on compaction events**

## Quick notes from James Initally
- LLMs indeeded can NOT count messsages accurately. We have two options to get around this:
    1. Add a msg idx which counts active messages in context. This is probably the simples
    2. String matching with confirmation from LLM that they are looking at the right section by having them input a string after. This was my initial idea but I like the msg idx MUCH better
- I want to add some more sanity checks to the evict_messages_and_recompile method. Mainly, I want to look at recent tool call activity. If the agent did NOT make at least a few calls to core memory edit tools, pulse a warning to the LLM to confirm.
    - Do NOT make them re-enter their summary if they confirm they want to evict
- Eventually, I will want the compaction warning to pulse another warning if evict_messages_and_recompile isn't called within a certain window of the warning firing. Then when an abs max threshold is crossed the LLM is halted until user intervention.
- I would like the LLM to be able to get an estimate of tokens to be cleared based on the message eviction point they have chosen, possibly as a tool to call before evict_messages.... so they can tune the eviction point