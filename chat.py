import io
import os
import shlex
import sys
from functools import cache
from typing import List

from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

from agent import Agent, Message, Role, Tool

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

BANNER = f"""Bounding box agent ({MODEL})
Write ONE line: what to box + the image path (JPEG or PNG).
  Format:   Draw a box around the <object> in <image path>
  Examples: Draw a box around the dog in dog.png
            Draw a box around the tree in environment.png
            Box the cat and the dog in environment.png
            Is there a car in environment.png?   (no box if it isn't there)
Output is saved next to the image as <name>_boxes.png. Type 'exit' to quit.
"""

BOX_FORMAT = """Bounding box format (strict):
- "box" is [y_min, x_min, y_max, x_max]: y first, then x.
- Integers from 0 to 1000, normalized to the image: y relative to height, x relative to width. (0, 0) is the top-left corner. Never pixel values.
- y_min < y_max and x_min < x_max.
- Box only the objects the user names; never box anything else. If the user does not say which object to box, reply with type "final" asking which object.
- Box tightly around the whole visible object. One box per object; if the user asks for several objects or "all" of something, box each one.
- "label" is a short name for the object, e.g. "dog".
- "boxes" always holds at least one box. If the requested object is not in the image, reply with type "final" and say so instead.
- Only send labels and coordinates. Never send file paths or anything else.

Example:
{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}"""


def draw_bounding_boxes(image: Image.Image, image_path: str, boxes: list) -> dict:
    if not isinstance(boxes, list) or not boxes:
        raise ValueError("boxes must be a non-empty list")
    out = image.convert("RGBA")
    w, h = out.size
    draw = ImageDraw.Draw(out)
    line = max(2, round(min(w, h) / 150))
    font = ImageFont.load_default(size=max(12, min(w, h) // 25))
    drawn = []
    for b in boxes:
        label, box = (b.get("label"), b.get("box")) if isinstance(b, dict) else (None, None)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"each box needs a non-empty 'label': {b}")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            raise ValueError(f"'box' must be 4 numbers [y_min, x_min, y_max, x_max]: {b}")
        y0, x0, y1, x1 = box
        if not (0 <= y0 < y1 <= 1000 and 0 <= x0 < x1 <= 1000):
            raise ValueError(f"box {box} for {label!r} must be [y_min, x_min, y_max, x_max], 0-1000, min < max")
        px = [round(x0 * w / 1000), round(y0 * h / 1000), round(x1 * w / 1000), round(y1 * h / 1000)]
        draw.rectangle(px, outline="red", width=line)
        draw.rectangle(draw.textbbox((px[0], px[1]), label, font=font), fill="red")
        draw.text((px[0], px[1]), label, fill="white", font=font)
        drawn.append({"label": label, "box_px": px})  # [x_min, y_min, x_max, y_max] in pixels

    out_path = os.path.splitext(image_path)[0] + "_boxes.png"
    out.save(out_path)
    labels = ", ".join(d["label"] for d in drawn)
    return {"success": True, "output_path": out_path, "num_boxes": len(drawn), "boxes": drawn,
            "message": f"Drew {len(drawn)} box(es) ({labels}). Saved to {out_path}"}


@cache
def client() -> genai.Client:
    return genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY


def gemini_llm(system_prompt: str, messages: List[Message], image: Image.Image) -> str:
    """Plain text in, plain text out. No SDK tool calling: the protocol is our own JSON."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    # Attach the image once, to the newest message that named it (older images are not resent).
    image_at = max(i for i, m in enumerate(messages) if m.image_path)
    contents = []
    for i, m in enumerate(messages):
        text = m.content
        if m.role in (Role.TOOL, Role.SYSTEM):
            text = f"[{m.tool_name or m.role.value} result] {m.content}"
        parts = [types.Part.from_text(text=text)]
        if i == image_at:
            parts.insert(0, types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
        contents.append(types.Content(role="model" if m.role == Role.ASSISTANT else "user", parts=parts))
    response = client().models.generate_content(
        model=MODEL, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt))
    return response.text or ""


tools = [Tool(
    name="draw_bounding_boxes",
    description="Draw labeled bounding boxes on the user's image and save a new copy.",
    parameter_schema={"boxes": 'list of {"label": str, "box": [y_min, x_min, y_max, x_max]}, coordinates 0-1000'},
    function=draw_bounding_boxes,
)]

agent = Agent(tools=tools, llm=gemini_llm, instructions=BOX_FORMAT)


def find_image_path(text: str):
    """First token in the input that is an existing file (validated later by load_image)."""
    try:
        tokens = shlex.split(text)  # handles quoted / backslash-escaped paths from drag-and-drop
    except ValueError:
        tokens = text.split()
    paths = (os.path.expanduser(t.rstrip("?!.,;:")) for t in tokens)  # "dog.png?" -> "dog.png"
    return next((p for p in paths if os.path.isfile(p)), None)


def handle(messages: List[Message], user_input: str):
    messages.append(Message(Role.USER, user_input, image_path=find_image_path(user_input)))
    before = len(messages)
    try:
        response = agent.run(messages=messages)
    except Exception as e:  # API/network errors
        response = Message(Role.ASSISTANT, f"Model call failed: {e}")
    for m in messages[before:]:  # show the trail: model replies, tool results, retries
        print(f"  [{m.tool_name or m.role.value}] {m.content}")
    messages.append(response)
    print(f"AI: {response.content}")


def chat():
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("GEMINI_API_KEY is not set. Run: export GEMINI_API_KEY=your-key")
    messages = []
    if sys.argv[1:]:  # one-shot: python chat.py "box the dog in dog.png"
        return handle(messages, " ".join(sys.argv[1:]))
    print(BANNER)
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            break
        handle(messages, user_input)


if __name__ == "__main__":
    chat()
