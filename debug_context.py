"""Debug script: what does messages.list return and what does /context give us?"""
from letta_client import Letta

client = Letta(base_url="http://localhost:8284")

agent = client.agents.create(
    name="debug-context-test3",
    include_base_tools=True,
    model="anthropic/claude-haiku-4-5",
    embedding="letta/letta-free",
)
print(f"Agent: {agent.id}")

# Check messages list
print("\n--- messages.list ---")
msgs_page = client.agents.messages.list(agent_id=agent.id)
for i, m in enumerate(msgs_page):
    role = getattr(m, "role", "?")
    content = str(getattr(m, "content", ""))
    print(f"  [{i}] type={type(m).__name__} role={role} content={content[:120]}")

# Check context endpoint
print("\n--- /context endpoint ---")
ctx = client.get(f"/v1/agents/{agent.id}/context", cast_to=object)
print(f"Keys: {list(ctx.keys())}")
print(f"system_prompt (first 300): {str(ctx.get('system_prompt', ''))[:300]}")
print(f"core_memory (first 300): {str(ctx.get('core_memory', ''))[:300]}")

# Check agent_state for message_ids
print("\n--- agent state message_ids ---")
agent_state = client.agents.retrieve(agent.id)
print(f"message_ids count: {len(getattr(agent_state, 'message_ids', []))}")
msg_ids = getattr(agent_state, "message_ids", [])
if msg_ids:
    print(f"  message_ids[0]: {msg_ids[0]}")

client.agents.delete(agent.id)
print("\nCleaned up.")
