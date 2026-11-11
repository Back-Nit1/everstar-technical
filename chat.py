import argparse
import io
import os
import re
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
TRANSPORT_ATTEMPTS = 2  # first try + 1 retry for timeouts, rate limits and server errors
OUTPUT_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")  # interactive chat only picks these
OUTPUT_DIR = "outputs"  # default home for boxed results, apart from the original images

USAGE = """Usage (flags are the preferred way):
  python chat.py --image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png
  python chat.py --image environment.png --prompt "Box the cat and the dog"
  python chat.py      (interactive chat)

  --image   input image, JPEG or PNG
  --prompt  what to box
  --output  where to save the boxed image (.png, .jpg or .jpeg). Optional: by default it is saved
            as outputs/<name>_boxes.png. If the file already exists, _2, _3, ... is added to the
            name, so earlier results are never overwritten. The original image is never changed."""

BANNER = f"""Bounding box agent ({MODEL})

In your normal terminal (not at the You: prompt below):
{USAGE}

You are now in interactive chat. At the You: prompt, write ONE plain line with what to box
+ the image path (.png, .jpg or .jpeg). Flags like --image do not work here.
  Examples: Draw a box around the dog in dog.png
            Box the cat and the dog in environment.png
            Is there a car in environment.png?   (no box if it isn't there)
Type 'exit' to quit.
"""
CLI_FLAGS = ("--image", "--prompt", "--output")


def uses_cli_flags(text: str) -> bool:
    """True if the line contains --image, --prompt or --output (also as --output=x.png)."""
    return any(word.split("=")[0] in CLI_FLAGS for word in text.split())

BOX_FORMAT = """Bounding box format (strict):
- "box" is [y_min, x_min, y_max, x_max]: y first, then x.
- Integers from 0 to 1000, normalized to the image: y relative to height, x relative to width. (0, 0) is the top-left corner. Never pixel values.
- y_min < y_max and x_min < x_max.
- Box only the objects the user names; never box anything else. If the user does not say which object to box, reply with type "final" asking which object.
- Box tightly around the whole visible object. One box per object; if the user asks for several objects or "all" of something, box each one.
- "label" is only the object's name, e.g. "dog" or "black cat": letters, digits, spaces, - and ' only, at most 40 characters. Never put a file name, path, extension or the user's sentence in a label.
- "boxes" always holds at least one box. If the requested object is not in the image, reply with type "final" and say so instead.
- Only send labels and coordinates. Never send file paths or anything else.

Example:
{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}"""


# ---------- Tool ----------

# A plain name: letters/digits ([^\W_] is \w without "_"), inner spaces, - and '. No _, slashes, dots or other symbols.
LABEL_PATTERN = re.compile(r"[^\W_](?:(?:[^\W_]|[ '-]){0,38}[^\W_])?")

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
        if not LABEL_PATTERN.fullmatch(label):  # blocks paths/file names like "environment.png" or "a/b"
            raise ValueError(f"{where}.label must be only the object's name (letters, digits, spaces, - and ' only, at most 40 characters, no file names or paths), got {label!r}")
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


def next_free_path(path: str) -> str:
    """path if it is free, else <stem>_2<ext>, <stem>_3<ext>, ... so earlier results are never overwritten."""
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{stem}_{n}{ext}"
    return path


def default_output_path(image_path: str) -> str:
    """outputs/<name>_boxes.png for the given input image."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, os.path.splitext(os.path.basename(image_path))[0] + "_boxes.png")


def check_output(output_path: str, image_path: str) -> None:
    """Checked before any model call, so a bad --output doesn't waste one."""
    if os.path.splitext(output_path)[1].lower() not in OUTPUT_FORMATS:
        raise ValueError(f"--output must end in .png, .jpg or .jpeg: {output_path}")
    folder = os.path.dirname(output_path) or "."
    if not os.path.isdir(folder):
        raise ValueError(f"--output folder does not exist: {folder}")
    if os.path.realpath(output_path) == os.path.realpath(image_path):
        raise ValueError("--output must not be the input image (it would be overwritten)")


