"""Analyze a job's model and fix it with chosen options. Runs in the worker process (worker.py).

A job folder holds input/<file>, analysis.json, result.json, renders/ and out/ (jobs.py).
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from src.print_helper import geometry as G
from src.print_helper import transforms as X
from src.print_helper.meshio import load_model, write_stl

BED_MM = X.BED_MM
USABLE_MM = X.BED_MM - X.BED_MARGIN_MM

HOLLOWS = ("roof", "fill", "leave")
COLORS = ("single", "ams")
DEFAULT_OPTIONS = {
    "scale_percent": 100.0,   # the whole model, so parts still fit together
    "hollows": "roof",        # roof: keep hollows open with 45 degree roofs; fill: fill enclosed hollows; leave
    "keep_holes": True,       # keep sockets, stud holes and pin holes open
    "allow_cut": False,       # cut parts into pieces (with pin holes) to avoid overhangs or to fit the bed
    "posts": False,           # posts to the plate under outside step-outs (changes the look)
    "parts": [],              # names of the parts to work on; empty: all
    "colors": "single",       # single filament or AMS colors (the slice check reports filament swaps)
    "slice_check": True,      # slice every piece on the PC with Bambu Studio
    "notes": "",              # what failed before and how, in the user's words
}

Progress = Callable[[float, str], None]


def normalize_options(raw: Optional[Dict], part_names: Optional[List[str]] = None) -> Dict:
    """Options from the user or the model, checked and clamped. Unknown keys are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    o = dict(DEFAULT_OPTIONS)
    try:
        s = float(raw.get("scale_percent", o["scale_percent"]))
        if not np.isfinite(s):
            raise ValueError
        o["scale_percent"] = round(min(max(s, 1.0), 1000.0), 2)
    except (TypeError, ValueError):
        pass
    if raw.get("hollows") in HOLLOWS:
        o["hollows"] = raw["hollows"]
    if raw.get("colors") in COLORS:
        o["colors"] = raw["colors"]
    for k in ("keep_holes", "allow_cut", "posts", "slice_check"):
        if isinstance(raw.get(k), bool):
            o[k] = raw[k]
    parts = raw.get("parts")
    if isinstance(parts, list):
        names = [str(p)[:80] for p in parts if isinstance(p, (str, int))]
        if part_names is not None:
            low = {n.lower(): n for n in part_names}
            names = [low[n.lower()] for n in names if n.lower() in low]
        o["parts"] = list(dict.fromkeys(names))[:200]
    if isinstance(raw.get("notes"), str):
        o["notes"] = raw["notes"][:2000]
    return o


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:60] or "part"


def _input_file(job_dir: str):
    d = os.path.join(job_dir, "input")
    files = sorted(os.listdir(d))
    if not files:
        raise ValueError("the job has no model file")
    return os.path.join(d, files[0]), files[0]


def _stage(progress: Optional[Progress], lo: float, hi: float) -> Progress:
    def p(frac: float, msg: str = "") -> None:
        if progress:
            progress(lo + (hi - lo) * max(0.0, min(1.0, frac)), msg)
    return p


# ── Analyze ─────────────────────────────────────────────────────────────────

def analyze_part(V: np.ndarray, T: np.ndarray, deep: bool = True):
    """(measurements, mask of the faces to show red)."""
    ext = V.max(0) - V.min(0)
    row = {"tris": int(len(T)), "size_mm": [round(float(e), 1) for e in ext],
           "current": G.overhang(V, T), "best": G.rank_orientations(V, T),
           "ceilings_cm2": G.ceilings_cm2(V, T), "bands": G.height_bands(V, T)[:12],
           "fits_bed": bool(max(ext) <= USABLE_MM), "closed": False}
    try:
        m = G.to_manifold(V, T)
        row["closed"] = True
        row["volume_cm3"] = round(m.volume() / 1000, 1)
    except ValueError as e:
        row["problem"] = str(e)
        return row, None
    red = G.unsupported_mask(m, V, T)
    row["current"]["unsupported_cm2"] = round(float(G.face_data(V, T)[1][red].sum() / 100), 2)
    if deep:
        row["islands"] = len(G.islands(m, limit=20))
        zs = X._layers(m, 1.0)
        hollow = 0.0
        for z in zs:
            s = m.slice(z)
            hollow += (X.outline(s) - s).area()
        row["hollow_cm3"] = round(hollow / 1000, 1)
        ko = X.find_keepouts(m)
        row["sockets"] = len(ko.decompose()) if ko is not None else 0
    return row, red


