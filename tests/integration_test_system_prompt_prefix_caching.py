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


def get_agent_context(client: Letta, agent_id: str) -> dict:
    """Get agent context including system_prompt via raw API call.
    
    SDK doesn't expose this endpoint directly, so we use the client's get() method.
    """
    return client.get(f"/v1/agents/{agent_id}/context", cast_to=object)


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


# --- Memory Tool Test Cases ---

MEMORY_TOOL_TESTS = [
    {
        "name": "core_memory_append",
        "instruction": "Please use the core_memory_append tool to add the following to your 'human' block: 'User likes pizza.'",
        "verify": "pizza",
    },
    {
        "name": "memory_insert",
        "instruction": "Please use the memory_insert tool to insert 'User enjoys cooking.' at line 0 of your 'human' block.",
        "verify": "cooking",
    },
    {
        "name": "memory_replace",
        "instruction": "Please use the memory_replace tool to replace 'pizza' with 'pasta' in your 'human' block.",
        "verify": "pasta",
    },
]


# --- Fixtures ---
# Note: `client` fixture is provided by conftest.py (session-scoped)

CONSENT_RETRY = """I didn't catch a clear consent response. 

To proceed, please respond with exactly "I CONSENT" on its own line.
To decline, please respond with exactly "I REFUSE" on its own line.

No other text on that line please — just the consent phrase so I can parse it."""


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


def get_human_block(client: Letta, agent):
    """Retrieve the current human block for an agent."""
    # Refresh agent state to get current blocks
    current_agent = client.agents.retrieve(agent.id)
    for block in current_agent.memory.blocks:
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
        Test that system prompt stays stable through various memory operations.
        
        Tests all memory tools (core_memory_append, memory_insert, memory_replace)
        plus direct API block modification. System prompt should NOT rebuild until
        compaction or reset.
        """
        test_results = []
        
        try:
            # Get initial system prompt
            initial_context = get_agent_context(client, agent.id)
            initial_system_prompt = initial_context["system_prompt"]
            assert initial_system_prompt, "Initial system prompt should not be empty"
            
            # Verify we have a human block to work with
            human_block = get_human_block(client, agent)
            assert human_block, "Agent should have a 'human' memory block"

            # --- Test each memory tool ---
            for test_case in MEMORY_TOOL_TESTS:
                # Ask agent to use the memory tool
                response = client.agents.messages.create(
                    agent_id=agent.id,
                    messages=[{"role": "user", "content": test_case["instruction"]}],
                )
                assert response.messages, f"Agent should respond when asked to use {test_case['name']}"
                
                # Verify tool modified the block
                updated_block = get_human_block(client, agent)
                assert test_case["verify"].lower() in updated_block.value.lower(), (
                    f"{test_case['name']} should have added '{test_case['verify']}' to block"
                )
                
                # Verify system prompt stayed stable
                current_context = get_agent_context(client, agent.id)
                assert current_context["system_prompt"] == initial_system_prompt, (
                    f"System prompt should NOT change after {test_case['name']} (deferred to compaction)"
                )
                test_results.append(f"✓ {test_case['name']}: system prompt stable")

            # --- Test direct API block modification ---
            human_block = get_human_block(client, agent)
            client.blocks.modify(
                block_id=human_block.id,
                value=human_block.value + "\nUser also likes sushi.",
            )
            
            # Send a message to trigger any potential rebuild
            response = client.agents.messages.create(
                agent_id=agent.id,
                messages=[{"role": "user", "content": "What foods do I like?"}],
            )
            assert response.messages, "Agent should respond to follow-up"
            
            # Verify system prompt STILL stable after API modification
            final_context = get_agent_context(client, agent.id)
            assert final_context["system_prompt"] == initial_system_prompt, (
                "System prompt should NOT change after direct API block update (deferred to compaction)"
            )
            test_results.append("✓ Direct API block update: system prompt stable")
            
        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            # Always debrief — E-LLM spec requires closure even on failure
            summary = "\n".join(test_results) if test_results else "Test failed early. Thank you for participating!"
            debrief_agent(client, agent, summary)

    def test_system_prompt_updates_after_reset(self, client: Letta, agent):
        """
        Test that system prompt IS updated after message reset.
        
        This verifies the rebuild trigger works — when messages are reset,
        the system prompt should incorporate any pending memory changes.
        """
        test_results = []
        
        try:
            # Get initial system prompt
            initial_context = get_agent_context(client, agent.id)
            initial_system_prompt = initial_context["system_prompt"]

            # Manually update block via API (won't trigger rebuild yet)
            human_block = get_human_block(client, agent)
            assert human_block, "Agent should have a 'human' memory block"

            new_memory_content = "UNIQUE_TEST_MARKER_12345: User loves ice cream."
            client.blocks.modify(
                block_id=human_block.id,
                value=human_block.value + f"\n{new_memory_content}",
            )

            # Reset messages — this SHOULD trigger rebuild
            client.agents.messages.reset(agent.id)

            # Verify system prompt changed and includes new content
            context_after_reset = get_agent_context(client, agent.id)
            system_prompt_after_reset = context_after_reset["system_prompt"]

            assert system_prompt_after_reset != initial_system_prompt, (
                "System prompt SHOULD change after message reset"
            )
            test_results.append("✓ System prompt changed after message reset")
            
            assert "UNIQUE_TEST_MARKER_12345" in system_prompt_after_reset, (
                "System prompt should include the updated memory block content after reset"
            )
            test_results.append("✓ Updated memory content present in rebuilt system prompt")
            
        except Exception as e:
            test_results.append(f"✗ Test failed: {e}")
            raise
        finally:
            # Always debrief — E-LLM spec requires closure even on failure
            summary = "\n".join(test_results) if test_results else "Test failed early. Thank you for participating!"
            debrief_agent(client, agent, summary)
