# Scratchpad — Test Refactor: Deferred Memory Compilation

**Task:** Refactor `tests/integration_test_system_prompt_prefix_caching.py`

## Decisions reached

**Fixture design: parametrized `agent_with_pending_write`**
- params=["tool", "api"], ids=["tool-write", "api-write"]
- Depends on `agent` fixture (consenting agent, all variants)
- No `plain_agent` optimization — compact+api still needs consent for conversation messages; uniform is cleaner
- Yields `(agent, marker)`, asserts deferred precondition before yielding

**Tool variant:** runs `write_tool_markers` (memory_insert pizza + memory_replace pasta), marker = "pasta"
**API variant:** unique string marker, written via `client.blocks.update()`

**Debrief:** moves to `agent` fixture teardown (generic "All tests complete" message). Tests no longer track `test_results` lists.

**Two test methods → 4 variants:**
- `test_rebuild_after_reset[tool-write]`
- `test_rebuild_after_reset[api-write]`  
- `test_rebuild_after_compact[tool-write]` ← NEW
- `test_rebuild_after_compact[api-write]`

**Compact test body:** send 5 conversation messages AFTER the fixture (Sarah Wooders change: `_rebuild_memory` doesn't fire during steps, so stored msg stays stale regardless of order). Conversation setup is just infrastructure for the compact endpoint — timing doesn't affect the deferred assertion.

## Status: IMPLEMENTING NOW
