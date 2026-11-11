import inspect
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"  # output of a Python tool function, not the model or user


@dataclass
class Message:
    role: Role
    content: str                     # assistant: one JSON object; tool: JSON result
    image_path: Optional[str] = None
    tool_name: Optional[str] = None  # tool that produced this message (Role.TOOL only)
    image: Optional[Image.Image] = None  # picture a tool returned, e.g. a close-up (Role.TOOL only; JSON can't hold it)

    def __post_init__(self):
        if self.role == Role.TOOL and not (self.tool_name or "").strip():
            raise ValueError("a tool message needs a non-empty tool_name")
        if self.role != Role.TOOL and self.tool_name is not None:
            raise ValueError("only tool messages have a tool_name")
        if self.role != Role.TOOL and self.image is not None:
            raise ValueError("only tool messages have an image")


@dataclass
class Tool:
    name: str
    description: str
    parameter_schema: Dict[str, Any]         # argument name -> expected shape (shown to the model); all required, no others allowed
    function: Callable[..., Dict[str, Any]]  # called with **arguments, plus whichever of original_image=, image_path=, output_path=, state= it takes
    validate: Optional[Callable[..., None]] = None  # validate(**arguments): raises TypeError/ValueError on bad values
    return_direct: bool = True  # True: success ends the turn. False: the model sees the result and continues.

    def __post_init__(self):
        """Reject bad tool definitions at startup, before any model call."""
        if not isinstance(self.name, str) or not isinstance(self.description, str):
            raise TypeError("tool name and description must be strings")
        if not isinstance(self.return_direct, bool):
            raise TypeError(f"tool {self.name!r}: return_direct must be True or False")
        if not isinstance(self.parameter_schema, dict):
            raise TypeError(f"tool {self.name!r}: parameter_schema must be a dict")
        if not callable(self.function):
            raise TypeError(f"tool {self.name!r}: function must be callable")
        if self.validate is not None and not callable(self.validate):
            raise TypeError(f"tool {self.name!r}: validate must be callable")
        if not self.name.strip() or not self.description.strip():
            raise ValueError("tool name and description must not be empty")
        if not all(isinstance(k, str) and k.strip() for k in self.parameter_schema):
            raise ValueError(f"tool {self.name!r}: parameter names must be non-empty strings")


# Provider = any function (system_prompt, messages, original_image) -> raw model text.
# Kept separate so the agent can be tested with a fake model and no API calls.
# The provider may send the model a smaller copy, but must never change original_image:
# boxes are drawn on the full-quality original. Exceptions it raises stop the run.
# A tool message may carry an image (Message.image); how it is sent is up to the provider.
LLM = Callable[[str, List[Message], Image.Image], str]


def load_image(path: str) -> Image.Image:
    """Open a still JPEG/PNG that passes verify(). Returns an upright (EXIF-rotated), decoded copy."""
    if not os.path.isfile(path):
        raise ValueError(f"image not found: {path}")
    with Image.open(path) as img:
        if img.format not in ("JPEG", "PNG"):
            raise ValueError(f"unsupported image type {img.format}; only JPEG and PNG are allowed")
        if getattr(img, "n_frames", 1) != 1:  # animated PNG, multi-page files
            raise ValueError(f"image has {img.n_frames} pages/frames; "
                             "multi-page and animated images are not supported")
        img.verify()  # integrity check; Pillow needs the file reopened afterwards
    with Image.open(path) as img:
        upright = ImageOps.exif_transpose(img)  # returns a fully loaded copy; boxes match what the model sees
        upright.format = img.format  # the copy forgets it; the Gemini copy uses it to pick JPEG or PNG
        return upright


# ---------- Parsing the model's reply ----------

def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"duplicate JSON keys: {duplicates}")
    return dict(pairs)


def _reject_constant(name):
    # Python's json accepts NaN, Infinity and -Infinity; standard JSON does not.
    raise ValueError(f"{name} is not valid JSON")


def _is_final(reply: dict) -> bool:
    return (reply.keys() == {"type", "message"} and reply["type"] == "final"
            and isinstance(reply["message"], str))


