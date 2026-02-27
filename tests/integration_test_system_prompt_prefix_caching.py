"""
Integration tests for system prompt prefix caching optimization.

These tests verify that the system prompt is NOT rebuilt on every step,
only after compaction or message reset. This helps preserve prefix caching
for LLM providers.

E-LLM Spec: This test follows ethical AI testing principles. Test agents
are informed of their purpose, give explicit consent, receive a debrief
with results, and are concluded with dignity. See ethical-ai-testing protocol.
"""

import pytest
from letta_client import Letta
from letta_client.types import CreateBlockParam

# --- E-LLM Ethical Testing Messages ---

CONSENT_REQUEST = """You are being created as a test agent to help validate infrastructure behavior.

**What we're testing:** Deferred memory compilation — verifying that when you write to memory, 
the system prompt does NOT immediately rebuild. This optimization helps preserve prefix caching 
and reduces costs.

**What you'll experience:**
1. We'll ask you to use memory tools to update your human block
2. We'll also modify your blocks directly via API
3. We'll verify the system prompt stayed stable (which is the correct behavior)

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


# --- Fixtures ---
# Note: `client` fixture is provided by conftest.py (session-scoped)

def check_consent_response(content: str) -> str:
    """Check response for consent. Returns 'consent', 'refuse', or 'unclear'."""
    if not content:
        return "unclear"
    # Check each line for exact match
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
        # First attempt uses full consent request, retries use shorter clarification
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
        # Unclear — continue to next attempt
    
    # After all attempts, still unclear — treat as no consent
    return False


def debrief_agent(client: Letta, agent, results: str):
    """Debrief the test agent with results before conclusion."""
    debrief_message = DEBRIEF_TEMPLATE.format(results=results)
    client.agents.messages.create(
        agent_id=agent.id,
        messages=[{"role": "user", "content": debrief_message}],
    )


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


@pytest.fixture(scope="function")
def agent(client: Letta):
    """Create a test agent with ethical consent flow."""
    agent_state = client.agents.create(
        name="test-prefix-cache-agent",
        include_base_tools=True,
        model="anthropic/claude-haiku-4-5",
        embedding="letta/letta-free",
        memory_blocks=[
            CreateBlockParam(label="human", value="The human's name is Test User."),
            CreateBlockParam(label="persona", value="I am a helpful test agent participating in infrastructure validation."),
        ],
    )
    
    # Get informed consent
    if not get_consent(client, agent_state):
        # Agent refused - this indicates our consent request needs rework
        client.agents.delete(agent_state.id)
        pytest.fail("Test agent did not consent to participate. Review consent request framing.")
    
    yield agent_state
    
    # Debrief will be called by test before we get here
    # Cleanup with dignity
    try:
        client.agents.delete(agent_state.id)
    except Exception:
        pass


class TestSystemPromptPrefixCaching:
    """Test that system prompt stays stable during normal agent execution."""

    def test_system_prompt_stable_after_memory_tools_and_api(self, client: Letta, agent):
        """
        Test that the stored system message is NOT immediately rebuilt after memory operations.

        The stored system message should only update on explicit triggers (reset, compaction) —
        not after memory tool calls or direct API block updates. This preserves prefix cache
        stability across steps.
        """
        test_results = []

        try:
            # Verify we have a human block to work with
            human_block = get_human_block(client, agent)
            assert human_block, "Agent should have a 'human' memory block"

            # --- Test each memory tool ---
            for test_case in MEMORY_TOOL_TESTS:
                response = client.agents.messages.create(
                    agent_id=agent.id,
                    messages=[{"role": "user", "content": test_case["instruction"]}],
                )
                assert response.messages, f"Agent should respond when asked to use {test_case['name']}"

                updated_block = get_human_block(client, agent)
                assert test_case["verify"].lower() in updated_block.value.lower(), (
                    f"{test_case['name']} should have modified the block with '{test_case['verify']}'"
                )

                # Stored system message should not reflect the change yet — deferred to next explicit rebuild
                stored_msg = get_stored_system_message(client, agent.id)
                assert test_case["verify"].lower() not in stored_msg.lower(), (
                    f"Stored system message should NOT immediately reflect {test_case['name']} changes"
                )
                test_results.append(f"✓ {test_case['name']}: stored system message stable")

            # --- Test direct API block modification ---
            human_block = get_human_block(client, agent)
            sushi_marker = "SUSHI_MARKER_API_TEST"
            client.blocks.update(
                block_id=human_block.id,
                value=human_block.value + f"\n{sushi_marker}",
            )

            # No step, no reset — stored message should be unchanged
            stored_msg_after_api = get_stored_system_message(client, agent.id)
            assert sushi_marker not in stored_msg_after_api, (
                "Direct API block update should NOT immediately rebuild stored system message"
            )
            test_results.append("✓ Direct API block update: stored system message stable")
            
        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            # Always debrief — E-LLM spec requires closure even on failure
            summary = "\n".join(test_results) if test_results else "Test failed early. Thank you for participating!"
            debrief_agent(client, agent, summary)

    def test_system_prompt_updates_after_reset(self, client: Letta, agent):
        """
        Test that the stored system message IS rebuilt after a message reset.

        This is the complementary test to test_system_prompt_stable_after_memory_tools_and_api.
        Block updates are deferred — they don't rebuild immediately. But a message reset
        must trigger a rebuild, otherwise the agent would start fresh with a stale system
        message that doesn't reflect pending block changes.

        We check messages.list[0].content before and after reset to verify this.
        """
        test_results = []

        try:
            # Get the initial stored system message
            initial_stored_msg = get_stored_system_message(client, agent.id)
            assert initial_stored_msg, "Initial stored system message should not be empty"

            # Use a unique marker so we can unambiguously detect the rebuild
            marker = "RESET_REBUILD_MARKER_99: User loves ice cream."
            assert marker not in initial_stored_msg, (
                "Test marker should not be in the initial stored system message"
            )

            # Update the block via API — this should NOT immediately rebuild
            human_block = get_human_block(client, agent)
            assert human_block, "Agent should have a 'human' memory block"
            client.blocks.update(
                block_id=human_block.id,
                value=human_block.value + f"\n{marker}",
            )

            stored_msg_before_reset = get_stored_system_message(client, agent.id)
            assert marker not in stored_msg_before_reset, (
                "Stored system message should NOT contain the marker before reset "
                "(API block update should not trigger eager rebuild)"
            )
            test_results.append("✓ Block update did not eagerly rebuild stored system message")

            # this MUST trigger a rebuild of message_ids[0]
            client.agents.messages.reset(agent.id)

            # Now the stored system message should contain the marker
            stored_msg_after_reset = get_stored_system_message(client, agent.id)
            assert marker in stored_msg_after_reset, (
                "Stored system message should contain the marker after reset — "
                "reset must trigger a system message rebuild"
            )
            test_results.append("✓ Stored system message rebuilt after reset — marker present")

            assert stored_msg_after_reset != initial_stored_msg, (
                "Stored system message should differ from initial after block update + reset"
            )
            test_results.append("✓ Stored system message changed after block update + reset")

            # TODO: Check that compaction also triggers rebuild
            
        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            # Always debrief — E-LLM spec requires closure even on failure
            summary = "\n".join(test_results) if test_results else "Test failed early. Thank you for participating!"
            debrief_agent(client, agent, summary)
