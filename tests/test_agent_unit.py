"""Offline unit tests for agent.py. No Gemini, no API key, no network.
Image files are made in a temporary folder that is deleted after each test.

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent, Message, Role, Tool, load_image, parse_reply  # noqa: E402

FINAL = '{"type":"final","message":"No cat in this image."}'
CALL = '{"type":"tool_call","name":"echo","arguments":{"text":"hi"}}'


def make_tool(name="echo", function=None, validate=None):
    return Tool(name=name, description=f"{name} tool", parameter_schema={"text": "string to echo"},
                function=function or (lambda text: {"success": True, "message": text}), validate=validate)


def must_be_string(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")


class FakeLLM:
    """Returns scripted replies in order and counts how often it was called."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, system_prompt, messages, image):
        self.calls += 1
        return self.replies[self.calls - 1]


class TempDirTest(unittest.TestCase):
    """Gives each test its own temporary folder, removed afterwards."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def save_image(self, name, size=(40, 30), fmt=None):
        path = self.path(name)
        Image.new("RGB", size, "blue").save(path, format=fmt)
        return path


# ---------- Messages, tools, system prompt ----------

class MessageTest(unittest.TestCase):
    def test_only_tool_messages_carry_a_non_empty_tool_name(self):
        cases = [
            (Role.USER, None, True),                   # normal request with an image
            (Role.TOOL, "draw_bounding_boxes", True),  # a Python tool's result
            (Role.TOOL, None, False), (Role.TOOL, "", False), (Role.TOOL, "   ", False),
            (Role.USER, "draw_bounding_boxes", False),
            (Role.ASSISTANT, "draw_bounding_boxes", False),
            (Role.SYSTEM, "draw_bounding_boxes", False),
        ]
        for role, tool_name, valid in cases:
            with self.subTest(role=role, tool_name=tool_name):
                make = lambda: Message(role, "Draw a box around the dog", image_path="dog.png", tool_name=tool_name)
                if valid:
                    m = make()
                    self.assertEqual((m.content, m.image_path, m.tool_name),
                                     ("Draw a box around the dog", "dog.png", tool_name))
                else:
                    self.assertRaises(ValueError, make)


class ToolDefinitionTest(unittest.TestCase):
    def test_tool_definition_is_validated_on_creation(self):
        self.assertEqual(make_tool().name, "echo")  # a valid tool is created
        cases = [
            ("", "d", {}, print, ValueError), ("   ", "d", {}, print, ValueError),          # empty name
            (None, "d", {}, print, TypeError), (5, "d", {}, print, TypeError),             # non-string name
            ("t", "", {}, print, ValueError), ("t", "  ", {}, print, ValueError),           # empty description
            ("t", None, {}, print, TypeError), ("t", ["d"], {}, print, TypeError),         # non-string description
            ("t", "d", ["boxes"], print, TypeError), ("t", "d", "boxes", print, TypeError),  # schema not a dict
            ("t", "d", {"": "x"}, print, ValueError), ("t", "d", {"  ": "x"}, print, ValueError),  # empty param name
            ("t", "d", {1: "x"}, print, ValueError),                                        # non-string param name
            ("t", "d", {}, "not callable", TypeError),                                      # function not callable
        ]
        for name, description, schema, function, error in cases:
            with self.subTest(name=name, description=description, schema=schema, function=function):
                with self.assertRaises(error):
                    Tool(name, description, schema, function)

    def test_agent_rejects_duplicate_names_and_non_tools(self):
        for tools, error in [([make_tool("echo"), make_tool("echo")], ValueError), (["echo"], TypeError)]:
            with self.subTest(tools=tools), self.assertRaises(error):
                Agent(tools=tools, llm=FakeLLM())


class SystemPromptTest(unittest.TestCase):
    def test_lists_every_tool_and_appends_instructions(self):
        tools = [Tool("draw_bounding_boxes", "Draw labeled boxes.", {"boxes": "list of label + box"}, print),
                 Tool("count_objects", "Count objects.", {"label": "object name"}, print)]
        prompt = Agent(tools=tools, llm=FakeLLM(), instructions="Box format rules").system_prompt
        for text in ["draw_bounding_boxes", "Draw labeled boxes.", '"boxes"', "list of label + box",
                     "count_objects", "Count objects.", '"label"', "object name"]:
            with self.subTest(text=text):
                self.assertIn(text, prompt)
        self.assertTrue(prompt.endswith("Box format rules"))


# ---------- Strict JSON parsing ----------

class ParseReplyTest(unittest.TestCase):
    def test_accepts_tool_call_and_final_answer(self):
        self.assertEqual(parse_reply(CALL), {"type": "tool_call", "name": "echo", "arguments": {"text": "hi"}})
        self.assertEqual(parse_reply(FINAL), {"type": "final", "message": "No cat in this image."})

    def test_rejects_malformed_replies(self):
        cases = {
            "prose before": "Here you go: " + CALL,
            "prose after": CALL + " Done.",
            "json fence": "```json\n" + CALL + "\n```",
            "plain fence": "```\n" + FINAL + "\n```",
            "two objects": CALL + CALL,
            "object then final": CALL + "\n" + FINAL,
            "top-level list": "[" + CALL + "]",
            "empty list": "[]",
            "string": '"final"', "number": "42", "true": "true", "null": "null", "empty": "",
            "missing message": '{"type":"final"}',
            "missing arguments": '{"type":"tool_call","name":"echo"}',
            "extra field on final": '{"type":"final","message":"x","extra":1}',
            "extra field on call": '{"type":"tool_call","name":"echo","arguments":{},"id":1}',
            "unknown type": '{"type":"answer","message":"x"}',
            "message not a string": '{"type":"final","message":5}',
            "empty name": '{"type":"tool_call","name":"","arguments":{}}',
            "blank name": '{"type":"tool_call","name":"  ","arguments":{}}',
            "name not a string": '{"type":"tool_call","name":7,"arguments":{}}',
            "arguments not an object": '{"type":"tool_call","name":"echo","arguments":[]}',
            "final with tool fields": '{"type":"final","name":"echo","arguments":{}}',
            "duplicate top-level key": '{"type":"final","message":"a","message":"b"}',
            "duplicate argument key": '{"type":"tool_call","name":"echo","arguments":{"text":"a","text":"b"}}',
            "NaN": '{"type":"tool_call","name":"echo","arguments":{"text":NaN}}',
            "Infinity": '{"type":"tool_call","name":"echo","arguments":{"text":Infinity}}',
            "-Infinity": '{"type":"tool_call","name":"echo","arguments":{"text":-Infinity}}',
        }
        for case, text in cases.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_reply(text)


# ---------- call_tool ----------

class CallToolTest(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (4, 4))

    def call(self, agent, name, arguments):
        return agent.call_tool({"name": name, "arguments": arguments}, self.image, "x.png")

    def test_bad_calls_are_rejected_before_the_tool_runs(self):
        ran = []
        agent = Agent(tools=[make_tool(function=lambda text: ran.append(text) or {"success": True},
                                       validate=must_be_string)], llm=FakeLLM())
        cases = [
            ("nope", {"text": "hi"}, "unknown_tool"),
            ("echo", {}, "missing_arguments"),
            ("echo", {"text": "hi", "output_path": "/tmp/x.png"}, "unexpected_arguments"),
            ("echo", {"text": 5}, "invalid_arguments"),  # caught by the tool's validator
        ]
        for name, arguments, error_type in cases:
            with self.subTest(error_type=error_type):
                result = self.call(agent, name, arguments)
                self.assertEqual((result["success"], result["error_type"]), (False, error_type))
        self.assertEqual(ran, [])  # the tool function never ran

    def test_valid_call_runs_with_only_the_context_it_accepts(self):
        seen = {}
        def plain(text):
            return {"success": True, "message": text}
        def with_image(original_image, image_path, output_path, text):
            seen.update(size=original_image.size, image_path=image_path, output_path=output_path, text=text)
            return {"success": True}
        agent = Agent(tools=[make_tool("plain", plain), make_tool("with_image", with_image)], llm=FakeLLM())
        self.assertEqual(self.call(agent, "plain", {"text": "hi"}), {"success": True, "message": "hi"})
        self.assertTrue(self.call(agent, "with_image", {"text": "hi"})["success"])
        self.assertEqual(seen, {"size": (4, 4), "image_path": "x.png", "output_path": None, "text": "hi"})

    def test_tool_exception_becomes_tool_failed(self):
        def broken(text):
            raise OSError("disk full")
        result = self.call(Agent(tools=[make_tool(function=broken)], llm=FakeLLM()), "echo", {"text": "hi"})
        self.assertEqual(result["error_type"], "tool_failed")
        self.assertIn("disk full", result["error"])


# ---------- Agent.run decisions (fake model) ----------

class AgentRunTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.image_path = self.save_image("dog.png")

    def run_agent(self, llm, image_path="default", max_attempts=3, function=None, validate=None):
        agent = Agent(tools=[make_tool(function=function, validate=validate)], llm=llm, max_attempts=max_attempts)
        path = self.image_path if image_path == "default" else image_path
        messages = [Message(Role.USER, "box the dog", image_path=path)]
        return agent.run(messages), messages

    def test_no_usable_image_means_no_model_call(self):
        for case, path, answer_start in [("no image", None, "Please give an image"),
                                         ("missing file", self.path("missing.png"), "Can't use that image"),
                                         ("unsupported format", self.save_image("x.gif", fmt="GIF"),
                                          "Can't use that image")]:
            with self.subTest(case=case):
                llm = FakeLLM(FINAL)
                answer, _ = self.run_agent(llm, image_path=path)
                self.assertEqual(llm.calls, 0)
                self.assertTrue(answer.content.startswith(answer_start))

    def test_final_answer_is_returned_without_running_a_tool(self):
        llm = FakeLLM(FINAL)
        answer, messages = self.run_agent(llm)
        self.assertEqual((llm.calls, answer.content), (1, "No cat in this image."))
        self.assertNotIn(Role.TOOL, [m.role for m in messages])

    def test_successful_tool_call_ends_the_turn_after_one_model_call(self):
        llm = FakeLLM(CALL, FINAL)
        answer, messages = self.run_agent(llm)
        self.assertEqual((llm.calls, answer.content), (1, "hi"))
        self.assertEqual((messages[-1].role, messages[-1].tool_name), (Role.TOOL, "echo"))

    def test_fixable_mistakes_are_retried_until_a_valid_call(self):
        cases = {
            "malformed JSON": ("Sure! " + CALL, Role.SYSTEM),
            "unknown tool": ('{"type":"tool_call","name":"nope","arguments":{"text":"hi"}}', Role.TOOL),
            "invalid argument value": ('{"type":"tool_call","name":"echo","arguments":{"text":5}}', Role.TOOL),
        }
        for case, (first_reply, feedback_role) in cases.items():
            with self.subTest(case=case):
                llm = FakeLLM(first_reply, CALL)
                answer, messages = self.run_agent(llm, validate=must_be_string)
                self.assertEqual((llm.calls, answer.content), (2, "hi"))
                self.assertEqual(messages[2].role, feedback_role)  # the error went back to the model

    def test_malformed_replies_stop_when_the_retry_budget_is_used(self):
        for max_attempts in [1, 2, 3]:
            with self.subTest(max_attempts=max_attempts):
                llm = FakeLLM(*["not json"] * max_attempts)
                answer, _ = self.run_agent(llm, max_attempts=max_attempts)
                self.assertEqual(llm.calls, max_attempts)
                self.assertTrue(answer.content.startswith(f"Gave up after {max_attempts} attempts"))

    def test_unfixable_problems_stop_without_a_retry(self):
        def broken(text):
            raise OSError("disk full")
        for case, reply, function, answer_start in [
                ("empty reply", "", None, "Stopped [empty_response]"),
                ("whitespace reply", "  \n", None, "Stopped [empty_response]"),
                ("tool crashed", CALL, broken, "Stopped [tool_failed]")]:
            with self.subTest(case=case):
                llm = FakeLLM(reply, CALL)
                answer, _ = self.run_agent(llm, function=function)
                self.assertEqual(llm.calls, 1)
                self.assertTrue(answer.content.startswith(answer_start))


# ---------- load_image ----------

class LoadImageTest(TempDirTest):
    def test_png_and_jpeg_load_with_size_and_format(self):
        for name, fmt in [("a.png", "PNG"), ("a.jpg", "JPEG")]:
            with self.subTest(fmt=fmt):
                image = load_image(self.save_image(name, size=(40, 30)))
                self.assertEqual((image.size, image.format), ((40, 30), fmt))

    def test_unusable_files_are_rejected(self):
        with open(self.save_image("good.png"), "rb") as f:
            good = f.read()
        for name, data in [("truncated.png", good[: len(good) // 2]), ("garbage.png", b"not an image")]:
            with open(self.path(name), "wb") as f:
                f.write(data)
        animated = self.path("anim.png")
        Image.new("RGB", (4, 4)).save(animated, save_all=True, append_images=[Image.new("RGB", (4, 4), "red")])
        cases = [
            ("missing", self.path("missing.png"), ValueError),
            ("truncated", self.path("truncated.png"), OSError),
            ("not an image", self.path("garbage.png"), OSError),
            ("GIF", self.save_image("a.gif", fmt="GIF"), ValueError),
            ("BMP", self.save_image("a.bmp", fmt="BMP"), ValueError),
            ("WEBP", self.save_image("a.webp", fmt="WEBP"), ValueError),
            ("animated PNG", animated, ValueError),
        ]
        for case, path, error in cases:
            with self.subTest(case=case), self.assertRaises(error):
                load_image(path)


if __name__ == "__main__":
    unittest.main()
