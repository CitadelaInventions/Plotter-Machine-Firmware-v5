"""Plan connected, contour-following paths through a classified image grid."""

from __future__ import annotations

from collections import defaultdict, deque
from heapq import heappop, heappush
from math import hypot


Point = tuple[int, int]
_DIRECTIONS = {(1, 0): 0, (1, 1): 1, (0, 1): 2, (-1, 1): 3,
               (-1, 0): 4, (-1, -1): 5, (0, -1): 6, (1, -1): 7}
_TURN_RANK = (0, 1, 3, 5, 7, 6, 4, 2)


# Shared spatial routing remains compatible with callers importing this module.
from plotter_routing import order_for_travel


def _neighbors(index: int, width: int, height: int):
    x, y = index % width, index // width
    if x + 1 < width:
        yield index + 1
    if y + 1 < height:
        yield index + width
    if x:
        yield index - 1
    if y:
        yield index - width


def _neighbors8(index: int, width: int, height: int):
    yield from _neighbors(index, width, height)
    x, y = index % width, index // width
    if y:
        if x: yield index - width - 1
        if x + 1 < width: yield index - width + 1
    if y + 1 < height:
        if x: yield index + width - 1
        if x + 1 < width: yield index + width + 1


def _direction(a: int, b: int, width: int) -> int:
    dx, dy = b % width - a % width, b // width - a // width
    return _DIRECTIONS[(dx, dy)]


def _join_points(a: int, b: int, occupied: bytearray, width: int) -> list[int] | None:
    ax, ay = a % width, a // width
    bx, by = b % width, b // width
    dx, dy = bx - ax, by - ay
    if max(abs(dx), abs(dy)) > 1:
        return None
    if not dx and not dy:
        return []
    if abs(dx) == abs(dy) == 1:
        # Cell centers in a thin diagonal meet at the shared corner. No white
        # cell interior is crossed; requiring side cells breaks diagonal ink.
        return [b]
    return [b]


