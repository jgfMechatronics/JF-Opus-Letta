"""Debug: what memory tools does the test agent actually have?"""
from letta_client import Letta
from letta_client.types import CreateBlockParam

client = Letta(base_url="http://localhost:8284")

agent = client.agents.create(
    name="debug-tools-test",
    include_base_tools=True,
    model="anthropic/claude-haiku-4-5",
    embedding="letta/letta-free",
    memory_blocks=[
        CreateBlockParam(label="human", value="The human's name is Test User."),
        CreateBlockParam(label="persona", value="I am a test agent."),
    ],
)
print(f"Agent: {agent.id}")

# List tools
tools = client.agents.tools.list(agent_id=agent.id)
print("\n--- Agent tools ---")
for t in tools:
    name = getattr(t, "name", "?")
    desc = str(getattr(t, "description", ""))[:80]
    print(f"  {name}: {desc}")

# Also get the FULL system message content from messages.list
msgs = list(client.agents.messages.list(agent_id=agent.id))
if msgs:
    print(f"\n--- messages.list[0] (SystemMessage) ---")
    print(f"type: {type(msgs[0]).__name__}")
    content = getattr(msgs[0], "content", "")
    print(f"Full content ({len(content)} chars):")
    print(content[:800])

client.agents.delete(agent.id)
print("\nCleaned up.")
