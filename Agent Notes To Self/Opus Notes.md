# Opus Notes

## Compaction Warning Feature (Memory Pressure Alert)

### What it does
Warns me when context is approaching compaction threshold by injecting a message so I can save important stuff before compaction hits.

### Files Modified
1. **letta/agents/letta_agent_v3.py** - Main logic
2. **letta/schemas/agent.py** - `UpdateAgent` schema field
3. **letta/services/agent_manager.py** - `scalar_updates` whitelist
4. **letta/orm/agent.py** - Column declaration + `to_pydantic()` mappings
5. **alembic migration** - `a1b2c3d4e5f9_add_memory_pressure_alerted_to_agents.py`

### Implementation Details

**In letta_agent_v3.py:**
- Check happens inside the main stream loop
- Conditions for triggering:
  1. `summarizer_settings.send_memory_warning_message` is True
  2. `memory_pressure_alerted` flag is False (haven't warned yet)
  3. `message_buffer_autoclear` is False
  4. `context_token_estimate` exists
  5. Token estimate > `context_window * memory_warning_threshold`
- When triggered: injects warning message, sets flag True, forces continuation
- Flag reset: happens in `_rebuild_memory()` after compaction completes

**Persistence:**
- `_set_memory_pressure_alerted()` helper updates both in-memory state and DB
- Uses `UpdateAgent` schema + `agent_manager.update_agent_async()`
- **Critical fix (Feb 17):** The `to_pydantic()` AND `to_pydantic_async()` mappings were missing — field existed in DB but never loaded back. Fixed with `or False` for NULL handling.

**How the flag works:**
- Threshold check runs every turn: `context_tokens > (context_window * threshold)`
- Flag = "did we alert" = disarm mechanism (same thing)
- When True → check short-circuits, no alert fires
- Without persistence: flag reset each session → spam every turn past threshold
- Bug: flag written to DB but never read back → no effective persistence → spam

### Known Edge Cases
- **User-triggered compaction or message clearing** may bypass the reset in `_rebuild_memory()`, leaving `memory_pressure_alerted=True` stuck and suppressing future alerts. TODO in code says: "Audit all compaction/clear paths to ensure flag resets appropriately."

### Test Plan (TODO)
1. Normal flow: verify warning fires when approaching threshold
2. Verify flag persists across sessions
3. Verify flag resets after compaction
4. Edge case: user-triggered compaction
5. Edge case: message clearing
6. Verify warning doesn't fire if already alerted

### Git Commits (chronological)
- `bd9048968` - Prototype (instance-level flag only)
- `2d03d2771` - Bug fix for non-existent member access
- `6743afbbf` - Proper message format
- `9d92d92c1` - DB persistence work begins
- `f44d82adf` - Added UpdateAgent field, agent_manager whitelist
- `a1f4bcca8` - THE FIX: to_pydantic mappings added, it works

---
*Last updated: Feb 20, 2026*
