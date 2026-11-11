import argparse
import io
import os
import re
import shlex
import sys
from fractions import Fraction
from functools import cache
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

from agent import Agent, Message, Role, Tool, tool_error

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
            name, so earlier results are never overwritten. The original image is never changed.
            Crops ("Crop the dog") are saved as outputs/<name>_<label>.png, or numbered from --output."""

BANNER = f"""Bounding box agent ({MODEL})

In your normal terminal (not at the You: prompt below):
{USAGE}

You are now in interactive chat. At the You: prompt, write ONE plain line with what to box
+ the image path (.png, .jpg or .jpeg). Flags like --image do not work here.
  Examples: Draw a box around the dog in dog.png
            Box the cat and the dog in environment.png
            Crop the dog out of dog.png          (saves the dog as its own image)
            Is there a car in environment.png?   (no box if it isn't there)
Type 'exit' to quit.
"""
CLI_FLAGS = ("--image", "--prompt", "--output")


def uses_cli_flags(text: str) -> bool:
    """True if the line contains --image, --prompt or --output (also as --output=x.png)."""
    return any(word.split("=")[0] in CLI_FLAGS for word in text.split())

BOX_FORMAT = """draw_bounding_boxes and crop_object take the same "boxes" argument; zoom_region's "region" uses the same [y_min, x_min, y_max, x_max] format.

Bounding box format (strict):
- "box" is [y_min, x_min, y_max, x_max]: y first, then x.
- Integers from 0 to 1000, normalized to the image: y relative to height, x relative to width. (0, 0) is the top-left corner. Never pixel values.
- y_min < y_max and x_min < x_max.
- Include only the objects the user names; never anything else. If the user does not say which object, reply with type "final" asking which object.
- Box tightly around the whole visible object. One box per object; if the user asks for several objects or "all" of something, box each one.
- "label" is only the object's name, e.g. "dog" or "black cat": letters, digits, spaces, - and ' only, at most 40 characters. Never put a file name, path, extension or the user's sentence in a label.
- "boxes" always holds at least one box. If the requested object is not in the image, reply with type "final" and say so instead.
- Only send labels and coordinates. Never send file paths or anything else.

Choosing a tool:
- draw_bounding_boxes: the user asks to box, draw, mark or highlight objects. Saves one copy of the image with labeled boxes.
  Example: {"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}
- crop_object: the user asks to crop, cut out, extract or save just the object(s). Saves each box as its own image file.
  Example: {"type":"tool_call","name":"crop_object","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}
- zoom_region is optional. Use it first only when it really helps: the object is very small or hard to see, or the scene is very cluttered, AND every object the user asks for fits inside one area. It cuts that area out of the full-quality photo, enlarges it, and shows you the close-up. It saves nothing.
  Example: {"type":"tool_call","name":"zoom_region","arguments":{"region":[550,600,900,950]}}
- You can zoom at most 2 times per request.
- After a zoom, every coordinate you send (boxes, or the region of a second zoom) is 0-1000 relative to the latest close-up, not the full photo. Python converts it back to the full photo; never do that math yourself.
- After a zoom you can only box things inside the close-up, so only zoom when every object the user asks for fits in that area.
- One tool per request: finish by calling exactly one of draw_bounding_boxes or crop_object. If the request fits neither tool, reply with type "final" and say what you can do."""


# ---------- Tool ----------

# A plain name: letters/digits ([^\W_] is \w without "_"), inner spaces, - and '. No _, slashes, dots or other symbols.
LABEL_PATTERN = re.compile(r"[^\W_](?:(?:[^\W_]|[ '-]){0,38}[^\W_])?")

def check_box(box, where: str) -> None:
    """[y_min, x_min, y_max, x_max]: exactly 4 whole numbers from 0 to 1000, y_min < y_max and x_min < x_max.
    Shared by validate_boxes and validate_region. `where` names the value in the error, e.g. "boxes[0].box"."""
    if not isinstance(box, list):
        raise TypeError(f"{where} must be a list [y_min, x_min, y_max, x_max], got {type(box).__name__}")
    if len(box) != 4:
        raise ValueError(f"{where} must have exactly 4 values [y_min, x_min, y_max, x_max], got {len(box)}")
    not_whole = [v for v in box if type(v) is not int]  # rejects decimals, true/false, null, strings
    if not_whole:
        raise TypeError(f"{where} values must be whole numbers, got {not_whole}")
    if not all(0 <= v <= 1000 for v in box):
        raise ValueError(f"{where} values must be between 0 and 1000, got {box}")
    y_min, x_min, y_max, x_max = box
    if not (y_min < y_max and x_min < x_max):
        raise ValueError(f"{where} needs y_min < y_max and x_min < x_max, got {box}")


def validate_boxes(boxes) -> None:
    """Checked by the agent before draw_bounding_boxes or crop_object runs. Messages go back to the model to fix its next try."""
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
        check_box(box, f"{where}.box")


def validate_region(region) -> None:
    """Checked by the agent before zoom_region runs: the same coordinate checks as every box."""
    check_box(region, "region")


def next_free_path(path: str) -> str:
    """path if it is free, else <stem>_2<ext>, <stem>_3<ext>, ... so earlier results are never overwritten."""
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{stem}_{n}{ext}"
    return path


def default_output_path(image_path: str, suffix: str = "boxes") -> str:
    """outputs/<name>_<suffix>.png for the given input image."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(image_path))[0]}_{suffix}.png")


FULL_VIEW = (0, 0, 1000, 1000)  # no zoom yet: the model sees the whole photo


def to_full(box: list, state: Optional[Dict[str, Any]]) -> List[Fraction]:
    """A [y_min, x_min, y_max, x_max] box measured inside the current view (0-1000 of the latest close-up)
    -> the same box in full-photo 0-1000 coordinates: view top + y * (view height / 1000), and the same for x.
    Exact fractions, not rounded: to_pixels (or crop's floor/ceil) rounds once at the end, so nothing is lost."""
    top, left, bottom, right = (state or {}).get("view", FULL_VIEW)
    y_scale, x_scale = Fraction(bottom - top, 1000), Fraction(right - left, 1000)
    y0, x0, y1, x1 = box
    return [top + y0 * y_scale, left + x0 * x_scale, top + y1 * y_scale, left + x1 * x_scale]


def to_pixels(box: list, w: int, h: int) -> List[int]:
    """[y_min, x_min, y_max, x_max] in 0-1000 -> [x_min, y_min, x_max, y_max] in pixels (1000 maps to w or h)."""
    y0, x0, y1, x1 = box
    return [round(x0 * w / 1000), round(y0 * h / 1000), round(x1 * w / 1000), round(y1 * h / 1000)]


def save_image(image: Image.Image, path: str) -> None:
    """Save as PNG or JPEG by the path's extension. JPEG has no alpha channel, so it gets an RGB copy."""
    fmt = OUTPUT_FORMATS[os.path.splitext(path)[1].lower()]
    (image.convert("RGB") if fmt == "JPEG" else image).save(path, format=fmt)


def check_output(output_path: str, image_path: str) -> None:
    """Checked before any model call, so a bad --output doesn't waste one."""
    if os.path.splitext(output_path)[1].lower() not in OUTPUT_FORMATS:
        raise ValueError(f"--output must end in .png, .jpg or .jpeg: {output_path}")
    folder = os.path.dirname(output_path) or "."
    if not os.path.isdir(folder):
        raise ValueError(f"--output folder does not exist: {folder}")
    if os.path.realpath(output_path) == os.path.realpath(image_path):
        raise ValueError("--output must not be the input image (it would be overwritten)")


def draw_bounding_boxes(original_image: Image.Image, image_path: str, output_path: Optional[str], boxes: list,
                        state: Optional[Dict[str, Any]] = None) -> dict:
    """Draw already-validated boxes (see validate_boxes) on a copy of the full-quality original.
    Saves to output_path (or outputs/<name>_boxes.png if None), numbered if that file already exists."""
    boxes = [{**b, "box": to_full(b["box"], state)} for b in boxes]  # close-up coordinates -> full photo
    out = original_image.convert("RGBA")  # convert() returns a new image; the original is untouched
    w, h = out.size
    draw = ImageDraw.Draw(out)
    line = max(2, round(min(w, h) / 150))
    font = ImageFont.load_default(size=max(12, min(w, h) // 25))
    drawn = []
    for b in boxes:
        label = b["label"]
        # Clamped to real pixels: 1000 maps to w (or h), but the last pixel is w-1 (or h-1).
        px = [min(v, limit - 1) for v, limit in zip(to_pixels(b["box"], w, h), (w, h, w, h))]
        draw.rectangle(px, outline="red", width=line)
        draw.rectangle(draw.textbbox((px[0], px[1]), label, font=font), fill="red")
        draw.text((px[0], px[1]), label, fill="white", font=font)
        drawn.append({"label": label, "box_px": px})

    out_path = next_free_path(output_path or default_output_path(image_path))
    save_image(out, out_path)
    boxes_px = "; ".join(f"{d['label']} {d['box_px']}" for d in drawn)  # pixels [x_min, y_min, x_max, y_max]
    return {"success": True, "output_path": out_path, "num_boxes": len(drawn), "boxes": drawn,
            "message": f"Drew {len(drawn)} box(es): {boxes_px}. Saved to {out_path}"}


def crop_object(original_image: Image.Image, image_path: str, output_path: Optional[str], boxes: list,
                state: Optional[Dict[str, Any]] = None) -> dict:
    """Save each already-validated box (see validate_boxes) as its own image, cut from the full-quality original.
    Saves to outputs/<name>_<label>.png (or output_path if given), numbered if that file already exists,
    so duplicate labels and repeat runs give new files."""
    boxes = [{**b, "box": to_full(b["box"], state)} for b in boxes]  # close-up coordinates -> full photo
    w, h = original_image.size
    saved = []
    for b in boxes:
        y0, x0, y1, x1 = b["box"]
        # Round outward (floor the start, ceil the end; -(-a // b) is ceil), so the crop covers the whole
        # requested region and never trims part of the object. x_max/y_max are exclusive; always at least 1x1.
        px = [x0 * w // 1000, y0 * h // 1000, -(-x1 * w // 1000), -(-y1 * h // 1000)]
        out_path = next_free_path(output_path or default_output_path(image_path, b["label"].replace(" ", "_")))
        save_image(original_image.crop(px).convert("RGBA"), out_path)  # crop() returns a new image
        saved.append({"label": b["label"], "box_px": px, "output_path": out_path})
    files = "; ".join(f"{s['label']} {s['box_px']} -> {s['output_path']}" for s in saved)
    return {"success": True, "num_crops": len(saved), "crops": saved,
            "message": f"Cropped {len(saved)} object(s): {files}"}


ZOOM_LIMIT = 2         # zooms per request
ZOOM_LONG_SIDE = 1024  # the close-up's longer side, in pixels
MIN_ZOOM_SIDE = 32     # smaller areas would only enlarge into blur


def zoom_region(original_image: Image.Image, state: Dict[str, Any], region: list) -> dict:
    """Cut an already-validated region (see validate_region) out of the full-quality original and enlarge it,
    so the model can look closer. The region is relative to the current view, so a second zoom lands inside
    the first. Records the shown area in state["view"] (full-photo 0-1000) and the count in state["zooms"].
    The close-up goes back under "image"; the agent moves it onto the tool message. Saves nothing."""
    zooms = state.get("zooms", 0)
    if zooms >= ZOOM_LIMIT:
        return tool_error("limit_reached", f"Limit reached for zooming in ({ZOOM_LIMIT} zooms per request). "
                                           "Call draw_bounding_boxes or crop_object now, with coordinates "
                                           "relative to the latest close-up.")
    top, left, bottom, right = to_full(region, state)
    w, h = original_image.size
    # Round outward like crop_object, so the whole requested area is shown.
    px = [left * w // 1000, top * h // 1000, -(-right * w // 1000), -(-bottom * h // 1000)]
    pw, ph = px[2] - px[0], px[3] - px[1]
    if pw < MIN_ZOOM_SIDE or ph < MIN_ZOOM_SIDE:
        return tool_error("invalid_arguments", f"region is only {pw}x{ph} pixels of the original photo; each side "
                                               f"must be at least {MIN_ZOOM_SIDE} pixels. Choose a larger region.")
    scale = ZOOM_LONG_SIDE / max(pw, ph)  # longer side becomes 1024, proportions kept
    size = (max(1, round(pw * scale)), max(1, round(ph * scale)))
    closeup = safe_rgb(original_image.crop(px)).resize(size, Image.Resampling.LANCZOS)  # crop() is a new image

    # The view is exactly the area shown (the outward-rounded pixels), so close-up coordinates map back exactly.
    state["view"] = [Fraction(px[1] * 1000, h), Fraction(px[0] * 1000, w),
                     Fraction(px[3] * 1000, h), Fraction(px[2] * 1000, w)]
    state["zooms"] = zooms + 1
    area = [round(float(v), 1) for v in state["view"]]
    return {"success": True, "zoom": zooms + 1, "max_zooms": ZOOM_LIMIT, "area": area, "region_px": px,
            "closeup_size": list(size), "image": closeup,
            "message": f"Zoom {zooms + 1} of {ZOOM_LIMIT}: the attached close-up shows the area {area} of the full "
                       f"photo ([y_min, x_min, y_max, x_max], 0-1000), which is pixels {px} of the original, "
                       f"enlarged to {size[0]}x{size[1]} pixels. Every coordinate you send next (boxes, or "
                       f"another zoom region) must be 0-1000 relative to this close-up, not the full photo."}


BOXES_SCHEMA = {"boxes": {
        "type": "list, at least 1 item",
        "items": {
            "label": "string: object name only, e.g. \"dog\"; letters, digits, spaces, - and ' only, max 40 characters, no file names or paths",
            "box": "list of exactly 4 whole numbers [y_min, x_min, y_max, x_max], each 0-1000, "
                   "y_min < y_max, x_min < x_max",
        },
        "rules": "each item has only \"label\" and \"box\"; no decimals, true/false, null or extra fields",
    }}

tools = [
    Tool(name="draw_bounding_boxes",  # must stay first: tests patch tools[0]
         description="Draw labeled bounding boxes on the user's image and save a new copy.",
         parameter_schema=BOXES_SCHEMA, function=draw_bounding_boxes, validate=validate_boxes),
    Tool(name="crop_object",
         description="Crop each box out of the user's image and save every crop as its own file.",
         parameter_schema=BOXES_SCHEMA, function=crop_object, validate=validate_boxes),
    Tool(name="zoom_region",  # never ends the turn: the model sees the close-up and continues
         description="Cut an area out of the full-quality photo and enlarge it, to look closer at small "
                     "or crowded objects. Optional, at most 2 per request. Saves nothing.",
         parameter_schema={"region": "list of exactly 4 whole numbers [y_min, x_min, y_max, x_max], each 0-1000, "
                                     "y_min < y_max, x_min < x_max; relative to what you see now "
                                     "(the full photo, or the latest close-up)"},
         function=zoom_region, validate=validate_region, return_direct=False),
]


# ---------- Gemini copy of the image ----------
# Three kinds of image, kept apart:
#   original image  the user's file on disk; never changed or overwritten
#   Gemini copy     in memory only, never saved; compressed or shrunk only if the request would be too big
#   boxed result    a new file in outputs/ (or --output), drawn on the full-quality original
#   close-up        zoom_region's enlarged cut-out of the original; in memory only, sent with its tool result
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
    # Tool images (close-ups) from the current request only: after the newest user message. Older ones are not resent.
    last_user = max((i for i, m in enumerate(messages) if m.role == Role.USER), default=-1)
    closeups = {i: encode(safe_rgb(m.image), "JPEG") for i, m in enumerate(messages)
                if i > last_user and m.image is not None}
    text_bytes = (len(system_prompt.encode()) + sum(len(t.encode()) for t in texts)
                  + sum(base64_size(len(c)) for c in closeups.values()))  # close-ups count toward the budget too
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
        if i in closeups:
            parts.append(types.Part.from_bytes(data=closeups[i], mime_type="image/jpeg"))
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
        picture = f" + image {m.image.width}x{m.image.height}" if m.image is not None else ""  # a close-up went to the model
        print(f"  [{m.tool_name or m.role.value}] {m.content}{picture}")
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
