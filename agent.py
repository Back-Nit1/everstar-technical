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

    def __post_init__(self):
        if self.role == Role.TOOL and not (self.tool_name or "").strip():
            raise ValueError("a tool message needs a non-empty tool_name")
        if self.role != Role.TOOL and self.tool_name is not None:
            raise ValueError("only tool messages have a tool_name")


@dataclass
class Tool:
    name: str
    description: str
    parameter_schema: Dict[str, Any]         # argument name -> description; all are required
    function: Callable[..., Dict[str, Any]]  # called as function(image=, image_path=, **arguments)

    def __post_init__(self):
        """Reject bad tool definitions at startup, before any model call."""
        if not isinstance(self.name, str) or not isinstance(self.description, str):
            raise TypeError("tool name and description must be strings")
        if not isinstance(self.parameter_schema, dict):
            raise TypeError(f"tool {self.name!r}: parameter_schema must be a dict")
        if not callable(self.function):
            raise TypeError(f"tool {self.name!r}: function must be callable")
        if not self.name.strip() or not self.description.strip():
            raise ValueError("tool name and description must not be empty")
        if not all(isinstance(k, str) and k.strip() for k in self.parameter_schema):
            raise ValueError(f"tool {self.name!r}: parameter names must be non-empty strings")


# Provider = any function (system_prompt, messages, image) -> raw model text.
# Kept separate so the agent can be tested with a fake model and no API calls.
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
        return ImageOps.exif_transpose(img)  # returns a fully loaded copy; boxes match what the model sees


# ---------- Parsing the model's reply ----------

def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"duplicate JSON keys: {duplicates}")
    return dict(pairs)


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
        # Fails on prose, markdown fences, more than one object, or duplicate keys.
        reply = json.loads(text, object_pairs_hook=_no_duplicate_keys)
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

If a tool result reports an error, fix the arguments and call the tool again."""


# ---------- Agent ----------

class Agent:
    def __init__(self, tools: List[Tool], llm: LLM, instructions: str = "", max_attempts: int = 3):
        self.tools: Dict[str, Tool] = {}
        for tool in tools:
            if not isinstance(tool, Tool):
                raise TypeError(f"expected a Tool, got {type(tool).__name__}")
            if tool.name in self.tools:  # never overwrite an earlier tool
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            self.tools[tool.name] = tool
        self.llm = llm
        self.max_attempts = max_attempts
        listing = "\n".join(f"- {t.name}: {t.description}\n  arguments: {json.dumps(t.parameter_schema)}"
                            for t in self.tools.values())
        self.system_prompt = PROTOCOL.format(tools=listing) + (f"\n\n{instructions}" if instructions else "")

    def run(self, messages: List[Message]) -> Message:
        """Handle the latest user request. The model/tool trail is appended to `messages`;
        the final answer is returned (not appended)."""
        # 1. Find and load the newest image in the conversation (no image = no model call).
        image_path = next((m.image_path for m in reversed(messages) if m.image_path), None)
        if image_path is None:
            return Message(Role.ASSISTANT, "Please write your request and the image path in one line, "
                                           "e.g. Draw a box around the dog in dog.png")
        try:
            image = load_image(image_path)
        except Exception as e:
            return Message(Role.ASSISTANT, f"Can't use that image: {e}")

        # 2. Ask the model (whole conversation), parse, run the tool; retry on any error.
        for _ in range(self.max_attempts):
            reply = self.llm(self.system_prompt, messages, image)
            messages.append(Message(Role.ASSISTANT, reply))
            try:
                call = parse_reply(reply)
            except ValueError as e:
                messages.append(Message(Role.SYSTEM, f"Your reply was rejected: {e}. Reply with exactly one JSON object, "
                                                     "with no code fences and no other text."))
                continue
            if call["type"] == "final":
                return Message(Role.ASSISTANT, call["message"])

            result = self.call_tool(call, image, image_path)
            messages.append(Message(Role.TOOL, json.dumps(result), tool_name=call["name"]))
            if result.get("success"):
                # Success ends the turn; the result is not sent back to the model (saves an API call).
                return Message(Role.ASSISTANT, result.get("message") or json.dumps(result))
            # ponytail: every tool error is retried, even ones the model can't fix; add a non-retryable flag if that wastes calls

        return Message(Role.ASSISTANT,
                       f"Gave up after {self.max_attempts} attempts. Last error: {messages[-1].content}")

    def call_tool(self, call: Dict[str, Any], image: Image.Image, image_path: str) -> Dict[str, Any]:
        """Run a tool. Never raises: problems come back as {"success": False, "error": ...}."""
        tool = self.tools.get(call["name"])
        if tool is None:
            return {"success": False, "error": f"unknown tool {call['name']!r}; available: {list(self.tools)}"}
        missing = [p for p in tool.parameter_schema if p not in call["arguments"]]
        if missing:
            return {"success": False, "error": f"missing arguments: {missing}"}
        try:
            # image/image_path come from Python; the model only supplies the arguments.
            return tool.function(image=image, image_path=image_path, **call["arguments"])
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
