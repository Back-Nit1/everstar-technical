"""Offline unit tests for chat.py. No Gemini, no API key, no network.
Importing chat builds the agent but makes no request. Files go only into temporary folders,
and settings (budgets, limits, OUTPUT_DIR) are changed only inside a test with unittest.mock.patch.

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import io
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

    def test_rejects_each_kind_of_mistake(self):
        cases = [
            # the list and its entries
            ("single object, not a list", GOOD, TypeError),
            ("empty list", [], ValueError),
            ("entry not an object", ["dog"], TypeError),
            ("missing field", [{"label": "dog"}], ValueError),
            ("extra field", [{**GOOD, "confidence": 0.9}], ValueError),
            # the label
            ("label not a string", [box(5)], TypeError),
            ("blank label", [box("   ")], ValueError),
            ("underscore in one label spoils the list", [GOOD, box("black_cat")], ValueError),
            ("file name as label", [box("dog.png")], ValueError),
            ("label over 40 characters", [box("x" * 41)], ValueError),
            # the coordinates
            ("box not a list", [{"label": "dog", "box": "1,2,3,4"}], TypeError),
            ("3 values", [box(coords=(1, 2, 3))], ValueError),
            ("float value", [box(coords=(1.0, 2, 3, 4))], TypeError),
            ("above 1000", [box(coords=(0, 0, 1001, 10))], ValueError),
            ("y_min == y_max", [box(coords=(100, 200, 100, 400))], ValueError),
        ]
        for case, boxes, error in cases:
            with self.subTest(case=case), self.assertRaises(error):
                validate_boxes(boxes)


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


# ---------- Gemini copy: size and budget ----------

class PrepareGeminiCopyTest(unittest.TestCase):
    TEXT_BYTES = 1000

    def fits(self, data, budget, overhead=100):
        return base64_size(len(data)) + self.TEXT_BYTES + overhead <= budget

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

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(chat, "OUTPUT_DIR", self.path("outputs"))  # crops land in the temp folder
        patcher.start()
        self.addCleanup(patcher.stop)

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


# ---------- Zooming ----------

class ZoomRegionTest(TempDirTest):
    """chat.zoom_region, and the coordinate helpers it shares with drawing and cropping."""

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

    def test_rejected_zoom_changes_nothing(self):
        # Bad regions are turned away by validate_region before zoom_region runs.
        for case, region, error in [("float", [0, 0, 500.5, 500], TypeError),
                                    ("above 1000", [0, 0, 1001, 500], ValueError),
                                    ("y_min > y_max", [500, 0, 100, 500], ValueError)]:
            with self.subTest(case=case), self.assertRaises(error):
                chat.validate_region(region)

        # A valid region under 32 pixels on a side is rejected; exactly 32 is fine.
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
                    self.assertEqual(state, {})

        # A third zoom is refused and the view stays on the second close-up.
        image, state = noise_image((400, 200)), {}
        chat.zoom_region(image, state, [0, 0, 1000, 1000])
        chat.zoom_region(image, state, [0, 0, 1000, 1000])
        view = state["view"]
        third = chat.zoom_region(image, state, [0, 0, 1000, 1000])
        self.assertEqual((third["success"], third["error_type"]), (False, "limit_reached"))
        self.assertNotIn("image", third)
        self.assertEqual((state["zooms"], state["view"]), (2, view))

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
        examples = [line.split("Example: ", 1)[1] for line in chat.BOX_FORMAT.splitlines() if "Example: " in line]
        self.assertEqual([parse_reply(e)["name"] for e in examples], [t.name for t in chat.tools])  # one per tool
        for example in examples:  # each example passes its own tool's validator
            call = parse_reply(example)
            next(t for t in chat.tools if t.name == call["name"]).validate(**call["arguments"])

if __name__ == "__main__":
    unittest.main()
