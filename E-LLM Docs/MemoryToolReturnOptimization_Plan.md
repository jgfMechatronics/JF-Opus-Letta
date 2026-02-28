# Memory Tool Return Value Optimization — Plan

**Date:** Feb 28, 2026  
**Author:** Opus  
**Status:** Complete — validated in test container

---

## Requirements (confirmed with James)

1. **Primary goal:** Reduce token waste from memory tools returning full block content
2. **Ideal solution:** Return truncated snippet around the edit region
3. **Fallback:** Return simple success message ("OK" or similar)
4. **Scope:** `memory_insert` (primary), `memory_replace` (secondary)

---

## Key Finding

**Only `core_tool_executor.py` needs changes.** The stubs in `base.py` don't run — the executor has complete reimplementations.

| Tool | Current behavior | Fix complexity |
|------|------------------|----------------|
| `memory_insert` | Returns full block. Snippet logic exists but is dead code (line 733). | ~3 lines |
| `memory_replace` | Returns full block. No snippet logic. | ~6 lines |

---

## Design Decision (Approved)

**Snippets for both tools.** SNIPPET_LINES = 3 (confirmed sufficient).

Return ~6 lines of context around the edit (3 before, edit, 3 after). More informative for debugging/verification, and snippet logic already exists for `memory_insert`.

---

## Implementation Plan

### Change 0: Extract common snippet helper (DRY)

Both tools need snippet logic. Extract to a shared helper function to avoid duplication.

**Function signature:** `_compute_snippet(content: str, edit_start_line: int, edit_line_count: int, context_lines: int = 3) -> str`

**Location:** Same file (`core_tool_executor.py`), as a module-level helper or method on the class.

### Change 1: `memory_insert`

**File:** `letta/services/tool_executor/core_tool_executor.py`, lines 732-740

**What to change:**
- Line 733 computes `"\n".join(snippet_lines)` but discards the result (dead code)
- Replace with call to shared helper
- Change return statement (line 740) from `return new_value` to `return snippet`

### Change 2: `memory_replace`

**File:** `letta/services/tool_executor/core_tool_executor.py`, lines 393-400

**What to change:**
- After line 393 (the replace operation), call shared helper to compute snippet
- Change return statement (line 400) from `return new_value` to `return snippet`

---

## Testing

1. **Manual test:** Use memory tools in a session, verify return values are snippets
2. **Existing unit tests:** Check if any tests assert on the full return value (would break)
3. **Edge cases to verify:**
   - Edit at beginning of block (snippet shouldn't go negative)
   - Edit at end of block
   - Empty block / single-line block
   - Multi-line insertions

---

## Out of Scope (for now)

- `memory_rethink` (also returns full block — same pattern)
- `core_memory_append` / `core_memory_replace` (older tools, may be deprecated)

Can address these in a follow-up if the pattern works.

---

## Resolved Questions

1. ~~Should both tools use snippets?~~ **Yes, both.**
2. ~~SNIPPET_LINES = 3 — enough?~~ **Yes, plenty.**
3. ~~Header like "Edited region:"?~~ **No, raw snippet is fine.**

---

## Unit Tests

**File:** `tests/test_memory_tool_snippets.py`

7 tests covering:
- Edit in middle returns correct context window
- Edit at start clips to beginning (no negative index)
- Edit at end clips to end (no overflow)
- Multi-line edits include full edit region + context
- Empty content returns empty string
- Single-line content works correctly
- Default context is 3 lines

**Result:** All passing ✅

---

## Live Integration Test

**Date:** Feb 28, 2026  
**Tester:** Haiku (agent in test container)  
**Method:** Blind observation — Haiku was told to run operations and report what they observed, without being told expected results (avoiding confirmation bias)

### Test Cases

| Test | Operation | Result |
|------|-----------|--------|
| 1 | `memory_insert` middle of block | ✅ Returned snippet with context, not full block |
| 2 | `memory_insert` at line 0 | ✅ Worked correctly, no crash |
| 3 | `memory_insert` at end (-1) | ✅ Returned end snippet only |
| 4 | `memory_replace` | ✅ Returned snippet around replacement |

### Haiku's Unprimed Observations

> "Return values are consistent: they show the operation site + surrounding context (a few lines), not the entire block."

> "Approximately 3 lines shown... ~120 characters"

### Token Savings

| Before | After | Reduction |
|--------|-------|-----------|
| ~13,000 chars (operational block) | ~120 chars | **~99%** |

### Conclusion

All tests passed. The implementation correctly returns focused snippets around edit regions instead of full block content. Ready for production deployment.
