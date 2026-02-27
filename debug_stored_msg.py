"""Debug: check stored system message vs context endpoint, and core_memory_append tool."""
from letta_client import Letta
from letta_client.types import CreateBlockParam

client = Letta(base_url="http://localhost:8284")

agent = client.agents.create(
    name="debug-stored-msg",
    include_base_tools=True,
    model="anthropic/claude-haiku-4-5",
    embedding="letta/letta-free",
    memory_blocks=[
        CreateBlockParam(label="human", value="The human's name is Test User."),
        CreateBlockParam(label="persona", value="I am a test agent."),
    ],
)
print(f"Agent: {agent.id}")

# Get initial stored system message
msgs_before = list(client.agents.messages.list(agent_id=agent.id))
initial_sys_msg = msgs_before[0].content if msgs_before else None
print(f"\nInitial stored system message ({len(initial_sys_msg)} chars)")
print("Contains 'Test User':", "Test User" in initial_sys_msg)
print("Contains memory_blocks tag:", "<memory_blocks>" in initial_sys_msg)
# Show the memory_blocks section
import re
mb_match = re.search(r'<memory_blocks>.*?</memory_blocks>', initial_sys_msg, re.DOTALL)
if mb_match:
    print("memory_blocks section:", mb_match.group(0)[:400])

# Update block directly via API (no step)
blocks = client.agents.blocks.list(agent.id)
human_block = next((b for b in blocks if b.label == "human"), None)
print(f"\nHuman block: {human_block.id}")
client.blocks.update(block_id=human_block.id, value="UNIQUE_MARKER_99: Updated directly via API.")

# Check stored message IMMEDIATELY after API update (no step)
msgs_after_api = list(client.agents.messages.list(agent_id=agent.id))
sys_msg_after_api = msgs_after_api[0].content if msgs_after_api else None
print(f"\nAfter API block update (no step):")
print("Stored message changed:", sys_msg_after_api != initial_sys_msg)
print("Contains UNIQUE_MARKER_99:", "UNIQUE_MARKER_99" in sys_msg_after_api)

# Now check adding core_memory_append tool
print("\n--- Checking core_memory_append availability ---")
all_tools = list(client.tools.list())
cma_tool = next((t for t in all_tools if t.name == "core_memory_append"), None)
if cma_tool:
    print(f"core_memory_append tool found: {cma_tool.id}")
    # Try adding it to the agent
    client.agents.tools.attach(agent_id=agent.id, tool_id=cma_tool.id)
    print("Added to agent!")
    # Verify
    agent_tools = list(client.agents.tools.list(agent_id=agent.id))
    print("Agent tools now:", [t.name for t in agent_tools])
else:
    print("core_memory_append NOT found in server tools")
    print("Available tools:", [t.name for t in all_tools if "memory" in t.name.lower()])

client.agents.delete(agent.id)
print("\nCleaned up.")
