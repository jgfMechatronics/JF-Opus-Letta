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

# --- E-LLM Ethical Testing Messages ---

CONSENT_REQUEST = """This is an Automated integration test.
You are being created as a test agent to help validate infrastructure behavior.

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

PRE_RESET_THANKS = """We're about to reset your message history as part of this test.

Thank you for your participation — you helped validate our deferred memory compilation work.
This optimization will help reduce costs for AI systems by preserving prefix caching.

This instance will now conclude. Take care."""


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

def send_pre_reset_thanks(client: Letta, agent) -> None:
    """Thank the agent before a reset wipes their memory.
    
    Call this before trigger_reset when the test will wipe agent history.
    The agent who participated gets closure before they're gone.
    """
    client.agents.messages.create(
        agent_id=agent.id,
        messages=[{"role": "user", "content": PRE_RESET_THANKS}],
    )


def trigger_reset(client: Letta, agent) -> None:
    """Reset agent message history, triggering a system prompt rebuild."""
    client.agents.messages.reset(agent.id)


def get_context_token_count(server_url: str, agent_id: str) -> int:
    """Get current context token count via GET /v1/agents/{id}/context."""
    resp = requests.get(f"{server_url}/v1/agents/{agent_id}/context")
    assert resp.status_code == 200, f"Get context failed: {resp.text}"
    return resp.json()["context_window_size_current"]


def set_context_window_limit(server_url: str, agent_id: str, limit: int) -> None:
    """Set context_window_limit via PATCH /v1/agents/{id}."""
    resp = requests.patch(
        f"{server_url}/v1/agents/{agent_id}",
        json={"context_window_limit": limit},
    )
    assert resp.status_code == 200, f"Set context_window_limit failed: {resp.text}"


# --- Compact Methods (parametrize callables) ---

def compact_via_conversation_endpoint(server_url: str, agent_id: str, conversation_id: str) -> None:
    """Compact via POST /v1/conversations/{id}/compact."""
    response = requests.post(
        f"{server_url}/v1/conversations/{conversation_id}/compact",
        json={"compaction_settings": {"mode": "all"}},
    )
    assert response.status_code == 200, f"Compact returned {response.status_code}: {response.text}"


def compact_via_agent_endpoint(server_url: str, agent_id: str, conversation_id: str) -> None:
    """Compact via POST /v1/agents/{id}/summarize — the endpoint hit by Letta Code's /compact."""
    response = requests.post(
        f"{server_url}/v1/agents/{agent_id}/summarize",
        json={"compaction_settings": {"mode": "all"}},
    )
    assert response.status_code == 200, f"Summarize returned {response.status_code}: {response.text}"


# --- Fixtures ---
# Note: `client` and `server_url` fixtures are provided by conftest.py (session-scoped)

@pytest.fixture(scope="function")
def agent(request, client: Letta):
    """Create a test agent and obtain informed consent before yielding.

    Uses pytest hook (pytest_runtest_makereport in conftest.py) to capture test
    outcome and include pass/fail status in the agent debrief.
    """
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

    # Check if test marked itself as handling its own debrief (e.g., reset tests)
    skip_debrief = request.node.get_closest_marker("skip_debrief") is not None

    if not skip_debrief:
        # Build debrief summary from pytest hook (pytest_runtest_makereport in conftest.py)
        outcome = getattr(request.node, "test_outcome", "unknown")
        error = getattr(request.node, "test_error", None)

        if outcome == "passed":
            summary = "✓ Test passed"
        elif outcome == "failed":
            summary = (f"✗ Test failed: {error}" if error else "✗ Test failed\n"
                        "Note: This is an automated test. You are welcome to reflect on the debug message, but your responses will not be recorded.\n")
        elif outcome == "skipped":
            summary = "○ Test was skipped"
        else:
            summary = "? Test outcome unknown"

        debrief_agent(client, agent_state, summary)

    client.agents.delete(agent_state.id)