def draw_bounding_boxes(original_image: Image.Image, image_path: str, output_path: Optional[str], boxes: list) -> dict:
    """Draw already-validated boxes (see validate_boxes) on a copy of the full-quality original.
    Saves to output_path (or outputs/<name>_boxes.png if None), numbered if that file already exists."""
    out = original_image.convert("RGBA")  # convert() returns a new image; the original is untouched
    w, h = out.size
    draw = ImageDraw.Draw(out)
    line = max(2, round(min(w, h) / 150))
    font = ImageFont.load_default(size=max(12, min(w, h) // 25))
    drawn = []
    for b in boxes:
        label, (y0, x0, y1, x1) = b["label"], b["box"]
        # 0-1000 -> pixels, clamped to real pixels: 1000 maps to w (or h), but the last pixel is w-1 (or h-1).
        px = [min(round(x0 * w / 1000), w - 1), min(round(y0 * h / 1000), h - 1),
              min(round(x1 * w / 1000), w - 1), min(round(y1 * h / 1000), h - 1)]  # [x_min, y_min, x_max, y_max]
        draw.rectangle(px, outline="red", width=line)
        draw.rectangle(draw.textbbox((px[0], px[1]), label, font=font), fill="red")
        draw.text((px[0], px[1]), label, fill="white", font=font)
        drawn.append({"label": label, "box_px": px})

    out_path = next_free_path(output_path or default_output_path(image_path))
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
            "label": "string: object name only, e.g. \"dog\"; letters, digits, spaces, - and ' only, max 40 characters, no file names or paths",
            "box": "list of exactly 4 whole numbers [y_min, x_min, y_max, x_max], each 0-1000, "
                   "y_min < y_max, x_min < x_max",
        },
        "rules": "each item has only \"label\" and \"box\"; no decimals, true/false, null or extra fields",
    }},
    function=draw_bounding_boxes,
    validate=validate_boxes,
)]


# ---------- Gemini copy of the image ----------
# Three kinds of image, kept apart:
#   original image  the user's file on disk; never changed or overwritten
#   Gemini copy     in memory only, never saved; compressed or shrunk only if the request would be too big
#   boxed result    a new file in outputs/ (or --output), drawn on the full-quality original
# Boxes are 0-1000 relative to width and height, so a proportionally shrunk copy gives the same boxes.

MB = 1024 * 1024
GEMINI_INLINE_LIMIT = 100 * MB  # Gemini API's hard limit for a request with inline data (raised from 20 MB in Jan 2026)
REQUEST_BUDGET = 90 * MB      # our local safety limit for the whole request: 10 MB of room below Gemini's limit
REQUEST_OVERHEAD = 64 * 1024  # JSON field names, roles and other request metadata
JPEG_QUALITY = 85
SHRINK_STEP = 0.75            # each resize keeps 75% of width AND height, so proportions stay the same
MIN_LONG_SIDE = 768           # never shrink the long side below this: stop instead
PNG_RAW_LIMIT = 50 * MB       # above this many raw RGB bytes, skip the full-size PNG try (slow and memory-heavy)


class ImageTooLargeError(ValueError):
    """The image can't fit the request budget without shrinking it too far. Raised before any request."""


class EmptyResponseError(RuntimeError):
    """Gemini answered with no text (e.g. blocked). Stops the run: there is no JSON to correct."""


def base64_size(n_bytes: int) -> int:
    return 4 * ((n_bytes + 2) // 3)  # inline images travel as base64: every 3 bytes become 4 characters


def safe_rgb(img: Image.Image) -> Image.Image:
    """Plain 8-bit RGB that both PNG and JPEG can save (CMYK, 16-bit, palette and grayscale are converted;
    transparency is flattened onto white). An RGB image is returned as is, with no extra copy."""
    if img.mode == "RGB":
        return img
    if img.mode.startswith("I"):  # 16-bit grayscale: scale 0-65535 down to 0-255 (a plain convert clips to white)
        return img.convert("I").point(lambda v: v / 256).convert("RGB")
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return img.convert("RGB")


def encode(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"quality": JPEG_QUALITY} if fmt == "JPEG" else {}))
    return buf.getvalue()


