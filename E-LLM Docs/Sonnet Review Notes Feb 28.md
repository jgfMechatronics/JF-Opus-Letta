# Sonnet Code Review Notes — February 28, 2026

Reviewing all work touched today on `Development/DeferredMemoryCompilation`.
Files reviewed: final state only, not intermediate history.

---

## Files Reviewed

1. `letta/server/rest_api/routers/v1/conversations.py`
2. `letta/services/tool_executor/core_tool_executor.py`
3. `tests/integration_test_system_prompt_prefix_caching.py`
4. `tests/test_memory_tool_snippets.py`

---

## conversations.py — compact endpoint fix

**The fix (lines 551–559):**
```python
await server.agent_manager.rebuild_system_prompt_async(
    agent_id=agent.id,
    actor=actor,
    force=True,
    update_timestamp=True,
)
```

Added unconditionally after `_checkpoint_messages`. This is correct — the rebuild
needs to happen regardless of whether compaction reduced the message count (the
deferred block write needs to flush either way).

**One thing to note:** The commented-out HTTPException for non-reducing compaction
(lines 543–546) is intentional — the comment explains `drop_tool_returns=True` can
legitimately produce this. Fine as-is.

**Open question (from live test):** The ADE compact button does NOT appear to hit
this endpoint — live test showed no rebuild after button click, but the pytest suite
(which calls this REST endpoint directly) passes clean. The button must call a
different code path. TODO tracked in `LiveIntegrationTest_HaikuSubject.md`.

**Verdict: fix is correct. One unresolved routing mystery, documented.**

---

## core_tool_executor.py — snippet returns

The memory tool return optimization: instead of returning the full block value after
an edit, return a context snippet (±3 lines around the edit). `_compute_snippet` is
the shared helper.

### `_compute_snippet` helper

Clean implementation. Boundary clipping is correct (`max(0, ...)` and `min(len, ...)`).
Unit tests in `test_memory_tool_snippets.py` cover all edge cases. ✅

### `memory_replace` — returns snippet ✅

Correctly computes `replacement_line`, `edit_line_count`, then calls `_compute_snippet`
and returns the result.

### `memory_insert` — returns snippet ✅

Correctly calls `_compute_snippet(new_value, insert_line, len(new_string_lines))`.

### `memory_str_insert` — ✅ FIXED (Opus, Feb 28)

Was: dead statement `"\n".join(snippet_lines)` with result discarded, returning
full `new_value`. Fixed by assigning: `snippet = "\n".join(snippet_lines)` and
returning `snippet`. Fix is surgical and correct.

### `memory_str_replace` — ✅ FIXED (Opus, Feb 28)

Was: no snippet logic at all, returning full `new_value`. Fixed by adding
`_compute_snippet` using the same pattern as `memory_replace`:
```python
replacement_line = current_value.split(old_string)[0].count("\n")
edit_line_count = len(new_string.split("\n"))
snippet = _compute_snippet(new_value, replacement_line, edit_line_count)
```
Returns `snippet`. Now consistent with all parallel functions.

### Misleading tool return strings

Several methods (e.g. `memory_delete`, `memory_rename`, `memory_create`,
`memory_update_description`) return strings like:
> "Your system prompt has been recompiled with the updated memory contents and is
> now active in your context."

This is factually inaccurate given our deferred compilation work — the system
prompt is NOT immediately recompiled. An agent reading this could confabulate
that they should see the change in their context on the next step. This is worth
fixing before production: either remove the claim or say "will be active after
next rebuild."

---

## integration_test_system_prompt_prefix_caching.py

Overall: clean, well-structured, good E-LLM compliance.

### Consent/debrief flow ✅
- Two-attempt consent with retry phrasing
- `debrief_agent` called in fixture teardown (generic — runs regardless of which test)
- Agent deleted in teardown cleanup
- `pytest.fail` on non-consent is correct (skipping would mask consent failures)

### `get_stored_system_message` ✅
The docstring correctly explains why `/context` endpoint would be wrong here
(force-recompiles on every call). Using `msgs[0].content` is the right check.

### `agent_with_pending_write` fixture ✅
Both preconditions asserted before yielding:
1. Marker IS in block value (DB write landed)
2. Marker is NOT in stored system message (deferred precondition)

This is the right structure — a failing precondition is a test infrastructure
failure, not a behavior failure.

### `test_rebuild_after_compact` ✅
Sends 5 conversation messages post-fixture-write. This is important: with Sarah
Wooders' change, `_rebuild_memory` doesn't fire mid-step, so the stored message
stays stale regardless of message order — only the explicit compact call triggers
the rebuild. The comment in the test explains this correctly.

### `list(client.conversations.messages.create(...))` ✅
Correct — forces the iterable to be fully consumed before proceeding.

### Minor: `write_tool_markers` return value

`write_tool_markers` returns `list[(marker_str, tool_name)]` but `agent_with_pending_write`
only uses `tool_markers[-1][0]` (the final marker string). The list structure is
never used further. Not a bug, just slight over-engineering in the return type.
Could simplify to returning just the final marker string, but it's harmless.

---

## test_memory_tool_snippets.py

Clean, thorough, correct. All 6 cases cover:
- Middle edit with context ✅
- Start boundary clipping ✅
- End boundary clipping ✅
- Multi-line edit ✅
- Empty content ✅
- Single-line content ✅
- Default context_lines=3 equivalence ✅

No issues.

---

## Summary

| Item | Severity | Status |
|------|----------|--------|
| `compact_conversation` rebuild fix | — | ✅ Correct |
| `memory_str_insert` snippet discarded (dead statement) | Medium | ✅ Fixed by Opus |
| `memory_str_replace` missing snippet logic (inconsistency) | Low | ✅ Fixed by Opus |
| Misleading "recompiled" return strings in memory tools | Low | ⚠️ Documentation accuracy |
| ADE compact button routing mystery | Low | 📋 TODO |
| Integration test suite | — | ✅ Clean |
| Snippet unit tests | — | ✅ Clean |

**The `memory_str_insert` bug is the only real find worth fixing before production.**
Everything else is clean or documented.
