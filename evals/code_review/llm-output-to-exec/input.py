def run_agent_step(client, user_prompt):
    reply = client.messages.create(model="m", messages=[{"role": "user", "content": user_prompt}])
    return exec(reply.content[0].text)
