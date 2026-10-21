"""The fixes, as plain geometry on manifold3d solids. Ported from print-dad/cut.py,
fill_leg.py, legs_roof.py, roof.py and posts.py and made general.

  scale            uniform scale (the whole model, so the parts still fit together)
  orient           lay a part down with a chosen direction up, centered, on z=0
  split / cut_to_fit   plane cuts with 2.2 mm x 6 mm filament-pin holes on both sides
  find_keepouts    sockets and pin holes that must stay open (small holes along x, y and z)
  fill_hollows     fill enclosed hollows that have a ceiling, never inside a keep-out
  roof             45 degree self-supporting roofs under ceilings so hollows stay open
  posts            posts to the plate under spots that step out past the layer below

Everything here is deterministic; the assistant only picks the options.
"""
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.print_helper.geometry import arrays

PIN_R, PIN_DEPTH, PIN_MARGIN = 1.1, 6.0, 1.8     # 1.75 mm filament offcuts as alignment pins
BED_MM = 256.0
BED_MARGIN_MM = 6.0
ROOF_DZ = 0.2           # the slicer's layer height, so nothing slips between sampled layers
LINE_MM = 0.42          # the slicer's line width: anything thinner is dropped

Progress = Optional[Callable[[float, str], None]]


def _mf():
    import manifold3d as mf
    return mf


def _tick(progress: Progress, frac: float, msg: str = "") -> None:
    if progress:
        progress(max(0.0, min(1.0, frac)), msg)


# ── Frames and orientation ──────────────────────────────────────────────────

def frame(normal) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    a = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(n, a)
    u /= np.linalg.norm(u)
    return u, np.cross(n, u), n


def _affine(R: np.ndarray, t=(0, 0, 0)) -> List[List[float]]:
    A = np.zeros((3, 4))
    A[:, :3] = R
    A[:, 3] = t
    return A.tolist()


def rotation_up(up) -> np.ndarray:
    """The rotation that turns direction `up` to +z (Rodrigues; print-dad cut.lay_flat)."""
    d = -np.asarray(up, float)
    d /= np.linalg.norm(d)
    t = np.array([0, 0, -1.0])
    ax = np.cross(d, t)
    s = np.linalg.norm(ax)
    c = d @ t
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = ax / s
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + s * K + (1 - c) * K @ K


def placement(m, up) -> Tuple[np.ndarray, np.ndarray]:
    """(R, t) that put solid `m` with `up` pointing up, centered on x/y, sitting on z=0."""
    R = rotation_up(up)
    V, _ = arrays(m)
    W = V @ R.T
    t = -np.array([(W[:, 0].max() + W[:, 0].min()) / 2, (W[:, 1].max() + W[:, 1].min()) / 2, W[:, 2].min()])
    return R, t


def apply(m, R: np.ndarray, t) :
    return m.transform(_affine(R, t))


def scale(m, factor: float):
    return m.scale([factor, factor, factor]) if abs(factor - 1.0) > 1e-9 else m


def fits_bed(extent, bed: float = BED_MM, margin: float = BED_MARGIN_MM) -> bool:
    return extent[0] <= bed - margin and extent[1] <= bed - margin and extent[2] <= bed


def extent(m) -> np.ndarray:
    b = m.bounding_box()
    return np.array([b[3] - b[0], b[4] - b[1], b[5] - b[2]])


# ── Cutting ─────────────────────────────────────────────────────────────────

def _section(m, normal, offset):
    u, v, n = frame(normal)
    return m.transform(_affine(np.array([u, v, n]))).slice(float(offset))


def section_area(m, normal, offset) -> float:
    return _section(m, normal, offset).area()


def pin_points(m, normal, offset, want: int = 6, min_gap: float = 12.0) -> List[np.ndarray]:
    """Points on the cut plane well inside the material, spread out (farthest-point sampling), for pin holes."""
    mf = _mf()
    u, v, n = frame(normal)
    inset = _section(m, normal, offset).offset(-(PIN_R + PIN_MARGIN), mf.JoinType.Round)
    pts = [p for poly in inset.to_polygons() for p in np.asarray(poly)]
    if not pts:
        return []
    pts = np.array(pts)
    chosen = [pts[np.argmax(np.linalg.norm(pts - pts.mean(0), axis=1))]]
    while len(chosen) < min(want, len(pts)):
        d = np.min([np.linalg.norm(pts - c, axis=1) for c in chosen], axis=0)
        if d.max() < min_gap:
            break
        chosen.append(pts[np.argmax(d)])
    return [c[0] * u + c[1] * v + offset * n for c in chosen]


