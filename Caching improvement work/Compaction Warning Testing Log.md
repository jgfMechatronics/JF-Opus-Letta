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
