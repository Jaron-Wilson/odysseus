"""Read STL and 3MF into triangle meshes, write binary STL.

A 3MF build item becomes one part, with its component and build transforms
applied (Bambu Studio keeps each object's meshes in 3D/Objects/*.model and
places them with components). Part names come from Bambu's
Metadata/model_settings.config when there is one, else the object's name.

Ported from print-dad/analyze.py, which was checked against Bambu Studio on a
real Bambu 3MF.
"""
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS = {"m": CORE}
PROD_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
UNITS_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "meter": 1000.0, "inch": 25.4, "foot": 304.8}
MAX_TRIANGLES = 3_000_000           # a sane cap for one upload, about 150 MB of binary STL
MAX_XML_BYTES = 400 * 1024 * 1024   # uncompressed size of one model file inside a 3MF


class ModelError(ValueError):
    """The file is not a model this helper can read."""


@dataclass
class Part:
    name: str
    V: np.ndarray                    # (n, 3) float vertices, millimeters
    T: np.ndarray                    # (m, 3) int triangle indices
    plate: Optional[int] = None
    source_id: str = ""
    extra: Dict = field(default_factory=dict)


# ── 3MF ─────────────────────────────────────────────────────────────────────

def _transform(s: Optional[str]) -> np.ndarray:
    """A 3MF transform ("m00 m01 m02 m10 ... m32", row vectors) as a 4x4 column-vector matrix."""
    if not s:
        return np.eye(4)
    v = [float(x) for x in s.split()]
    if len(v) != 12:
        raise ModelError(f"bad 3MF transform: {s[:80]}")
    m = np.eye(4)
    m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
    m[:3, 3] = v[9:12]
    return m


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ModelError(f"{name} is too large ({info.file_size // 1_000_000} MB uncompressed)")
    return z.read(name)


def _objects(xml: bytes) -> Tuple[Dict[str, ET.Element], float, ET.Element]:
    root = ET.fromstring(xml)
    unit = UNITS_MM.get(root.get("unit", "millimeter"), 1.0)
    objs = {o.get("id"): o for o in root.iterfind("m:resources/m:object", NS)}
    return objs, unit, root


def _mesh(obj: ET.Element) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    mesh = obj.find("m:mesh", NS)
    if mesh is None:
        return None
    verts = mesh.find("m:vertices", NS)
    tris = mesh.find("m:triangles", NS)
    if verts is None or tris is None:
        return None
    V = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))]
                  for v in verts.iterfind("m:vertex", NS)], dtype=float).reshape(-1, 3)
    T = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                  for t in tris.iterfind("m:triangle", NS)], dtype=np.int64).reshape(-1, 3)
    if len(T) and (T.min() < 0 or T.max() >= len(V)):
        raise ModelError("a triangle points at a vertex that does not exist")
    return V, T


def load_3mf(path: str) -> List[Part]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ModelError("not a 3MF file (it is not a zip archive)")
    names = set(z.namelist())
    main = next((n for n in ("3D/3dmodel.model", "3D/3DModel.model") if n in names), None)
    if main is None:
        main = next((n for n in names if n.lower().endswith(".model") and n.lower().startswith("3d/")), None)
    if main is None:
        raise ModelError("no 3D model inside this 3MF")
    files: Dict[str, Tuple[Dict[str, ET.Element], float]] = {}

    def file_objs(fname: str):
        if fname not in files:
            objs, unit, _ = _objects(_read_member(z, fname))
            files[fname] = (objs, unit)
        return files[fname]

    main_objs, main_unit, main_root = _objects(_read_member(z, main))
    files[main] = (main_objs, main_unit)
    count = [0]

    def resolve(fname: str, oid: str, depth: int = 0) -> List[Tuple[np.ndarray, np.ndarray]]:
        """Meshes of object `oid` in file `fname`, in that object's frame (millimeters)."""
        if depth > 16:
            raise ModelError("3MF components nest too deep")
        objs, unit = file_objs(fname)
        obj = objs.get(oid)
        if obj is None:
            raise ModelError(f"3MF object {oid} is missing from {fname}")
        mesh = _mesh(obj)
        out = []
        if mesh is not None:
            count[0] += len(mesh[1])
            if count[0] > MAX_TRIANGLES:
                raise ModelError(f"more than {MAX_TRIANGLES:,} triangles; decimate it first")
            out.append((mesh[0] * unit, mesh[1]))
        for c in obj.iterfind("m:components/m:component", NS):
            sub = c.get(PROD_PATH)
            sub_file = sub.lstrip("/") if sub else fname
            if sub_file not in names:
                raise ModelError(f"3MF component file {sub_file} is missing")
            C = _transform(c.get("transform"))
            for V, T in resolve(sub_file, c.get("objectid"), depth + 1):
                H = np.c_[V, np.ones(len(V))] @ C.T
                out.append((H[:, :3], T))
        return out

    meta_names, plates = _bambu_meta(z, names)
    parts: List[Part] = []
    items = list(main_root.iterfind("m:build/m:item", NS))
    if not items:   # no build section: every top-level object once
        items = [ET.Element("item", {"objectid": oid}) for oid in main_objs]
    for i, it in enumerate(items):
        oid = it.get("objectid")
        M = _transform(it.get("transform"))
        meshes = resolve(main, oid)
        Vs, Ts, off = [], [], 0
        for V, T in meshes:
            H = np.c_[V, np.ones(len(V))] @ M.T
            Vs.append(H[:, :3])
            Ts.append(T + off)
            off += len(V)
        if not Vs or not off:
            continue
        obj = main_objs.get(oid)
        name = meta_names.get(oid) or (obj.get("name") if obj is not None else None) or f"Part {i + 1}"
        parts.append(Part(name=_clean_name(name), V=np.vstack(Vs), T=np.vstack(Ts), plate=plates.get(oid), source_id=oid))
    if not parts:
        raise ModelError("this 3MF has no printable objects")
    return _unique_names(parts)


