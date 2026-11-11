# AI Tool Calling from Scratch

A small agent library with hand-built tool calling, plus a demo where Gemini finds objects in an image and Python boxes them, crops them out, or zooms in for a closer look first.

No SDK tool calling is used. The prompt protocol, JSON parsing, argument validation, retry logic and conversation state are all plain Python. The only third-party libraries are `google-genai` (to send text and an image to Gemini) and Pillow (to load and draw on images).

```
$ python chat.py --image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png
  [assistant] {"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[205,161,835,867]}]}}
  [draw_bounding_boxes] {"success": true, "output_path": "dog_boxed.png", "num_boxes": 1, ...}
AI: Drew 1 box(es): dog [50, 40, 269, 162]. Saved to dog_boxed.png
```

## The challenge

Build a minimal library for defining and running agents that call tools, without using any provider's built-in tool calling. Main use case: given a photo and a text request, return bounding boxes for the requested object(s) and save a copy of the photo with the boxes drawn on it. Only LLM/vision API libraries and Pillow are allowed.

## Quick start

```bash
pip install google-genai pillow
export GEMINI_API_KEY=your-key          # or GOOGLE_API_KEY
export GEMINI_MODEL=gemini-3.8-flash    # optional; this is the default
```

If you keep the key in a local `.env` file (it is gitignored), load it into the shell with `set -a; source .env; set +a`. The code itself only reads environment variables.

**One-shot (preferred):**
```bash
python chat.py --image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png
python chat.py --image environment.png --prompt "Box the cat and the dog"
python chat.py --image dog.png --prompt "Crop the dog"
```
`--output` is optional. By default results go to `outputs/<name>_boxes.png` (crops: `outputs/<name>_<label>.png`). If the file already exists, `_2`, `_3`, ... is added, so earlier results are never overwritten. The input image is never modified.

**Interactive chat:** run `python chat.py` and write one plain line with the request and the image path:
```
You: Draw a box around the dog in dog.png
You: Is there a car in environment.png?     # answers in text, draws nothing
You: now box the tree                        # follow-up reuses the last image
```
CLI flags (`--image` etc.) don't work at the `You:` prompt. The chat says so and makes no model call.

## Files

| File | What it holds |
|---|---|
| `agent.py` | The reusable library: `Role`, `Message`, `Tool`, `Agent`, strict reply parser, system prompt, image loader. Knows nothing about Gemini. |
| `chat.py` | The demo: the three tools and their validators, coordinate helpers, the Gemini provider, image preparation, error classification, CLI and chat loop. |
| `tests/` | 41 offline tests (`unittest` only, no API key, no network). |
| `session*.md` | Transcripts of the build sessions, where the decisions below were made. |

## The tools

| Tool | Argument | What it does | Ends the turn? |
|---|---|---|---|
| `draw_bounding_boxes` | `boxes` | Draws labeled boxes on a copy of the photo and saves it. The answer lists each box in pixels. | Yes |
| `crop_object` | `boxes` | Saves each box as its own image file, cut from the full-quality original. Rounds outward, so nothing of the object is trimmed. | Yes |
| `zoom_region` | `region` | Cuts an area out of the original, enlarges it (longer side 1024 px) and shows the close-up to Gemini. Saves nothing. Optional, at most 2 per request. | No: Gemini sees the close-up and continues |

`boxes` is a list of `{"label": ..., "box": [y_min, x_min, y_max, x_max]}`. `region` is one `[y_min, x_min, y_max, x_max]`. All coordinates are whole numbers from 0 to 1000, checked by the same `check_box` helper.

## How it works

```
user text + image path
        │
        ▼
Agent.run ── load_image (JPEG/PNG only, verified, EXIF-rotated)
        │    state = {}   (fresh notepad for this request)
        ▼
  ┌─► llm(system_prompt, messages, image) ──► raw text        at most max_steps (6) model calls,
  │     │                                                     at most max_attempts (3) mistakes
  │   parse_reply (strict JSON)
  │    ├─ {"type":"final","message":...}            ──► return the answer
  │    └─ {"type":"tool_call","name":...,"arguments":{...}}
  │             │
  │         call_tool: known tool? exact args? validate() passes? signature binds?
  │             ├─ no  ──► error goes back to the model ─────────────────────────────┐
  │             └─ yes ──► run the tool (with original_image, image_path,            │
  │                        output_path, state, whichever it takes)                   │
  │                          │                                                       │
  │                          ├─ return_direct=True (draw, crop) ──► the answer       │
  │                          └─ return_direct=False (zoom) ──► result + close-up      │
  │                                                            go back to the model  │
  └──────────────────────────────────────────────────────────────────────────────────┘
```

