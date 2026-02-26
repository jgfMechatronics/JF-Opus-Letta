# Prefix Cache Revert Investigation

**Date:** Feb 26, 2026
**Goal:** Understand WHY #9380 reverted Sarah's #9080 prefix caching optimization

## Key Questions
1. Was it a bug? **Unknown - no PR discussion found**
2. Fundamental flaw in the approach? **Possibly - used extract_memory_section() local function**
3. Edge case they punted on? **Unknown**

## Timeline
- **Feb 6:** Sarah's #9080 merged (prefix caching via extract_memory_section comparison)
- **Feb 8:** Sarah's #9380 reverted her own PR (no explanation in commit)
- **Feb 24:** Amy's #9372 added "self" and "self_sliding_window" modes with proper caching

## MAJOR FINDING

**The revert-the-revert approach is a dead end.** Trunk already has prefix caching optimization via Amy's #9372, implemented differently:
- Comments out pre-compaction system prompt refresh to preserve cache
- Uses "self" compaction mode with proper caching
- Key comment in v3.py line 1017: "we no longer refresh the system prompt before compaction so we can leverage cache for self mode"

## REMAINING ISSUE

We still observed system prompt recompiling on **memory writes** (core_memory_replace, etc.). The cache busts on every memory tool call, not deferring until compaction.

**Next step:** Trace memory write path to find where force_system_prompt_refresh=True is triggered. Likely just need to flip a flag or two.

## Files to investigate
- letta/functions/function_sets/base.py (memory tools)
- letta/services/tool_executor/core_tool_executor.py
- letta/agents/letta_agent_v2.py (_refresh_messages, _rebuild_memory)
- letta/services/agent_manager.py (refresh_memory_async, rebuild_system_prompt_async)
