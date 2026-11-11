"""Offline integration tests: the real Agent, JSON parser, box validator, drawing tool and Pillow code,
with only the Gemini call replaced by a scripted fake. No network, no API key.
Output images go to a temporary folder; dog.png is only read.

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chat  # noqa: E402
from agent import Agent, Message, Role  # noqa: E402

DOG = os.path.join(ROOT, "dog.png")
VALID_CALL = json.dumps({"type": "tool_call", "name": "draw_bounding_boxes",
                         "arguments": {"boxes": [{"label": "dog", "box": [205, 161, 835, 867]}]}})
BAD_COORDS_CALL = json.dumps({"type": "tool_call", "name": "draw_bounding_boxes",  # x/y swapped, out of range
                              "arguments": {"boxes": [{"label": "dog", "box": [700, 0, 1200, 100]}]}})


class FakeGemini:
    """Same interface as chat.gemini_llm: (system_prompt, messages, original_image) -> raw text.
    Returns scripted replies in order and records what each call received."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system_prompt, messages, original_image):
        self.calls.append({"system_prompt": system_prompt,
                           "messages": [(m.role, m.content, m.tool_name) for m in messages],  # snapshot
                           "image_size": original_image.size})
        return self.replies[len(self.calls) - 1]


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def open_rgb(path):
    with Image.open(path) as image:
        return image.convert("RGB")


class BoundingBoxWorkflowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.output = os.path.join(self.dir, "dog_boxed.png")
        self.dog_hash = sha256(DOG)
        # Safety net: even a default output path lands in the temporary folder, never in the repo.
        for patcher in [mock.patch.object(chat, "OUTPUT_DIR", os.path.join(self.dir, "outputs")),
                        mock.patch.object(chat.tools[0], "function", mock.create_autospec(
                            chat.draw_bounding_boxes, side_effect=chat.draw_bounding_boxes))]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.draw = chat.tools[0].function  # spy around the real drawing tool

    def run_agent(self, fake):
        agent = Agent(tools=chat.tools, llm=fake, instructions=chat.BOX_FORMAT)
        messages = [Message(Role.USER, "Draw a box around the dog in the image", image_path=DOG)]
        return agent.run(messages, output_path=self.output), messages

    def assert_boxed_output(self):
        """The result exists in the temp folder, has dog.png's size, differs from it, and dog.png is untouched."""
        self.assertTrue(os.path.isfile(self.output))
        self.assertEqual(os.listdir(self.dir), ["dog_boxed.png"])  # exactly one image written
        original, boxed = open_rgb(DOG), open_rgb(self.output)
        self.assertEqual(boxed.size, original.size)
        self.assertIsNotNone(ImageChops.difference(original, boxed).getbbox())  # boxes were drawn
        self.assertEqual(sha256(DOG), self.dog_hash)

    def test_valid_tool_call_draws_box_in_one_model_call(self):
        fake = FakeGemini(VALID_CALL)
        answer, messages = self.run_agent(fake)

        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertIn("draw_bounding_boxes", call["system_prompt"])
        self.assertIn("[y_min, x_min, y_max, x_max]", call["system_prompt"])  # box rules reach the model
        self.assertEqual(call["image_size"], open_rgb(DOG).size)              # model got the image
        self.assertEqual(call["messages"], [(Role.USER, "Draw a box around the dog in the image", None)])

        self.assertEqual(self.draw.call_count, 1)
        self.assertEqual(answer.content, f"Drew 1 box(es) (dog). Saved to {self.output}")
        result = json.loads(messages[-1].content)
        self.assertEqual((messages[-1].role, messages[-1].tool_name), (Role.TOOL, "draw_bounding_boxes"))
        self.assertEqual((result["success"], result["output_path"], result["num_boxes"]), (True, self.output, 1))
        self.assert_boxed_output()

    def test_drawn_box_is_at_the_converted_pixels(self):
        _, messages = self.run_agent(FakeGemini(VALID_CALL))
        x0, y0, x1, y1 = json.loads(messages[-1].content)["boxes"][0]["box_px"]
        w, h = open_rgb(DOG).size
        self.assertEqual([x0, y0, x1, y1], [round(161 * w / 1000), round(205 * h / 1000),
                                            round(867 * w / 1000), round(835 * h / 1000)])
        self.assertEqual(open_rgb(self.output).getpixel((x1 - 1, (y0 + y1) // 2)), (255, 0, 0))  # right edge

    def test_malformed_json_then_valid_call_retries_once(self):
        malformed = "Sure! Here is the box:\n```json\n" + VALID_CALL + "\n```"
        fake = FakeGemini(malformed, VALID_CALL)
        answer, _ = self.run_agent(fake)

        self.assertEqual(len(fake.calls), 2)
        second = fake.calls[1]["messages"]
        self.assertEqual([role for role, _, _ in second], [Role.USER, Role.ASSISTANT, Role.SYSTEM])
        self.assertEqual(second[1][1], malformed)                    # the model sees its own bad reply
        self.assertIn("Your reply was rejected", second[2][1])       # and why it was rejected
        self.assertIn("exactly one JSON object", second[2][1])

        self.assertEqual(self.draw.call_count, 1)
        self.assertTrue(answer.content.startswith("Drew 1 box(es) (dog)"))
        self.assert_boxed_output()

    def test_invalid_coordinates_then_valid_call_retries_without_drawing(self):
        fake = FakeGemini(BAD_COORDS_CALL, VALID_CALL)
        answer, _ = self.run_agent(fake)

        self.assertEqual(len(fake.calls), 2)
        second = fake.calls[1]["messages"]
        self.assertEqual([role for role, _, _ in second], [Role.USER, Role.ASSISTANT, Role.TOOL])
        role, content, tool_name = second[2]
        error = json.loads(content)                                  # the validator's error went back to the model
        self.assertEqual(tool_name, "draw_bounding_boxes")
        self.assertEqual((error["success"], error["error_type"]), (False, "invalid_arguments"))
        self.assertIn("between 0 and 1000", error["error"])

        self.assertEqual(self.draw.call_count, 1)                    # the bad call never reached the tool
        self.assertEqual(self.draw.call_args.kwargs["boxes"][0]["box"], [205, 161, 835, 867])
        self.assertTrue(answer.content.startswith("Drew 1 box(es) (dog)"))
        self.assert_boxed_output()


if __name__ == "__main__":
    unittest.main()
