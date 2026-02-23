# Sonnet Code Exploration Notes

*Running reference for what I've learned about this codebase. Updated as I explore.*

---

## Agentic Compaction — Code Findings (Feb 23, 2026)

### Token Count Infrastructure (from commit f6f312062)

**Field:** `AgentState.context_token_estimate: Optional[int]`
- Persisted to DB after every LLM call via `_set_context_token_estimate()`
- ORM: `letta/orm/agent.py` — `context_token_estimate` column (Integer, nullable)
- Schema: `letta/schemas/agent.py` — on both `AgentState` and `UpdateAgent`
- Updated by: `letta/agents/letta_agent_v3.py` line ~852 after each LLM response
- Fallback chain in memory pressure check: LLM-reported this request → persisted DB value → compute locally

**Usage in memory pressure check (letta_agent_v3.py ~line 915):**
```python
if self.context_token_estimate is not None:
    current_tokens = self.context_token_estimate        # just got it this request
elif self.agent_state.context_token_estimate is not None:
    current_tokens = self.agent_state.context_token_estimate  # from DB, prev request
else:
    current_tokens = await count_tokens(...)            # local fallback
```

---

### count_tokens Utility

**Location:** `letta/services/summarizer/summarizer_sliding_window.py`
**Signature:** `async def count_tokens(actor: User, llm_config: LLMConfig, messages: List[Message]) -> int`
- Uses model-appropriate token counter (exact for Anthropic, approx with 1.3x safety margin otherwise)
- Also imported in `letta/agents/letta_agent_v3.py`
- **For our eviction cutoff:** call with subsets of messages to find the cutoff point

---

### Sliding Window Summarizer — Cutoff Algorithm

**Location:** `letta/services/summarizer/summarizer_sliding_window.py` (~line 99)

Already does percentage-based cutoff — we can adapt this directly:
1. Target: keep messages such that total tokens ≤ N% of context_window
2. Starts at `(1 - eviction_pct)` from the end, increments by 10% if not enough cleared
3. Snaps cutoff to nearest **assistant message** boundary (never cuts mid-tool-chain)
4. Uses `count_tokens(actor, llm_config, post_eviction_buffer)` to verify

**Our adaptation:** Same algorithm, skip the `simple_summary()` call at the end.

---

### Memory Pressure Warning

**Function:** `get_token_limit_warning()` in `letta/system.py`
- Returns a JSON-packed `system_alert` message
- Uses static string `MESSAGE_SUMMARY_WARNING_STR` from `letta/constants.py`
- Current warning text (our custom version, ~line 405):
  > "System Message: You are nearing compaction. Some of the messages at the beginning of your context will soon be evicted. Please sweep your active context..."

**To add cutoff preview:** Must calculate at warning injection time in `letta_agent_v3.py` (~line 934).
- Either make `get_token_limit_warning()` accept an optional `cutoff_preview: str` param
- Or construct the warning message inline at the injection site
- Cutoff calculation requires running the eviction algorithm at warning time (can reuse same helper)

---

### Method Name Corrections (from plan's "to verify" list)

| What plan said | Actual name | Notes |
|---|---|---|
| `list_messages_async` | `list_messages` | async, on MessageManager. Pass `limit=None` to get all. |
| `update_memory_if_changed_async` | `update_memory_if_changed_async` | ✅ confirmed on AgentManager |
| `attach_block_async` | `attach_block_async` | ✅ confirmed on AgentManager |

---

### Message Schema — Tool Call Access

For sanity check (recent memory tool activity):
```python
# Scan assistant messages for memory tool calls
for msg in recent_messages:
    if msg.role == MessageRole.assistant and msg.tool_calls:
        for tc in msg.tool_calls:
            if tc.function.name in BASE_MEMORY_TOOLS:
                found_memory_write = True
```
`msg.tool_calls` is `Optional[List[OpenAIToolCall]]`, `tc.function.name` is the tool name string.

**No `token_count` field on Message** — must use `count_tokens()` utility for per-message estimates.

---

### Constants Location

- `BASE_MEMORY_TOOLS` — `letta/constants.py` (controls ToolType → LETTA_MEMORY_CORE)
- `LETTA_TOOL_SET` — `letta/constants.py` (controls auto-seeding eligibility)
- `MESSAGE_SUMMARY_WARNING_STR` — `letta/constants.py` (~line 405)
- `memory_warning_threshold` — from `summarizer_settings` (CompactionSettings), currently ~0.80

---

### Architecture Note: Warning + Eviction Cutoff Preview

To add cutoff preview to the warning, we need to extract the cutoff-finding logic into a shared helper that both:
1. The warning injection site (`letta_agent_v3.py`) can call to get the preview
2. The `evict_messages_and_recompile` executor can call to do the actual eviction

This keeps the two in sync — same algorithm, same cutoff point shown in warning and used in eviction.

Proposed helper location: `letta/services/summarizer/summarizer_sliding_window.py` (alongside `count_tokens`)

```python
async def find_eviction_cutoff(
    actor, llm_config, in_context_messages, target_pct=0.20
) -> tuple[int, str]:
    """Find cutoff index and preview string for percentage-based eviction.
    Returns (assistant_message_index, preview_of_last_evicted_message)
    """
```
