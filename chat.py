import argparse
import io
import os
import shlex
import sys
from functools import cache
from typing import Dict, List, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

from agent import Agent, Message, Role, Tool

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
TIMEOUT_SECONDS = 30
OUTPUT_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}

USAGE = """Usage:
  python chat.py --image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png
  python chat.py --image environment.png --prompt "Box the cat and the dog"
  python chat.py      (interactive chat)

  --image   input image, JPEG or PNG
  --prompt  what to box
  --output  where to save the boxed image (.png, .jpg or .jpeg). Optional: by default it is saved
            next to the image as <name>_boxes.png, then <name>_boxes_2.png, ... so earlier
            results are never overwritten."""

BANNER = f"""Bounding box agent ({MODEL})
{USAGE}

Interactive chat: write ONE line with what to box + the image path.
  Examples: Draw a box around the dog in dog.png
            Box the cat and the dog in environment.png
            Is there a car in environment.png?   (no box if it isn't there)
Type 'exit' to quit.
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


# ---------- Tool ----------

def validate_boxes(boxes) -> None:
    """Checked by the agent before draw_bounding_boxes runs. Messages go back to the model to fix its next try."""
    if not isinstance(boxes, list):
        raise TypeError(f"'boxes' must be a list, got {type(boxes).__name__}")
    if not boxes:
        raise ValueError("'boxes' must contain at least one box")
    for i, b in enumerate(boxes):
        where = f"boxes[{i}]"
        if not isinstance(b, dict):
            raise TypeError(f"{where} must be an object with 'label' and 'box', got {type(b).__name__}")
        if b.keys() != {"label", "box"}:
            raise ValueError(f"{where} must have exactly the keys 'label' and 'box', got {sorted(b)}")
        label, box = b["label"], b["box"]
        if not isinstance(label, str):
            raise TypeError(f"{where}.label must be a string, got {type(label).__name__}")
        if not label.strip():
            raise ValueError(f"{where}.label must not be empty")
        if not isinstance(box, list):
            raise TypeError(f"{where}.box must be a list [y_min, x_min, y_max, x_max], got {type(box).__name__}")
        if len(box) != 4:
            raise ValueError(f"{where}.box must have exactly 4 values [y_min, x_min, y_max, x_max], got {len(box)}")
        not_whole = [v for v in box if type(v) is not int]  # rejects decimals, true/false, null, strings
        if not_whole:
            raise TypeError(f"{where}.box values must be whole numbers, got {not_whole}")
        if not all(0 <= v <= 1000 for v in box):
            raise ValueError(f"{where}.box values must be between 0 and 1000, got {box}")
        y_min, x_min, y_max, x_max = box
        if not (y_min < y_max and x_min < x_max):
            raise ValueError(f"{where}.box needs y_min < y_max and x_min < x_max, got {box}")


def next_free_path(image_path: str) -> str:
    """<name>_boxes.png, then <name>_boxes_2.png, _3, ... so earlier results are never overwritten."""
    stem = os.path.splitext(image_path)[0] + "_boxes"
    path, n = stem + ".png", 1
    while os.path.exists(path):
        n += 1
        path = f"{stem}_{n}.png"
    return path


def check_output(output_path: str, image_path: str) -> None:
    """Checked before any model call, so a bad --output doesn't waste one."""
    if os.path.splitext(output_path)[1].lower() not in OUTPUT_FORMATS:
        raise ValueError(f"--output must end in .png, .jpg or .jpeg: {output_path}")
    folder = os.path.dirname(output_path) or "."
    if not os.path.isdir(folder):
        raise ValueError(f"--output folder does not exist: {folder}")
    if os.path.realpath(output_path) == os.path.realpath(image_path):
        raise ValueError("--output must not be the input image (it would be overwritten)")


def draw_bounding_boxes(image: Image.Image, image_path: str, output_path: Optional[str], boxes: list) -> dict:
    """Draw already-validated boxes (see validate_boxes). Saves to output_path, or next_free_path() if None."""
    out = image.convert("RGBA")
    w, h = out.size
    draw = ImageDraw.Draw(out)
    line = max(2, round(min(w, h) / 150))
    font = ImageFont.load_default(size=max(12, min(w, h) // 25))
    drawn = []
    for b in boxes:
        label, (y0, x0, y1, x1) = b["label"], b["box"]
        px = [round(x0 * w / 1000), round(y0 * h / 1000), round(x1 * w / 1000), round(y1 * h / 1000)]  # [x_min, y_min, x_max, y_max]
        draw.rectangle(px, outline="red", width=line)
        draw.rectangle(draw.textbbox((px[0], px[1]), label, font=font), fill="red")
        draw.text((px[0], px[1]), label, fill="white", font=font)
        drawn.append({"label": label, "box_px": px})

    out_path = output_path or next_free_path(image_path)
    fmt = OUTPUT_FORMATS[os.path.splitext(out_path)[1].lower()]
    (out.convert("RGB") if fmt == "JPEG" else out).save(out_path, format=fmt)  # JPEG has no alpha channel
    labels = ", ".join(d["label"] for d in drawn)
    return {"success": True, "output_path": out_path, "num_boxes": len(drawn), "boxes": drawn,
            "message": f"Drew {len(drawn)} box(es) ({labels}). Saved to {out_path}"}


tools = [Tool(
    name="draw_bounding_boxes",
    description="Draw labeled bounding boxes on the user's image and save a new copy.",
    parameter_schema={"boxes": {
        "type": "list, at least 1 item",
        "items": {
            "label": "string: non-empty object name, e.g. \"dog\"",
            "box": "list of exactly 4 whole numbers [y_min, x_min, y_max, x_max], each 0-1000, "
                   "y_min < y_max, x_min < x_max",
        },
        "rules": "each item has only \"label\" and \"box\"; no decimals, true/false, null or extra fields",
    }},
    function=draw_bounding_boxes,
    validate=validate_boxes,
)]


# ---------- Gemini provider ----------

@cache
def client() -> genai.Client:
    # Reads GEMINI_API_KEY / GOOGLE_API_KEY. The SDK does not retry by default, so this is the full wait.
    return genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000))  # milliseconds


