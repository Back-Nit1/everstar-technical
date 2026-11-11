"""Offline unit tests for chat.py. No Gemini, no API key, no network.
Importing chat builds the agent but makes no request. Files go only into temporary folders,
and settings (budgets, limits, OUTPUT_DIR) are changed only inside a test with unittest.mock.patch.

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import base64
import contextlib
import io
import json
import os
import random
import sys
import tempfile
import unittest
from fractions import Fraction
from unittest import mock

import httpx
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat  # noqa: E402
from agent import Message, Role  # noqa: E402
from agent import parse_reply  # noqa: E402
from chat import (EmptyResponseError, ImageTooLargeError, base64_size, check_output,  # noqa: E402
                  draw_bounding_boxes, explain_api_error, prepare_gemini_copy, safe_rgb,
                  split_image_paths, validate_boxes)

GOOD = {"label": "dog", "box": [100, 200, 300, 400]}


def box(label="dog", coords=(100, 200, 300, 400)):
    return {"label": label, "box": list(coords)}


def noise_image(size=(400, 200), fmt="PNG"):
    """Random pixels compress badly, so the encoded size is predictable enough to force budgets.
    .format is set like load_image does, since prepare_gemini_copy picks PNG or JPEG from it."""
    image = Image.merge("RGB", [Image.effect_noise(size, 100) for _ in range(3)])
    image.format = fmt
    return image


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def open_rgb(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def decode(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def small_budget(budget, overhead=100, min_long_side=10):
    """Patch the request budget down so tiny images exercise the compress/shrink paths."""
    return mock.patch.multiple(chat, REQUEST_BUDGET=budget, REQUEST_OVERHEAD=overhead, MIN_LONG_SIDE=min_long_side)


class TempDirTest(unittest.TestCase):
    """Gives each test its own temporary folder, removed afterwards."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)


# ---------- Box validation ----------

class ValidateBoxesTest(unittest.TestCase):
    def assert_all_rejected(self, cases):
        for case, boxes, error in cases:
            with self.subTest(case=case, boxes=boxes), self.assertRaises(error):
                validate_boxes(boxes)

    def test_accepts_valid_boxes(self):
        cases = {
            "one box": [GOOD],
            "several boxes": [box("dog"), box("black cat", (0, 0, 500, 500)), box("tree")],
            "full image": [box(coords=(0, 0, 1000, 1000))],
            "smallest box": [box(coords=(0, 0, 1, 1))],
            "plain names": [box(label) for label in
                            ["black cat", "golden retriever", "St Bernard's dog", "T-rex", "chien", "a", "x" * 40]],
        }
        for case, boxes in cases.items():
            with self.subTest(case=case):
                self.assertIsNone(validate_boxes(boxes))

    def test_rejects_bad_list_or_entry_structure(self):
        self.assert_all_rejected([
            ("single object, not a list", GOOD, TypeError),
            ("string", "dog", TypeError),
            ("null", None, TypeError),
            ("empty list", [], ValueError),
            ("entry is a string", ["dog"], TypeError),
            ("entry is a list", [[100, 200, 300, 400]], TypeError),
            ("entry is null", [None], TypeError),
            ("missing box", [{"label": "dog"}], ValueError),
            ("missing label", [{"box": [1, 2, 3, 4]}], ValueError),
            ("extra field", [{**GOOD, "confidence": 0.9}], ValueError),
            ("path field", [{**GOOD, "output_path": "/tmp/x.png"}], ValueError),
            ("one bad box spoils the list", [GOOD, box("black_cat")], ValueError),
        ])

    def test_rejects_invalid_labels(self):
        self.assert_all_rejected([
            ("null", [box(None)], TypeError),
            ("number", [box(5)], TypeError),
            ("empty", [box("")], ValueError),
            ("blank", [box("   ")], ValueError),
            ("underscore", [box("black_cat")], ValueError),
            ("leading underscore", [box("_dog")], ValueError),
            ("file name", [box("dog.png")], ValueError),
            ("sentence with file name", [box("the tree in environment.png")], ValueError),
            ("absolute path", [box("/Users/x/dog")], ValueError),
            ("backslash", [box("a\\b")], ValueError),
            ("41 characters", [box("x" * 41)], ValueError),
            ("exclamation", [box("dog!")], ValueError),
            ("trailing dot", [box("dog.")], ValueError),
            ("parentheses", [box("dog (left)")], ValueError),
            ("newline", [box("dog\ncat")], ValueError),
            ("leading space", [box(" dog")], ValueError),
            ("leading hyphen", [box("-dog")], ValueError),
        ])

    def test_rejects_invalid_coordinates(self):
        self.assert_all_rejected([
            ("string box", [{"label": "dog", "box": "1,2,3,4"}], TypeError),
            ("tuple box", [{"label": "dog", "box": (1, 2, 3, 4)}], TypeError),
            ("no values", [box(coords=())], ValueError),
            ("3 values", [box(coords=(1, 2, 3))], ValueError),
            ("5 values", [box(coords=(1, 2, 3, 4, 5))], ValueError),
            ("string value", [box(coords=("1", 2, 3, 4))], TypeError),
            ("float value", [box(coords=(1.0, 2, 3, 4))], TypeError),
            ("true", [box(coords=(True, 2, 3, 4))], TypeError),
            ("false", [box(coords=(0, 0, False, 1))], TypeError),
            ("null", [box(coords=(None, 2, 3, 4))], TypeError),
            ("below 0", [box(coords=(-1, 0, 10, 10))], ValueError),
            ("above 1000", [box(coords=(0, 0, 1001, 10))], ValueError),
            ("y_min > y_max", [box(coords=(300, 200, 100, 400))], ValueError),
            ("x_min > x_max", [box(coords=(100, 400, 300, 200))], ValueError),
            ("y_min == y_max", [box(coords=(100, 200, 100, 400))], ValueError),
            ("x_min == x_max", [box(coords=(100, 200, 300, 200))], ValueError),
        ])


