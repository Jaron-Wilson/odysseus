"""Measuring a part: overhang and plate contact per orientation, where the
overhangs are, floating islands. Ported from print-dad/analyze.py, where.py
and islands.py.

An overhang is a face pointing down steeper than 45 degrees that is not on
the plate (the slicer would want support under it). Plate contact is the flat
down-facing area at the bottom.
"""
import itertools
from typing import Dict, List, Optional, Tuple

import numpy as np

LIMIT_DEG = 45.0
BED_TOL_MM = 0.3
MIN_BED_CM2 = 8.0       # an orientation with less plate contact than this tips over or peels
LAYER_MM = 0.2


def face_data(V: np.ndarray, T: np.ndarray):
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    n = np.cross(b - a, c - a)
    area2 = np.linalg.norm(n, axis=1)
    n = n / np.maximum(area2, 1e-12)[:, None]
    return n, area2 / 2, (a + b + c) / 3


def overhang_mask(V: np.ndarray, T: np.ndarray, up=(0, 0, 1.0), limit_deg: float = LIMIT_DEG,
                  bed_tol: float = BED_TOL_MM) -> np.ndarray:
    up = np.asarray(up, float)
    up = up / np.linalg.norm(up)
    n, _, cen = face_data(V, T)
    zmin = (V @ up).min()
    return (n @ up < -np.cos(np.radians(90 - limit_deg))) & (cen @ up > zmin + bed_tol)


def overhang(V: np.ndarray, T: np.ndarray, up=(0, 0, 1.0), limit_deg: float = LIMIT_DEG,
             bed_tol: float = BED_TOL_MM) -> Dict[str, float]:
    """Overhang area, plate contact area (cm2) and height (mm) with `up` pointing up."""
    up = np.asarray(up, float)
    up = up / np.linalg.norm(up)
    n, area, cen = face_data(V, T)
    h = V @ up
    zmin = h.min()
    fz = cen @ up
    nz = n @ up
    down = nz < -np.cos(np.radians(90 - limit_deg))
    on_bed = fz < zmin + bed_tol
    return {"overhang_cm2": round(float(area[down & ~on_bed].sum() / 100), 2),
            "bed_cm2": round(float(area[(nz < -0.99) & on_bed].sum() / 100), 2),
            "height_mm": round(float(h.max() - zmin), 2)}


def directions(extra: Optional[List] = None) -> List[np.ndarray]:
    """The 26 axis, edge and corner directions (plus any extra, e.g. a cut normal)."""
    out = set()
    for v in itertools.product((-1, 0, 1), repeat=3):
        if any(v):
            u = np.array(v, float)
            out.add(tuple(np.round(u / np.linalg.norm(u), 4)))
    for e in extra or []:
        u = np.asarray(e, float)
        out.add(tuple(np.round(u / np.linalg.norm(u), 4)))
    return [np.array(d) for d in sorted(out)]


def rank_orientations(V: np.ndarray, T: np.ndarray, extra=None, top: int = 3) -> List[Dict]:
    """Orientations (the direction that points up) ranked like print-dad: enough plate contact first, then the
    least overhang, then the lowest height."""
    rows = []
    for d in directions(extra):
        r = overhang(V, T, d)
        rows.append({"up": [round(float(x), 4) for x in d], **r})
    # a small part can't have 8 cm2 on the plate: then half its best contact is enough
    need = min(MIN_BED_CM2, 0.5 * max(r["bed_cm2"] for r in rows))
    rows.sort(key=lambda r: (r["bed_cm2"] < need, r["overhang_cm2"], r["height_mm"]))
    return rows[:top]


def height_bands(V: np.ndarray, T: np.ndarray, band_mm: float = 5.0, min_cm2: float = 0.5) -> List[Dict]:
    """Where the overhangs are (z up): area per height band, how much of it is flat ceiling, and its extent."""
    n, ar, cen = face_data(V, T)
    zmin = V[:, 2].min()
    over = (n[:, 2] < -np.cos(np.radians(LIMIT_DEG))) & (cen[:, 2] > zmin + BED_TOL_MM)
    flat = over & (n[:, 2] < -0.985)
    zs = cen[over, 2] - zmin
    if not len(zs):
        return []
    out = []
    for lo in np.arange(0, zs.max() + band_mm, band_mm):
        m = over.copy()
        m[over] = (zs >= lo) & (zs < lo + band_mm)
        A = ar[m].sum() / 100
        if A < min_cm2:
            continue
        lo_xy, hi_xy = cen[m, :2].min(0), cen[m, :2].max(0)
        out.append({"z_mm": [round(float(lo), 1), round(float(lo + band_mm), 1)], "cm2": round(float(A), 2),
                    "flat_cm2": round(float(ar[m & flat].sum() / 100), 2),
                    "x_mm": [round(float(lo_xy[0]), 1), round(float(hi_xy[0]), 1)],
                    "y_mm": [round(float(lo_xy[1]), 1), round(float(hi_xy[1]), 1)]})
    return out