def analyze(job_dir: str, progress: Optional[Progress] = None) -> Dict:
    from src.print_helper.render import draw
    path, fname = _input_file(job_dir)
    t0 = time.time()
    p = _stage(progress, 0.0, 0.1)
    p(0, "reading the model")
    parts = load_model(path, fname)
    os.makedirs(os.path.join(job_dir, "renders"), exist_ok=True)
    rows = []
    for i, part in enumerate(parts):
        p = _stage(progress, 0.1 + 0.85 * i / len(parts), 0.1 + 0.85 * (i + 1) / len(parts))
        p(0.0, f"measuring {part.name}")
        measured, red = analyze_part(part.V, part.T)
        row = {"index": i, "name": part.name, "plate": part.plate, **measured}
        p(0.7, f"rendering {part.name}")
        img = f"before_{i}.png"
        draw(part.V, part.T, os.path.join(job_dir, "renders", img), part.name, red=red)
        row["render"] = img
        rows.append(row)
    rows.sort(key=lambda r: (r["plate"] or 99, r["index"]))
    biggest = max(max(r["size_mm"]) for r in rows)
    analysis = {"file": fname, "parts": rows, "seconds": round(time.time() - t0, 1),
                "bed_mm": BED_MM, "max_scale_percent": round(100 * USABLE_MM / biggest, 1) if biggest else None,
                "total_overhang_cm2": round(sum(r["current"]["overhang_cm2"] for r in rows), 1)}
    analysis["summary"] = summary_text(analysis)
    _save(os.path.join(job_dir, "analysis.json"), analysis)
    if progress:
        progress(1.0, "analysis done")
    return analysis


def summary_text(a: Dict) -> str:
    """The analysis as short text for the assistant's context."""
    lines = [f"File {a['file']}: {len(a['parts'])} part(s). Bed {a['bed_mm']:.0f} mm (Bambu P1S, PLA, textured PEI). "
             f"The whole model fits the bed up to {a['max_scale_percent']}% scale without cutting."]
    for r in a["parts"]:
        c = r["current"]
        b = r["best"][0] if r.get("best") else None
        s = (f"- {r['name']}{' (plate ' + str(r['plate']) + ')' if r.get('plate') else ''}: "
             f"{r['size_mm'][0]} x {r['size_mm'][1]} x {r['size_mm'][2]} mm, {r['tris']} triangles; "
             f"as placed: overhang {c['overhang_cm2']} cm2"
             + (f" ({c['unsupported_cm2']} cm2 of it unsupported in mid-air)" if "unsupported_cm2" in c else "")
             + f", plate contact {c['bed_cm2']} cm2")
        if b:
            s += f"; best orientation (up {b['up']}): overhang {b['overhang_cm2']} cm2, plate contact {b['bed_cm2']} cm2"
        s += f"; flat ceilings {r.get('ceilings_cm2', 0)} cm2"
        if r.get("hollow_cm3") is not None:
            s += f"; hollow inside about {r['hollow_cm3']} cm3; sockets/holes {r.get('sockets', 0)}; floating islands {r.get('islands', 0)}"
        if not r.get("closed"):
            s += f"; NOT a closed solid ({r.get('problem', '')}), can only be analyzed, not changed"
        if not r.get("fits_bed"):
            s += "; does not fit the bed at 100%"
        lines.append(s)
    return "\n".join(lines)


# ── Fix ─────────────────────────────────────────────────────────────────────

@dataclass
class Piece:
    name: str
    solid: object                       # manifold, in the scaled model frame
    pins: List = field(default_factory=list)    # [(normal, [points])]
    cut_normals: List = field(default_factory=list)


ORIENTATIONS_TRIED = 3      # the best few by face angle are treated and compared by what is left unsupported