# ---------- Interactive image paths ----------

class SplitImagePathsTest(unittest.TestCase):
    def test_finds_every_image_path_in_the_line(self):
        cases = [
            ("Draw a box around the dog in dog.png", ["dog.png"]),
            ("Box the cat in /Users/me/pics/environment.PNG", ["/Users/me/pics/environment.PNG"]),
            ("Is there a car in photo.jpeg?", ["photo.jpeg"]),
            ("box the dog in 'My Photos/dog.png'", ["My Photos/dog.png"]),
            ('box the dog in "My Photos/dog.png"', ["My Photos/dog.png"]),
            (r"box the dog in My\ Photos/dog.png", ["My Photos/dog.png"]),
            ("now just the tree", []),
            ("", []),
            ("box the dog in dog.gif", []),
            ("box dog.png and environment.png and cat.jpg", ["dog.png", "environment.png", "cat.jpg"]),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(split_image_paths(line)[0], expected)


class InteractiveModeTest(unittest.TestCase):
    """Drives chat.main()'s chat loop with scripted input; handle (which would call Gemini) is a mock."""

    def chat(self, line):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["chat.py"]), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-not-used"}), \
                mock.patch("builtins.input", side_effect=[line, "exit"]), \
                mock.patch.object(chat, "handle") as handle, contextlib.redirect_stdout(out):
            chat.main()
        return handle, out.getvalue()

    def test_cli_flags_at_the_prompt_make_no_model_call(self):
        for line in ['--image dog.png --prompt "Draw a box around the dog" --output dog_boxed.png',
                     "python chat.py --image dog.png --prompt box",
                     "Draw a box around the dog in dog.png --output out.png",
                     "box the dog --output=out.png"]:
            with self.subTest(line=line):
                handle, printed = self.chat(line)
                handle.assert_not_called()
                self.assertIn("command-line flags", printed)
                self.assertNotIn("one image", printed)  # not mistaken for a second input image

    def test_plain_request_with_image_path_still_runs(self):
        dog = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dog.png")
        handle, _ = self.chat(f"Draw a box around the dog in {dog}")
        handle.assert_called_once()
        _, text, image_path = handle.call_args.args
        self.assertEqual((text, image_path), ("Draw a box around the dog in the image", dog))


# ---------- Gemini copy: size and budget ----------

