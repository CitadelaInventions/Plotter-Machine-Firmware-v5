"""Deterministic nearest-endpoint routing without a quadratic path scan.

The index keeps both endpoints of an open stroke in a balanced spatial tree.
Completed strokes are removed from subtree counts, so an exhausted subtree is
skipped immediately. No third-party spatial-index dependency is required.
"""

from __future__ import annotations

from collections import defaultdict


Point = tuple[int, int]
_Endpoint = tuple[int, int, int, bool]
_BUCKET_SIZE = 16


class _Node:
    __slots__ = ("bounds", "count", "first_key", "parent", "left", "right", "entries")

    def __init__(self, parent: _Node | None = None):
        self.parent = parent
        self.left: _Node | None = None
        self.right: _Node | None = None
        self.entries: list[_Endpoint] = []
        self.bounds = (0, 0, 0, 0)
        self.count = 0
        self.first_key = (float("inf"), False)


def _box_distance(bounds: tuple[int, int, int, int], point: Point) -> int:
    left, top, right, bottom = bounds
    x, y = point
    dx = max(left - x, 0, x - right)
    dy = max(top - y, 0, y - bottom)
    return dx * dx + dy * dy


class _EndpointIndex:
    def __init__(self, paths: list[list[Point]]):
        self.active = bytearray([1]) * len(paths)
        self.leaves: list[list[_Node]] = [[] for _ in paths]
        entries: list[_Endpoint] = []
        for index, path in enumerate(paths):
            if not path:
                raise ValueError("A plot path must contain at least one point.")
            entries.append((*path[0], index, False))
            if path[-1] != path[0]:
                entries.append((*path[-1], index, True))
        self.root = self._build(entries, 0)

    def _build(self, entries: list[_Endpoint], depth: int,
               parent: _Node | None = None) -> _Node:
        node = _Node(parent)
        node.count = len(entries)
        if len(entries) <= _BUCKET_SIZE:
            node.entries = entries
            node.first_key = min((entry[2], entry[3]) for entry in entries)
            node.bounds = (min(p[0] for p in entries), min(p[1] for p in entries),
                           max(p[0] for p in entries), max(p[1] for p in entries))
            for entry in entries:
                self.leaves[entry[2]].append(node)
            return node
        axis = depth % 2
        entries.sort(key=lambda entry: (entry[axis], entry[1 - axis], entry[2], entry[3]))
        middle = len(entries) // 2
        node.left = self._build(entries[:middle], depth + 1, node)
        node.right = self._build(entries[middle:], depth + 1, node)
        a, b = node.left.bounds, node.right.bounds
        node.bounds = (min(a[0], b[0]), min(a[1], b[1]),
                       max(a[2], b[2]), max(a[3], b[3]))
        node.first_key = min(node.left.first_key, node.right.first_key)
        return node

    def nearest(self, point: Point) -> tuple[int, bool]:
        # The final two fields reproduce the original greedy scan's tie rules:
        # earlier input path first, and its first endpoint before its last.
        best = (float("inf"), len(self.active), False)
        pending = [(0, self.root)]
        while pending:
            lower_bound, node = pending.pop()
            if not node.count or (lower_bound, *node.first_key) >= best:
                continue
            if node.entries:
                for x, y, index, reverse in node.entries:
                    if self.active[index]:
                        candidate = ((x - point[0]) ** 2 + (y - point[1]) ** 2,
                                     index, reverse)
                        if candidate < best:
                            best = candidate
                continue
            children = []
            for child in (node.left, node.right):
                if child is not None and child.count:
                    distance = _box_distance(child.bounds, point)
                    if distance <= best[0]:
                        children.append((distance, child))
            # Push the farther child first so the nearer child tightens the
            # bound before the second one is considered.
            children.sort(key=lambda item: (item[0], item[1].first_key), reverse=True)
            pending.extend(children)
        return best[1], best[2]

    def remove(self, index: int) -> None:
        self.active[index] = 0
        # Endpoints may share a leaf. Count both removals but walk each unique
        # ancestor chain only once for that leaf.
        for leaf, count in _leaf_counts(self.leaves[index]):
            leaf.first_key = min(((entry[2], entry[3]) for entry in leaf.entries
                                  if self.active[entry[2]]),
                                 default=(float("inf"), False))
            node: _Node | None = leaf
            while node is not None:
                node.count -= count
                if node.left is not None and node.right is not None:
                    node.first_key = min(node.left.first_key, node.right.first_key)
                node = node.parent


def _leaf_counts(leaves: list[_Node]):
    counts: dict[_Node, int] = defaultdict(int)
    for leaf in leaves:
        counts[leaf] += 1
    return counts.items()


def order_for_travel(paths: list[list[Point]], start: Point = (0, 0)) -> list[list[Point]]:
    """Visit the nearest remaining stroke endpoint, reversing open strokes.

    Equal distances choose the earliest original path, preserving the former
    exhaustive greedy planner's results. Input paths are not modified. Unlike
    that planner, this function also orders drawings with thousands of strokes.
    """
    if not paths:
        return []
    index = _EndpointIndex(paths)
    ordered: list[list[Point]] = []
    current = start
    for _ in paths:
        path_index, reverse = index.nearest(current)
        chosen = paths[path_index]
        if reverse:
            chosen = list(reversed(chosen))
        ordered.append(chosen)
        current = chosen[-1]
        index.remove(path_index)
    return ordered