### Zooming in

For a small, far-away or crowded object, Gemini can look closer before it boxes anything:

```
"Box the trunk of the tree in environment.png"
  1. Gemini ──► zoom_region {"region":[50,430,340,730]}            (full-photo 0-1000)
     Python: cut that area out of the original, enlarge it to 1024 px,
             state["view"] = the area shown, state["zooms"] = 1
     ◄── "Zoom 1 of 2: ... coordinates must be relative to this close-up" + image 1024x...
  2. Gemini ──► draw_bounding_boxes {"boxes":[{"label":"trunk","box":[640,430,990,560]}]}
                                                                   (0-1000 of the close-up)
     Python: to_full() converts the box back to the full photo, then draws it on the original
```

- **Gemini never does the math.** After a zoom, every coordinate it sends is relative to the latest close-up. `to_full` converts it: `view top + y * (view height / 1000)`, the same for x. The values stay exact fractions until `to_pixels` rounds once at the end.
- **A second zoom is inside the first.** Its region is relative to the first close-up and goes through `to_full` too.
- **Every request starts from the full photo**, because `state` is new for each request.
- **Limits.** At most 2 zooms per request. A third returns `limit_reached`, which tells Gemini to call `draw_bounding_boxes` or `crop_object` now. Areas smaller than 32 px on either side of the original are rejected (they would only enlarge into blur).
- **Pictures travel on the message, not in the JSON.** `zoom_region` returns its close-up under `"image"`. `Agent.run` moves it onto the tool message's `image` field, and only tool messages may have one. `gemini_llm` sends the close-ups of the current request only (after the newest user message) as JPEG q85, and counts them toward the request budget. In the terminal, such a message ends with `+ image 1024x732`.

### The protocol

The model must reply with exactly one JSON object, nothing else, in one of two shapes:

```json
{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}
{"type":"final","message":"The dog was not found."}
```

`final` is how the model answers without a tool: the object isn't in the image, the request is unclear, or no tool fits.

### Messages and roles

`Message(role, content, image_path=None, tool_name=None, image=None)` with four roles:

- `USER`: the request, with an optional `image_path`.
- `ASSISTANT`: the model's raw JSON reply, stored once, as sent.
- `TOOL`: the result of a Python tool function. It must carry a `tool_name`, and may carry an `image` (e.g. a close-up). No other role may have either.
- `SYSTEM`: rejection notes sent back to the model ("your reply was rejected: ...").

Every step is recorded in the shared message list, so the trail shows which tool produced which result. The CLI prints it, and it makes debugging and testing straightforward.

### Tools

```python
Tool(name, description, parameter_schema, function, validate=None, return_direct=True)
```

- `parameter_schema` lists every argument. All are required, and no others are allowed. It is shown to the model in the system prompt.
- `validate(**arguments)` raises `TypeError`/`ValueError` on bad values. It runs before the function, so bad model data never reaches the tool.
- `function` gets the model's arguments, plus any of `original_image`, `image_path`, `output_path` and `state` that it declares. These come from Python and never from the model, and tools that don't need them simply leave them out. `state` is one dict shared by every tool call in a request and empty for the next one.
- `return_direct=True` (the default): a successful result is the answer and ends the turn. `False`: the result goes back to the model, which calls another tool or answers. Successful calls never use up `max_attempts`; `max_steps` caps the total model calls.
- Bad definitions (empty or non-string names, non-dict schema, non-callable function) and duplicate tool names fail at startup, before any API call.

## Design decisions

**Strict parsing, no rescue.** `parse_reply` runs `json.loads` on the whole reply. Prose around the JSON, code fences, several objects, duplicate keys, `NaN`/`Infinity`, and missing or extra fields are all rejected. The model gets one clear correction message instead of the parser guessing at what it meant. The system prompt also tells it explicitly not to use code fences.

**Retries are sorted by cause.**