class PrepareGeminiCopyTest(unittest.TestCase):
    TEXT_BYTES = 1000

    def fits(self, data, budget, overhead=100):
        return base64_size(len(data)) + self.TEXT_BYTES + overhead <= budget

    def test_base64_size_matches_real_encoding(self):
        for n_bytes in list(range(0, 100)) + [3 * 1024 * 1024 + 1]:
            with self.subTest(n_bytes=n_bytes):
                self.assertEqual(base64_size(n_bytes), len(base64.b64encode(bytes(n_bytes))))

    def test_safety_budget_is_90_mb_below_gemini_100_mb_limit(self):
        self.assertEqual((chat.REQUEST_BUDGET, chat.GEMINI_INLINE_LIMIT), (90 * chat.MB, 100 * chat.MB))

    def test_image_within_budget_keeps_its_format_and_size(self):
        for fmt, mime in [("PNG", "image/png"), ("JPEG", "image/jpeg")]:
            with self.subTest(fmt=fmt):
                data, sent_mime, size = prepare_gemini_copy(noise_image((40, 30), fmt), self.TEXT_BYTES)
                self.assertEqual((sent_mime, size), (mime, (40, 30)))
                self.assertEqual((decode(data).format, decode(data).size), (fmt, (40, 30)))

    def test_png_over_budget_is_compressed_to_jpeg_at_full_size(self):
        image = noise_image((400, 200), "PNG")
        png, jpeg = len(chat.encode(image, "PNG")), len(chat.encode(image, "JPEG"))
        budget = (base64_size(png) + base64_size(jpeg)) // 2 + self.TEXT_BYTES + 100  # JPEG fits, PNG doesn't
        with small_budget(budget):
            data, mime, size = prepare_gemini_copy(image, self.TEXT_BYTES)
        self.assertEqual((mime, size), ("image/jpeg", (400, 200)))
        self.assertTrue(self.fits(data, budget))

    def test_image_over_budget_is_shrunk_proportionally_to_fit(self):
        for original in [(400, 200), (300, 450), (500, 500)]:
            with self.subTest(size=original):
                image = noise_image(original, "JPEG")
                budget = base64_size(len(chat.encode(image, "JPEG"))) // 2 + self.TEXT_BYTES + 100
                with small_budget(budget):
                    data, mime, (w, h) = prepare_gemini_copy(image, self.TEXT_BYTES)
                self.assertEqual(mime, "image/jpeg")
                self.assertLess(w, original[0])
                self.assertEqual(decode(data).size, (w, h))  # the reported size is the real one
                self.assertAlmostEqual(w / h, original[0] / original[1], delta=0.02)  # rounding only
                self.assertTrue(self.fits(data, budget))

    def test_image_that_would_go_below_minimum_size_raises_image_too_large(self):
        image = noise_image((400, 200), "PNG")
        with small_budget(3 * 1024 * 1024, min_long_side=300), \
                mock.patch.object(chat, "base64_size", lambda n: 10 ** 9), \
                self.assertRaises(ImageTooLargeError) as raised:
            prepare_gemini_copy(image, self.TEXT_BYTES)
        # The message tells our safety limit apart from Gemini's API limit.
        self.assertIn("our safety limit is 3 MB", str(raised.exception))
        self.assertIn("Gemini's API limit is 100 MB", str(raised.exception))

    def test_png_over_raw_limit_skips_the_full_size_png_encode(self):
        # The limit is patched down so a small image stands in for a huge one (no big memory use).
        image, raw = noise_image((40, 30), "PNG"), 40 * 30 * 3
        for limit, first_format in [(raw - 1, "JPEG"), (raw, "PNG")]:
            with self.subTest(limit=limit), mock.patch.object(chat, "PNG_RAW_LIMIT", limit), \
                    mock.patch.object(chat, "encode", wraps=chat.encode) as encode:
                prepare_gemini_copy(image, self.TEXT_BYTES)
                self.assertEqual(encode.call_args_list[0].args[1], first_format)


# ---------- Colour modes ----------

class SafeRgbTest(unittest.TestCase):
    def test_other_modes_become_plain_rgb(self):
        palette = Image.new("P", (1, 1), 1)
        palette.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)  # index 1 = red
        sixteen_bit = Image.new("I;16", (1, 1))
        sixteen_bit.putpixel((0, 0), 32768)                   # mid gray, must not clip to white
        cases = [
            ("RGB", Image.new("RGB", (1, 1), (10, 20, 30)), (10, 20, 30)),
            ("grayscale", Image.new("L", (1, 1), 128), (128, 128, 128)),
            ("palette", palette, (255, 0, 0)),
            ("CMYK", Image.new("CMYK", (1, 1), (0, 255, 255, 0)), (255, 0, 0)),
            ("16-bit grayscale", sixteen_bit, (128, 128, 128)),
        ]
        for case, image, pixel in cases:
            with self.subTest(case=case):
                out = safe_rgb(image)
                self.assertEqual((out.mode, out.getpixel((0, 0))), ("RGB", pixel))

    def test_transparent_pixels_are_flattened_onto_white(self):
        rgba = Image.new("RGBA", (2, 1))
        rgba.putpixel((0, 0), (255, 0, 0, 0))    # fully transparent red
        rgba.putpixel((1, 0), (255, 0, 0, 255))  # opaque red stays red
        self.assertEqual([safe_rgb(rgba).getpixel((x, 0)) for x in (0, 1)], [(255, 255, 255), (255, 0, 0)])
        self.assertEqual(safe_rgb(Image.new("LA", (1, 1), (0, 0))).getpixel((0, 0)), (255, 255, 255))


# ---------- Original image protection ----------

class OriginalImageProtectionTest(TempDirTest):
    def test_preparing_and_drawing_never_change_the_original(self):
        image = noise_image((400, 200), "PNG")
        before = (image.size, image.mode, image.tobytes())
        with small_budget(60_000):
            prepare_gemini_copy(image, 1000)  # forced to convert and shrink
        draw_bounding_boxes(image, "in.png", self.path("out.png"), [{"label": "dog", "box": [0, 0, 1000, 1000]}])
        self.assertEqual((image.size, image.mode, image.tobytes()), before)


# ---------- Drawing ----------

