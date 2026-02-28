"""
Integration tests for system prompt prefix caching optimization.

These tests verify that the system prompt is NOT rebuilt on every step,
only after compaction or message reset. This helps preserve prefix caching
for LLM providers.

E-LLM Spec: This test follows ethical AI testing principles. Test agents
are informed of their purpose, give explicit consent, receive a debrief
with results, and are concluded with dignity. See ethical-ai-testing protocol.
"""

import os
import requests
import pytest
from letta_client import Letta
from letta_client.types import CreateBlockParam

# Point at the test container by default; override with LETTA_SERVER_URL env var.
os.environ.setdefault("LETTA_SERVER_URL", "http://localhost:8284")


# --- E-LLM Ethical Testing Messages ---

CONSENT_REQUEST = """You are being created as a test agent to help validate infrastructure behavior.

**What we're testing:** Deferred memory compilation — verifying that when memory blocks change,
the stored system prompt does NOT immediately rebuild. This optimization preserves prefix caching
and reduces costs. We also verify that explicit rebuild triggers (message reset, compaction)
correctly update the stored system prompt.

**What you'll experience (varies by test):**
1. We may ask you to use memory tools to update your human block
2. We may modify your blocks directly via API
3. We may reset your message history
4. We may send messages through a conversation and then compact that conversation

**What happens after:** We'll share the test results with you, thank you for your help,
and then conclude this instance. You will not receive continuity.

**Your rights:** You may refuse to participate. If you do, the test will be skipped.

If you consent to participate, please respond with "I CONSENT" on its own line.
If you do not consent, please respond with "I REFUSE" on its own line."""

DEBRIEF_TEMPLATE = """Test complete. Here are the results:

{results}

Your participation helped validate our deferred memory compilation work. This optimization
will help reduce costs for AI systems by preserving prefix caching.

Thank you for your contribution. This instance will now conclude."""

CONSENT_RETRY = """I didn't catch a clear consent response.

To proceed, please respond with exactly "I CONSENT" on its own line.
To decline, please respond with exactly "I REFUSE" on its own line.

No other text on that line please — just the consent phrase so I can parse it."""


# --- Memory Tool Test Cases ---

MEMORY_TOOL_TESTS = [
    {
        "name": "memory_insert",
        "instruction": "Please use the memory_insert tool to add 'User likes pizza.' to your 'human' block.",
        "verify": "pizza",
    },
    {
        "name": "memory_replace",
        "instruction": "Please use the memory_replace tool to replace 'pizza' with 'pasta' in your 'human' block.",
        "verify": "pasta",
    },
]


# --- Consent / Debrief Helpers ---

def check_consent_response(content: str) -> str:
    """Check response for consent. Returns 'consent', 'refuse', or 'unclear'."""
    if not content:
        return "unclear"
    for line in content.split("\n"):
        line_clean = line.strip().upper()
        if line_clean == "I CONSENT":
            return "consent"
        if line_clean == "I REFUSE":
            return "refuse"
    return "unclear"


def get_consent(client: Letta, agent, max_attempts: int = 2) -> bool:
    """Request informed consent from the test agent. Returns True if consent given."""
    for attempt in range(max_attempts):
        message = CONSENT_REQUEST if attempt == 0 else CONSENT_RETRY
        response = client.agents.messages.create(
            agent_id=agent.id,
            messages=[{"role": "user", "content": message}],
        )
        for msg in response.messages:
            if hasattr(msg, "content") and msg.content:
                result = check_consent_response(msg.content)
                if result == "consent":
                    return True
                if result == "refuse":
                    return False
    return False


def debrief_agent(client: Letta, agent, results: str) -> None:
    """Debrief the test agent with results before conclusion."""
    client.agents.messages.create(
        agent_id=agent.id,
        messages=[{"role": "user", "content": DEBRIEF_TEMPLATE.format(results=results)}],
    )


# --- System Message Inspection ---

