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
from agent import load_image  # noqa: E402
from chat import validate_boxes  # noqa: E402

DOG = os.path.join(ROOT, "dog.png")
ENVIRONMENT = os.path.join(ROOT, "environment.png")  # tree, cat and dog on a white background
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
                           "image_size": original_image.size,
                           "images": [(m.tool_name, m.image.size) for m in messages if m.image is not None]})
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
        result = json.loads(messages[-1].content)
        self.assertEqual(answer.content, f"Drew 1 box(es): dog {result['boxes'][0]['box_px']}. Saved to {self.output}")
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
        self.assertTrue(answer.content.startswith("Drew 1 box(es): dog ["))
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
        self.assertTrue(answer.content.startswith("Drew 1 box(es): dog ["))
        self.assert_boxed_output()

    def test_crop_call_saves_the_dog_as_its_own_file_in_one_model_call(self):
        fake = FakeGemini(VALID_CALL.replace("draw_bounding_boxes", "crop_object"))
        agent = Agent(tools=chat.tools, llm=fake, instructions=chat.BOX_FORMAT)
        messages = [Message(Role.USER, "Crop the dog out of the image", image_path=DOG)]
        answer = agent.run(messages)  # no output path: goes to OUTPUT_DIR (patched to the temp folder)

        self.assertEqual(len(fake.calls), 1)
        self.assertIn("crop_object", fake.calls[0]["system_prompt"])
        self.assertEqual(self.draw.call_count, 0)                     # the model picked the crop tool
        crop = json.loads(messages[-1].content)["crops"][0]
        self.assertEqual(crop["output_path"], os.path.join(self.dir, "outputs", "dog_dog.png"))
        self.assertEqual(answer.content, f"Cropped 1 object(s): dog {crop['box_px']} -> {crop['output_path']}")
        x0, y0, x1, y1 = crop["box_px"]
        self.assertEqual(open_rgb(crop["output_path"]).size, (x1 - x0, y1 - y0))
        self.assertEqual(sha256(DOG), self.dog_hash)                  # dog.png untouched

    def test_zoom_then_draw_puts_the_box_on_the_dog_in_the_full_photo(self):
        before = sha256(ENVIRONMENT)
        image = load_image(ENVIRONMENT)
        w, h = image.size
        dog = object_box(image, (623, 400, 1246, 892))  # the dog's real pixels, measured from the photo
        # 1st reply: zoom on the lower right. The area shown is rounded outward to whole pixels.
        zoom = {"type": "tool_call", "name": "zoom_region", "arguments": {"region": [600, 600, 900, 900]}}
        zx0, zy0, zx1, zy1 = 600 * w // 1000, 600 * h // 1000, -(-900 * w // 1000), -(-900 * h // 1000)
        # 2nd reply: the dog as a perfect model would box it inside the close-up (0-1000 of the close-up).
        zw, zh = zx1 - zx0, zy1 - zy0
        in_closeup = [(dog[1] - zy0) * 1000 // zh, (dog[0] - zx0) * 1000 // zw,
                      -(-(dog[3] - zy0) * 1000 // zh), -(-(dog[2] - zx0) * 1000 // zw)]
        draw = {"type": "tool_call", "name": "draw_bounding_boxes",
                "arguments": {"boxes": [{"label": "dog", "box": in_closeup}]}}
        fake = FakeGemini(json.dumps(zoom), json.dumps(draw))
        agent = Agent(tools=chat.tools, llm=fake, instructions=chat.BOX_FORMAT)
        messages = [Message(Role.USER, "Draw a box around the dog in the image", image_path=ENVIRONMENT)]
        answer = agent.run(messages, output_path=self.output)

        # Gemini's second call received the close-up, with a note describing it.
        self.assertEqual(len(fake.calls), 2)
        zoom_result = json.loads(messages[2].content)
        cw, ch = zoom_result["closeup_size"]
        self.assertEqual((zoom_result["region_px"], max(cw, ch)), ([zx0, zy0, zx1, zy1], 1024))
        self.assertEqual(fake.calls[0]["images"], [])                           # 1st call: just the photo
        self.assertEqual(fake.calls[1]["images"], [("zoom_region", (cw, ch))])  # 2nd call: plus the close-up
        self.assertEqual([role for role, _, _ in fake.calls[1]["messages"]], [Role.USER, Role.ASSISTANT, Role.TOOL])
        self.assertIn("Zoom 1 of 2", zoom_result["message"])
        self.assertIn("relative to this close-up", zoom_result["message"])
        self.assertNotIn("image", zoom_result)                                  # the picture is not in the JSON text

        # The box sent in close-up coordinates is drawn on the dog in the full photo.
        self.assertEqual(self.draw.call_count, 1)
        self.assertEqual(self.draw.call_args.kwargs["boxes"][0]["box"], in_closeup)  # the model never did the math
        x0, y0, x1, y1 = json.loads(messages[-1].content)["boxes"][0]["box_px"]
        for drawn, real in zip([x0, y0, x1, y1], dog):
            self.assertLessEqual(abs(drawn - real), 2)                         # within 2 px of the real dog
        boxed, original = open_rgb(self.output), image.convert("RGB")
        self.assertEqual(boxed.size, original.size)
        for edge in [(x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2), ((x0 + x1) // 2, y1)]:  # left, right, bottom
            self.assertEqual(boxed.getpixel(edge), (255, 0, 0))
            self.assertNotEqual(original.getpixel(edge), (255, 0, 0))
        self.assertTrue(answer.content.startswith("Drew 1 box(es): dog ["))
        self.assertEqual(sha256(ENVIRONMENT), before)


def non_white(image):
    """Mask of the object pixels: everything darker than near-white (the backgrounds here are plain white)."""
    return chat.safe_rgb(image).convert("L").point(lambda v: 255 if v < 240 else 0)


def object_box(image, region):
    """Pixel box [x_min, y_min, x_max, y_max] (max exclusive) of the object pixels inside region, measured
    from the image itself, so the expected answer does not depend on hand-picked numbers."""
    left, top = region[:2]
    x0, y0, x1, y1 = non_white(image).crop(region).getbbox()
    return [x0 + left, y0 + top, x1 + left, y1 + top]


def tightest_box(px, w, h):
    """The tightest 0-1000 [y_min, x_min, y_max, x_max] box a perfect model could send that still covers px."""
    x0, y0, x1, y1 = px
    return [y0 * 1000 // h, x0 * 1000 // w, -(-y1 * 1000 // h), -(-x1 * 1000 // w)]


class CropKeepsTheWholeObjectTest(unittest.TestCase):
    """The real crop tool on the real images: a box that covers the dog gives a crop with all of the dog,
    none of the other objects, and almost none of the white space."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(chat, "OUTPUT_DIR", os.path.join(tmp.name, "outputs"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def crop_object(self, path, region, others=()):
        """Crop the object found in region with the tightest valid box; check nothing of it was cut off."""
        before = sha256(path)
        image = load_image(path)
        w, h = image.size
        dog = object_box(image, region)
        boxes = [{"label": "dog", "box": tightest_box(dog, w, h)}]
        validate_boxes(boxes)  # a box the model is allowed to send
        crop = chat.crop_object(image, path, None, boxes)["crops"][0]
        cx0, cy0, cx1, cy1 = crop["box_px"]
        saved = open_rgb(crop["output_path"])

        self.assertTrue(cx0 <= dog[0] and cy0 <= dog[1] and cx1 >= dog[2] and cy1 >= dog[3])  # whole dog inside
        self.assertEqual(saved.tobytes(), image.convert("RGB").crop(crop["box_px"]).tobytes())  # pixels unchanged
        self.assertEqual(non_white(saved).histogram()[255], non_white(image).crop(dog).histogram()[255])  # every dog pixel kept
        for edge_gap in [dog[0] - cx0, dog[1] - cy0, cx1 - dog[2], cy1 - dog[3]]:  # at most one 0-1000 step + 1 px
            self.assertLessEqual(edge_gap, max(w, h) / 1000 + 1)
        for other in others:  # e.g. the cat and the tree are cut away
            ox0, oy0, ox1, oy1 = object_box(image, other)
            self.assertTrue(ox1 <= cx0 or ox0 >= cx1 or oy1 <= cy0 or oy0 >= cy1, f"crop overlaps {other}")
        self.assertEqual(sha256(path), before)  # the original file is untouched
        return image.size, saved.size

    def test_dog_png_crop_keeps_the_whole_dog_and_trims_the_white_space(self):
        (w, h), (cw, ch) = self.crop_object(DOG, region=(0, 0, 310, 194))
        self.assertLess(cw * ch, w * h / 2)  # the white space around the dog is gone

    def test_environment_png_crop_isolates_the_dog_from_the_cat_and_tree(self):
        (w, h), (cw, ch) = self.crop_object(ENVIRONMENT, region=(623, 400, 1246, 892),           # lower right: dog
                                            others=[(0, 0, 1246, 400), (0, 300, 623, 892)])     # tree, cat
        self.assertLess(cw * ch, w * h / 20)  # only the dog's small corner of the scene is left


if __name__ == "__main__":
    unittest.main()
