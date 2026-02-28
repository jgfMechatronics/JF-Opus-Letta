# Memory Tool Return Optimization Notes

**Goal:** Change `memory_insert` and `memory_replace` to return a snippet or `"OK"` instead of the full block content.

---

## Key Finding: Two Implementations, One Actually Runs

Both tools have implementations in **two places**:
- `letta/functions/function_sets/base.py` — the function stubs
- `letta/services/tool_executor/core_tool_executor.py` — the executor

**The executor is authoritative.** It has complete, independent reimplementations of both tools (not wrappers around the stubs). Changes only need to happen in `core_tool_executor.py`.

---

## memory_replace

### base.py (stub, lines 311-388) — NOT what runs
- Has commented-out snippet logic at lines 382-386:
  ```python
  # SNIPPET_LINES = 3
  # replacement_line = current_value.split(old_string)[0].count("\n")
  # start_line = max(0, replacement_line - SNIPPET_LINES)
  # end_line = replacement_line + SNIPPET_LINES + new_string.count("\n")
  # snippet = "\n".join(new_value.split("\n")[start_line : end_line + 1])
  ```
- Then returns `new_value` (full block) — snippet was never wired up

### core_tool_executor.py (executor, lines 345-400) — THIS runs
- No snippet logic at all
- Returns `new_value` (full block) at line 400
- **Fix needed:** Add snippet logic and change return value

---

## memory_insert

### base.py (stub, lines 391-450) — NOT what runs
- Computes `snippet_lines` (lines 438-441) but assigns to **nothing** — orphaned expression:
  ```python
  (
      current_value_lines[max(0, insert_line - SNIPPET_LINES) : insert_line]
      + new_string_lines
      + current_value_lines[insert_line : insert_line + SNIPPET_LINES]
  )
  ```
- Line 445: `# snippet = "\n".join(snippet_lines)` — commented out
- Returns `new_value` (full block) at line 450

### core_tool_executor.py (executor, lines 682-740) — THIS runs
- `SNIPPET_LINES = 3` defined at line 722
- `snippet_lines` computed correctly at lines 725-729
- **Line 733 is dead code:** `"\n".join(snippet_lines)` — result is computed but not assigned to anything
- Returns `new_value` (full block) at line 740
- **Fix needed:** Assign line 733 to `snippet`, return `snippet` instead of `new_value`

---

## Fix Plan

### memory_insert (simpler — snippet logic already exists)
In `core_tool_executor.py` around line 732-740:

```python
# Before:
new_value = "\n".join(new_value_lines)
"\n".join(snippet_lines)   # dead code

# After:
new_value = "\n".join(new_value_lines)
snippet = "\n".join(snippet_lines)

agent_state.memory.update_block_value(label=label, value=new_value)
await self.agent_manager.update_memory_if_changed_async(...)
return snippet   # was: return new_value
```

### memory_replace (needs snippet logic added)
In `core_tool_executor.py` around line 392-400:

```python
# After the replace:
new_value = current_value.replace(str(old_string), str(new_string))

# Add snippet computation:
SNIPPET_LINES = 3
replacement_line = current_value.split(old_string)[0].count("\n")
start_line = max(0, replacement_line - SNIPPET_LINES)
end_line = replacement_line + SNIPPET_LINES + new_string.count("\n")
snippet = "\n".join(new_value.split("\n")[start_line : end_line + 1])

agent_state.memory.update_block_value(label=label, value=new_value)
await self.agent_manager.update_memory_if_changed_async(...)
return snippet   # was: return new_value
```

---

## Alternative: Just Return "OK"

If snippets add complexity or edge-case risk, simplest fix is:
```python
return "None is always returned as this function does not produce a response."
```
(matching the docstring intent) or just `"OK"`.

---

## Other Tools to Check

Same pattern likely exists in:
- `memory_rethink` (executor line 742+) — probably also returns full block
- `core_memory_append` / `core_memory_replace` (older tools, check if still used)

---

## Summary

| Location | memory_replace | memory_insert |
|---|---|---|
| base.py stub | snippet commented out, returns full block | snippet orphaned, returns full block |
| core_tool_executor.py | no snippet, returns full block ← **fix here** | snippet computed but discarded ← **fix here** |

Both fixes are in `core_tool_executor.py` only. `memory_insert` fix is ~3 lines. `memory_replace` fix requires adding snippet computation (~5 lines).