def _orientations(m, extra=None, k: int = ORIENTATIONS_TRIED) -> List[Dict]:
    """The best orientations (by face angle) that fit the bed."""
    V, T = G.arrays(m)
    out = []
    for row in G.rank_orientations(V, T, extra=extra, top=40):
        W = V @ X.rotation_up(row["up"]).T
        if X.fits_bed(W.max(0) - W.min(0)):
            out.append(row)
            if len(out) >= k:
                break
    return out


def _best_orientation(m, extra=None) -> Optional[Dict]:
    """The best orientation that fits the bed, or None."""
    rows = _orientations(m, extra, 1)
    return rows[0] if rows else None


def _quiet(frac: float, msg: str = "") -> None:
    pass


def treat(solid, up, opts: Dict, p: Progress = _quiet, name: str = "") -> Dict:
    """Place `solid` with `up` up and apply the hollow options: keep-outs, fill, roof, posts."""
    R, t = X.placement(solid, up)
    m = X.apply(solid, R, t)
    notes, keep, added = [], None, 0.0
    if opts["keep_holes"] and opts["hollows"] != "leave":
        p(0.05, "finding sockets and holes")
        keep = X.find_keepouts(m, progress=_sub(p, 0.05, 0.2, name))
        if keep is not None:
            notes.append(f"kept {len(keep.decompose())} socket(s)/hole(s) open")
    if opts["hollows"] == "fill":
        m, a = X.fill_hollows(m, keepout=keep, progress=_sub(p, 0.2, 0.45, name))
        added += a
        if a > 0.05:
            notes.append(f"filled {a:.1f} cm3 of enclosed hollow")
    if opts["hollows"] in ("fill", "roof"):
        m, a = X.roof(m, keepout=keep, progress=_sub(p, 0.45, 0.8, name))
        added += a
        if a > 0.05:
            notes.append(f"added {a:.1f} cm3 of 45 degree roof under ceilings")
    if opts["posts"]:
        m, a, n = X.posts(m, progress=_sub(p, 0.8, 0.88, name))
        added += a
        if n:
            notes.append(f"added {n} post(s) ({a:.1f} cm3)")
    lift = -m.bounding_box()[2]          # roofs and posts never go below the plate, but be exact
    m = m.translate([0, 0, lift])
    V, T = G.arrays(m)
    red = G.unsupported_mask(m, V, T)
    unsupported = float(G.face_data(V, T)[1][red].sum() / 100)
    floating = len(G.islands(m, limit=20))
    return {"m": m, "R": R, "t": np.asarray(t, float) + np.array([0, 0, lift]), "up": list(up), "notes": notes,
            "added": added, "unsupported_cm2": round(unsupported, 2), "floating": floating,
            # what is left: overhang in mid-air, and every floating region counts like a few cm2 of it
            "score": unsupported + 3.0 * floating}


def treat_best(pc: Piece, opts: Dict, p: Progress = _quiet, k: int = ORIENTATIONS_TRIED) -> Optional[Dict]:
    """The best of the top `k` orientations, compared after the treatment."""
    rows = _orientations(pc.solid, pc.cut_normals, k)
    best = None
    for i, row in enumerate(rows):
        r = treat(pc.solid, row["up"], opts, _sub(p, i / len(rows), (i + 1) / len(rows), pc.name), pc.name)
        if best is None or r["score"] < best["score"] - 0.05:
            best = r
    return best


def _score(pieces: List[Piece], opts: Dict, p: Progress = _quiet) -> float:
    total = 0.0
    for i, pc in enumerate(pieces):
        # one orientation per piece while searching for the cut: the final pieces get all of them
        r = treat_best(pc, opts, _sub(p, i / len(pieces), (i + 1) / len(pieces), pc.name), k=1)
        total += r["score"] if r else 1e6
    return total + 2.0 * (len(pieces) - 1)        # a cut is worth it only if it saves real overhang


def _split(pc: Piece, axis: int, offset: Optional[float] = None) -> List[Piece]:
    n = [0.0, 0.0, 0.0]
    n[axis] = 1.0
    off = X.best_cut_offset(pc.solid, axis) if offset is None else offset
    a, b, pts = X.split(pc.solid, n, off)
    out = []
    for sfx, s in (("A", a), ("B", b)):
        if s.volume() > 1.0:
            out.append(Piece(f"{pc.name} {sfx}", s, pc.pins + [(n, pts)], pc.cut_normals + [n, [-x for x in n]]))
    return out if len(out) == 2 else [pc]


