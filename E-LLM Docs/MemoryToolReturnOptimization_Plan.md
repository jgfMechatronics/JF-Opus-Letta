# Memory Tool Return Value Optimization — Plan

**Date:** Feb 28, 2026  
**Author:** Opus  
**Status:** Approved — ready for implementation

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

### Change 1: `memory_insert`

**File:** `letta/services/tool_executor/core_tool_executor.py`, lines 732-740

**What to change:**
- Line 733 computes `"\n".join(snippet_lines)` but discards the result (dead code)
- Assign result to `snippet` variable
- Change return statement (line 740) from `return new_value` to `return snippet`

### Change 2: `memory_replace`

**File:** `letta/services/tool_executor/core_tool_executor.py`, lines 393-400

**What to change:**
- Add snippet computation after line 393 (the replace operation)
- Use same approach as commented-out code in `base.py` lines 382-386: find replacement line, compute start/end with SNIPPET_LINES=3 buffer
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
