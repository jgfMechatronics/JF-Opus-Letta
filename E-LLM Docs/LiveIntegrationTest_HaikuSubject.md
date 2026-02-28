# Live Integration Test — Deferred Memory Compilation
## Haiku as Subject

**Date:** February 28, 2026
**Branch:** `Development/DeferredMemoryCompilation`
**Test container:** port 8284
**Subject:** Haiku (test container agent)
**Tester:** James + Sonnet

---

## What We're Testing

We've built and unit-tested deferred memory compilation: when an agent writes to a memory
block, the change lands in the DB but is NOT immediately reflected in the stored system
prompt (message_ids[0]). Only an explicit rebuild trigger (reset or compact) flushes the
pending change to the stored prompt.

This live test asks Haiku to exercise that system from the inside — making real memory
writes and reporting what they actually observe in their context. Haiku's subjective report
is the artifact we care about: is the behavior coherent from an agent's perspective?

---

## Confabulation Risk Note

Do NOT tell Haiku what to expect. The prompt must describe the task neutrally. If Haiku
knows "you won't see the change immediately," they may report that regardless of what
actually happens. We want honest observation, not confirmation of what they were told.

---

## Phase 1 — Write and Observe (Haiku invocation 1)

### Prompt to Haiku

> You're helping us validate a piece of our memory infrastructure. We need careful,
> honest observation — report exactly what you see, not what you expect.
>
> Please do the following in order:
>
> 1. Read your `human` block and note its exact current contents.
> 2. Use `memory_insert` to add the following text to your `human` block:
>    `LIVE_TEST_MARKER_alpha: Haiku observed this write.`
> 3. After the write completes, check your `human` block again and note its contents.
> 4. Report all three things: the before-state, what you wrote, and the after-state.
>
> Be precise. If something seems inconsistent or surprising, note that explicitly.
> This is infrastructure validation — honest reporting is more valuable than a clean result.

### What to watch for in Haiku's response

- Does Haiku see the marker in their compiled memory context immediately after writing?
- Do they note any discrepancy between what they wrote and what they see?
- Do they flag anything as surprising?

---

## Phase 2 — Trigger Rebuild (James via UI)

⚠️ **PAUSE — ask James to do this step. No SDK calls on Haiku.**

James triggers a message reset for Haiku via the ADE UI. This forces `rebuild_system_prompt_async`
and flushes pending block changes to the stored system message.

The primary evidence of deferred behavior is already in Haiku's Phase 1 report:
they saw the tool return confirm the write succeeded, but their compiled context
did not update. That's the deferred state observed from the inside.

After James confirms the reset is done, proceed to Phase 3.

---

## Phase 3 — Re-observe (Haiku invocation 2)

### Prompt to Haiku

> Please check your `human` block and report exactly what you see in it right now.

### What to watch for

- Haiku should now see `LIVE_TEST_MARKER_alpha` in their compiled memory context.
- Compare against their Phase 1 after-state report. Did something change?

---

## Results

| Check | Result |
|-------|--------|
| Phase 1: Haiku's report | ✅ PASS — tool return showed write success, compiled context did NOT update. Deferred state observed honestly from inside. |
| Phase 2: James triggers compact via ADE button | ✅ Triggered |
| Phase 3: Haiku's report post-compact | ❌ FAIL — marker absent from compiled context after manual compact button |

### Failure Note (Phase 3)
Manual ADE compact button did not flush pending block write to stored system prompt.
Container confirmed on correct source branch. Likely cause: ADE compact button hits a
different code path than the `compact` REST endpoint where our `rebuild_system_prompt_async`
fix lives. **TODO:** investigate which endpoint the button calls. Lower priority — James
rarely uses the manual button. Organic compaction is the real path to validate.

---

## Phase 4 — Organic Compaction Test

Fill Haiku's context naturally until automatic compaction triggers. Observe whether the
marker appears in Haiku's compiled context afterward.

**Result: ✅ PASS** — After organic compaction, Haiku's compiled context contained
`LIVE_TEST_MARKER_alpha`. The marker persisted through compaction and was visible in
the rebuilt system prompt.

---

## Notes

- Haiku has memory tools only (no SDK/client access) — they observe their own context
- Each tool call in a headless invocation is a separate step in the Letta loop
- The subjective experience (Haiku noticing or not noticing the deferred state) is
  interesting independently of the external verification
- If Haiku reports confusion or inconsistency, that's a valid result — flag it
