"""Exact byte parity for accelerated composition color opacity selection."""
from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from PIL import Image

from plotter_core import _classify
from plotter_pixels import COLORS, apply_layer_opacity


def settings(mode="four"):
    return dict(palette_mode=mode, mono_pen="BLACK", mono_threshold=221,
                white_cutoff=245, threshold_red=20, threshold_blue=20,
                threshold_green=20, threshold_black=15)


def reference(image, config, opacity):
    # Keep the former compose_page loop independent of the optimized helper.
    output = image.convert("RGBA").copy()
    four = {**config, "palette_mode": "four"}
    pixels = []
    for r, g, b, alpha in output.getdata():
        color = _classify((r, g, b), four) if alpha else None
        if (color is None and alpha and config["palette_mode"] == "mono" and
                _classify((r, g, b), config) is not None):
            color = "BLACK"
        pixels.append((r, g, b, round(alpha * opacity[color]) if color else 0))
    output.putdata(pixels)
    return output


class CompositionPixelTests(unittest.TestCase):
    def test_random_color_alpha_and_thresholds_have_exact_byte_parity(self):
        rng = random.Random(7451)
        image = Image.new("RGBA", (257, 256))  # Cross the processing chunk boundary.
        image.putdata([tuple(rng.randrange(256) for _ in range(4))
                       for _ in range(image.width * image.height)])
        original = image.tobytes()
        for mode in ("four", "mono"):
            for _ in range(3):
                config = settings(mode)
                config["mono_threshold"] = rng.uniform(0, 255)
                config["white_cutoff"] = rng.uniform(180, 255)
                for color in COLORS[1:]:
                    config[f"threshold_{color.lower()}"] = rng.uniform(0, 100)
                opacity = {color: rng.random() * rng.random() for color in COLORS[1:]}
                actual = apply_layer_opacity(image, config, opacity, _classify)
                self.assertEqual(actual.tobytes(), reference(image, config, opacity).tobytes())
        self.assertEqual(image.tobytes(), original)

    def test_faint_monochrome_detail_and_bankers_rounding_use_black_slot(self):
        config = settings("mono")
        config.update(mono_threshold=230, mono_pen="GREEN")
        opacity = dict(RED=0, BLUE=0.5, GREEN=1, BLACK=0.5)
        image = Image.new("RGBA", (8, 1))
        image.putdata([(225, 225, 225, alpha) for alpha in (0, 1, 2, 3, 127, 128, 254, 255)])
        output = apply_layer_opacity(image, config, opacity, _classify)
        self.assertEqual([pixel[3] for pixel in output.getdata()], [0, 0, 1, 2, 64, 64, 127, 128])
        self.assertEqual(output.tobytes(), reference(image, config, opacity).tobytes())
        # Monochrome still honors the original red color's own opacity slot.
        red = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
        self.assertEqual(apply_layer_opacity(red, config, opacity, _classify).getpixel((0, 0))[3], 0)

    def test_composited_layers_match_with_color_opacity_slots(self):
        rng = random.Random(821)
        layers = []
        for position in ((0, 0), (11, 7), (5, 25)):
            image = Image.new("RGBA", (43, 33))
            image.putdata([tuple(rng.randrange(256) for _ in range(4)) for _ in range(43 * 33)])
            layers.append((position, image))
        for mode in ("four", "mono"):
            for opacity in (dict.fromkeys(COLORS[1:], 0), dict.fromkeys(COLORS[1:], 1),
                            dict(RED=0.18, BLUE=0.64, GREEN=0.37, BLACK=0.91)):
                expected = Image.new("RGBA", (70, 70), "white")
                actual = expected.copy()
                for position, image in layers:
                    expected.alpha_composite(reference(image, settings(mode), opacity), position)
                    actual.alpha_composite(apply_layer_opacity(image, settings(mode), opacity, _classify), position)
                self.assertEqual(actual.convert("RGB").tobytes(), expected.convert("RGB").tobytes())

    def test_no_numpy_fallback_matches_acceleration_and_rgb_input(self):
        image = Image.new("RGB", (7, 1))
        image.putdata([(0, 0, 0), (255, 0, 0), (0, 0, 255), (0, 255, 0),
                       (225, 225, 225), (245, 245, 245), (255, 255, 255)])
        opacity = dict(RED=0.2, BLUE=0.4, GREEN=0.6, BLACK=0.8)
        for mode in ("four", "mono"):
            config = settings(mode)
            accelerated = apply_layer_opacity(image, config, opacity, _classify)
            with patch("plotter_pixels.np", None):
                fallback = apply_layer_opacity(image, config, opacity, _classify)
            self.assertEqual(fallback.tobytes(), accelerated.tobytes())
            self.assertEqual(fallback.tobytes(), reference(image, config, opacity).tobytes())


if __name__ == "__main__":
    unittest.main()