class DrawBoundingBoxesTest(TempDirTest):
    W, H = 200, 100

    def draw(self, boxes, name="out.png"):
        image = Image.new("RGB", (self.W, self.H), "black")
        return draw_bounding_boxes(image, self.path("in.png"), self.path(name), boxes)

    def test_normalized_coordinates_convert_to_pixels(self):
        result = self.draw([{"label": "dog", "box": [250, 100, 750, 500]},   # [y_min, x_min, y_max, x_max]
                            {"label": "cat", "box": [0, 0, 500, 500]}])
        self.assertEqual(result["boxes"], [{"label": "dog", "box_px": [20, 25, 100, 75]},  # [x_min, y_min, x_max, y_max]
                                           {"label": "cat", "box_px": [0, 0, 100, 50]}])
        saved = open_rgb(result["output_path"])
        self.assertEqual(saved.getpixel((20, 60)), (255, 0, 0))  # dog's left edge is drawn
        self.assertEqual(saved.getpixel((60, 60)), (0, 0, 0))    # inside is untouched

    def test_coordinate_1000_is_clamped_to_the_last_pixel(self):
        result = self.draw([{"label": "all", "box": [0, 0, 1000, 1000]}])
        self.assertEqual(result["boxes"][0]["box_px"], [0, 0, self.W - 1, self.H - 1])
        saved = open_rgb(result["output_path"])
        self.assertEqual(saved.getpixel((self.W - 1, self.H // 2)), (255, 0, 0))  # right edge visible
        self.assertEqual(saved.getpixel((self.W // 2, self.H - 1)), (255, 0, 0))  # bottom edge visible

    def test_output_keeps_the_original_dimensions(self):
        for name in ["out.png", "out.jpg", "out.jpeg"]:
            with self.subTest(name=name):
                result = self.draw([{"label": "dog", "box": [0, 0, 1000, 1000]}], name=name)
                self.assertEqual(open_rgb(result["output_path"]).size, (self.W, self.H))

    def test_default_output_goes_to_output_dir(self):
        with mock.patch.object(chat, "OUTPUT_DIR", self.path("outputs")):
            result = draw_bounding_boxes(Image.new("RGB", (self.W, self.H)), "/somewhere/dog.png", None,
                                         [{"label": "dog", "box": [0, 0, 10, 10]}])
        self.assertEqual(result["output_path"], os.path.join(self.path("outputs"), "dog_boxes.png"))
        self.assertTrue(os.path.isfile(result["output_path"]))

    def test_existing_output_is_never_overwritten(self):
        paths = [self.draw([{"label": "dog", "box": [0, 0, 10, 10]}])["output_path"]]
        first_bytes = read_bytes(paths[0])
        paths += [self.draw([{"label": "cat", "box": [500, 500, 900, 900]}])["output_path"] for _ in range(2)]
        self.assertEqual(paths, [self.path("out.png"), self.path("out_2.png"), self.path("out_3.png")])
        self.assertEqual(read_bytes(paths[0]), first_bytes)


# ---------- Output paths ----------

class CheckOutputTest(TempDirTest):
    def test_only_supported_paths_are_accepted(self):
        image_path = self.path("dog.png")
        Image.new("RGB", (4, 4)).save(image_path)
        cases = [
            ("out.png", True), ("out.jpg", True), ("out.jpeg", True), ("OUT.PNG", True),
            ("out.gif", False), ("out.webp", False), ("out.txt", False), ("out", False), ("out.png.exe", False),
            (os.path.join("no_such_folder", "out.png"), False),  # folder missing
            ("dog.png", False),                                  # the input image itself
            (os.path.join(".", "dog.png"), False),               # same file, different spelling
        ]
        for name, valid in cases:
            with self.subTest(name=name):
                path = self.path(name)
                if valid:
                    check_output(path, image_path)
                else:
                    self.assertRaises(ValueError, check_output, path, image_path)


# ---------- Error classification ----------

def api_error(code, details=None):
    return genai_errors.APIError(code, {"error": {"code": code, "status": "STATUS", "message": "msg",
                                                  **({"details": details} if details else {})}})


class ExplainApiErrorTest(unittest.TestCase):
    def test_each_failure_gets_its_error_type(self):
        cases = [
            (ImageTooLargeError("too big"), "image_too_large"),
            (EmptyResponseError("no text"), "empty_response"),
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectTimeout("slow"), "timeout"),
            (httpx.ConnectError("down"), "network_error"),
            (ConnectionError("down"), "network_error"),
            (ConnectionRefusedError("down"), "network_error"),
            (api_error(400), "bad_request"),
            (api_error(400, [{"reason": "API_KEY_INVALID"}]), "invalid_api_key"),  # Gemini's bad-key 400
            (api_error(401), "invalid_api_key"),
            (api_error(403), "permission_denied"),
            (api_error(404), "model_not_found"),
            (api_error(429), "rate_limited"),
            (api_error(500), "server_error"),
            (api_error(503), "server_error"),
            (api_error(418), "api_error"),
            (KeyError("x"), "unexpected_error"),
        ]
        for error, error_type in cases:
            with self.subTest(error=repr(error)):
                result = explain_api_error(error)
                self.assertEqual(result["error_type"], error_type)
                self.assertTrue(result["error"])  # always a message for the user


# ---------- Gemini replies (fake client) ----------

class GeminiReplyTest(unittest.TestCase):
    """gemini_llm with the client swapped for a fake: no network, no key."""

    def call(self, response):
        fake = mock.Mock()
        fake.models.generate_content.return_value = response
        image = Image.new("RGB", (8, 8))
        image.format = "PNG"
        with mock.patch.object(chat, "client", return_value=fake):
            return chat.gemini_llm("system", [Message(Role.USER, "box the dog", image_path="dog.png")], image)

    def test_text_reply_is_returned(self):
        reply = '{"type":"final","message":"ok"}'
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text=reply)]))])
        self.assertEqual(self.call(response), reply)

    def test_closeups_are_sent_from_the_current_request_only(self):
        old = noise_image((600, 600))  # close-up from an earlier request: big, so counting it would show
        new = Image.new("RGB", (64, 32), "red")
        call = '{"type":"tool_call","name":"zoom_region","arguments":{"region":[0,0,500,500]}}'
        messages = [Message(Role.USER, "box the dog", image_path="dog.png"),
                    Message(Role.ASSISTANT, call),
                    Message(Role.TOOL, '{"success": true}', tool_name="zoom_region", image=old),
                    Message(Role.ASSISTANT, '{"type":"final","message":"done"}'),
                    Message(Role.USER, "now box the cat"),  # the current request starts here
                    Message(Role.ASSISTANT, call),
                    Message(Role.TOOL, '{"success": true}', tool_name="zoom_region", image=new)]
        fake = mock.Mock()
        fake.models.generate_content.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part.from_text(text='{"type":"final","message":"ok"}')]))])
        photo = Image.new("RGB", (8, 8))
        photo.format = "PNG"
        with mock.patch.object(chat, "client", return_value=fake), \
                mock.patch.object(chat, "prepare_gemini_copy", wraps=chat.prepare_gemini_copy) as prepare:
            chat.gemini_llm("system", messages, photo)

        contents = fake.models.generate_content.call_args.kwargs["contents"]
        sent = [(i, p.inline_data.mime_type, decode(p.inline_data.data).size)
                for i, content in enumerate(contents) for p in content.parts if p.inline_data]
        self.assertEqual(sent, [(0, "image/png", (8, 8)),        # the original photo, as before
                                (6, "image/jpeg", (64, 32))])   # only the current request's close-up, as JPEG
        text_bytes = prepare.call_args.args[1]
        self.assertGreaterEqual(text_bytes, base64_size(len(chat.encode(new, "JPEG"))))  # counted in the budget
        self.assertLess(text_bytes, base64_size(len(chat.encode(old, "JPEG"))))          # the old one is not

    def test_empty_reply_raises_empty_response_with_the_reason(self):
        cases = [
            ("blocked", types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(
                block_reason="PROHIBITED_CONTENT")), "blocked.*PROHIBITED_CONTENT"),
            ("stopped", types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="SAFETY")]), "SAFETY"),
            ("no reason", types.GenerateContentResponse(), "no reason"),
        ]
        for case, response, reason in cases:
            with self.subTest(case=case), self.assertRaisesRegex(EmptyResponseError, reason):
                self.call(response)