def _is_tool_call(reply: dict) -> bool:
    return (reply.keys() == {"type", "name", "arguments"} and reply["type"] == "tool_call"
            and isinstance(reply["name"], str) and bool(reply["name"].strip())
            and isinstance(reply["arguments"], dict))


def parse_reply(text: str) -> Dict[str, Any]:
    """Strict: the whole reply must be exactly one JSON object with exactly the expected keys.
    No searching inside prose or code fences; anything else raises ValueError."""
    try:
        # Fails on prose, markdown fences, more than one object, duplicate keys, or NaN/Infinity.
        reply = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not a single JSON object ({e})") from None
    if not isinstance(reply, dict):
        raise ValueError("reply must be a JSON object")
    if _is_final(reply) or _is_tool_call(reply):
        return reply
    raise ValueError('expected exactly {"type":"tool_call","name":"...","arguments":{...}} '
                     'or {"type":"final","message":"..."}')


# ---------- System prompt ----------
# {{ and }} are literal braces (str.format); {tools} is filled with the tool list.

PROTOCOL = """You control Python tools by replying with JSON.

Your entire reply must be exactly ONE JSON object and nothing else: no markdown, no text before or after it. Any other reply is rejected.
Do NOT wrap the JSON in code fences: never write ``` or ```json. The first character of your reply must be {{ and the last must be }}.

To call a tool:
{{"type":"tool_call","name":"<tool name>","arguments":{{...}}}}

To answer without a tool (no tool fits, the request is unclear, or the requested object is not in the image):
{{"type":"final","message":"<plain text>"}}
Example: {{"type":"final","message":"The dog was not found."}}

Tools:
{tools}

If a tool result reports an error, fix the arguments and call the tool again.
If a tool result succeeds, call another tool if one is still needed, otherwise reply with a final answer."""


# ---------- Agent ----------