# HTTP status -> (error_type, what the user should do)
API_ERRORS = {
    400: ("bad_request", "Gemini rejected the request."),
    401: ("invalid_api_key", "Check GEMINI_API_KEY."),
    403: ("permission_denied", "This API key is not allowed to use this model."),
    404: ("model_not_found", f"Model {MODEL!r} was not found. Set GEMINI_MODEL to an available model."),
    429: ("rate_limited", "Too many requests or quota used up. Wait a bit and try again."),
}


def explain_api_error(e: Exception) -> Dict[str, str]:
    """Sort a failed model call into a category the user can act on: {"error_type", "error"}."""
    name = type(e).__name__
    if "Timeout" in name:  # httpx/httpx2 ConnectTimeout, ReadTimeout, ...
        return {"error_type": "timeout", "error": f"Gemini did not answer within {TIMEOUT_SECONDS} seconds. Try again."}
    if name == "ConnectError" or isinstance(e, ConnectionError):
        return {"error_type": "network_error", "error": f"Could not reach Gemini. Check the internet connection. ({e})"}
    if isinstance(e, genai_errors.APIError):
        detail = f"{e.code} {e.status}: {e.message}"
        if "API_KEY_INVALID" in str(e.details):  # Gemini reports a bad key as 400, not 401
            error_type, advice = API_ERRORS[401]
        elif e.code in API_ERRORS:
            error_type, advice = API_ERRORS[e.code]
        elif e.code >= 500:
            error_type, advice = "server_error", "Gemini had a server problem. Try again later."
        else:
            error_type, advice = "api_error", "Gemini returned an error."
        return {"error_type": error_type, "error": f"{advice} ({detail})"}
    return {"error_type": "unexpected_error", "error": f"{name}: {e}"}


def gemini_llm(system_prompt: str, messages: List[Message], image: Image.Image) -> str:
    """Plain text in, plain text out. No SDK tool calling: the protocol is our own JSON."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    # ponytail: full-size PNG sent inline; very large photos can pass Gemini's ~20MB request limit, downscale if that happens
    image_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")
    # The image goes once, on the newest message that named it (older images are not resent).
    image_at = max(i for i, m in enumerate(messages) if m.image_path)

    contents = []
    for i, m in enumerate(messages):
        # Tool results and rejection notes are sent as user turns, labeled so the model can tell them apart.
        text = f"[{m.tool_name or m.role.value} result] {m.content}" if m.role in (Role.TOOL, Role.SYSTEM) else m.content
        parts = [image_part] if i == image_at else []
        parts.append(types.Part.from_text(text=text))
        contents.append(types.Content(role="model" if m.role == Role.ASSISTANT else "user", parts=parts))

    response = client().models.generate_content(
        model=MODEL, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt))
    return response.text or ""


agent = Agent(tools=tools, llm=gemini_llm, instructions=BOX_FORMAT)


# ---------- Chat loop ----------

def find_image_path(text: str):
    """First token in the input that is an existing file (validated later by load_image)."""
    try:
        tokens = shlex.split(text)  # handles quoted / backslash-escaped paths from drag-and-drop
    except ValueError:              # e.g. an unmatched quote
        tokens = text.split()
    paths = (os.path.expanduser(t.rstrip("?!.,;:")) for t in tokens)  # "dog.png?" -> "dog.png"
    # ponytail: picks the first existing file, so a word that matches a file name (e.g. "agent.py") wins; prefer image extensions if that bites
    return next((p for p in paths if os.path.isfile(p)), None)


def handle(messages: List[Message], prompt: str, image_path: Optional[str], output_path: Optional[str] = None):
    """One user turn: record it, run the agent, print the trail and the answer."""
    messages.append(Message(Role.USER, prompt, image_path=image_path))
    before = len(messages)
    try:
        answer = agent.run(messages, output_path=output_path).content
    except Exception as e:
        error = explain_api_error(e)
        answer = f"Model call failed [{error['error_type']}]: {error['error']}"
    for m in messages[before:]:  # model replies, tool results, retries
        print(f"  [{m.tool_name or m.role.value}] {m.content}")
    # Not added to `messages`: the history already holds the model's JSON reply (see Agent.run).
    print(f"AI: {answer}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Draw bounding boxes around objects in an image with Gemini.",
                                     epilog=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", help="input image, JPEG or PNG")
    parser.add_argument("--prompt", help="what to box")
    parser.add_argument("--output", help="where to save the boxed image (.png, .jpg or .jpeg)")
    args = parser.parse_args()
    if bool(args.image) != bool(args.prompt):
        parser.error("--image and --prompt must be given together")
    if args.output and not args.image:
        parser.error("--output needs --image and --prompt")
    if args.output:
        try:
            check_output(args.output, args.image)
        except ValueError as e:
            parser.error(str(e))
    return args


def main():
    args = parse_args()  # before the key check, so --help works without a key
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("GEMINI_API_KEY is not set. Run: export GEMINI_API_KEY=your-key")
    if args.image:  # one request from the flags
        return handle([], args.prompt, args.image, args.output)

    print(BANNER)
    messages = []
    while True:
        try:
            user_input = input("You: ")
        except EOFError:  # Ctrl-D
            break
        if user_input.lower() == "exit":
            break
        handle(messages, user_input, find_image_path(user_input))


if __name__ == "__main__":
    main()