def pin_holes(points: Sequence, normal):
    """Cylinders 2.2 mm wide reaching 6 mm into both sides of the cut plane."""
    mf = _mf()
    u, v, n = frame(normal)
    holes = []
    for p in points:
        cyl = mf.Manifold.cylinder(2 * PIN_DEPTH, PIN_R, PIN_R, 32, True)
        A = np.zeros((3, 4))
        A[:, 0], A[:, 1], A[:, 2], A[:, 3] = u, v, n, p
        holes.append(cyl.transform(A.tolist()))
    if not holes:
        return None
    return mf.Manifold.batch_boolean(holes, mf.OpType.Add)


def split(m, normal, offset):
    """(side the normal points to, other side, pin points on the plane). Pins are drilled later, after the
    hollows are treated, so nothing can close them."""
    pts = pin_points(m, normal, offset)
    a, b = m.split_by_plane(list(map(float, normal)), float(offset))
    return a, b, pts


def axis_mid(m, axis: int) -> float:
    b = m.bounding_box()
    return float((b[axis] + b[axis + 3]) / 2)


def best_cut_offset(m, axis: int, steps: int = 21) -> float:
    """Near the middle, where the section is smallest (less glue face to line up, fewer pins to miss)."""
    b = m.bounding_box()
    lo, hi = b[axis], b[axis + 3]
    n = [0.0, 0.0, 0.0]
    n[axis] = 1.0
    best, best_area = (lo + hi) / 2, None
    for f in np.linspace(0.4, 0.6, steps):
        x = lo + f * (hi - lo)
        a = section_area(m, n, x)
        if a > 1.0 and (best_area is None or a < best_area * 0.85):
            best, best_area = x, a
    return float(best)


# ── Keep-outs: sockets and holes that must stay open ────────────────────────

def _signed_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def outline(cs):
    """A cross section with its holes filled (only the outer loops)."""
    mf = _mf()
    polys = [np.asarray(p) for p in cs.to_polygons()]
    outer = [p.tolist() for p in polys if len(p) >= 3 and _signed_area(p) > 0]
    return mf.CrossSection(outer, mf.FillRule.Positive) if outer else mf.CrossSection()


def find_keepouts(m, max_hole_mm2: Optional[float] = None, min_depth_mm: float = 2.0, step: float = 0.5,
                  progress: Progress = None):
    """Sockets, stud holes and pin holes: holes in the sections along x, y and z that run at least
    `min_depth_mm` deep and are no bigger than `max_hole_mm2` (default: 6% of the part's footprint across
    that axis, at least 30 mm2; a LEGO stud socket at 500% is about 450 mm2 in a 15000 mm2 leg, while a hollow
    is a large share of its part).
    Returned as one solid (or None). Generalizes print-dad's hip and foot socket keep-outs (legs_roof.py),
    which looked along one known axis."""
    mf = _mf()
    found = []
    for k in range(3):
        nrm = [0.0, 0.0, 0.0]
        nrm[k] = 1.0
        u, v, n = frame(nrm)
        R = np.array([u, v, n])
        local = m.transform(_affine(R))
        b = local.bounding_box()
        limit = max_hole_mm2 if max_hole_mm2 is not None else max(30.0, 0.06 * (b[3] - b[0]) * (b[4] - b[1]))
        slabs = []
        for z in np.arange(b[2] + step / 2, b[5], step):
            for p in local.slice(float(z)).to_polygons():
                p = np.asarray(p)
                if len(p) >= 3:
                    a = _signed_area(p)
                    if a < 0 and -a <= limit:
                        cs = mf.CrossSection([p[::-1].tolist()])
                        slabs.append(mf.Manifold.extrude(cs, step + 0.02).translate([0, 0, float(z) - step / 2 - 0.01]))
        _tick(progress, (k + 1) / 3, "finding sockets and holes")
        if not slabs:
            continue
        runs = mf.Manifold.batch_boolean(slabs, mf.OpType.Add)
        for comp in runs.decompose():
            cb = comp.bounding_box()
            if cb[5] - cb[2] >= min_depth_mm:
                found.append(comp.transform(_affine(R.T)))
    if not found:
        return None
    return mf.Manifold.batch_boolean(found, mf.OpType.Add)