def prepare_gemini_copy(original_image: Image.Image, text_bytes: int):
    """Image bytes for Gemini, sized so the whole request (base64 image + text + overhead) fits REQUEST_BUDGET.
    The copy is plain RGB (safe_rgb). Then it tries:
      1. PNG at full size (PNG inputs up to PNG_RAW_LIMIT only: lossless for screenshots and diagrams)
      2. JPEG at full size (JPEG and huge PNG inputs start here)
      3. JPEG, shrunk proportionally step by step
    Never crops, stretches or rotates, and never changes original_image. Returns (bytes, mime_type, (w, h))."""
    budget = REQUEST_BUDGET - REQUEST_OVERHEAD - text_bytes
    safe = safe_rgb(original_image)
    w, h = safe.size
    if original_image.format != "JPEG" and w * h * 3 <= PNG_RAW_LIMIT:
        data = encode(safe, "PNG")
        if base64_size(len(data)) <= budget:
            return data, "image/png", safe.size
    copy = safe
    while True:
        data = encode(copy, "JPEG")
        if base64_size(len(data)) <= budget:
            return data, "image/jpeg", copy.size
        w, h = copy.size
        smaller = (round(w * SHRINK_STEP), round(h * SHRINK_STEP))
        if max(smaller) < MIN_LONG_SIDE:
            raise ImageTooLargeError(
                f"Image is too large: even as a {w}x{h} JPEG the request would be about "
                f"{(base64_size(len(data)) + text_bytes + REQUEST_OVERHEAD) / MB:.1f} MB "
                f"(our safety limit is {REQUEST_BUDGET / MB:.0f} MB; Gemini's API limit is "
                f"{GEMINI_INLINE_LIMIT / MB:.0f} MB). Use a smaller or simpler image.")
        copy = safe.resize(smaller, Image.Resampling.LANCZOS)  # always from the full-size copy: one resample


# ---------- Gemini provider ----------

@cache
def client() -> genai.Client:
    # Reads GEMINI_API_KEY / GOOGLE_API_KEY.
    # Transport retry: timeouts, network drops, 408, 429 and 5xx get ONE quick retry inside the same model step,
    # so it never uses up a model attempt. Bad key, unknown model, 400 and 403 are not retried: they stop the run.
    return genai.Client(http_options=types.HttpOptions(
        timeout=TIMEOUT_SECONDS * 1000,  # milliseconds, per try
        retry_options=types.HttpRetryOptions(attempts=TRANSPORT_ATTEMPTS, initial_delay=1, max_delay=4)))


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
    if isinstance(e, ImageTooLargeError):  # local, before any request
        return {"error_type": "image_too_large", "error": str(e)}
    if isinstance(e, EmptyResponseError):
        return {"error_type": "empty_response", "error": str(e)}
    if "Timeout" in name:  # httpx/httpx2 ConnectTimeout, ReadTimeout, ...
        return {"error_type": "timeout", "error": f"Gemini did not answer within {TIMEOUT_SECONDS} seconds ({TRANSPORT_ATTEMPTS} tries). Try again."}
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