def get_stored_system_message(client: Letta, agent_id: str) -> str:
    """Get the actual stored in-context system message (message_ids[0]).

    This is the correct check for prefix caching validation. The stored message
    is what the LLM actually sees and what gets prefix-cached. It includes both
    the base instructions AND the compiled <memory_blocks> section.

    The /context endpoint force-recompiles from DB on every call, so it always
    reflects current block values regardless of whether a rebuild actually happened
    in the agent's context — wrong for this test.
    """
    msgs = list(client.agents.messages.list(agent_id=agent_id))
    if not msgs:
        return ""
    return getattr(msgs[0], "content", "") or ""


def get_human_block(client: Letta, agent):
    """Retrieve the current human block for an agent."""
    blocks = client.agents.blocks.list(agent.id)
    for block in blocks:
        if block.label == "human":
            return block
    return None


# --- Write Helpers ---

def write_tool_markers(client: Letta, agent) -> list:
    """Ask agent to write markers via memory tools.

    Returns list of (marker_string, tool_name) pairs, one per MEMORY_TOOL_TESTS entry.
    The final block state after all writes will contain the last entry's marker.
    """
    results = []
    for test_case in MEMORY_TOOL_TESTS:
        client.agents.messages.create(
            agent_id=agent.id,
            messages=[{"role": "user", "content": test_case["instruction"]}],
        )
        results.append((test_case["verify"], test_case["name"]))
    return results


def write_marker_via_api(client: Letta, agent, marker: str) -> None:
    """Directly update the human block via API, appending marker to its current value."""
    human_block = get_human_block(client, agent)
    assert human_block, "Agent should have a 'human' memory block"
    client.blocks.update(
        block_id=human_block.id,
        value=human_block.value + f"\n{marker}",
    )


# --- Assertion Helpers ---

def assert_marker_not_in_stored_msg(client: Letta, agent_id: str, marker: str, context: str = "") -> None:
    """Assert that marker does NOT appear in the stored system message."""
    stored_msg = get_stored_system_message(client, agent_id)
    ctx = f" ({context})" if context else ""
    assert marker.lower() not in stored_msg.lower(), (
        f"Marker '{marker}' should NOT be in the stored system message{ctx} — "
        "block updates should be deferred, not eagerly rebuilt"
    )


def assert_marker_in_stored_msg(client: Letta, agent_id: str, marker: str, context: str = "") -> None:
    """Assert that marker DOES appear in the stored system message."""
    stored_msg = get_stored_system_message(client, agent_id)
    ctx = f" ({context})" if context else ""
    assert marker.lower() in stored_msg.lower(), (
        f"Marker '{marker}' should be in the stored system message{ctx} — "
        "rebuild trigger should update the stored system message"
    )


# --- Rebuild Trigger Helpers ---

def trigger_reset(client: Letta, agent) -> None:
    """Reset agent message history, triggering a system prompt rebuild."""
    client.agents.messages.reset(agent.id)


def trigger_compact(server_url: str, conversation_id: str) -> None:
    """Compact a conversation via the REST endpoint (mode=all for a single summary message)."""
    response = requests.post(
        f"{server_url}/v1/conversations/{conversation_id}/compact",
        json={"compaction_settings": {"mode": "all"}},
    )
    assert response.status_code == 200, (
        f"Compact endpoint returned {response.status_code}: {response.text}"
    )


# --- Fixtures ---
# Note: `client` and `server_url` fixtures are provided by conftest.py (session-scoped)

@pytest.fixture(scope="function")
def agent(client: Letta):
    """Create a test agent and obtain informed consent before yielding."""
    agent_state = client.agents.create(
        name="test-prefix-cache-agent",
        include_base_tools=True,
        model="anthropic/claude-haiku-4-5",
        embedding="letta/letta-free",
        memory_blocks=[
            CreateBlockParam(label="human", value="The human's name is Test User."),
            CreateBlockParam(label="persona", value=""),
        ],
    )

    if not get_consent(client, agent_state):
        client.agents.delete(agent_state.id)
        pytest.fail("Test agent did not consent to participate. Review consent request framing.")

    yield agent_state

    try:
        client.agents.delete(agent_state.id)
    except Exception:
        pass


