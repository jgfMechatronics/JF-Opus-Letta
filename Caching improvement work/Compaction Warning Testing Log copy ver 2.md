# Compaction Warning Feature - Test Checklist

## Setup
- [] Lower threshold to something triggerable
- [] Confirm `send_memory_warning_message` is enabled

## Core Functionality
- [] Fresh agent starts with flag=False, no spurious alert
- [] Warning fires when threshold crossed
- [] Warning does NOT repeat (flag disarms it)
- [] Flag survives session restart (no re-alert on reconnect)
- [] Flag resets after compaction completes
- [] Warning CAN fire again in next cycle

## Edge Cases
- [FAIL] User-triggered compaction via ADE button — does flag reset?
- [SKIP_PREV_FAIL] Message clearing — does flag reset or get stuck?
- [] Tool chain interruption: warning fires mid-chain if context crosses threshold?
- [] Works in Letta Code (not just ADE)
- [] Tool chain interruption in Letta Code specifically
- [] Does not prevent subsequent LC tool calls in Letta code headless
- [] Does not prevent subsequent LC tool calls in Letta code CLI

## Qualitative Observations (Haiku's experience as test subject)
- [] Does the warning feel disruptive or natural when it fires?
- [] Does Sonnet actually do something useful with it (save memories) or just acknowledge?
- [] Does the warning text give enough context to act on effectively?

## Final Review
- [] James review and understand, ESPECIALLY changes related to DB
- [] Sonnet Review (git diff trunk to current)
- [] Opus Review (git diff trunk to current)