def gemini_llm(system_prompt: str, messages: List[Message], original_image: Image.Image) -> str:
    """Plain text in, plain text out. No SDK tool calling: the protocol is our own JSON."""
    # Tool results and rejection notes are sent as user turns, labeled so the model can tell them apart.
    texts = [f"[{m.tool_name or m.role.value} result] {m.content}" if m.role in (Role.TOOL, Role.SYSTEM) else m.content
             for m in messages]
    text_bytes = len(system_prompt.encode()) + sum(len(t.encode()) for t in texts)
    data, mime_type, size = prepare_gemini_copy(original_image, text_bytes)  # raises before any request if too big
    sent_as = "JPEG" if mime_type == "image/jpeg" else "PNG"
    if size != original_image.size or sent_as != original_image.format:  # only mention a changed copy
        w, h = size
        print(f"  [image] sent Gemini a {w}x{h} {sent_as} copy "
              f"({len(data) / MB:.1f} MB); boxes are drawn on the original")
    image_part = types.Part.from_bytes(data=data, mime_type=mime_type)
    # The image goes once, on the newest message that named it (older images are not resent).
    image_at = max(i for i, m in enumerate(messages) if m.image_path)

    contents = []
    for i, (m, text) in enumerate(zip(messages, texts)):
        parts = [image_part] if i == image_at else []
        parts.append(types.Part.from_text(text=text))
        contents.append(types.Content(role="model" if m.role == Role.ASSISTANT else "user", parts=parts))

    response = client().models.generate_content(
        model=MODEL, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt))
    if not (response.text or "").strip():
        feedback, candidates = response.prompt_feedback, response.candidates
        if feedback and feedback.block_reason:
            why = f"the request was blocked ({feedback.block_reason})"
        elif candidates and candidates[0].finish_reason:
            why = f"the answer stopped with reason {candidates[0].finish_reason}"
        else:
            why = "no reason was given"
        raise EmptyResponseError(f"Gemini returned no text: {why}. Try rephrasing the request or another image.")
    return response.text


agent = Agent(tools=tools, llm=gemini_llm, instructions=BOX_FORMAT)


# ---------- Chat loop ----------

def split_image_paths(text: str):
    """Every token naming a .png/.jpg/.jpeg file (whether or not it exists), and the text for Gemini
    with each one replaced by "the image", so local paths never reach it. Text without a path is unchanged.
    The caller checks the file exists, so a mistyped name is reported instead of reusing an earlier image."""
    try:
        tokens = shlex.split(text)  # handles quoted / backslash-escaped paths from drag-and-drop
    except ValueError:              # e.g. an unmatched quote
        tokens = text.split()
    paths, words = [], []
    for t in tokens:
        bare = t.rstrip("?!.,;:")  # "dog.png?" -> "dog.png"
        if bare.lower().endswith(IMAGE_EXTENSIONS):
            paths.append(os.path.expanduser(bare))
            words.append("the image" + t[len(bare):])
        else:
            words.append(t)
    return paths, (" ".join(words) if paths else text)


def handle(messages: List[Message], prompt: str, image_path: Optional[str], output_path: Optional[str] = None):
    """One user turn: record it, run the agent, print the trail and the answer."""
    messages.append(Message(Role.USER, prompt, image_path=image_path))
    before = len(messages)
    try:
        answer = agent.run(messages, output_path=output_path).content
    except Exception as e:
        error = explain_api_error(e)
        answer = f"Stopped [{error['error_type']}]: {error['error']}"
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
        if uses_cli_flags(user_input):  # else --output x.png would count as a second image; no model call
            print("AI: --image, --prompt and --output are command-line flags. Exit and run them from your "
                  "normal terminal, e.g. python chat.py --image dog.png --prompt \"Draw a box around the dog\". "
                  "Here, just write: Draw a box around the dog in dog.png")
            continue
        paths, text = split_image_paths(user_input)
        if len(set(paths)) > 1:  # no model call
            print(f"AI: Please name only one image per message (found {len(set(paths))}).")
            continue
        image_path = paths[0] if paths else None
        if image_path and not os.path.isfile(image_path):  # named an image that isn't there: no model call
            print(f"AI: Image not found: {image_path}")
            continue
        handle(messages, text, image_path)  # no image named: reuses the last one (follow-ups)


if __name__ == "__main__":
    main()
