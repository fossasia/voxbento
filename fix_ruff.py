# Fix W293 in bot.py
with open("floor-bot/bot.py", "r") as f:
    lines = f.readlines()
with open("floor-bot/bot.py", "w") as f:
    for line in lines:
        f.write(line.rstrip(" \t\n") + "\n")

# Fix W293 in test_transcription_concurrency.py
with open("tests/test_transcription_concurrency.py", "r") as f:
    lines = f.readlines()
with open("tests/test_transcription_concurrency.py", "w") as f:
    for line in lines:
        f.write(line.rstrip(" \t\n") + "\n")

# Fix E701 in base.py
with open("portal/transcription/providers/base.py", "r") as f:
    content = f.read()

content = content.replace("try: queue.put_nowait(chunk)", "try:\n                            queue.put_nowait(chunk)")
content = content.replace(
    "except asyncio.QueueFull: pass", "except asyncio.QueueFull:\n                            pass"
)

content = content.replace("try: queue.get_nowait()", "try:\n                            queue.get_nowait()")
content = content.replace(
    "except asyncio.QueueEmpty: break", "except asyncio.QueueEmpty:\n                            break"
)

with open("portal/transcription/providers/base.py", "w") as f:
    f.write(content)

# Fix F841 in test_database_e2e.py
with open("tests/test_database_e2e.py", "r") as f:
    content = f.read()
content = content.replace("pycon_ws_de = await create_booth(", "await create_booth(")
content = content.replace("tok_listener = await create_invite_token(", "await create_invite_token(")
with open("tests/test_database_e2e.py", "w") as f:
    f.write(content)

# Fix F841 in test_fastapi_app.py
with open("tests/test_fastapi_app.py", "r") as f:
    content = f.read()
content = content.replace("pid_a = ws_join(ws_a", "ws_join(ws_a")
with open("tests/test_fastapi_app.py", "w") as f:
    f.write(content)

# Fix F841 in test_join_flow.py
with open("tests/test_join_flow.py", "r") as f:
    lines = f.readlines()
with open("tests/test_join_flow.py", "w") as f:
    for line in lines:
        if "before = utc_now()" not in line:
            f.write(line)

# Fix F841 in test_memberships_tokens.py
with open("tests/test_memberships_tokens.py", "r") as f:
    content = f.read()
content = content.replace("user = await _create_test_user(", "await _create_test_user(")
with open("tests/test_memberships_tokens.py", "w") as f:
    f.write(content)

print("Applied manual fixes.")
