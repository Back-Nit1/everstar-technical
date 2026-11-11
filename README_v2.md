# AI Tool Calling from Scratch

A small agent library with hand-built tool calling, plus a demo that draws bounding boxes around objects in an image using Gemini.

No SDK tool calling is used. The prompt protocol, JSON parsing, argument validation, retry logic and conversation state are all plain Python. The only third-party libraries are `google-genai` (to send text and an image to Gemini) and Pillow (to load and draw on images).

```
$ python chat.py --image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png
  [assistant] {"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[205,161,835,867]}]}}
  [draw_bounding_boxes] {"success": true, "output_path": "dog_boxed.png", "num_boxes": 1, ...}
AI: Drew 1 box(es) (dog). Saved to dog_boxed.png
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
```
`--output` is optional. By default results go to `outputs/<name>_boxes.png`. If the file already exists, `_2`, `_3`, ... is added, so earlier results are never overwritten. The input image is never modified.

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
| `chat.py` | The demo: the `draw_bounding_boxes` tool and its validator, the Gemini provider, image preparation, error classification, CLI and chat loop. |
| `tests/` | 47 offline tests (`unittest` only, no API key, no network). |
| `session*.md` | Transcripts of the build sessions, where the decisions below were made. |

## How it works

```
user text + image path
        │
        ▼
Agent.run ── load_image (JPEG/PNG only, verified, EXIF-rotated)
        │
        ▼  up to max_attempts (3)
  llm(system_prompt, messages, image) ──► raw text
        │
  parse_reply (strict JSON)
   ├─ {"type":"final","message":...}            ──► return the answer
   └─ {"type":"tool_call","name":...,"arguments":{...}}
            │
        call_tool: known tool? exact args? validate() passes? signature binds?
            ├─ yes ──► run the tool ──► success ends the turn (no extra model call)
            └─ no  ──► error goes back into the conversation, model tries again
```

### The protocol

The model must reply with exactly one JSON object, nothing else, in one of two shapes:

```json
{"type":"tool_call","name":"draw_bounding_boxes","arguments":{"boxes":[{"label":"dog","box":[630,680,815,895]}]}}
{"type":"final","message":"The dog was not found."}
```

`final` is how the model answers without a tool: the object isn't in the image, the request is unclear, or no tool fits.

### Messages and roles

`Message(role, content, image_path=None, tool_name=None)` with four roles:

- `USER`: the request, with an optional `image_path`.
- `ASSISTANT`: the model's raw JSON reply, stored once, as sent.
- `TOOL`: the result of a Python tool function. It must carry a `tool_name`, and no other role may have one.
- `SYSTEM`: rejection notes sent back to the model ("your reply was rejected: ...").

Every step is recorded in the shared message list, so the trail shows which tool produced which result. The CLI prints it, and it makes debugging and testing straightforward.

### Tools

```python
Tool(name, description, parameter_schema, function, validate=None)
```

- `parameter_schema` lists every argument. All are required, and no others are allowed. It is shown to the model in the system prompt.
- `validate(**arguments)` raises `TypeError`/`ValueError` on bad values. It runs before the function, so bad model data never reaches the tool.
- `function` gets the model's arguments, plus any of `original_image`, `image_path` and `output_path` that it declares. These come from Python and never from the model, and tools that don't need images simply leave them out.
- Bad definitions (empty or non-string names, non-dict schema, non-callable function) and duplicate tool names fail at startup, before any API call.

## Design decisions

**Strict parsing, no rescue.** `parse_reply` runs `json.loads` on the whole reply. Prose around the JSON, code fences, several objects, duplicate keys, `NaN`/`Infinity`, and missing or extra fields are all rejected. The model gets one clear correction message instead of the parser guessing at what it meant. The system prompt also tells it explicitly not to use code fences.

**Retries are sorted by cause.**

| Problem | What happens |
|---|---|
| Malformed JSON, unknown tool, missing/extra/invalid arguments | Error goes back to the model and uses one of 3 attempts |
| Empty or blocked response | Stops at once (there is nothing to correct) |
| Tool crash, disk or output path error | Stops at once (the model can't fix it) |
| Timeout, 408, 429, 5xx | One quick retry inside the same step (doesn't use a model attempt) |
| Bad API key, unknown model, 400, 403 | Stops at once with a specific message |

**Success ends the turn.** A successful tool result is not sent back to Gemini for a summary. The tool's own message is the answer, which saves an API call per request.

**Only labels and coordinates come from the model.** File paths, output locations, the image and API keys stay in Python. In chat, the image path is replaced with "the image" before the text goes to Gemini, so local paths are never sent. Labels must be plain object names (letters, digits, spaces, `-` and `'`, at most 40 characters), so `black_cat`, `dog.png` and `a/b` are rejected.

**Box format.** Each box is `[y_min, x_min, y_max, x_max]` as whole numbers from 0 to 1000, normalized to the image's height and width. This is Gemini's native detection format. The validator rejects floats, booleans, null, strings, values out of range and inverted boxes. When drawing, coordinates are converted to pixels and clamped so that 1000 maps to the last pixel (`w-1`/`h-1`).

**Three kinds of image, kept apart.**
- *Original*: the user's file. It is never changed.
- *Gemini copy*: exists in memory only. It is compressed or shrunk only if the request would be too big.
- *Boxed result*: a new file, drawn on the full-quality original.

Because boxes are normalized and resizing keeps the aspect ratio, a smaller copy gives the same boxes.

**Request size budget.** Gemini's inline request limit is 100 MB, and the code keeps each request under 90 MB. The size estimate includes base64 growth (every 3 bytes become 4), the prompt text, and some overhead. The copy is tried in this order: full-size PNG (only for PNG inputs up to 50 MB raw, so screenshots stay lossless), then full-size JPEG (q85), then JPEG shrunk to 75% of its width and height per step. If the long side would go below 768 px, it stops with an `image_too_large` error before sending anything. The copy is never cropped, stretched or rotated. CMYK, 16-bit, palette and transparent images are converted to plain RGB, with transparency flattened onto white.

**Clear, specific errors.** API failures are sorted into `invalid_api_key`, `model_not_found`, `rate_limited`, `timeout`, `network_error`, `server_error`, `image_too_large`, `empty_response` and so on, each with what to do next. Requests time out after 30 s.

**Input checks before any model call.** The image must exist and be a still JPEG or PNG. `--output` must end in `.png`/`.jpg`/`.jpeg`, its folder must exist, and it can't be the input file. In chat, a message naming two images, or an image that doesn't exist, gets an answer without calling Gemini.

**Full conversation is sent.** Gemini sees the whole history so follow-ups work. The image is attached once, to the newest message that named it.

**Provider is a plain function.** `LLM = (system_prompt, messages, image) -> str`. The agent can run against a scripted fake model, and that is how the tests work. Swapping in another provider means writing one function.

## Tests

```bash
python -m unittest discover -s tests -v
```

| File | Covers |
|---|---|
| `test_agent_unit.py` (17) | Message/tool validation, system prompt, strict parsing, `call_tool` errors, `Agent.run` stop/retry rules, image loading |
| `test_chat_unit.py` (26) | Box and label validation, image budget and resizing, color-mode conversion, coordinate clamping, output paths, chat path parsing, CLI flag detection, API error classification |
| `test_integration.py` (4) | Real agent and drawing code with a scripted fake Gemini: valid call, box lands on the right pixels, malformed JSON then valid, invalid coordinates then valid |

Related cases are grouped with `subTest` tables. Outputs go to temp folders, and nothing touches the network.
