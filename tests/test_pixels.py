from __future__ import annotations

import colorsys
import random
import unittest
from unittest.mock import patch

from PIL import Image

from plotter_core import _classify
from plotter_pixels import classify_grid


def settings(**overrides):
    return dict(palette_mode="four", mono_pen="BLACK", mono_threshold=221,
                white_cutoff=245, threshold_red=20, threshold_blue=20,
                threshold_green=20, threshold_black=15, **overrides)


class PixelClassifierTests(unittest.TestCase):
    def assert_parity(self, pixels, config):
        image = Image.new("RGB", (len(pixels), 1))
        image.putdata(pixels)
        self.assertEqual(classify_grid(image, config, _classify)[0],
                         [_classify(pixel, config) for pixel in pixels])

    def test_random_pixels_and_thresholds_match_scalar(self):
        rng = random.Random(732)
        pixels = [tuple(rng.randrange(256) for _ in range(3)) for _ in range(20000)]
        for _ in range(8):
            config = settings()
            for color in ("red", "blue", "green", "black"):
                config[f"threshold_{color}"] = rng.uniform(0, 100)
            config["white_cutoff"] = rng.uniform(180, 255)
            self.assert_parity(pixels, config)
        config.update(palette_mode="mono", mono_pen="GREEN")
        for threshold in (0, 50, 127.5, 221, 255):
            config["mono_threshold"] = threshold
            self.assert_parity(pixels, config)

    def test_color_ties_and_boundary_pixels_match_scalar(self):
        pixels = [(r, g, b) for r in (0, 1, 38, 39, 180, 213, 214, 245, 255)
                  for g in (0, 1, 38, 39, 180, 213, 214, 245, 255)
                  for b in (0, 1, 38, 39, 180, 213, 214, 245, 255)]
        pixels.extend(tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(h / 360, 1, 1))
                      for h in range(360))
        config = settings()
        for cutoff in (180, 245, 255):
            config["white_cutoff"] = cutoff
            for threshold in (0, 20, 50, 100):
                for color in ("red", "green", "blue", "black"):
                    config[f"threshold_{color}"] = threshold
                self.assert_parity(pixels, config)
        # The pixel itself supplies thresholds exactly at arithmetic boundaries.
        for pixel in pixels[::23]:
            r, g, b = pixel
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            config = settings()
            config["threshold_black"] = 100 * (1 - lum / 255)
            hue, sat, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            for color, center in (("RED", 0), ("GREEN", 120), ("BLUE", 205)):
                distance = min(abs(hue * 360 - center), 360 - abs(hue * 360 - center))
                config[f"threshold_{color.lower()}"] = (
                    sat * max(0, 1 - distance / 100) * min(1, value / 0.15) * 100)
            self.assert_parity([pixel], config)
            config.update(palette_mode="mono", mono_threshold=lum)
            self.assert_parity([pixel], config)

    def test_scalar_fallback_and_non_rgb_input(self):
        image = Image.new("L", (3, 2))
        image.putdata([0, 30, 100, 180, 245, 255])
        config = settings()
        expected = [[_classify(image.convert("RGB").getpixel((x, y)), config)
                     for x in range(3)] for y in range(2)]
        with patch("plotter_pixels.np", None):
            self.assertEqual(classify_grid(image, config, _classify), expected)
        self.assertEqual(classify_grid(image, config, _classify), expected)

    def test_chunk_edges_preserve_rows(self):
        image = Image.new("RGB", (257, 256), (20, 40, 150))
        image.putpixel((256, 255), (255, 255, 255))
        result = classify_grid(image, settings(), _classify)
        self.assertEqual(len(result), 256)
        self.assertTrue(all(len(row) == 257 for row in result))
        self.assertEqual(result[-1][-2:], ["BLUE", None])


if __name__ == "__main__":
    unittest.main()