# ---------- Cropping ----------

class CropObjectTest(TempDirTest):
    """chat.crop_object: one file per box, cut from the original at the box's pixel region."""
    W, H = 200, 100

    def setUp(self):
        super().setUp()
        self.image = noise_image((self.W, self.H), "PNG")
        self.outputs = self.path("outputs")
        patcher = mock.patch.object(chat, "OUTPUT_DIR", self.outputs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def crop(self, boxes, output_path=None, image_path="/somewhere/dog.png"):
        return chat.crop_object(self.image, image_path, output_path, boxes)

    def out(self, name):
        return os.path.join(self.outputs, name)

    def test_each_box_is_saved_at_its_pixel_region(self):
        result = self.crop([box("dog", (250, 100, 750, 500)),   # [y_min, x_min, y_max, x_max] in 0-1000
                            box("tree", (0, 0, 1000, 1000)),
                            box("speck", (0, 0, 1, 1))])        # rounds to 0 pixels: still 1x1
        expected = [("dog", [20, 25, 100, 75], (80, 50)),          # [x_min, y_min, x_max, y_max], max exclusive
                    ("tree", [0, 0, self.W, self.H], (self.W, self.H)),
                    ("speck", [0, 0, 1, 1], (1, 1))]
        self.assertEqual(result["num_crops"], 3)
        for crop, (label, px, size) in zip(result["crops"], expected):
            with self.subTest(label=label):
                self.assertEqual((crop["label"], crop["box_px"], crop["output_path"]),
                                 (label, px, self.out(f"dog_{label}.png")))
                saved = open_rgb(crop["output_path"])
                self.assertEqual(saved.size, size)
                self.assertEqual(saved.tobytes(), self.image.crop(px).tobytes())  # the same pixels

    def test_message_lists_every_saved_file_and_pixel_box(self):
        result = self.crop([box("dog", (250, 100, 750, 500)), box("black cat", (0, 0, 500, 500))])
        self.assertEqual(result["message"],
                         f"Cropped 2 object(s): dog [20, 25, 100, 75] -> {self.out('dog_dog.png')}; "
                         f"black cat [0, 0, 100, 50] -> {self.out('dog_black_cat.png')}")

    def test_duplicate_labels_and_repeat_runs_get_new_files(self):
        first = self.crop([box("dog", (0, 0, 500, 500)), box("dog", (500, 500, 1000, 1000))])
        first_bytes = read_bytes(self.out("dog_dog.png"))
        again = self.crop([box("dog", (100, 100, 900, 900))])
        self.assertEqual([c["output_path"] for c in first["crops"] + again["crops"]],
                         [self.out("dog_dog.png"), self.out("dog_dog_2.png"), self.out("dog_dog_3.png")])
        self.assertEqual(read_bytes(self.out("dog_dog.png")), first_bytes)  # never overwritten

    def test_output_path_names_every_crop(self):
        result = self.crop([box("dog"), box("cat")], output_path=self.path("crop.jpg"))
        self.assertEqual([c["output_path"] for c in result["crops"]], [self.path("crop.jpg"), self.path("crop_2.jpg")])
        self.assertEqual(decode(read_bytes(self.path("crop.jpg"))).format, "JPEG")
        self.assertFalse(os.path.exists(self.outputs))

    def test_original_image_and_file_are_never_changed(self):
        original_path = self.path("dog.png")
        self.image.save(original_path)
        file_before, image_before = read_bytes(original_path), (self.image.size, self.image.mode, self.image.tobytes())
        self.crop([box("dog", (0, 0, 1000, 1000)), box("dog", (100, 100, 200, 200))], image_path=original_path)
        self.assertEqual((self.image.size, self.image.mode, self.image.tobytes()), image_before)
        self.assertEqual(read_bytes(original_path), file_before)

    def test_invalid_boxes_are_rejected_by_the_validator_before_cropping(self):
        agent = chat.Agent(tools=chat.tools, llm=None)  # real validator, no model
        for case, boxes in [("empty list", []), ("x/y out of range", [box(coords=(700, 0, 1200, 100))]),
                            ("y_min > y_max", [box(coords=(300, 200, 100, 400))]),
                            ("file name label", [box("dog.png")]), ("extra field", [{**GOOD, "path": "/tmp/x"}]),
                            ("float value", [box(coords=(1.0, 2, 3, 4))])]:
            with self.subTest(case=case):
                result = agent.call_tool({"name": "crop_object", "arguments": {"boxes": boxes}}, self.image, "dog.png")
                self.assertEqual((result["success"], result["error_type"]), (False, "invalid_arguments"))
        self.assertFalse(os.path.exists(self.outputs))  # nothing was saved

    def test_crop_always_covers_the_whole_requested_region(self):
        # Whatever valid box the model sends, every pixel it touches is kept, plus at most one partial pixel per side.
        rng = random.Random(0)
        for w, h in [(1, 1), (3, 7), (310, 194), (1246, 892), (1001, 999)]:
            boxes = [[0, 0, 1000, 1000], [0, 0, 1, 1], [999, 999, 1000, 1000], [1, 2, 3, 998]]
            for _ in range(40):
                (y0, y1), (x0, x1) = sorted(rng.sample(range(1001), 2)), sorted(rng.sample(range(1001), 2))
                boxes.append([y0, x0, y1, x1])
            validate_boxes(boxes)  # all boxes the model is allowed to send
            result = chat.crop_object(Image.new("RGB", (w, h), "blue"), f"/somewhere/{w}x{h}.png", None,
                                      [box("box", b) for b in boxes])
            for b, crop in zip(boxes, result["crops"]):
                with self.subTest(size=(w, h), box=b):
                    # The requested region in exact (fractional) pixels.
                    y0, x0, y1, x1 = (Fraction(v * side, 1000) for v, side in zip(b, (h, w, h, w)))
                    px0, py0, px1, py1 = crop["box_px"]
                    self.assertTrue(0 <= px0 <= x0 and 0 <= py0 <= y0)  # starts at or before the region
                    self.assertTrue(x1 <= px1 <= w and y1 <= py1 <= h)  # ends at or after it
                    self.assertTrue(x0 - px0 < 1 and y0 - py0 < 1 and px1 - x1 < 1 and py1 - y1 < 1)  # stays tight
                    with Image.open(crop["output_path"]) as saved:
                        self.assertEqual(saved.size, (px1 - px0, py1 - py0))

    def test_crop_tool_shares_the_box_validator_and_ends_the_turn(self):
        draw, crop = chat.tools[:2]
        self.assertEqual((draw.name, crop.name), ("draw_bounding_boxes", "crop_object"))
        self.assertIs(crop.validate, validate_boxes)
        self.assertIs(crop.parameter_schema, draw.parameter_schema)
        self.assertTrue(crop.return_direct)


class DrawLabelTest(TempDirTest):
    def test_image_label_is_the_name_and_message_has_pixel_coordinates(self):
        with mock.patch.object(chat.ImageDraw.ImageDraw, "text", autospec=True) as text:
            result = draw_bounding_boxes(Image.new("RGB", (200, 100)), "in.png", self.path("out.png"),
                                         [box("dog", (250, 100, 750, 500)), box("black cat", (0, 0, 500, 500))])
        self.assertEqual([c.args[2] for c in text.call_args_list], ["dog", "black cat"])  # names only on the image
        self.assertEqual(result["message"], "Drew 2 box(es): dog [20, 25, 100, 75]; black cat [0, 0, 100, 50]. "
                                            f"Saved to {self.path('out.png')}")


class HandleTest(unittest.TestCase):
    def test_trail_shows_when_a_picture_went_back_to_the_model(self):
        def run(messages, output_path=None):
            messages.append(Message(Role.TOOL, '{"success": true}', tool_name="zoom_region",
                                    image=Image.new("RGB", (1024, 732))))
            return Message(Role.ASSISTANT, "done")
        out = io.StringIO()
        with mock.patch.object(chat.agent, "run", side_effect=run), contextlib.redirect_stdout(out):
            chat.handle([], "box the dog", "dog.png")
        self.assertIn('[zoom_region] {"success": true} + image 1024x732', out.getvalue())


# ---------- Zooming ----------

def raised(function, *args):
    """The TypeError/ValueError that function(*args) raises (None if it raises nothing)."""
    try:
        function(*args)
    except (TypeError, ValueError) as e:
        return e
    return None


class ZoomRegionTest(TempDirTest):
    """chat.zoom_region, and the coordinate helpers it shares with drawing and cropping."""

    def test_validate_region_uses_the_same_coordinate_checks_as_boxes(self):
        cases = {"string": "1,2,3,4", "tuple": (1, 2, 3, 4), "3 values": [1, 2, 3], "float": [1.0, 2, 3, 4],
                 "true": [True, 2, 3, 4], "null": [None, 2, 3, 4], "below 0": [-1, 0, 10, 10],
                 "above 1000": [0, 0, 1001, 10], "y_min > y_max": [300, 200, 100, 400],
                 "x_min == x_max": [100, 200, 300, 200]}
        for case, coords in cases.items():
            with self.subTest(case=case):
                as_region = raised(chat.validate_region, coords)
                as_box = raised(validate_boxes, [{"label": "dog", "box": coords}])
                self.assertIsNotNone(as_region)
                self.assertEqual(type(as_region), type(as_box))
                self.assertEqual(str(as_region), str(as_box).replace("boxes[0].box", "region"))
        self.assertIsNone(chat.validate_region([0, 0, 1000, 1000]))
        with mock.patch.object(chat, "check_box", wraps=chat.check_box) as check:
            chat.validate_region([1, 2, 3, 4])
            validate_boxes([box("dog", (1, 2, 3, 4))])
        self.assertEqual(check.call_args_list, [mock.call([1, 2, 3, 4], "region"), mock.call([1, 2, 3, 4], "boxes[0].box")])
        zoom = next(t for t in chat.tools if t.name == "zoom_region")
        self.assertIs(zoom.validate, chat.validate_region)

    def test_closeup_holds_the_region_enlarged_to_1024_with_proportions_kept(self):
        image = Image.new("RGB", (400, 200), "blue")
        image.paste((255, 0, 0), (40, 50, 240, 150))  # a red block exactly where the region is
        before = image.tobytes()
        result = chat.zoom_region(image, {}, [250, 100, 750, 600])  # [y_min, x_min, y_max, x_max]
        self.assertEqual(result["region_px"], [40, 50, 240, 150])
        self.assertEqual(result["image"].size, (1024, 512))  # longer side 1024, still 2:1
        self.assertEqual(result["image"].getcolors(), [(1024 * 512, (255, 0, 0))])  # only the region, no blue
        self.assertEqual(image.tobytes(), before)  # the original is untouched

        noise = noise_image((400, 200))  # the exact pixels: the region cut out of the original, enlarged
        closeup = chat.zoom_region(noise, {}, [250, 100, 750, 600])["image"]
        self.assertEqual(closeup.tobytes(),
                         noise.crop((40, 50, 240, 150)).resize((1024, 512), Image.Resampling.LANCZOS).tobytes())
        tall = chat.zoom_region(noise, {}, [0, 0, 600, 125])  # 50x120 pixels: portrait
        self.assertEqual((tall["region_px"], tall["image"].size), ([0, 0, 50, 120], (427, 1024)))

    def test_zoom_result_describes_the_closeup_and_records_the_view(self):
        state = {}
        result = chat.zoom_region(noise_image((400, 200)), state, [250, 100, 750, 600])
        text = json.loads(json.dumps({k: v for k, v in result.items() if k != "image"}))  # the rest is plain JSON
        self.assertEqual((text["zoom"], text["max_zooms"], text["closeup_size"]), (1, 2, [1024, 512]))
        self.assertEqual(text["area"], [250.0, 100.0, 750.0, 600.0])
        for words in ["Zoom 1 of 2", "[250.0, 100.0, 750.0, 600.0]", "[40, 50, 240, 150]", "1024x512",
                      "relative to this close-up"]:
            with self.subTest(words=words):
                self.assertIn(words, text["message"])
        self.assertEqual((state["view"], state["zooms"]), ([250, 100, 750, 600], 1))

    def test_too_small_region_is_rejected(self):
        image = Image.new("RGB", (1000, 500))
        for region, side, ok in [([0, 0, 64, 500], "32 px tall", True), ([0, 0, 62, 500], "31 px tall", False),
                                 ([0, 0, 500, 32], "32 px wide", True), ([0, 0, 500, 31], "31 px wide", False)]:
            with self.subTest(side=side):
                state = {}
                result = chat.zoom_region(image, state, region)
                if ok:
                    self.assertEqual((result["success"], state["zooms"]), (True, 1))
                else:
                    self.assertEqual((result["success"], result["error_type"]), (False, "invalid_arguments"))
                    self.assertIn("at least 32 pixels", result["error"])
                    self.assertEqual(state, {})  # a rejected zoom changes nothing

    def test_third_zoom_gives_limit_reached(self):
        image, state = noise_image((400, 200)), {}
        first = chat.zoom_region(image, state, [0, 0, 1000, 1000])
        second = chat.zoom_region(image, state, [0, 0, 1000, 1000])
        view = state["view"]
        third = chat.zoom_region(image, state, [0, 0, 1000, 1000])
        self.assertEqual((first["zoom"], second["zoom"]), (1, 2))
        self.assertEqual((third["success"], third["error_type"]), (False, "limit_reached"))
        self.assertTrue(third["error"].startswith("Limit reached for zooming in"))
        self.assertIn("draw_bounding_boxes or crop_object", third["error"])  # tells the model what to do now
        self.assertNotIn("image", third)
        self.assertEqual((state["zooms"], state["view"]), (2, view))  # no third zoom happened

    def test_second_zoom_and_final_boxes_are_relative_to_the_latest_closeup(self):
        self.assertEqual(chat.to_full([100, 200, 300, 400], {}), [100, 200, 300, 400])  # no view: the whole photo
        self.assertEqual(chat.to_full([1, 1, 2, 2], {"view": [0, 0, 1, 1]}),             # exact, never rounded
                         [Fraction(1, 1000), Fraction(1, 1000), Fraction(2, 1000), Fraction(2, 1000)])
        image, state = noise_image((1000, 1000)), {}
        chat.zoom_region(image, state, [0, 0, 500, 500])                 # top-left quarter of the photo
        second = chat.zoom_region(image, state, [500, 500, 1000, 1000])  # bottom-right quarter of that close-up
        self.assertEqual((second["region_px"], state["view"]), ([250, 250, 500, 500], [250, 250, 500, 500]))
        self.assertEqual(chat.to_full([0, 0, 1000, 1000], state), [250, 250, 500, 500])
        # The whole latest close-up, boxed or cropped, lands on the same area of the original.
        whole = [box("dog", (0, 0, 1000, 1000))]
        drawn = draw_bounding_boxes(image, "in.png", self.path("out.png"), whole, state)
        cropped = chat.crop_object(image, "in.png", self.path("crop.png"), whole, state)
        self.assertEqual(drawn["boxes"][0]["box_px"], [250, 250, 500, 500])
        self.assertEqual(cropped["crops"][0]["box_px"], [250, 250, 500, 500])
        self.assertEqual(open_rgb(self.path("crop.png")).tobytes(), image.crop((250, 250, 500, 500)).tobytes())


class BoxFormatPromptTest(unittest.TestCase):
    def test_explains_how_to_choose_a_tool_with_a_valid_example_for_each(self):
        prompt = chat.BOX_FORMAT
        self.assertEqual(prompt.count("Bounding box format"), 1)  # the format is explained once
        for word in ["Choosing a tool", "box", "draw", "mark", "highlight", "crop", "cut out", "extract", "save just"]:
            with self.subTest(word=word):
                self.assertIn(word, prompt)
        examples = [line.split("Example: ", 1)[1] for line in prompt.splitlines() if "Example: " in line]
        self.assertEqual([parse_reply(e)["name"] for e in examples], [t.name for t in chat.tools])
        for example in examples:  # each example passes its own tool's validator
            call = parse_reply(example)
            next(t for t in chat.tools if t.name == call["name"]).validate(**call["arguments"])

    def test_explains_when_and_how_to_zoom(self):
        prompt = chat.BOX_FORMAT
        for text in ["zoom_region is optional", "at most 2 times per request",
                     "relative to the latest close-up, not the full photo", "only box things inside the close-up",
                     "exactly one of draw_bounding_boxes or crop_object"]:
            with self.subTest(text=text):
                self.assertIn(text, prompt)


if __name__ == "__main__":
    unittest.main()
