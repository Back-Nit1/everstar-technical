"""Offline check: a scripted fake model replaces Gemini, so no API key or calls are needed."""
import os

from PIL import Image

from agent import Agent, Message, Role, Tool, load_image, parse_reply
from chat import BOX_FORMAT, tools, validate_boxes


def fake_llm(replies):
    calls = []
    def llm(system_prompt, messages, image):
        calls.append([m.role for m in messages])
        return replies[len(calls) - 1]
    return llm, calls


CALL = '{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[150,100,950,900]}]}}'

# 0. Strict parsing: only a bare JSON object with exactly the expected keys is accepted.
assert parse_reply(CALL)["name"] == "draw_bounding_boxes"
assert parse_reply('{"type":"final","message":"The dog was not found."}')["type"] == "final"
for bad in ["Here you go: " + CALL, "```json\n" + CALL + "\n```", CALL + CALL, "[1]",
            '{"type":"final","message":"x","extra":1}', '{"type":"tool_call","name":"","arguments":{}}',
            '{"type":"final","message":"a","message":"b"}']:
    try:
        parse_reply(bad)
        raise AssertionError(f"should reject: {bad}")
    except ValueError:
        pass

# 0b. Bad tool definitions, duplicate tool names, and bad tool messages are rejected.
for make in [lambda: Tool("", "d", {}, print), lambda: Tool("  ", "d", {}, print),
             lambda: Tool(None, "d", {}, print), lambda: Tool("t", "", {}, print),
             lambda: Tool("t", "d", ["boxes"], print), lambda: Tool("t", "d", {"": "x"}, print),
             lambda: Tool("t", "d", {}, "not callable"), lambda: Agent(tools=tools * 2, llm=None),
             lambda: Agent(tools=["draw_bounding_boxes"], llm=None),
             lambda: Message(Role.TOOL, "{}"), lambda: Message(Role.USER, "hi", tool_name="x")]:
    try:
        make()
        raise AssertionError("should raise")
    except (TypeError, ValueError):
        pass

# 1. Prose-wrapped JSON (rejected), then x/y-swapped out-of-range box, then a valid call: succeeds on attempt 3.
llm, calls = fake_llm([
    "Sure! Here is the box: " + CALL,
    '{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[700,0,1200,100]}]}}',
    CALL,
])
agent = Agent(tools=tools, llm=llm, instructions=BOX_FORMAT)
messages = [Message(Role.USER, "box the dog", image_path="dog.png")]
final = agent.run(messages)
assert len(calls) == 3, calls
assert calls[2] == [Role.USER, Role.ASSISTANT, Role.SYSTEM, Role.ASSISTANT, Role.TOOL], calls[2]
assert messages[-1].role == Role.TOOL and messages[-1].tool_name == "draw_bounding_boxes"
saved = final.content.split("Saved to ")[-1]
assert saved.startswith(os.path.join("outputs", "dog_boxes")) and os.path.isfile(saved), final.content
os.remove(saved)  # don't leave test output in outputs/

# 2. Gives up after max_attempts.
llm, calls = fake_llm(['{"type":"tool_call","name":"nope","arguments":{}}'] * 3)
final = Agent(tools=tools, llm=llm).run([Message(Role.USER, "box it", image_path="dog.png")])
assert len(calls) == 3 and final.content.startswith("Gave up"), final.content

# 3. Final answer passes straight through; no image means no model call at all.
llm, calls = fake_llm(['{"type":"final","message":"No cat in this image."}'])
assert Agent(tools=tools, llm=llm).run([Message(Role.USER, "box the cat", image_path="dog.png")]).content == "No cat in this image."
assert "image" in Agent(tools=tools, llm=llm).run([Message(Role.USER, "box the dog")]).content and len(calls) == 1

# 4. Image validation: missing file, wrong type, animation.
Image.new("RGB", (4, 4)).save("_t.gif")
Image.new("RGB", (4, 4)).save("_t_anim.png", save_all=True, append_images=[Image.new("RGB", (4, 4), "red")])
for bad in ["missing.png", "_t.gif", "_t_anim.png"]:
    try:
        load_image(bad)
        raise AssertionError(f"{bad} should be rejected")
    except ValueError:
        pass
os.remove("_t.gif"), os.remove("_t_anim.png")
# 5. Labels are plain object names: paths and file names are rejected.
for ok in ["dog", "black cat", "golden retriever", "St Bernard's dog", "T-rex", "chien"]:
    validate_boxes([{"label": ok, "box": [1, 1, 2, 2]}])
for bad in ["the tree in environment.png", "environment.png", "/Users/x/dog", "a\\b", "dog.", " dog", "x" * 41, "dog!"]:
    try:
        validate_boxes([{"label": bad, "box": [1, 1, 2, 2]}])
        raise AssertionError(f"label {bad!r} should be rejected")
    except ValueError:
        pass
print("all checks passed")