| Problem | What happens |
|---|---|
| Malformed JSON, unknown tool, missing/extra/invalid arguments, `limit_reached` | Error goes back to the model and uses one of 3 attempts |
| More than 6 model calls in one request | Stops with `max_steps` (a looping model can't run forever) |
| Empty or blocked response | Stops at once (there is nothing to correct) |
| Tool crash, disk or output path error | Stops at once (the model can't fix it) |
| Timeout, 408, 429, 5xx | One quick retry inside the same step (doesn't use a model attempt) |
| Bad API key, unknown model, 400, 403 | Stops at once with a specific message |

**Success ends the turn, unless the tool says otherwise.** A successful draw or crop is not sent back to Gemini for a summary. The tool's own message is the answer, which saves an API call per request. `zoom_region` is the exception (`return_direct=False`): its result exists to be looked at.

**Only labels and coordinates come from the model.** File paths, output locations, the image and API keys stay in Python. In chat, the image path is replaced with "the image" before the text goes to Gemini, so local paths are never sent. Labels must be plain object names (letters, digits, spaces, `-` and `'`, at most 40 characters), so `black_cat`, `dog.png` and `a/b` are rejected.

**Box format.** Each box is `[y_min, x_min, y_max, x_max]` as whole numbers from 0 to 1000, normalized to the image's height and width. This is Gemini's native detection format. The validator rejects floats, booleans, null, strings, values out of range and inverted boxes. When drawing, coordinates are converted to pixels and clamped so that 1000 maps to the last pixel (`w-1`/`h-1`).

**Three kinds of image, kept apart.**
- *Original*: the user's file. It is never changed.
- *Gemini copy*: exists in memory only. It is compressed or shrunk only if the request would be too big.
- *Boxed result / crops*: new files, made from the full-quality original.
- *Close-up*: `zoom_region`'s enlarged cut-out of the original. In memory only, sent with its tool result.

Because boxes are normalized and resizing keeps the aspect ratio, a smaller copy gives the same boxes.

**Request size budget.** Gemini's inline request limit is 100 MB, and the code keeps each request under 90 MB. The size estimate includes base64 growth (every 3 bytes become 4), the prompt text, and some overhead. The copy is tried in this order: full-size PNG (only for PNG inputs up to 50 MB raw, so screenshots stay lossless), then full-size JPEG (q85), then JPEG shrunk to 75% of its width and height per step. If the long side would go below 768 px, it stops with an `image_too_large` error before sending anything. The copy is never cropped, stretched or rotated. CMYK, 16-bit, palette and transparent images are converted to plain RGB, with transparency flattened onto white.

**Clear, specific errors.** API failures are sorted into `invalid_api_key`, `model_not_found`, `rate_limited`, `timeout`, `network_error`, `server_error`, `image_too_large`, `empty_response` and so on, each with what to do next. Requests time out after 30 s.

**Input checks before any model call.** The image must exist and be a still JPEG or PNG. `--output` must end in `.png`/`.jpg`/`.jpeg`, its folder must exist, and it can't be the input file. In chat, a message naming two images, or an image that doesn't exist, gets an answer without calling Gemini.

**Full conversation is sent.** Gemini sees the whole history so follow-ups work. The image is attached once, to the newest message that named it. Close-ups are attached only during the request that made them.

**Provider is a plain function.** `LLM = (system_prompt, messages, image) -> str`. The agent can run against a scripted fake model, and that is how the tests work. Swapping in another provider means writing one function.

## Tests

```bash
python -m unittest discover -s tests -v
```

| File | Covers |
|---|---|
| `test_agent_unit.py` (15) | Message/tool validation (tool images), system prompt, strict parsing, `call_tool` errors, `Agent.run` stop/retry rules, `return_direct`, `max_steps`, per-request `state`, `limit_reached`, image loading |
| `test_chat_unit.py` (22) | Box/label/region validation, drawing, cropping (never trims the object), zooming (pixels, 1024 px, proportions, size floor, 2-zoom limit, close-up coordinates), image budget and resizing, close-ups sent to Gemini, color-mode conversion, output paths, chat path parsing, CLI flags, API error classification, the tool prompt |
| `test_integration.py` (4) | Real agent and tools with a scripted fake Gemini: valid call, retry after bad coordinates, crop, zoom then draw lands on the dog in the full photo |

Related cases are grouped with `subTest` tables. Outputs go to temp folders, and nothing touches the network.
