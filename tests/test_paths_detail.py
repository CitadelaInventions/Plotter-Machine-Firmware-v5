"""Check planned ink geometry against source masks, independently of raster code."""

from __future__ import annotations

import math
import random
import unittest

from plotter_paths import join_strokes, order_for_travel, trace_color


def labels_for(cells, width, height):
    return [["BLACK" if (x, y) in cells else None for x in range(width)]
            for y in range(height)]


def point_segment_distance(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    projection = (max(0, min(1, ((point[0] - start[0]) * dx +
                                (point[1] - start[1]) * dy) / length_squared))
                  if length_squared else 0)
    return math.hypot(point[0] - start[0] - projection * dx,
                      point[1] - start[1] - projection * dy)


def enters_open_cell(start, end, cell):
    """Intersect a segment with the cell's open interior, excluding corner touch.

    This uses continuous geometry instead of the planner's raster traversal.
    A diagonal touching a white cell at exactly one corner is therefore legal.
    """
    first, last = 0.0, 1.0
    for axis in (0, 1):
        low, high = cell[axis] - 0.5, cell[axis] + 0.5
        delta = end[axis] - start[axis]
        if delta == 0:
            if not low < start[axis] < high:
                return False
            continue
        enter, leave = sorted(((low - start[axis]) / delta,
                               (high - start[axis]) / delta))
        first, last = max(first, enter), min(last, leave)
        if last - first <= 1e-12:
            return False
    return last - first > 1e-12


class PathDetailTests(unittest.TestCase):
    def assert_mask_fidelity(self, cells, width, height, *, tolerance=0.35,
                             join_budget=4, final_join=True):
        labels = labels_for(cells, width, height)
        paths = trace_color(labels, "BLACK", tolerance=tolerance, join_budget=join_budget)
        if final_join:
            paths = join_strokes(order_for_travel(paths), labels, "BLACK",
                                 join_budget, tolerance)
        self.assertTrue(paths)
        segments = [(start, end) for path in paths for start, end in zip(path, path[1:])]
        segments.extend((path[0], path[0]) for path in paths if len(path) == 1)
        allowed = min(0.45, max(0, tolerance)) + 1e-9
        for cell in sorted(cells):
            distance = min(point_segment_distance(cell, start, end)
                           for start, end in segments)
            self.assertLessEqual(distance, allowed,
                                 f"Source cell {cell} omitted; nearest ink is {distance:g} away")
        white = {(x, y) for y in range(height) for x in range(width)} - cells
        for start, end in segments:
            left, right = sorted((start[0], end[0]))
            top, bottom = sorted((start[1], end[1]))
            for cell in white:
                if left <= cell[0] <= right and top <= cell[1] <= bottom:
                    self.assertFalse(enters_open_cell(start, end, cell),
                                     f"Ink segment {start} → {end} crosses blank cell {cell}")
        return paths

    def test_one_cell_diagonal_is_one_continuous_stroke(self):
        cells = {(x, x) for x in range(2, 38)}
        paths = self.assert_mask_fidelity(cells, 40, 40)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(paths[0]), 2)

    def test_T_branch_is_connected_and_every_arm_is_covered(self):
        cells = {(x, 2) for x in range(2, 19)} | {(10, y) for y in range(2, 19)}
        paths = self.assert_mask_fidelity(cells, 21, 21)
        self.assertEqual(len(paths), 1)

    def test_Y_branch_can_retrace_without_cutting_across_white(self):
        cells = ({(15 - i, 15 - i) for i in range(14)} |
                 {(15 + i, 15 - i) for i in range(14)} |
                 {(15, y) for y in range(15, 29)})
        paths = self.assert_mask_fidelity(cells, 31, 31)
        self.assertEqual(len(paths), 1)

    def test_one_cell_neck_between_filled_areas_is_preserved(self):
        cells = ({(x, y) for x in range(2, 8) for y in range(5, 11)} |
                 {(x, y) for x in range(17, 23) for y in range(5, 11)} |
                 {(x, 8) for x in range(7, 18)})
        self.assert_mask_fidelity(cells, 25, 16, join_budget=8)

    def test_multiple_interior_holes_are_left_blank(self):
        cells = {(x, y) for x in range(2, 27) for y in range(2, 23)}
        holes = ({(x, y) for x in range(5, 10) for y in range(5, 11)} |
                 {(x, y) for x in range(17, 23) for y in range(12, 20)} |
                 {(14, y) for y in range(4, 10)})
        self.assert_mask_fidelity(cells - holes, 30, 26, join_budget=8)

    def test_island_inside_a_hole_stays_disconnected(self):
        cells = {(x, y) for x in range(2, 23) for y in range(2, 23)
                 if x < 6 or x > 18 or y < 6 or y > 18}
        cells |= {(x, y) for x in range(10, 15) for y in range(10, 15)}
        paths = self.assert_mask_fidelity(cells, 25, 25, join_budget=20)
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            inside = [10 <= x < 15 and 10 <= y < 15 for x, y in path]
            self.assertTrue(all(inside) or not any(inside))

    def test_disconnected_islands_are_not_joined_across_white(self):
        cells = ({(x, y) for x in range(2, 6) for y in range(2, 7)} |
                 {(x, y) for x in range(10, 14) for y in range(2, 7)} |
                 {(x, y) for x in range(6, 10) for y in range(12, 17)})
        paths = self.assert_mask_fidelity(cells, 18, 20, join_budget=30)
        self.assertEqual(len(paths), 3)

    def test_high_requested_tolerance_still_preserves_sub_cell_detail(self):
        cells = {(x, x // 2) for x in range(2, 28)}
        self.assert_mask_fidelity(cells, 31, 17, tolerance=10)

    def test_physical_tolerance_is_respected_with_unequal_grid_axis_pitch(self):
        from PIL import Image
        from plotter_core import build_job

        settings = {"bed_width": 20, "bed_height": 20, "draw_width": 7,
                    "draw_height": 14, "origin_x": 0, "origin_y": 0,
                    "spacing": 0.35, "steps_x": 80, "steps_y": 80,
                    "feed": 1200, "white_cutoff": 230, "path_tolerance": 0.2,
                    "servo_up": 20, "servo_red": 50, "servo_blue": 50,
                    "servo_green": 50, "servo_black": 50}
        for color in ("red", "blue", "green", "black"):
            settings[f"offset_{color}_x"] = settings[f"offset_{color}_y"] = 0
        cells = {(0, 0), (1, 0), (1, 1), (2, 1)}
        image = Image.new("RGB", (20, 20), "white")
        for point in cells:
            image.putpixel(point, (0, 0, 0))
        job = build_job(image, settings, full_page=True)
        for x, y in cells:
            physical_center = ((x + 0.5) * 7 / 20, (y + 0.5) * 14 / 20)
            distance = min(point_segment_distance(physical_center,
                           (segment.x1, segment.y1), (segment.x2, segment.y2))
                           for segment in job.segments)
            self.assertLessEqual(distance, settings["path_tolerance"] + 1e-9)

    def test_random_masks_keep_all_ink_centers_and_avoid_white_interiors(self):
        for seed in range(30):
            randomizer = random.Random(seed)
            cells = {(x, y) for y in range(10) for x in range(12)
                     if randomizer.random() < 0.57}
            with self.subTest(seed=seed):
                self.assert_mask_fidelity(cells, 12, 10, join_budget=6)


if __name__ == "__main__":
    unittest.main()