def ceilings_cm2(V: np.ndarray, T: np.ndarray) -> float:
    """Flat (within 10 degrees) down-facing area above the plate: the ceilings of hollows and bridges."""
    n, ar, cen = face_data(V, T)
    m = (n[:, 2] < -0.985) & (cen[:, 2] > V[:, 2].min() + BED_TOL_MM)
    return round(float(ar[m].sum() / 100), 2)


# ── Solids (manifold3d) ─────────────────────────────────────────────────────

def weld(V: np.ndarray, T: np.ndarray, tol: float = 1e-4) -> Tuple[np.ndarray, np.ndarray]:
    """Merge duplicate vertices and drop degenerate triangles."""
    q = np.round(V / tol).astype(np.int64)
    _, idx, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    V2, T2 = V[idx], inv.reshape(-1)[T]
    T2 = T2[(T2[:, 0] != T2[:, 1]) & (T2[:, 1] != T2[:, 2]) & (T2[:, 0] != T2[:, 2])]
    return V2, T2


def to_manifold(V: np.ndarray, T: np.ndarray):
    """A closed solid, or ValueError if the mesh has holes or flipped faces."""
    import manifold3d as mf
    V2, T2 = weld(V, T)
    m = mf.Manifold(mf.Mesh(vert_properties=V2.astype(np.float32), tri_verts=T2.astype(np.uint32)))
    if m.status() != mf.Error.NoError:
        raise ValueError(f"the mesh is not a closed solid ({m.status().name})")
    return m


def arrays(m) -> Tuple[np.ndarray, np.ndarray]:
    mesh = m.to_mesh()
    return (np.asarray(mesh.vert_properties)[:, :3].astype(float),
            np.asarray(mesh.tri_verts).astype(np.int64).reshape(-1, 3))


def islands(m, layer: float = LAYER_MM, min_area: float = 0.2, limit: int = 50, line: float = 0.42) -> List[Dict]:
    """Floating islands the way a slicer sees them (print-dad islands.py): a region of a layer with (almost)
    nothing under it. With `line` > 0 each layer first loses what is thinner than one line, as the slicer
    drops it; a piece standing on such a sliver floats ("floating regions" in Bambu Studio)."""
    import manifold3d as mf

    def seen(z):
        sec = m.slice(float(z))
        if line > 0:
            sec = sec.offset(-line / 2, mf.JoinType.Miter).offset(line / 2, mf.JoinType.Miter)
        return sec
    lo, hi = m.bounding_box()[2], m.bounding_box()[5]
    prev, found = None, []
    for z in np.arange(lo + layer / 2, hi, layer):
        sec = seen(z)
        if prev is not None:
            for isl in sec.decompose():
                if isl.area() < min_area:
                    continue
                touch = (isl ^ prev).area()
                if touch < 0.05 * isl.area():
                    b = isl.bounds()
                    found.append({"z_mm": round(float(z - lo), 1), "area_mm2": round(float(isl.area()), 1),
                                  "x_mm": [round(b[0], 1), round(b[2], 1)], "y_mm": [round(b[1], 1), round(b[3], 1)]})
                    if len(found) >= limit:
                        return found
        prev = sec
    return found


def _inside(polys, pts: np.ndarray) -> np.ndarray:
    """Points inside a cross section given as loops (outer loops counterclockwise, holes clockwise)."""
    from matplotlib.path import Path
    wind = np.zeros(len(pts), dtype=int)
    for p in polys:
        p = np.asarray(p)
        if len(p) < 3:
            continue
        x, y = p[:, 0], p[:, 1]
        sign = 1 if np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)) > 0 else -1
        wind += sign * Path(p).contains_points(pts)
    return wind > 0


def unsupported_mask(m, V: np.ndarray, T: np.ndarray, drop: float = 0.4, angle: float = LIMIT_DEG) -> np.ndarray:
    """Down-facing faces a slicer would print in mid-air: nothing within drop * tan(angle) of them in the
    material `drop` mm below. Unlike the face-angle test, the small steps of a layered 45 degree roof count
    as supported. `m` is the solid of (V, T), z up."""
    import manifold3d as mf
    n, _, cen = face_data(V, T)
    zmin = V[:, 2].min()
    cand = np.where((n[:, 2] < -0.3) & (cen[:, 2] > zmin + BED_TOL_MM))[0]
    out = np.zeros(len(T), dtype=bool)
    if not len(cand):
        return out
    reach = drop * np.tan(np.radians(angle))
    bins = np.round((cen[cand, 2] - drop) / 0.1).astype(np.int64)
    for b in np.unique(bins):
        idx = cand[bins == b]
        z = float(b) * 0.1
        if z <= zmin:
            continue
        sec = m.slice(z).offset(reach, mf.JoinType.Miter)
        out[idx] = ~_inside(sec.to_polygons(), cen[idx, :2])
    return out


def unsupported_cm2(m, V: np.ndarray, T: np.ndarray) -> float:
    _, ar, _ = face_data(V, T)
    return round(float(ar[unsupported_mask(m, V, T)].sum() / 100), 2)