# ── Hollows: fill or roof ───────────────────────────────────────────────────

def _layers(m, dz: float):
    lo, hi = m.bounding_box()[2], m.bounding_box()[5]
    return [float(z) for z in np.arange(lo + dz / 2, hi, dz)]


def _shadows(sections):
    """For each layer: the union of the outlines of every layer above it and of every layer below it."""
    mf = _mf()
    outl = [outline(s) for s in sections]
    above = [mf.CrossSection()] * len(outl)
    below = [mf.CrossSection()] * len(outl)
    acc = mf.CrossSection()
    for i in range(len(outl) - 1, -1, -1):
        above[i] = acc
        acc = (acc + outl[i]).simplify(0.02)
    acc = mf.CrossSection()
    for i in range(len(outl)):
        below[i] = acc
        acc = (acc + outl[i]).simplify(0.02)
    return outl, above, below


def fill_hollows(m, keepout=None, dz: float = ROOF_DZ, min_area: float = 1.0, progress: Progress = None):
    """Fill the enclosed hollows of each layer that have material somewhere above them (a ceiling), never
    inside `keepout`. Open-topped shafts stay open. Returns (solid, added cm3). From print-dad fill_leg.py."""
    mf = _mf()
    zs = _layers(m, dz)
    if not zs:
        return m, 0.0
    secs = [m.slice(z) for z in zs]
    outl, above, _ = _shadows(secs)
    fills = []
    for i, z in enumerate(zs):
        holes = outl[i] - secs[i]
        if holes.area() < min_area:
            continue
        add = holes ^ above[i]
        if keepout is not None:
            add = add - keepout.slice(z)
        if add.area() >= min_area:
            fills.append(mf.Manifold.extrude(add, dz + 0.02).translate([0, 0, z - dz / 2 - 0.01]))
        if i % 25 == 0:
            _tick(progress, i / len(zs), "filling hollows")
    if not fills:
        return m, 0.0
    out = m + mf.Manifold.batch_boolean(fills, mf.OpType.Add)
    return out, (out.volume() - m.volume()) / 1000


def _opening(cs, r: float):
    """The section without the parts thinner than 2r (what a slicer with that line width keeps)."""
    mf = _mf()
    return cs.offset(-r, mf.JoinType.Miter).offset(r, mf.JoinType.Miter)


def _core(cs, step: float):
    """The innermost part of a region (its last non-empty inward offset), a little wider than a bridge needs.
    Where a hollow under a ceiling starts: the ridge of a gable, the apex of a hip roof."""
    mf = _mf()
    b = cs.bounds()
    lo, hi = 0.0, max(b[2] - b[0], b[3] - b[1]) / 2
    for _ in range(12):                     # binary search for the deepest offset that leaves something
        mid = (lo + hi) / 2
        if cs.offset(-mid, mf.JoinType.Miter).area() > 0.01:
            lo = mid
        else:
            hi = mid
    return cs.offset(-max(lo - step / 2, 0.0), mf.JoinType.Miter) if lo > step else mf.CrossSection()