def plan_pieces(m, name: str, allow_cut: bool, log: List[str], max_pieces: int = 12, opts: Optional[Dict] = None,
                progress: Progress = _quiet) -> List[Piece]:
    """One piece, or (cutting allowed) the cut in two across x, y or z that leaves the least unsupported after
    the hollow treatment; then more cuts until every piece fits the bed."""
    opts = normalize_options(opts or {})
    pieces = [Piece(name, m)]
    if allow_cut:
        progress(0.0, f"{name}: trying it whole")
        base = _score(pieces, opts, _sub(progress, 0.0, 0.25, name))
        best, best_score = pieces, base
        for axis in range(3):
            progress(0.25 + 0.25 * axis, f"{name}: trying a cut across {'xyz'[axis]}")
            cand = _split(pieces[0], axis)
            if len(cand) == 2:
                sc = _score(cand, opts, _sub(progress, 0.25 + 0.25 * axis, 0.5 + 0.25 * axis, name))
                if sc < best_score * 0.75 - 0.5:
                    best, best_score = cand, sc
        if best is not pieces:
            axis = "xyz"[int(np.argmax(np.abs(best[0].cut_normals[-2])))]
            log.append(f"{name}: cut in two across {axis} (left unsupported {base:.1f} -> {best_score - 2:.1f}, "
                       f"counting 3 cm2 per floating region, after the hollow treatment)")
            pieces = best
    # cut until every piece fits the bed in some orientation
    changed = True
    while changed and len(pieces) < max_pieces:
        changed = False
        for i, pc in enumerate(pieces):
            if _best_orientation(pc.solid, pc.cut_normals) is None:
                if not allow_cut:
                    log.append(f"{pc.name}: does not fit the bed at this scale; allow cutting or scale it down")
                    continue
                axis = int(np.argmax(X.extent(pc.solid)))
                parts = _split(pc, axis)
                if len(parts) == 2:
                    log.append(f"{pc.name}: cut across its longest side to fit the {BED_MM:.0f} mm bed")
                    pieces[i:i + 1] = parts
                    changed = True
                    break
    return pieces


def fix(job_dir: str, options: Dict, progress: Optional[Progress] = None, slicer=None) -> Dict:
    """Make the chosen parts easier to print. Writes out/*.stl, renders/after_*.png and result.json."""
    from src.print_helper.render import draw
    t0 = time.time()
    path, fname = _input_file(job_dir)
    p = _stage(progress, 0, 0.03)
    p(0, "reading the model")
    parts = load_model(path, fname)
    opts = normalize_options(options, [pt.name for pt in parts])
    chosen = [pt for pt in parts if not opts["parts"] or pt.name in opts["parts"]]
    out_dir = os.path.join(job_dir, "out")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))
    rdir = os.path.join(job_dir, "renders")
    os.makedirs(rdir, exist_ok=True)
    for f in os.listdir(rdir):
        if f.startswith("after_"):
            os.remove(os.path.join(rdir, f))
    factor = opts["scale_percent"] / 100.0
    log: List[str] = []
    if abs(factor - 1) > 1e-9:
        log.append(f"scaled the whole model to {opts['scale_percent']:g}%")
    results, skipped = [], []
    slice_share = 0.25 if opts["slice_check"] and slicer else 0.0
    span = (0.97 - 0.03 - slice_share) / max(1, len(chosen))
    used_names = set()
    for k, part in enumerate(chosen):
        base = 0.03 + k * span
        p = _stage(progress, base, base + span)
        p(0, f"{part.name}: preparing")
        try:
            m = X.scale(G.to_manifold(part.V, part.T), factor)
        except ValueError as e:
            skipped.append({"name": part.name, "why": str(e)})
            log.append(f"{part.name}: skipped, {e}")
            continue
        before = G.overhang(part.V * factor, part.T)
        plan_span = 0.5 if opts["allow_cut"] else 0.0
        pieces = plan_pieces(m, part.name, opts["allow_cut"], log, opts=opts,
                             progress=_stage(progress, base, base + span * plan_span))
        b2, s2 = base + span * plan_span, span * (1 - plan_span)
        for j, pc in enumerate(pieces):
            pp = _stage(progress, b2 + s2 * j / len(pieces), b2 + s2 * (j + 1) / len(pieces))
            results.append(_finish_piece(pc, opts, out_dir, rdir, pp, log, used_names, part.name, before, draw))
    slice_res = None
    if opts["slice_check"] and slicer and results:
        p = _stage(progress, 0.97 - slice_share, 0.97)
        p(0, "slicing on the PC with Bambu Studio")
        slice_res = slicer([os.path.join(out_dir, r["file"]) for r in results], out_dir, opts, p)
        for r in results:
            r["slice"] = (slice_res.get("pieces") or {}).get(r["file"])
    result = {"options": opts, "pieces": results, "skipped": skipped, "log": log,
              "slice": {k: v for k, v in (slice_res or {}).items() if k != "pieces"} if slice_res else None,
              "seconds": round(time.time() - t0, 1), "finished": time.time()}
    _save(os.path.join(job_dir, "result.json"), result)
    if progress:
        progress(1.0, "done")
    return result


