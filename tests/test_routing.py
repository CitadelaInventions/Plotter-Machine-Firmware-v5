from __future__ import annotations

import random
import unittest

from plotter_routing import order_for_travel


def exhaustive_order(paths, start=(0, 0)):
    remaining = list(enumerate(paths))
    result = []
    current = start
    while remaining:
        best = (float("inf"), len(paths), False)
        best_remaining = 0
        for position, (original, path) in enumerate(remaining):
            for reverse, point in ((False, path[0]), (True, path[-1])):
                candidate = ((point[0] - current[0]) ** 2 + (point[1] - current[1]) ** 2,
                             original, reverse)
                if candidate < best:
                    best, best_remaining = candidate, position
        _, chosen = remaining.pop(best_remaining)
        if best[2]:
            chosen = list(reversed(chosen))
        result.append(chosen)
        current = chosen[-1]
    return result


class RoutingTests(unittest.TestCase):
    def test_matches_exhaustive_greedy_for_random_open_and_closed_paths(self):
        for seed in range(30):
            randomizer = random.Random(seed)
            paths = []
            for _ in range(100):
                path = [(randomizer.randrange(-12, 13), randomizer.randrange(-12, 13))
                        for _ in range(randomizer.randrange(1, 6))]
                if len(path) > 2 and randomizer.randrange(3) == 0:
                    path[-1] = path[0]
                paths.append(path)
            start = (randomizer.randrange(-15, 16), randomizer.randrange(-15, 16))
            self.assertEqual(order_for_travel(paths, start), exhaustive_order(paths, start))

    def test_equal_distance_prefers_input_order_and_forward_endpoint(self):
        paths = [[(-1, 0), (1, 0)], [(0, 1)], [(0, -1)]]
        self.assertEqual(order_for_travel(paths), exhaustive_order(paths))
        self.assertEqual(order_for_travel(paths)[0], paths[0])

    def test_reverses_open_strokes_without_mutating_input(self):
        paths = [[(8, 0), (3, 0)], [(10, 0), (9, 0)]]
        original = [path[:] for path in paths]
        self.assertEqual(order_for_travel(paths), [[(3, 0), (8, 0)], [(9, 0), (10, 0)]])
        self.assertEqual(paths, original)

    def test_orders_more_than_three_thousand_isolated_points(self):
        paths = [[(x, 0)] for x in range(4097)]
        random.Random(82).shuffle(paths)
        self.assertEqual(order_for_travel(paths, (-1, 0)), [[(x, 0)] for x in range(4097)])

    def test_duplicate_points_are_all_retained(self):
        paths = [[(3, 3)]] * 80 + [[(4, 4), (3, 3)]] * 50
        self.assertEqual(order_for_travel(paths), exhaustive_order(paths))
        self.assertEqual(len(order_for_travel(paths)), len(paths))

    def test_thousands_of_coincident_endpoints_keep_input_tie_order(self):
        paths = [[(3, 3), (3, 3), (3, 3)] for _ in range(4097)]
        ordered = order_for_travel(paths)
        self.assertEqual([id(path) for path in ordered], [id(path) for path in paths])

    def test_empty_input(self):
        self.assertEqual(order_for_travel([]), [])

    def test_empty_stroke_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one point"):
            order_for_travel([[]])


if __name__ == "__main__":
    unittest.main()
