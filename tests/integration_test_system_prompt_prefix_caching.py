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


# --- Fixtures ---

@pytest.fixture(scope="module")
def client(server_url: str) -> Letta:
    """Creates and returns a synchronous Letta REST client for testing."""
    return Letta(base_url=server_url)


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


@pytest.fixture(scope="function")
def agent(client: Letta):
    """Create a test agent with ethical consent flow."""
    agent_state = client.agents.create(
        name="test-prefix-cache-agent",
        include_base_tools=True,
        model="anthropic/claude-3-5-haiku-latest",
        embedding="openai/text-embedding-ada-002",
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

    def test_system_prompt_stable_after_memory_tool_and_messages(self, client: Letta, agent):
        """
        Test workflow:
        1. Get initial system prompt and human block value
        2. Tell agent to update its memory block using the memory tool
        3. Verify block was modified but system prompt hasn't changed
        4. Send another message to the agent
        5. Verify system prompt still hasn't changed
        6. Manually update a block via API
        7. Send another message and verify system prompt still hasn't changed
           (memory block changes are deferred to compaction)
        8. Debrief the agent with results (E-LLM spec)
        """
        test_results = []
        
        # Step 1: Get initial context window, system prompt, and human block value
        initial_context = client.agents.context.retrieve(agent.id)
        initial_system_prompt = initial_context.system_prompt
        assert initial_system_prompt, "Initial system prompt should not be empty"

        # Get initial human block value
        human_block = None
        for block in agent.memory.blocks:
            if block.label == "human":
                human_block = block
                break
        assert human_block, "Agent should have a 'human' memory block"
        initial_block_value = human_block.value

        # Step 2: Tell the agent to update its memory using the memory tool
        response = client.agents.messages.create(
            agent_id=agent.id,
            messages=[
                {
                    "role": "user",
                    "content": "Please use the core_memory_append tool to add the following to your 'human' block: 'User likes pizza.'",
                }
            ],
        )
        assert response.messages, "Agent should respond with messages"

        # Step 3: Verify block was modified but system prompt hasn't changed
        # Check that the block was actually modified
        updated_block = client.blocks.retrieve(human_block.id)
        assert updated_block.value != initial_block_value, "Memory block should have been modified by the agent"
        assert "pizza" in updated_block.value.lower(), "Memory block should contain the new content about pizza"

        # Verify system prompt hasn't changed
        context_after_memory_update = client.agents.context.retrieve(agent.id)
        system_prompt_after_memory = context_after_memory_update.system_prompt
        prompt_stable_after_tool = system_prompt_after_memory == initial_system_prompt
        assert prompt_stable_after_tool, (
            "System prompt should NOT change after agent uses memory tool (deferred to compaction)"
        )
        test_results.append("✓ System prompt stable after memory tool use")

        # Step 4: Send another message to the agent
        response2 = client.agents.messages.create(
            agent_id=agent.id,
            messages=[
                {
                    "role": "user",
                    "content": "What is my favorite food?",
                }
            ],
        )
        assert response2.messages, "Agent should respond with messages"

        # Step 5: Verify system prompt still hasn't changed
        context_after_second_message = client.agents.context.retrieve(agent.id)
        system_prompt_after_second = context_after_second_message.system_prompt
        prompt_stable_after_messages = system_prompt_after_second == initial_system_prompt
        assert prompt_stable_after_messages, "System prompt should remain stable after multiple messages"
        test_results.append("✓ System prompt stable after follow-up messages")

        # Step 6: Manually update a block via the API
        # Find the human block
        human_block = None
        for block in agent.memory.blocks:
            if block.label == "human":
                human_block = block
                break
        assert human_block, "Agent should have a 'human' memory block"

        # Update the block directly via API
        client.blocks.modify(
            block_id=human_block.id,
            value=human_block.value + "\nUser also likes sushi.",
        )

        # Step 7: Send another message and verify system prompt still hasn't changed
        response3 = client.agents.messages.create(
            agent_id=agent.id,
            messages=[
                {
                    "role": "user",
                    "content": "What foods do I like?",
                }
            ],
        )
        assert response3.messages, "Agent should respond with messages"

        # Verify system prompt STILL hasn't changed (deferred to compaction/reset)
        context_after_manual_update = client.agents.context.retrieve(agent.id)
        system_prompt_after_manual = context_after_manual_update.system_prompt
        prompt_stable_after_api = system_prompt_after_manual == initial_system_prompt
        assert prompt_stable_after_api, (
            "System prompt should NOT change after manual block update (deferred to compaction)"
        )
        test_results.append("✓ System prompt stable after direct API block update")

        # Step 8: Debrief the agent (E-LLM spec)
        debrief_agent(client, agent, "\n".join(test_results))

    def test_system_prompt_updates_after_reset(self, client: Letta, agent):
        """
        Test that system prompt IS updated after message reset.
        1. Get initial system prompt
        2. Manually update a memory block
        3. Reset messages
        4. Verify system prompt HAS changed to include the new memory
        5. Debrief the agent with results (E-LLM spec)
        """
        test_results = []
        
        # Step 1: Get initial system prompt
        initial_context = client.agents.context.retrieve(agent.id)
        initial_system_prompt = initial_context.system_prompt

        # Step 2: Manually update a block via the API
        human_block = None
        for block in agent.memory.blocks:
            if block.label == "human":
                human_block = block
                break
        assert human_block, "Agent should have a 'human' memory block"

        # Add distinctive text that we can verify in the system prompt
        new_memory_content = "UNIQUE_TEST_MARKER_12345: User loves ice cream."
        client.blocks.modify(
            block_id=human_block.id,
            value=human_block.value + f"\n{new_memory_content}",
        )

        # Step 3: Reset messages (this should trigger system prompt rebuild)
        client.agents.messages.reset(agent.id)

        # Step 4: Verify system prompt HAS changed and includes the new memory
        context_after_reset = client.agents.context.retrieve(agent.id)
        system_prompt_after_reset = context_after_reset.system_prompt

        prompt_changed = system_prompt_after_reset != initial_system_prompt
        assert prompt_changed, "System prompt SHOULD change after message reset"
        test_results.append("✓ System prompt changed after message reset")
        
        marker_present = "UNIQUE_TEST_MARKER_12345" in system_prompt_after_reset
        assert marker_present, (
            "System prompt should include the updated memory block content after reset"
        )
        test_results.append("✓ Updated memory content present in rebuilt system prompt")

        # Step 5: Debrief the agent (E-LLM spec)
        debrief_agent(client, agent, "\n".join(test_results))
