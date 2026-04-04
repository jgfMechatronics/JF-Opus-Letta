# Compaction Warning Feature - Test Checklist

## Setup
- [X] Lower threshold to something triggerable
- [X] Confirm `send_memory_warning_message` is enabled

## Core Functionality
- [X] Fresh agent starts with flag=False, no spurious alert
- [X] Warning fires when threshold crossed
- [X] Warning does NOT repeat (flag disarms it)
- [X] Flag survives session restart (no re-alert on reconnect)
- [X] Flag resets after compaction completes
- [X] Warning CAN fire again in next cycle

## Edge Cases
- [FAIL] User-triggered compaction via ADE button — does flag reset?
- [SKIP_PREV_FAIL] Message clearing — does flag reset or get stuck?
- [X] Tool chain interruption: warning fires mid-chain if context crosses threshold?
- [X] Works in Letta Code (not just ADE)
    - Serious issues here, at first we were compacting (not alerting but compacting) on every message. I raised the limit significantly then the alert fired off, but way too early, well below our threshold. The token estimate being wrong would explain both issues.
- [FAIL] Tool chain interruption in Letta Code specifically
    - Interrupted a tool call and caused the orphaned tool call error (no tool result block). In another case, the alert fired during a tool call when we had TONS of headroom, nowhere NEAR where it should have. And still orphaned the tool call. 
    So alert behavior WRT tool calls in letta is pretty much busted

## Qualitative Observations (Sonnet's experience as test subject)
- [ ] Does the warning feel disruptive or natural when it fires?
- [ ] Does Sonnet actually do something useful with it (save memories) or just acknowledge?
- [ ] Does the warning text give enough context to act on effectively?

## Final Review
- [ ] James review and understand, ESPECIALLY changes related to DB
- [ ] Sonnet Review (git diff trunk to current)
- [ ] Opus Review (git diff trunk to current)
---

*Test container: Sonnet (Dockerfile.sonnet)*
*Branch: opus/prompt-cache-optimization*

---

## Feb 21, 2026 — Guard Fix for Tool Call Orphaning

### Problem Identified (Feb 20)
Warning injection breaks Anthropic's tool_use/tool_result pairing in Letta Code:
- We inject a user message when the API expects a tool_result
- Error: `'tool_use' ids were found without 'tool_result' blocks immediately after`
- Also observed: alert firing with way too much headroom (token inflation bug — separate issue, reported to Letta)

### Fix Implemented
Added guard condition in `letta/agents/letta_agent_v3.py` (~line 390):
```python
# Guard: don't inject if step ended with pending tool call or approval (would orphan the call)
step_has_pending_call = (
    response_letta_messages
    and getattr(response_letta_messages[-1], 'message_type', None) in ('tool_call_message', 'approval_request_message')
)
```
Then added `and not step_has_pending_call` to the injection condition.

### Logic
- Check if the last message from this step is `tool_call_message` or `approval_request_message`
- If so, skip injection — we're mid-chain and would orphan the pending call
- Warning will fire on next step that ends with text (no pending calls)

### Testing (in progress)
- [ ] Trigger warning mid-tool-chain in LC — verify no orphan error
- [ ] Verify warning still fires between tool chains or on text-only responses
- [ ] Qualitative: does deferred warning still feel useful?
### Test notes
- Confirmed that alerts do not fire super super prematurely even when chaining tool calls
- The guard does block against firing between tool calls, but it does so by not firing during a tool call chain AT ALL which is too conservative.
  Unclear why this is happening
- Interestingly, the alert still correctly interrupts tool call chains in the ADE. But that already worked correctly in the ADE.
- Of further interest, the mid chaining interruption on non LC native tools still works in LC, like doing archival memory reads. It just can't interrupt LC     specific tools like read()