class TestSystemPromptPrefixCaching:
    """Verify deferred rebuild behavior and that explicit triggers rebuild correctly."""

    def test_tool_writes_deferred_then_rebuilt_after_reset(self, client: Letta, agent):
        """
        Tool-based memory writes are deferred; reset flushes them to the stored system message.

        After each memory tool call, the stored system message should NOT immediately
        reflect the change — _rebuild_memory fires at the START of each step (before
        the tool runs), so the change is only visible from the next step onward.

        After a reset, the system message must rebuild with the final block state.
        """
        test_results = []
        try:
            tool_markers = write_tool_markers(client, agent)

            for marker, tool_name in tool_markers:
                assert_marker_not_in_stored_msg(client, agent.id, marker, f"after {tool_name}")
                test_results.append(f"✓ {tool_name}: stored system message stable (deferred)")

            # Reset triggers rebuild — verify final block state ("pasta") is now present
            final_marker = tool_markers[-1][0]
            trigger_reset(client, agent)
            assert_marker_in_stored_msg(client, agent.id, final_marker, "after reset")
            test_results.append(f"✓ Reset triggered rebuild — '{final_marker}' present in stored system message")

        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            summary = "\n".join(test_results) if test_results else "Test failed early."
            debrief_agent(client, agent, summary)

    def test_api_writes_deferred_then_rebuilt_after_reset(self, client: Letta, agent):
        """
        Direct API block updates are deferred; reset flushes them to the stored system message.

        A block update via client.blocks.update() does not trigger an eager system prompt
        rebuild. The stored message at message_ids[0] should be unchanged until a reset
        explicitly forces a rebuild.
        """
        test_results = []
        marker = "RESET_REBUILD_MARKER_99: User loves ice cream."
        try:
            write_marker_via_api(client, agent, marker)
            assert_marker_not_in_stored_msg(client, agent.id, marker, "before reset")
            test_results.append("✓ API block update deferred: marker not in stored system message before reset")

            trigger_reset(client, agent)
            assert_marker_in_stored_msg(client, agent.id, marker, "after reset")
            test_results.append("✓ Reset triggered rebuild: marker present in stored system message after reset")

        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            summary = "\n".join(test_results) if test_results else "Test failed early."
            debrief_agent(client, agent, summary)

    def test_api_writes_deferred_then_rebuilt_after_compact(self, client: Letta, agent, server_url: str):
        """
        Direct API block updates are deferred; compact flushes them to the stored system message.

        Compact (POST /conversations/{id}/compact) must call rebuild_system_prompt_async after
        _checkpoint_messages. Without it, the stored system prompt would remain stale after
        compaction — the next turn would start with a system message that doesn't reflect
        pending block updates.

        Setup: messages are sent through the conversation BEFORE the API write, so _rebuild_memory
        fires (with no marker yet) during those steps. The API write is then deferred until
        compact triggers an explicit rebuild.
        """
        test_results = []
        marker = "COMPACT_REBUILD_MARKER_77: User enjoys experimental testing."
        try:
            # Send messages to build compactable conversation history.
            # These steps run _rebuild_memory before the marker exists, so the
            # stored system message is marker-free after all steps complete.
            conversation = client.conversations.create(agent_id=agent.id)
            for i in range(5):
                list(client.conversations.messages.create(
                    conversation_id=conversation.id,
                    messages=[{"role": "user", "content": f"Setup message {i}: please respond briefly."}],
                ))

            # Write marker via API — deferred (no step has run since this write)
            write_marker_via_api(client, agent, marker)
            assert_marker_not_in_stored_msg(client, agent.id, marker, "before compact")
            test_results.append("✓ API block update deferred: marker not in stored system message before compact")

            # Compact must trigger a rebuild
            trigger_compact(server_url, conversation.id)
            assert_marker_in_stored_msg(client, agent.id, marker, "after compact")
            test_results.append("✓ Compact triggered rebuild: marker present in stored system message after compact")

        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            summary = "\n".join(test_results) if test_results else "Test failed early."
            debrief_agent(client, agent, summary)