def _simplify(points: list[int], width: int) -> list[Point]:
    """Keep only corners and endpoints of a cell-center path."""
    if len(points) <= 2:
        return [(index % width, index // width) for index in points]
    result = [points[0]]
    previous_direction = None
    for before, after in zip(points, points[1:]):
        dx = (after % width) - (before % width)
        dy = (after // width) - (before // width)
        direction = (dx, dy)
        if previous_direction is not None and direction != previous_direction:
            result.append(before)
        previous_direction = direction
    result.append(points[-1])
    return [(index % width, index // width) for index in result]


def _line_inside(start: Point, end: Point, occupied: bytearray, width: int) -> bool:
    """A supercover raster check prevents a shortcut from crossing blank paper."""
    x, y = start
    end_x, end_y = end
    dx, dy = end_x - x, end_y - y
    nx, ny = abs(dx), abs(dy)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    ix = iy = 0
    while ix < nx or iy < ny:
        decision = (1 + 2 * ix) * ny - (1 + 2 * iy) * nx
        if decision == 0:
            # Ignore zero-area contact with the two side cells at an exact
            # corner, while checking every cell the centerline enters.
            x += sx
            y += sy
            ix += 1
            iy += 1
        elif decision < 0:
            x += sx
            ix += 1
        else:
            y += sy
            iy += 1
        if not occupied[y * width + x]:
            return False
    return True


def _shorten(points: list[Point], occupied: bytearray, width: int,
             tolerance: float = 0.35) -> list[Point]:
    """Bound deviation of every removed center; never cross blank cell interiors."""
    if len(points) < 3:
        return points
    tolerance_squared = min(0.45, max(0.0, tolerance)) ** 2
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        first, last = pending.pop()
        if last - first <= 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        length_squared = (bx - ax) ** 2 + (by - ay) ** 2
        farthest = first + 1
        greatest = -1.0
        for index in range(first + 1, last):
            px, py = points[index]
            if length_squared:
                projection = max(0.0, min(1.0,
                    ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length_squared))
                distance_squared = (px - ax - projection * (bx - ax)) ** 2 + \
                                   (py - ay - projection * (by - ay)) ** 2
            else:
                distance_squared = (px - ax) ** 2 + (py - ay) ** 2
            if distance_squared > greatest:
                greatest = distance_squared
                farthest = index
        if greatest <= tolerance_squared and _line_inside(points[first], points[last], occupied, width):
            continue
        if greatest <= tolerance_squared:
            farthest = (first + last) // 2
        keep.add(farthest)
        pending.extend(((first, farthest), (farthest, last)))
    return [points[index] for index in sorted(keep)]


def _connect_inside(a: int, b: int, occupied: bytearray, width: int,
                    extra_distance: float) -> list[int] | None:
    immediate = _join_points(a, b, occupied, width)
    if immediate is not None:
        return immediate
    start, end = (a % width, a // width), (b % width, b // width)
    if _line_inside(start, end, occupied, width):
        return [b]
    # A little retracing often costs less than two servo waits. Bound A* work
    # and detour length; white gaps remain pen-up travel, never drawn bridges.
    height = len(occupied) // width
    direct = hypot(end[0] - start[0], end[1] - start[1])
    maximum = (direct + max(0.0, extra_distance)) * 1000
    unseen_cost = maximum + 1
    expansion_limit = min(4096, max(64, int(maximum / 1000) * 24))
    pending = [(direct * 1000, 0, a)]
    cost = {a: 0}
    parent: dict[int, int] = {}
    expanded = 0
    while pending and expanded < expansion_limit:
        _, travelled, point = heappop(pending)
        if travelled != cost.get(point):
            continue
        if point == b:
            result = [b]
            while parent[result[-1]] != a:
                result.append(parent[result[-1]])
            result.reverse()
            return result
        expanded += 1
        x, y = point % width, point // width
        for neighbor in _neighbors8(point, width, height):
            if not occupied[neighbor]:
                continue
            nx, ny = neighbor % width, neighbor // width
            candidate = travelled + (1414 if nx != x and ny != y else 1000)
            if candidate >= cost.get(neighbor, unseen_cost):
                continue
            estimate = hypot(end[0] - nx, end[1] - ny) * 1000
            if candidate + estimate > maximum:
                continue
            cost[neighbor] = candidate
            parent[neighbor] = point
            heappush(pending, (candidate + estimate, candidate, neighbor))
    return None


def join_strokes(paths: list[list[Point]], labels: list[list[str | None]], color: str,
                 extra_distance: float, tolerance: float) -> list[list[Point]]:
    """Join nearby ordered strokes by drawing only within existing ink."""
    if len(paths) < 2:
        return paths
    width = len(labels[0])
    occupied = bytearray(cell == color for row in labels for cell in row)
    joined = [list(paths[0])]
    for path in paths[1:]:
        a, b = joined[-1][-1], path[0]
        # A disconnected island cannot benefit from a far-away graph search.
        if hypot(b[0] - a[0], b[1] - a[1]) > max(8, extra_distance * 4):
            joined.append(list(path))
            continue
        connector = _connect_inside(a[1] * width + a[0], b[1] * width + b[0],
                                    occupied, width, extra_distance)
        if connector is None:
            joined.append(list(path))
        else:
            # Simplify only the new connector. Re-simplifying an already
            # shortened stroke could compound its original error bound.
            link = [a] + [(point % width, point // width) for point in connector]
            joined[-1].extend(_shorten(link, occupied, width, tolerance)[1:])
            joined[-1].extend(path[1:])
    return joined


def trace_color(labels: list[list[str | None]], color: str, *,
                tolerance: float = 0.35, join_budget: float = 4) -> list[list[Point]]:
    """Follow successive edge-distance contours, preserving every colored cell.

    The breadth-first distance transform gives each cell its distance from the
    nearest blank cell. Following equal-distance bands creates nested contours
    around solid areas instead of sweeping the page in horizontal rows.
    """
    height, width = len(labels), len(labels[0])
    occupied = bytearray(cell == color for row in labels for cell in row)
    cells = [index for index, value in enumerate(occupied) if value]
    if not cells:
        return []

    distance = [0] * len(occupied)
    queue = deque()
    for index in cells:
        x, y = index % width, index // width
        if (x == 0 or x == width - 1 or y == 0 or y == height - 1 or
                not all(occupied[neighbor] for neighbor in _neighbors(index, width, height))):
            distance[index] = 1
            queue.append(index)
    while queue:
        index = queue.popleft()
        for neighbor in _neighbors(index, width, height):
            if occupied[neighbor] and not distance[neighbor]:
                distance[neighbor] = distance[index] + 1
                queue.append(neighbor)

    visited = bytearray(len(occupied))
    result: list[list[Point]] = []
    for seed in cells:
        if visited[seed]:
            continue
        component_queue = deque([seed])
        visited[seed] = 1
        bands: dict[int, list[int]] = defaultdict(list)
        while component_queue:
            index = component_queue.popleft()
            bands[distance[index]].append(index)
            for neighbor in _neighbors8(index, width, height):
                if occupied[neighbor] and not visited[neighbor]:
                    visited[neighbor] = 1
                    component_queue.append(neighbor)

        current: list[int] = []
        for radius in sorted(bands):
            remaining = set(bands[radius])
            degrees = {point: sum(neighbor in remaining for neighbor in
                                 _neighbors8(point, width, height)) for point in remaining}
            endpoints = deque(sorted(point for point in remaining if degrees[point] <= 1))
            def consume(point):
                remaining.remove(point)
                for neighbor in _neighbors8(point, width, height):
                    if neighbor in remaining:
                        degrees[neighbor] -= 1
                        if degrees[neighbor] == 1:
                            endpoints.append(neighbor)
            while remaining:
                if current:
                    nearby = [neighbor for neighbor in _neighbors8(current[-1], width, height)
                              if neighbor in remaining]
                    cardinal = [neighbor for neighbor in _neighbors(current[-1], width, height)
                                if neighbor in remaining]
                    if cardinal:
                        nearby = cardinal
                else:
                    nearby = []
                if nearby:
                    start = min(nearby, key=lambda point: (degrees[point], point))
                else:
                    candidates = []
                    while endpoints and len(candidates) < 32:
                        point = endpoints.popleft()
                        if point in remaining and degrees[point] <= 1:
                            candidates.append(point)
                    if not candidates:
                        candidates = [min(remaining)]
                    if current:
                        x, y = current[-1] % width, current[-1] // width
                        start = min(candidates, key=lambda point:
                            ((point % width - x) ** 2 + (point // width - y) ** 2, point))
                    else:
                        start = candidates[0]
                    endpoints.extend(point for point in candidates if point != start)
                consume(start)
                trail = [start]
                last_direction = None
                while True:
                    options = [neighbor for neighbor in _neighbors8(trail[-1], width, height)
                               if neighbor in remaining]
                    cardinal = [neighbor for neighbor in _neighbors(trail[-1], width, height)
                                if neighbor in remaining]
                    if cardinal:
                        options = cardinal
                    if not options:
                        break
                    if last_direction is None:
                        next_point = min(options, key=lambda point:
                                         _direction(trail[-1], point, width))
                    else:
                        # Continue straight, then turn right, left, or back.
                        next_point = min(options, key=lambda point: _TURN_RANK[
                            (_direction(trail[-1], point, width) - last_direction) % 8])
                    last_direction = _direction(trail[-1], next_point, width)
                    consume(next_point)
                    trail.append(next_point)
                if len(trail) > 2 and trail[-1] != trail[0] and \
                        trail[0] in _neighbors8(trail[-1], width, height):
                    trail.append(trail[0])
                connector = _connect_inside(current[-1], trail[0], occupied, width, join_budget) if current else None
                if connector is not None:
                    current.extend(connector)
                    current.extend(trail[1:])
                else:
                    if current:
                        result.append(_shorten(_simplify(current, width), occupied, width, tolerance))
                    current = trail
        if current:
            result.append(_shorten(_simplify(current, width), occupied, width, tolerance))
    return result