class Agent:
    def __init__(self, tools: List[Tool], llm: LLM, instructions: str = "", max_attempts: int = 3,
                 max_steps: int = 6):
        self.tools: Dict[str, Tool] = {}
        for tool in tools:
            if not isinstance(tool, Tool):
                raise TypeError(f"expected a Tool, got {type(tool).__name__}")
            if tool.name in self.tools:  # never overwrite an earlier tool
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            self.tools[tool.name] = tool
        self.llm = llm
        self.max_attempts = max_attempts  # failed replies/calls the model may fix per run
        self.max_steps = max_steps        # model calls per run, successful or not (stops a looping model)
        listing = "\n".join(f"- {t.name}: {t.description}\n  arguments: {json.dumps(t.parameter_schema)}"
                            for t in self.tools.values())
        self.system_prompt = PROTOCOL.format(tools=listing) + (f"\n\n{instructions}" if instructions else "")

    def run(self, messages: List[Message], output_path: Optional[str] = None) -> Message:
        """Handle the latest user request. The model/tool trail is appended to `messages`.
        The final answer is only returned: the history already holds the model's own JSON reply,
        so appending a plain-text copy would show the model the same answer twice.
        output_path is passed to the tool (None = the tool picks a default)."""
        # 1. Find and load the newest image in the conversation (no image = no model call).
        image_path = next((m.image_path for m in reversed(messages) if m.image_path), None)
        if image_path is None:
            return Message(Role.ASSISTANT, "Please give an image, e.g. --image dog.png, "
                                           "or write the request and the image path in one line.")
        try:
            original_image = load_image(image_path)  # full quality; tools draw on this
        except Exception as e:
            return Message(Role.ASSISTANT, f"Can't use that image: {e}")

        # 2. Ask the model (whole conversation), parse, run the tool. Bad JSON or bad arguments use
        #    one of max_attempts; any other error stops (see MODEL_FIXABLE). Successful calls use no
        #    attempt, but every model call counts toward max_steps.
        failures = 0
        state: Dict[str, Any] = {}  # this request's notepad, shared by its tool calls; never sent to the model
        for _ in range(self.max_steps):
            reply = self.llm(self.system_prompt, messages, original_image)
            if not reply.strip():
                # Nothing to correct (e.g. a blocked response): retrying would only repeat it.
                return Message(Role.ASSISTANT, "Stopped [empty_response]: the model returned no text.")
            messages.append(Message(Role.ASSISTANT, reply))
            try:
                call = parse_reply(reply)
            except ValueError as e:
                messages.append(Message(Role.SYSTEM, f"Your reply was rejected: {e}. Reply with exactly one JSON object, "
                                                     "with no code fences and no other text."))
            else:
                if call["type"] == "final":
                    return Message(Role.ASSISTANT, call["message"])

                result = self.call_tool(call, original_image, image_path, output_path, state)
                image = result.pop("image", None)  # a picture can't go in JSON text: it rides on the message
                messages.append(Message(Role.TOOL, json.dumps(result), tool_name=call["name"], image=image))
                if result.get("success"):
                    if self.tools[call["name"]].return_direct:
                        # The result is the answer; no extra model call to restate it.
                        return Message(Role.ASSISTANT, result.get("message") or json.dumps(result))
                    continue  # the model sees the result next call and picks another tool or a final answer
                if result.get("error_type") not in MODEL_FIXABLE:
                    # e.g. a disk or output-path error: another model attempt can't fix it, so stop now.
                    return Message(Role.ASSISTANT, f"Stopped [{result.get('error_type')}]: {result.get('error')}")

            failures += 1
            if failures >= self.max_attempts:
                return Message(Role.ASSISTANT,
                               f"Gave up after {self.max_attempts} attempts. Last error: {messages[-1].content}")

        return Message(Role.ASSISTANT, f"Stopped [max_steps]: reached the limit of {self.max_steps} model calls "
                                       "in one request without a final answer.")

    def call_tool(self, call: Dict[str, Any], original_image: Image.Image, image_path: str,
                  output_path: Optional[str] = None, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check the model's arguments, then run the tool. The tool function runs only if every check passes.
        Never raises: problems come back as tool_error(...) for the model to fix."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return tool_error("unknown_tool", f"unknown tool {call['name']!r}; available: {list(self.tools)}")
        arguments = call["arguments"]
        # From Python, never the model. Only the ones the function takes, so non-image tools work too.
        accepted = inspect.signature(tool.function).parameters
        context = {k: v for k, v in {"original_image": original_image, "image_path": image_path,
                                     "output_path": output_path, "state": {} if state is None else state}.items()
                   if k in accepted}

        # 1. Exactly the declared arguments: none missing, nothing extra (e.g. a path or image from the model).
        missing = [p for p in tool.parameter_schema if p not in arguments]
        if missing:
            return tool_error("missing_arguments", f"missing arguments: {missing}")
        unexpected = [a for a in arguments if a not in tool.parameter_schema]
        if unexpected:
            return tool_error("unexpected_arguments",
                              f"unexpected arguments: {unexpected}; expected only {list(tool.parameter_schema)}")
        # 2. Values are valid (tool-specific checks), and 3. the call fits the function's signature.
        try:
            if tool.validate is not None:
                tool.validate(**arguments)
            inspect.signature(tool.function).bind(**context, **arguments)
        except (TypeError, ValueError) as e:
            return tool_error("invalid_arguments", f"{type(e).__name__}: {e}")

        # 4. Run it.
        try:
            return tool.function(**context, **arguments)
        except Exception as e:
            return tool_error("tool_failed", f"{type(e).__name__}: {e}")


# Tool errors the model can fix by sending a corrected call (each fix uses one model attempt).
# limit_reached: e.g. no zooms left; the model can still finish with another tool.
# Anything else (tool_failed: disk, output path, a bug in the tool) stops the run at once.
MODEL_FIXABLE = {"unknown_tool", "missing_arguments", "unexpected_arguments", "invalid_arguments", "limit_reached"}


def tool_error(error_type: str, message: str) -> Dict[str, Any]:
    """Every failed tool call has this shape. error_type is one of MODEL_FIXABLE, or tool_failed."""
    return {"success": False, "error_type": error_type, "error": message}
