"""Bounded, optional NumPy acceleration for the existing palette classifier."""
from __future__ import annotations

try:
    import numpy as np
except ImportError:  # The desktop app remains usable without this optional library.
    np = None

# Float64 deliberately follows colorsys.rgb_to_hsv and _classify operation order;
# using float32 changes classifications at user-selected threshold boundaries.
CHUNK_PIXELS = 65536
COLORS = (None, "RED", "BLUE", "GREEN", "BLACK")


def _classify_codes(image, settings: dict):
    """Classify an RGB image into compact codes using bounded float arrays."""
    pixels = np.asarray(image).reshape(-1, 3)
    codes = np.empty(len(pixels), dtype=np.uint8)
    for start in range(0, len(pixels), CHUNK_PIXELS):
        rgb = pixels[start:start + CHUNK_PIXELS].astype(np.float64)
        r, g, b = rgb.T
        luminance = (0.2126 * r + 0.7152 * g) + 0.0722 * b
        code = np.zeros(len(rgb), dtype=np.uint8)
        if settings["palette_mode"] == "mono":
            code[luminance <= settings["mono_threshold"]] = COLORS.index(settings["mono_pen"])
        else:
            near_white = ((r + g + b) / 3 >= settings["white_cutoff"]) & (
                rgb.max(axis=1) - rgb.min(axis=1) < 42)
            normalized = rgb / 255
            nr, ng, nb = normalized.T
            maximum = normalized.max(axis=1)
            minimum = normalized.min(axis=1)
            delta = maximum - minimum
            chromatic = delta != 0
            saturation = np.divide(delta, maximum, out=np.zeros_like(delta), where=chromatic)
            rc = np.divide(maximum - nr, delta, out=np.zeros_like(delta), where=chromatic)
            gc = np.divide(maximum - ng, delta, out=np.zeros_like(delta), where=chromatic)
            bc = np.divide(maximum - nb, delta, out=np.zeros_like(delta), where=chromatic)
            hue = np.where(nr == maximum, bc - gc,
                           np.where(ng == maximum, 2.0 + rc - bc, 4.0 + gc - rc))
            angle = ((hue / 6.0) % 1.0) * 360
            best = np.zeros_like(delta)
            # max((score, color)) uses color-name lexical order to break ties.
            for color, center in (("BLUE", 205), ("GREEN", 120), ("RED", 0)):
                distance = np.abs(angle - center)
                distance = np.minimum(distance, 360 - distance)
                score = (saturation * np.maximum(0.0, 1.0 - distance / 100.0) *
                         np.minimum(1.0, maximum / 0.15))
                selected = ((score > 0) &
                            (score * 100 >= settings[f"threshold_{color.lower()}"]) &
                            (score >= best))
                code[selected] = COLORS.index(color)
                best[selected] = score[selected]
            darkness = 100 * (1 - luminance / 255)
            code[(code == 0) & (darkness >= settings["threshold_black"])] = 4
            code[near_white] = 0
        codes[start:start + len(rgb)] = code
    return codes.reshape(image.height, image.width)


def classify_grid(image, settings: dict, scalar_classifier) -> list[list[str | None]]:
    """Return palette labels, preserving scalar behavior and string tie breaks.

    Working arrays are limited to 65,536 pixels at a time. The returned lists
    have the same shape/type as the original scalar image classifier.
    """
    image = image.convert("RGB") if image.mode != "RGB" else image
    if np is None:
        return [[scalar_classifier(image.getpixel((x, y)), settings)
                 for x in range(image.width)] for y in range(image.height)]
    lookup = np.asarray(COLORS, dtype=object)
    return [lookup[row].tolist() for row in _classify_codes(image, settings)]


def apply_layer_opacity(image, settings: dict, opacity: dict, scalar_classifier):
    """Return RGBA pixels with the composition's existing color opacity rules.

    Colored pixels use their four-color opacity slot even in monochrome mode.
    A neutral pixel rejected by that palette but accepted by the monochrome
    threshold uses BLACK's slot. RGB bytes and Python's round-to-even behavior
    are preserved; the input image is not modified.
    """
    from PIL import Image

    image = image.convert("RGBA") if image.mode != "RGBA" else image
    four_settings = {**settings, "palette_mode": "four"}
    if np is None:
        pixels = []
        for r, g, b, alpha in image.getdata():
            color = scalar_classifier((r, g, b), four_settings) if alpha else None
            if (color is None and alpha and settings["palette_mode"] == "mono" and
                    scalar_classifier((r, g, b), settings) is not None):
                color = "BLACK"
            pixels.append((r, g, b, round(alpha * opacity[color]) if color else 0))
        output = image.copy()
        output.putdata(pixels)
        return output
    rgb = image.convert("RGB")
    codes = _classify_codes(rgb, four_settings)
    if settings["palette_mode"] == "mono":
        monochrome = _classify_codes(rgb, settings)
        codes[(codes == 0) & (monochrome != 0)] = 4
    pixels = np.array(image)
    flattened = pixels.reshape(-1, 4)
    flattened_codes = codes.reshape(-1)
    factors = np.asarray((0, *(opacity[color] for color in COLORS[1:])), dtype=np.float64)
    for start in range(0, len(flattened), CHUNK_PIXELS):
        chunk = flattened[start:start + CHUNK_PIXELS]
        selected = flattened_codes[start:start + len(chunk)]
        chunk[:, 3] = np.rint(chunk[:, 3] * factors[selected]).astype(np.uint8)
    return Image.fromarray(pixels)
