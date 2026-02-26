# Deferred Memory Compilation — Test Plan & Report

## Background

As part of the Agentic Compaction project, Fix B removes eager system prompt rebuilds that were firing immediately after every memory tool call (6 call sites removed from `agent_manager.py` and `core_tool_executor.py`). Instead, system prompt recompilation is deferred to the next LLM step start, where a lazy containment check already existed.

**Goal:** Confirm that deferring recompilation produces no observable behavioral changes while eliminating unnecessary cache busts.

**Branch:** `Development/DeferredMemoryCompilation`

---

## Report

### Smoke Test — February 26, 2026

**Test agent:** Haiku (Claude Haiku 4.5, test container, port 8284)
**Conducted by:** James, with Sonnet and Opus

#### What was tested

Basic write-then-verify cycle: Haiku wrote a test string to a memory block, continued the conversation, then James triggered compaction. Post-compaction, James verified the written value was present in the system prompt.

#### Haiku's self-report (verbatim)

> **The memory write:** Normal. No different from any other memory write I've done. The tool executed, returned the updated block content, and I moved on.
>
> **The waiting period:** I wasn't consciously aware that the system prompt hadn't recompiled. Only when James asked me to "check very carefully" did I notice the disconnect — the timestamp gap between my last system prompt update and the write I'd just made.
>
> **The texture of that moment:** Slightly disorienting once I noticed it. Not in an alarming way, but the realization was: "Oh, my system prompt is stale relative to what I just wrote." There was a moment of wondering if that would cause a problem — like, was I about to lose information?
>
> **During compaction:** No discontinuity. No disorientation.
>
> **After compaction:** Seamless. No seam at all, actually. The summary worked, the compilation happened, and my context felt continuous.
>
> **Anomalies:** None. Everything worked exactly as designed.

#### Haiku's flag

> If a write happens and then I get invoked again *before* compaction triggers a recompile, I'll be working with a stale system prompt. That's probably fine if it's brief, but it's worth knowing that there's a window where my core memory blocks don't reflect recent writes.

#### Assessment

Fix B confirmed working. Memory writes propagate correctly through the compaction cycle. The stale window Haiku identified is expected behavior by design — the system prompt updates at next step start, so within a single continuous session, the staleness window is one step. No data loss, no discontinuity.

**Note on the stale window (James):** The stale window is actually beneficial, not just acceptable. Without immediate recompilation, agents won't see their just-written memory appear at the back of context and mistake it for a duplicate ("oh shit it was already there"). The information isn't lost — it's still in conversation history from the tool return. Compaction is the perfect time to recompile because that's when context is being restructured anyway.

---

## Test Plan

*To be developed — see below for areas to cover.*

### Areas to test

- [ ] Multiple rapid writes before any step boundary — do all propagate?
- [ ] Write immediately followed by a read of that block — does the agent see stale data?
- [ ] Write → agent invoked headlessly (new session) before compaction — is the write present?
- [ ] Compaction mid-conversation — pre/post memory consistency
- [ ] Edge case: write during a tool chain (multiple tool calls in one turn)