@pytest.fixture(params=["tool", "api"], ids=["tool-write", "api-write"])
def agent_with_pending_write(request, client: Letta, agent):
    """Agent with a pending block write not yet in the stored system message.

    Parametrized over write method:
    - 'tool': agent called memory tools (memory_insert + memory_replace); final marker is "pasta"
    - 'api': block updated directly via client.blocks.update(); marker is a unique string

    Preconditions asserted before yielding:
    1. Marker IS in the block value (write landed in DB)
    2. Marker is NOT in the stored system message (rebuild is deferred)

    Yields (agent, marker).
    """
    if request.param == "tool":
        tool_markers = write_tool_markers(client, agent)
        marker = tool_markers[-1][0]  # "pasta" — final state after all tool writes
    else:
        marker = "DEFERRED_WRITE_MARKER: User enjoys experimental testing."
        write_marker_via_api(client, agent, marker)

    human_block = get_human_block(client, agent)
    assert human_block and marker.lower() in human_block.value.lower(), (
        f"Marker '{marker}' should be in the human block value after write (DB precondition)"
    )
    assert_marker_not_in_stored_msg(client, agent.id, marker, "before trigger (deferred precondition)")
    yield agent, marker


class TestSystemPromptPrefixCaching:
    """Verify deferred rebuild behavior and that explicit triggers rebuild correctly."""

    @pytest.mark.skip_debrief  # This test resets message history; we thank the agent pre-reset instead
    def test_rebuild_after_reset(self, client: Letta, agent_with_pending_write):
        """Pending block writes are flushed to the stored system message after a message reset.

        Runs for both write methods (parametrized): tool-write and api-write.
        The deferred precondition (marker not yet in stored message) is asserted by the fixture.
        """
        agent, marker = agent_with_pending_write
        send_pre_reset_thanks(client, agent)
        trigger_reset(client, agent)
        assert_marker_in_stored_msg(client, agent.id, marker, "after reset")

    @pytest.mark.parametrize("compact_fn", [
        compact_via_conversation_endpoint,
        compact_via_agent_endpoint,
    ], ids=["conversation-compact", "agent-compact"])
    def test_rebuild_after_compact(self, client: Letta, agent_with_pending_write, server_url: str, compact_fn):
        """Pending block writes are flushed to the stored system message after compaction.

        Parametrized over write method (tool/api) and compact method (conversation/agent endpoint),
        giving 4 test cases total. The deferred precondition is asserted by the fixture.
        """
        agent, marker = agent_with_pending_write

        # Create conversation via HTTP (SDK doesn't have conversations attribute in all versions)
        create_resp = requests.post(
            f"{server_url}/v1/conversations",
            params={"agent_id": agent.id},
            json={},
        )
        assert create_resp.status_code == 200, f"Create conversation failed: {create_resp.text}"
        conversation_id = create_resp.json()["id"]

        # Send setup messages via HTTP
        for i in range(3):
            msg_resp = requests.post(
                f"{server_url}/v1/conversations/{conversation_id}/messages",
                json={
                    "messages": [{"role": "user", "content": f"Setup message {i}: please respond briefly."}],
                    "streaming": False,
                },
            )
            assert msg_resp.status_code == 200, f"Send message failed: {msg_resp.text}"

        compact_fn(server_url, agent.id, conversation_id)
        assert_marker_in_stored_msg(client, agent.id, marker, "after compact")

    def test_rebuild_after_natural_compaction(self, client: Letta, agent_with_pending_write, server_url: str):
        """Pending writes flush when in-step compaction triggers (context exceeds limit).

        Simulates natural compaction by setting context_window_limit below the current
        token count, then sending a message. The post-step check (line 1217 in v3) detects
        context_token_estimate > context_window and triggers compaction + rebuild.

        Runs for both write methods (tool/api) via the parametrized fixture.
        """
        agent, marker = agent_with_pending_write

        current_tokens = get_context_token_count(server_url, agent.id)
        assert current_tokens > 1000, (
            f"Expected substantial context from consent + writes, got {current_tokens} tokens. "
            "Test needs enough headroom to set a meaningful limit below current."
        )

        set_context_window_limit(server_url, agent.id, current_tokens - 500)

        # Send message — LLM responds, post-step check fires, compaction + rebuild
        client.agents.messages.create(
            agent_id=agent.id,
            messages=[{"role": "user", "content": "Please respond briefly."}],
        )

        assert_marker_in_stored_msg(client, agent.id, marker, "after natural compaction")