def _finish_piece(pc: Piece, opts: Dict, out_dir: str, rdir: str, p: Progress, log: List[str], used: set,
                  part_name: str, part_before: Dict, draw) -> Dict:
    import manifold3d as mf
    row = _best_orientation(pc.solid, pc.cut_normals)
    R0, t0 = X.placement(pc.solid, row["up"] if row else [0, 0, 1.0])
    m0 = X.apply(pc.solid, R0, t0)
    V0, T0 = G.arrays(m0)
    before = G.overhang(V0, T0)
    before["unsupported_cm2"] = G.unsupported_cm2(m0, V0, T0)
    r = treat_best(pc, opts, _sub(p, 0.0, 0.85, pc.name)) if row else None
    if r is None:
        r = treat(pc.solid, [0, 0, 1.0], opts, _sub(p, 0.0, 0.85, pc.name), pc.name)
    m, notes = r["m"], list(r["notes"])
    # pin holes last, so nothing above can close them
    holes = []
    for normal, pts in pc.pins:
        h = X.pin_holes(pts, normal)
        if h is not None:
            holes.append(X.apply(h, r["R"], r["t"]))
    if holes:
        m = m - mf.Manifold.batch_boolean(holes, mf.OpType.Add)
        notes.append(f"{sum(len(pts) for _, pts in pc.pins)} pin hole(s), 2.2 mm x 6 mm, for 1.75 mm filament pins")
        m = m.translate([0, 0, -m.bounding_box()[2]])
    V, T = G.arrays(m)
    red = G.unsupported_mask(m, V, T)
    after = G.overhang(V, T)
    after["unsupported_cm2"] = round(float(G.face_data(V, T)[1][red].sum() / 100), 2)
    p(0.9, f"{pc.name}: rendering")
    isl = G.islands(m, limit=20)
    slug = _slug(pc.name)
    while slug in used:
        slug += "_2"
    used.add(slug)
    fn = slug + ".stl"
    write_stl(os.path.join(out_dir, fn), V, T)
    img = f"after_{slug}.png"
    draw(V, T, os.path.join(rdir, img), pc.name, red=red)
    ext = V.max(0) - V.min(0)
    up = [round(float(x), 3) for x in r["up"]]
    log.append(f"{pc.name}: up {up}, unsupported {before['unsupported_cm2']:.1f} -> {after['unsupported_cm2']:.1f} cm2"
               + (f", {len(isl)} floating region(s)" if isl else "") + (f"; {'; '.join(notes)}" if notes else ""))
    return {"name": pc.name, "part": part_name, "file": fn, "render": img, "up": up,
            "size_mm": [round(float(e), 1) for e in ext], "volume_cm3": round(m.volume() / 1000, 1),
            "added_cm3": round(r["added"], 1), "before": part_before, "oriented": before, "after": after,
            "islands": len(isl), "notes": notes}


def _sub(p: Progress, lo: float, hi: float, name: str) -> Progress:
    def q(frac: float, msg: str = "") -> None:
        p(lo + (hi - lo) * frac, msg if msg.startswith(name) else (f"{name}: {msg}" if msg else name))
    return q
