"""Skyline bottom-left rectangle packer.

Tries power-of-two atlas widths and keeps the layout with the smallest area,
which is what Habbo's own spritesheets look like (POT width, tight height).
"""

from __future__ import annotations


def _place(sizes: list[tuple[int, int]], order: list[int], width: int, padding: int):
    skyline = [(0, 0, width)]  # (x, y, segment width)
    positions: dict[int, tuple[int, int]] = {}
    height = 0
    for i in order:
        w, h = sizes[i]
        pw, ph = w + padding, h + padding
        best = None
        for s in range(len(skyline)):
            x = skyline[s][0]
            if x + pw > width:
                break
            # max y across segments covered by [x, x + pw)
            y = 0
            remaining = pw
            k = s
            while remaining > 0 and k < len(skyline):
                y = max(y, skyline[k][1])
                remaining -= skyline[k][2] if k > s else skyline[k][2] - (x - skyline[k][0])
                k += 1
            if remaining > 0:
                continue
            score = (y + ph, x)
            if best is None or score < best[0]:
                best = (score, s, x, y)
        if best is None:
            return None
        _, s, x, y = best
        positions[i] = (x, y)
        height = max(height, y + h)
        # update skyline: new segment [x, x + pw) at y + ph
        new = []
        right = x + pw
        for sx, sy, sw in skyline:
            ex = sx + sw
            if ex <= x or sx >= right:
                new.append((sx, sy, sw))
                continue
            if sx < x:
                new.append((sx, sy, x - sx))
            if ex > right:
                new.append((right, sy, ex - right))
        new.append((x, y + ph, pw))
        new.sort()
        merged = []
        for seg in new:
            if merged and merged[-1][1] == seg[1] and merged[-1][0] + merged[-1][2] == seg[0]:
                merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + seg[2])
            else:
                merged.append(seg)
        skyline = merged
    return positions, height


def pack(sizes: list[tuple[int, int]], padding: int = 0, max_width: int = 8192, max_height: int | None = None):
    """Return (atlas_w, atlas_h, [(x, y)...]) for the given (w, h) sizes in a single atlas."""
    if not sizes:
        return 0, 0, []
    order = sorted(range(len(sizes)), key=lambda i: (-max(sizes[i]), -sizes[i][1], -sizes[i][0], i))
    widest = max(w for w, _ in sizes) + padding
    total = sum((w + padding) * (h + padding) for w, h in sizes)
    width = 1
    while width < widest:
        width *= 2
    best = None
    while width <= max_width:
        placed = _place(sizes, order, width, padding)
        if placed is not None and (max_height is None or placed[1] <= max_height):
            positions, height = placed
            area = width * height
            if best is None or area < best[0] or (area == best[0] and width < best[1]):
                best = (area, width, height, positions)
            # Once the layout is a single tall-enough strip, wider atlases only waste area.
            if height <= width and width * width >= total * 2:
                break
        width *= 2
    if best is None:
        raise ValueError("images do not fit in a %d px wide atlas" % max_width)
    _, width, height, positions = best
    return width, height, [positions[i] for i in range(len(sizes))]


def pack_multi(sizes: list[tuple[int, int]], padding: int = 0, max_side: int = 8192):
    """Pack into as few atlases as needed so that no atlas side exceeds ``max_side``.

    Returns [(atlas_w, atlas_h, {item index: (x, y)}), ...]. Almost every library fits in one.
    """
    bins = []
    remaining = []
    for i, (w, h) in enumerate(sizes):
        if w > max_side or h > max_side:
            bins.append((w, h, {i: (0, 0)}))   # an oversized image gets an atlas of its own
        else:
            remaining.append(i)
    while remaining:
        # Largest prefix (by the packer's own ordering) that still fits under max_side.
        order = sorted(remaining, key=lambda i: (-max(sizes[i]), -sizes[i][1], -sizes[i][0], i))
        lo, hi, best = 1, len(order), None
        while lo <= hi:
            mid = (lo + hi) // 2
            subset = order[:mid]
            try:
                w, h, pos = pack([sizes[i] for i in subset], padding, max_side, max_side)
            except ValueError:
                w = h = max_side + 1
            if w <= max_side and h <= max_side:
                best = (w, h, dict(zip(subset, pos)), subset)
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            raise ValueError("could not fit images under %d px" % max_side)
        bins.append(best[:3])
        taken = set(best[3])
        remaining = [i for i in remaining if i not in taken]
    return bins