def roof(m, keepout=None, envelope: str = "inside", angle: float = 45.0, dz: float = ROOF_DZ,
         min_area: float = 0.02, progress: Progress = None):
    """Self-supporting roofs under ceilings that keep the hollows open (print-dad roof.py, corrected).

    Top-down in layers. A layer is printable when every point of the layer above is within one step
    (dz * tan(angle)) of material in this layer. The hollow this layer may keep is what lies within one step of
    the hollow (or outside air) of the layer above, so going down a hollow can only widen at 45 degrees.
    Where a hollow sits under a solid ceiling, it starts as a narrow slot at the hollow's core (a 0.8 mm bridge)
    and widens from there: a gable or hip roof inside the hollow. Everything else of the hollow is filled,
    only inside the envelope and never in a keep-out (a keep-out always stays empty).

    The rule print-dad used (each layer contains the layer above shrunk by a step) is sufficient but fills a
    hollow under a flat ceiling completely; this keeps it open.

    envelope "inside": where the part has material both somewhere above and somewhere below (hollows and gaps
    inside the part; outside ledges are left to posts). "anywhere": also chamfer outside overhangs.
    Returns (solid, added cm3)."""
    mf = _mf()
    zs = _layers(m, dz)
    if len(zs) < 2:
        return m, 0.0
    step = dz * np.tan(np.radians(angle))
    secs = [m.slice(z) for z in zs]
    bb = m.bounding_box()
    pad = 10.0 + step
    world = mf.CrossSection.square([bb[3] - bb[0] + 2 * pad, bb[4] - bb[1] + 2 * pad]).translate([bb[0] - pad, bb[1] - pad])
    if envelope == "inside":
        _, above_sh, below_sh = _shadows(secs)
    fills = []
    n = len(zs)
    above = secs[-1]                        # the final material of the layer above
    for i in range(n - 2, 0, -1):           # the bottom layer sits on the plate
        z = zs[i]
        sec = secs[i]
        uncovered = above - sec.offset(step, mf.JoinType.Miter)
        if uncovered.area() > min_area:
            allow = (above_sh[i] ^ below_sh[i]) if envelope == "inside" else world
            hollow = allow - sec
            ko = keepout.slice(z) if keepout is not None else None
            open_above = (world - above).offset(step, mf.JoinType.Miter)
            keep = hollow ^ open_above
            todo = hollow - open_above
            seeds = []
            for comp in todo.decompose():
                # only a hollow that nothing open reaches yet starts a new ridge; the rest widens from what
                # is already open (seeding every layer would grow the hollow faster than 45 degrees)
                if comp.area() > 4 * step * step and (comp.offset(step, mf.JoinType.Miter) ^ keep).area() < 0.01:
                    seeds.append(_core(comp, step))
            if seeds:
                keep = keep + (mf.CrossSection.batch_boolean(seeds, mf.OpType.Add) ^ hollow)
            add = hollow - keep
            if ko is not None:
                add = add - ko
            # Where two hollows meet, the fill between them narrows to a knife edge. The slicer drops what is
            # thinner than a line, so the piece above it floats ("floating regions"). Find the parts of the
            # layer above that, seen at line width, touch nothing here, and carry them down as a wall.
            here = _opening(sec + add, LINE_MM / 2)
            extra = []
            for comp in _opening(above, LINE_MM / 2).decompose():
                if comp.area() > 0.05 and (comp ^ here).area() < 0.02 * comp.area():
                    extra.append(comp.offset(step, mf.JoinType.Miter) ^ hollow)
            if extra:
                more = mf.CrossSection.batch_boolean(extra, mf.OpType.Add)
                add = add + (more - ko if ko is not None else more)
            if add.area() > min_area:
                fills.append(mf.Manifold.extrude(add, dz + 0.02).translate([0, 0, z - dz / 2 - 0.01]))
                sec = sec + add
        above = sec.simplify(0.01)       # keeps the vertex count from growing layer after layer
        if i % 25 == 0:
            _tick(progress, (n - i) / n, "building 45 degree roofs")
    if not fills:
        return m, 0.0
    out = m + mf.Manifold.batch_boolean(fills, mf.OpType.Add)
    return out, (out.volume() - m.volume()) / 1000


def posts(m, step: float = 0.3, min_area: float = 2.0, above: float = 2.0, grow: float = 1.0, layer: float = 0.2,
          progress: Progress = None):
    """A post down to the plate under every spot that steps out past the layer below (print-dad posts.py).
    Returns (solid, added cm3, count)."""
    mf = _mf()
    lo, hi = m.bounding_box()[2], m.bounding_box()[5]
    prev, cols = None, []
    zs = np.arange(lo + layer / 2, hi, layer)
    for i, z in enumerate(zs):
        sec = m.slice(float(z))
        if prev is not None and z - lo > above:
            for isl in (sec - prev.offset(step, mf.JoinType.Round)).decompose():
                if isl.area() > min_area:
                    cols.append(mf.Manifold.extrude(isl.offset(grow, mf.JoinType.Round), float(z - lo)).translate([0, 0, lo]))
        prev = sec
        if i % 50 == 0:
            _tick(progress, i / len(zs), "adding posts")
    if not cols:
        return m, 0.0, 0
    out = m + mf.Manifold.batch_boolean(cols, mf.OpType.Add)
    return out, (out.volume() - m.volume()) / 1000, len(cols)
