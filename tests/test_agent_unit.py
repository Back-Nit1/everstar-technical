"""Offline unit tests for agent.py. No Gemini, no API key, no network.
Image files are made in a temporary folder that is deleted after each test.

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import Agent, Message, Role, Tool, load_image, parse_reply  # noqa: E402
from agent import tool_error  # noqa: E402

FINAL = '{"type":"final","message":"No cat in this image."}'
CALL = '{"type":"tool_call","name":"echo","arguments":{"text":"hi"}}'


def make_tool(name="echo", function=None, validate=None, return_direct=True):
    return Tool(name=name, description=f"{name} tool", parameter_schema={"text": "string to echo"},
                function=function or (lambda text: {"success": True, "message": text}), validate=validate,
                return_direct=return_direct)


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
    def test_only_tool_messages_carry_a_tool_name_or_an_image(self):
        closeup = Image.new("RGB", (4, 4))
        cases = [
            (Role.USER, None, None, True),                      # normal request with an image path
            (Role.TOOL, "draw_bounding_boxes", None, True),     # a Python tool's result
            (Role.TOOL, "zoom_region", closeup, True),          # a tool result with a picture
            (Role.TOOL, None, None, False), (Role.TOOL, "", None, False), (Role.TOOL, "   ", None, False),
            (Role.USER, "draw_bounding_boxes", None, False),
            (Role.ASSISTANT, "draw_bounding_boxes", None, False),
            (Role.SYSTEM, "draw_bounding_boxes", None, False),
            (Role.USER, None, closeup, False),
            (Role.ASSISTANT, None, closeup, False),
            (Role.SYSTEM, None, closeup, False),
        ]
        for role, tool_name, image, valid in cases:
            with self.subTest(role=role, tool_name=tool_name, image=image is not None):
                make = lambda: Message(role, "Draw a box around the dog", image_path="dog.png",
                                       tool_name=tool_name, image=image)
                if valid:
                    m = make()
                    self.assertEqual((m.content, m.image_path, m.tool_name), ("Draw a box around the dog", "dog.png", tool_name))
                    self.assertIs(m.image, image)
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


# ---------- Strict JSON parsing ----------

class ParseReplyTest(unittest.TestCase):
    def test_rejects_malformed_replies(self):
        cases = {
            "code fence": "```json\n" + CALL + "\n```",
            "two objects": CALL + CALL,
            "list, not an object": "[" + CALL + "]",
            "string, not an object": '"final"',
            "missing message": '{"type":"final"}',
            "missing arguments": '{"type":"tool_call","name":"echo"}',
            "extra field": '{"type":"tool_call","name":"echo","arguments":{},"id":1}',
            "message not a string": '{"type":"final","message":5}',
            "arguments not an object": '{"type":"tool_call","name":"echo","arguments":[]}',
            "blank name": '{"type":"tool_call","name":"  ","arguments":{}}',
            "duplicate key": '{"type":"final","message":"a","message":"b"}',
            "NaN": '{"type":"tool_call","name":"echo","arguments":{"text":NaN}}',
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


# ---------- Agent.run decisions (fake model) ----------

class AgentRunTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.image_path = self.save_image("dog.png")

    def run_agent(self, llm, image_path="default", max_attempts=3, function=None, validate=None,
                  return_direct=True, max_steps=4):
        agent = Agent(tools=[make_tool(function=function, validate=validate, return_direct=return_direct)],
                      llm=llm, max_attempts=max_attempts, max_steps=max_steps)
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

    def test_fixable_mistakes_are_retried_until_a_valid_call(self):
        results = iter([tool_error("limit_reached", "Limit reached for zooming in")])  # fails once, then works
        limit_once = lambda text: next(results, {"success": True, "message": text})
        cases = {
            "malformed JSON": ("Sure! " + CALL, Role.SYSTEM, None),
            "unknown tool": ('{"type":"tool_call","name":"nope","arguments":{"text":"hi"}}', Role.TOOL, None),
            "invalid argument value": ('{"type":"tool_call","name":"echo","arguments":{"text":5}}', Role.TOOL, None),
            "limit reached": (CALL, Role.TOOL, limit_once),
        }
        for case, (first_reply, feedback_role, function) in cases.items():
            with self.subTest(case=case):
                llm = FakeLLM(first_reply, CALL)
                answer, messages = self.run_agent(llm, validate=must_be_string, function=function)
                self.assertEqual((llm.calls, answer.content), (2, "hi"))
                self.assertEqual(messages[2].role, feedback_role)  # the error went back to the model

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

    # ---------- return_direct and max_steps ----------

    def test_return_direct_decides_if_the_model_sees_a_successful_result(self):
        for return_direct, calls, content, roles in [
                (True, 1, "hi", [Role.USER, Role.ASSISTANT, Role.TOOL]),  # result is the answer
                (False, 2, "No cat in this image.",                       # model saw the result, then replied
                 [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT])]:
            with self.subTest(return_direct=return_direct):
                llm = FakeLLM(CALL, FINAL)
                answer, messages = self.run_agent(llm, return_direct=return_direct)
                self.assertEqual((llm.calls, answer.content), (calls, content))
                self.assertEqual([m.role for m in messages], roles)
                self.assertEqual(messages[2].tool_name, "echo")

    def test_successful_calls_do_not_use_the_retry_budget(self):
        # One failure allowed, yet three successful calls and a final answer all fit.
        llm = FakeLLM(CALL, CALL, CALL, FINAL)
        answer, _ = self.run_agent(llm, max_attempts=1, return_direct=False)
        self.assertEqual((llm.calls, answer.content), (4, "No cat in this image."))
        # Failures still count: success, failure, success, failure -> gives up at 2 failures.
        llm = FakeLLM(CALL, "not json", CALL, "not json")
        answer, _ = self.run_agent(llm, max_attempts=2, return_direct=False)
        self.assertEqual(llm.calls, 4)
        self.assertTrue(answer.content.startswith("Gave up after 2 attempts"))

    def test_looping_model_stops_at_max_steps(self):
        for max_steps in [1, 4, 6]:
            with self.subTest(max_steps=max_steps):
                llm = FakeLLM(*[CALL] * (max_steps + 1))
                answer, messages = self.run_agent(llm, return_direct=False, max_steps=max_steps)
                self.assertEqual(llm.calls, max_steps)
                self.assertEqual(answer.content, f"Stopped [max_steps]: reached the limit of {max_steps} "
                                                 "model calls in one request without a final answer.")
                self.assertEqual([m.role for m in messages].count(Role.TOOL), max_steps)

    # ---------- tool images, state ----------

    def test_tool_image_rides_on_the_tool_message_not_in_the_json(self):
        closeup = Image.new("RGB", (8, 4), "red")
        llm = FakeLLM(CALL, FINAL)
        answer, messages = self.run_agent(llm, return_direct=False,
                                          function=lambda text: {"success": True, "message": text, "image": closeup})
        tool_message = messages[2]
        self.assertEqual((tool_message.role, tool_message.tool_name), (Role.TOOL, "echo"))
        self.assertIs(tool_message.image, closeup)                                       # picture on the message
        self.assertEqual(json.loads(tool_message.content), {"success": True, "message": "hi"})  # not in the text
        self.assertEqual([m.image for m in messages if m is not tool_message], [None, None, None])
        self.assertEqual((llm.calls, answer.content), (2, "No cat in this image."))  # the model saw it and replied

    def test_state_is_shared_within_a_request_and_fresh_for_the_next(self):
        seen = []  # (the state object, its count right after this call)
        def count(state, text):
            state["calls"] = state.get("calls", 0) + 1
            seen.append((state, state["calls"]))
            return {"success": True, "calls": state["calls"]}
        agent = Agent(tools=[make_tool(function=count, return_direct=False)],
                      llm=FakeLLM(CALL, CALL, FINAL, CALL, FINAL))
        for _ in range(2):  # two requests, same agent
            agent.run([Message(Role.USER, "box the dog", image_path=self.image_path)])
        (first, a), (second, b), (next_request, c) = seen
        self.assertEqual((a, b, c), (1, 2, 1))
        self.assertIs(first, second)             # one notepad for both calls of a request
        self.assertIsNot(first, next_request)    # a new, empty one for the next request
        # state comes from Python only: the model can't send it as an argument.
        result = agent.call_tool({"name": "echo", "arguments": {"text": "hi", "state": {}}}, Image.new("RGB", (4, 4)), "x.png")
        self.assertEqual(result["error_type"], "unexpected_arguments")


# ---------- load_image ----------

class LoadImageTest(TempDirTest):
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