def _bambu_meta(z: zipfile.ZipFile, names) -> Tuple[Dict[str, str], Dict[str, int]]:
    if "Metadata/model_settings.config" not in names:
        return {}, {}
    cfg = _read_member(z, "Metadata/model_settings.config").decode("utf-8", errors="replace")
    meta_names = dict(re.findall(r'<object id="(\d+)">\s*<metadata key="name" value="([^"]*)"', cfg))
    plates: Dict[str, int] = {}
    for blk in re.findall(r"<plate>(.*?)</plate>", cfg, re.S):
        pid = re.search(r'key="plater_id" value="(\d+)"', blk)
        for oid in re.findall(r'key="object_id" value="(\d+)"', blk):
            if pid:
                plates[oid] = int(pid.group(1))
    return meta_names, plates


def _clean_name(name: str) -> str:
    name = re.sub(r"\.(stl|3mf|obj|step)$", "", name.strip(), flags=re.I)
    return name[:80] or "Part"


def _unique_names(parts: List[Part]) -> List[Part]:
    seen: Dict[str, int] = {}
    for p in parts:
        n = seen.get(p.name, 0)
        seen[p.name] = n + 1
        if n:
            p.name = f"{p.name} ({n + 1})"
    return parts


# ── STL ──────────────────────────────────────────────────────────────────────

def read_stl(path: str) -> Tuple[np.ndarray, np.ndarray]:
    """Binary or ASCII STL as (V, T), with one vertex per triangle corner (weld later)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(84)
        if len(head) == 84:
            n = struct.unpack("<I", head[80:84])[0]
            if 84 + 50 * n == size:
                if n > MAX_TRIANGLES:
                    raise ModelError(f"more than {MAX_TRIANGLES:,} triangles; decimate it first")
                rec = np.frombuffer(f.read(50 * n), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3),
                                                          ("c", "<f4", 3), ("x", "<u2")])
                V = np.stack([rec["a"], rec["b"], rec["c"]], 1).reshape(-1, 3).astype(float)
                return V, np.arange(3 * n, dtype=np.int64).reshape(n, 3)
    with open(path, "rb") as f:
        text = f.read().decode("ascii", errors="replace")
    if not text.lstrip().lower().startswith("solid"):
        raise ModelError("not an STL file")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not nums or len(nums) % 3:
        raise ModelError("this STL has no triangles")
    if len(nums) // 3 > MAX_TRIANGLES:
        raise ModelError(f"more than {MAX_TRIANGLES:,} triangles; decimate it first")
    try:
        V = np.array(nums, dtype=float)
    except ValueError:
        raise ModelError("this STL has a vertex that is not a number")
    return V, np.arange(len(V), dtype=np.int64).reshape(-1, 3)


def write_stl(path: str, V: np.ndarray, T: np.ndarray, header: bytes = b"Odysseus 3D print helper") -> None:
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
    rec = np.zeros(len(T), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3), ("x", "<u2")])
    rec["n"], rec["a"], rec["b"], rec["c"] = n, a, b, c
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(header[:80].ljust(80, b" ") + struct.pack("<I", len(T)))
        f.write(rec.tobytes())
    os.replace(tmp, path)


def load_model(path: str, display_name: str = "") -> List[Part]:
    """Every part of an STL or 3MF file."""
    low = (display_name or path).lower()
    if low.endswith(".3mf"):
        return load_3mf(path)
    if low.endswith(".stl"):
        V, T = read_stl(path)
        if not len(T):
            raise ModelError("this STL has no triangles")
        return [Part(name=_clean_name(os.path.basename(display_name or path)), V=V, T=T)]
    raise ModelError("upload an .stl or .3mf file")
