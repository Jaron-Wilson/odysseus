"""The 3D print helper's geometry, files, options, slicer parsing and model choice (src/print_helper/).

The fixture is a synthetic hollow box: 40 x 40 x 30 mm with a 30 x 30 x 17 mm closed cavity (a flat ceiling
at z = 22), a stud socket coming up from the bottom (6 mm wide, 4 mm deep) and a pin socket going into the
side of the top slab (4 mm wide, 8 mm deep). Nothing from a real (licensed) model is committed.
"""
import datetime
import json
import os
import struct
import zipfile

import numpy as np
import pytest

mf = pytest.importorskip("manifold3d")
pytest.importorskip("matplotlib")

from src.print_helper import geometry as G  # noqa: E402
from src.print_helper import transforms as X  # noqa: E402
from src.print_helper import meshio, pipeline, slicer  # noqa: E402

CAVITY_CM3 = 30 * 30 * 17 / 1000


def hollow_box():
    outer = mf.Manifold.cube([40, 40, 30])
    cavity = mf.Manifold.cube([30, 30, 17]).translate([5, 5, 5])
    socket = mf.Manifold.cylinder(4, 3, 3, 32).translate([20, 20, -0.01])
    side = mf.Manifold.cylinder(8, 2, 2, 32).rotate([0, 90, 0]).translate([-0.01, 20, 26])
    return outer - cavity - socket - side, socket, side, cavity


@pytest.fixture(scope="module")
def box():
    return hollow_box()


def write_box_stl(path):
    m = hollow_box()[0]
    V, T = G.arrays(m)
    meshio.write_stl(path, V, T)
    return V, T


# ── Measuring ───────────────────────────────────────────────────────────────

def test_the_ceiling_is_the_overhang_and_the_floor_is_on_the_plate(box):
    m = box[0]
    V, T = G.arrays(m)
    o = G.overhang(V, T)
    # the 30 x 30 ceiling plus the two socket ceilings; the 40 x 40 bottom minus the socket is on the plate
    assert 9.3 < o["overhang_cm2"] < 9.8
    assert 15.5 < o["bed_cm2"] < 16.0
    assert o["height_mm"] == 30.0
    assert 9.2 < G.ceilings_cm2(V, T) < 9.5
    assert 9.2 < G.unsupported_cm2(m, V, T) < 9.8
    bands = G.height_bands(V, T)
    assert any(b["z_mm"][0] <= 22 < b["z_mm"][1] and b["flat_cm2"] > 8.9 for b in bands)


def test_orientations_prefer_plate_contact_and_little_overhang():
    # an upside-down T: printed as is it has a 30 mm shelf overhang; stood on the bar's end it has none
    t = mf.Manifold.cube([60, 10, 10]) + mf.Manifold.cube([10, 10, 40]).translate([25, 0, -40])
    V, T = G.arrays(t)
    assert G.overhang(V, T)["overhang_cm2"] > 4
    best = G.rank_orientations(V, T)[0]
    assert best["overhang_cm2"] < 0.01 and best["bed_cm2"] >= G.MIN_BED_CM2


def test_analysis_finds_the_hollow_and_the_sockets(box):
    V, T = G.arrays(box[0])
    row, red = pipeline.analyze_part(V, T)
    assert row["closed"] and row["fits_bed"]
    assert abs(row["hollow_cm3"] - CAVITY_CM3) < 0.6
    assert row["sockets"] == 2
    assert row["islands"] == 0
    assert red.sum() > 0


def test_the_keepouts_are_the_sockets_not_the_cavity(box):
    m, socket, side, cavity = box
    ko = X.find_keepouts(m)
    parts = ko.decompose()
    assert len(parts) == 2
    assert (ko ^ socket).volume() > 0.9 * socket.volume() - 15
    assert (ko ^ side).volume() > 0.5 * side.volume()
    assert (ko ^ cavity).volume() < 1.0


# ── Fixing ──────────────────────────────────────────────────────────────────

def test_a_45_degree_roof_keeps_the_hollow_and_the_sockets_open(box):
    m, socket, side, cavity = box
    ko = X.find_keepouts(m)
    r, added = X.roof(m, keepout=ko)
    V, T = G.arrays(r)
    # a hip roof over a 30 mm wide ceiling: 30 x 30 x 15 minus the pyramid left open is about 9 cm3
    assert 8.0 < added < 10.0
    left = cavity.volume() - (r ^ cavity).volume()
    assert left > 0.35 * cavity.volume()                  # the hollow stays open
    assert (r ^ socket).volume() < 1e-6 and (r ^ side).volume() < 1e-6
    assert G.unsupported_cm2(r, V, T) < 0.6               # only the small socket bridges remain
    assert G.islands(r) == []


def test_without_keepouts_the_roof_closes_the_stud_socket(box):
    m, socket, _, _ = box
    r, _ = X.roof(m)
    assert (r ^ socket).volume() > 10                      # its flat ceiling got a roof: why keep-outs exist


def test_two_hollows_that_meet_leave_no_floating_knife_edge():
    # two 20 mm cavities joined by a channel: their roofs meet in a wedge that would end in a knife edge
    outer = mf.Manifold.cube([60, 30, 30])
    cav = (mf.Manifold.cube([20, 20, 18]).translate([4, 5, 4]) + mf.Manifold.cube([20, 20, 18]).translate([36, 5, 4])
           + mf.Manifold.cube([16, 6, 18]).translate([22, 12, 4]))
    m = outer - cav
    r, added = X.roof(m)
    V, T = G.arrays(r)
    assert added > 0
    assert G.islands(r) == []
    assert G.unsupported_cm2(r, V, T) < 0.3


def test_fill_fills_the_enclosed_hollow_and_keeps_the_sockets(box):
    m, socket, side, _ = box
    ko = X.find_keepouts(m)
    f, added = X.fill_hollows(m, keepout=ko)
    assert abs(added - CAVITY_CM3) < 0.3
    assert (f ^ socket).volume() < 1e-6 and (f ^ side).volume() < 1e-6


def test_a_cut_gets_pin_holes_on_both_sides():
    m = mf.Manifold.cube([40, 40, 30])
    a, b, pts = X.split(m, [0, 0, 1.0], 15.0)
    assert len(pts) >= 2
    assert abs(a.volume() + b.volume() - m.volume()) < 1.0
    holes = X.pin_holes(pts, [0, 0, 1.0])
    a2, b2 = a - holes, b - holes
    per_side = np.pi * X.PIN_R ** 2 * X.PIN_DEPTH
    assert a.volume() - a2.volume() == pytest.approx(len(pts) * per_side, rel=0.08)
    assert b.volume() - b2.volume() == pytest.approx(len(pts) * per_side, rel=0.08)


def test_a_part_too_big_for_the_bed_is_cut_to_fit_only_when_allowed():
    bar = mf.Manifold.cube([400, 30, 20])
    log = []
    assert len(pipeline.plan_pieces(bar, "Bar", allow_cut=False, log=log)) == 1
    assert any("does not fit" in line for line in log)
    pieces = pipeline.plan_pieces(bar, "Bar", allow_cut=True, log=[])
    assert len(pieces) == 2
    for pc in pieces:
        assert pipeline._best_orientation(pc.solid, pc.cut_normals) is not None
        assert pc.pins and len(pc.pins[0][1]) >= 1


def test_placement_puts_the_chosen_side_up_on_the_plate(box):
    m = box[0]
    R, t = X.placement(m, [1.0, 0, 0])
    p = X.apply(m, R, t)
    b = p.bounding_box()
    assert abs(b[2]) < 1e-6
    assert abs((b[0] + b[3]) / 2) < 1e-6 and abs((b[1] + b[4]) / 2) < 1e-6
    assert b[5] - b[2] == pytest.approx(40.0)


def test_posts_hold_up_an_outside_ledge():
    ledge = mf.Manifold.cube([10, 10, 30]) + mf.Manifold.cube([30, 10, 4]).translate([10, 0, 26])
    out, added, n = X.posts(ledge)
    assert n >= 1 and added > 0
    V, T = G.arrays(out)
    assert G.unsupported_cm2(out, V, T) < 0.5


# ── Files ───────────────────────────────────────────────────────────────────

def test_binary_and_ascii_stl_read_back(tmp_path, box):
    V, T = write_box_stl(str(tmp_path / "box.stl"))
    V2, T2 = meshio.read_stl(str(tmp_path / "box.stl"))
    assert len(T2) == len(T)
    m = G.to_manifold(V2, T2)
    assert m.volume() == pytest.approx(box[0].volume(), rel=1e-4)
    asc = "solid t\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid t\n"
    (tmp_path / "a.stl").write_text(asc)
    V3, T3 = meshio.read_stl(str(tmp_path / "a.stl"))
    assert V3.shape == (3, 3) and T3.tolist() == [[0, 1, 2]]


def test_bad_files_are_refused(tmp_path):
    (tmp_path / "x.stl").write_bytes(b"\x00" * 200)
    with pytest.raises(meshio.ModelError):
        meshio.load_model(str(tmp_path / "x.stl"))
    (tmp_path / "x.3mf").write_bytes(b"not a zip")
    with pytest.raises(meshio.ModelError):
        meshio.load_model(str(tmp_path / "x.3mf"))
    with pytest.raises(meshio.ModelError):
        meshio.load_model(str(tmp_path / "x.obj"))


def _model_xml(verts, tris, extra_objects="", build=""):
    vs = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{meshio.CORE}" '
            f'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            f'<resources><object id="1" type="model"><mesh><vertices>{vs}</vertices><triangles>{ts}</triangles>'
            f'</mesh></object>{extra_objects}</resources><build>{build}</build></model>')


def test_a_bambu_style_3mf_applies_component_and_build_transforms(tmp_path, box):
    V, T = G.arrays(box[0])
    sub = _model_xml(V.tolist(), T.tolist())
    # object 2 in the main file places object 1 of the sub file shifted +5 x; the build item shifts +100 y
    main = (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{meshio.CORE}" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
            '<object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>'
            '</components></object></resources>'
            '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 100 0"/></build></model>')
    cfg = '<config><object id="2">\n<metadata key="name" value="Hollow Box.stl"/></object>' \
          '<plate><metadata key="plater_id" value="3"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate></config>'
    path = tmp_path / "box.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/Objects/object_1.model", sub)
        z.writestr("Metadata/model_settings.config", cfg)
    (part,) = meshio.load_model(str(path))
    assert part.name == "Hollow Box" and part.plate == 3
    assert part.V.min(0) == pytest.approx([5, 100, 0], abs=1e-6)
    assert part.V.max(0) == pytest.approx([45, 140, 30], abs=1e-6)


# ── Options ─────────────────────────────────────────────────────────────────

def test_options_are_checked_and_clamped():
    o = pipeline.normalize_options({"scale_percent": "5000", "hollows": "melt", "allow_cut": "yes", "posts": True,
                                    "parts": ["torso", "nope", 3], "colors": "ams", "evil": 1},
                                   part_names=["Torso", "Leg"])
    assert o["scale_percent"] == 1000.0
    assert o["hollows"] == "roof"
    assert o["allow_cut"] is False and o["posts"] is True
    assert o["parts"] == ["Torso"]
    assert o["colors"] == "ams"
    assert "evil" not in o
    assert pipeline.normalize_options({"scale_percent": float("nan")})["scale_percent"] == 100.0


# ── The pipeline on a job folder ────────────────────────────────────────────

def test_analyze_and_fix_a_job_folder(tmp_path):
    job = tmp_path / "job"
    (job / "input").mkdir(parents=True)
    write_box_stl(str(job / "input" / "box.stl"))
    seen = []
    a = pipeline.analyze(str(job), lambda f, m: seen.append(f))
    assert a["parts"][0]["name"] == "box" and (job / "renders" / a["parts"][0]["render"]).exists()
    assert "hollow inside" in a["summary"] and a["max_scale_percent"] > 600
    assert seen[-1] == 1.0
    calls = []

    def fake_slicer(files, out_dir, opts, p):
        calls.append([os.path.basename(f) for f in files])
        return {"ok": True, "pc": "PC", "pieces": {os.path.basename(f): {"ok": True, "time": "1h 2m", "weight_g": 20.5,
                                                                         "needs_support": False} for f in files}}
    r = pipeline.fix(str(job), {"hollows": "roof", "scale_percent": 50}, slicer=fake_slicer)
    (piece,) = r["pieces"]
    assert sorted(piece["size_mm"]) == [15.0, 20.0, 20.0]   # half size, on whichever side overhangs least
    assert piece["after"]["unsupported_cm2"] < 0.2
    assert piece["oriented"]["unsupported_cm2"] > 1
    assert (job / "out" / piece["file"]).exists() and (job / "renders" / piece["render"]).exists()
    assert calls == [[piece["file"]]] and piece["slice"]["weight_g"] == 20.5
    assert json.loads((job / "result.json").read_text())["options"]["scale_percent"] == 50
    assert any("scaled the whole model to 50%" in line for line in r["log"])


# ── The slicer's output ─────────────────────────────────────────────────────

def test_the_slicer_output_is_read():
    r = slicer.parse_piece({
        "file": "leg.stl", "exit": 0, "seconds": 3.1,
        "cantilever": ["0", "3.21873e+07"],
        "warnings": ["It seems object leg.stl has floating cantilever. Please re-orient the object or enable support generation."],
        "header": ["; model printing time: 46m 21s; total estimated time: 52m 8s", "; total layer number: 323",
                   "; total filament weight [g] : 41.09"],
        "support_lines": 0, "m620": 2})
    assert r["ok"] and r["time"] == "52m 8s" and r["weight_g"] == 41.09 and r["layers"] == 323
    assert r["max_cantilever_mm"] == 32.2
    assert r["floating_cantilever"] and r["needs_support"] and not r["floating_regions"]
    assert r["filament_swaps"] == 0
    bad = slicer.parse_piece({"file": "x.stl", "exit": 1, "errors": ["process not compatible with printer"]})
    assert not bad["ok"] and "not compatible" in bad["error"]


def test_an_unreachable_pc_skips_the_slice_check(monkeypatch, tmp_path):
    def nope(cfg):
        raise slicer.SliceError("the PC did not answer within 25 s")
    monkeypatch.setattr(slicer, "_resolve_target", nope)
    out = slicer.slice_pieces([str(tmp_path / "a.stl")], str(tmp_path), {}, {})
    assert out == {"ok": False, "skipped": "the PC did not answer within 25 s"}


def test_the_powershell_quotes_paths():
    s = slicer.slice_script(slicer.DEFAULTS, r"C:\Temp\odysseus-print\ab", r"C:\it's here", ["a.stl"])
    assert r"$p='C:\it''s here'" in s and "'a.stl'" in s
    assert "enable_support" in slicer.prepare_script(slicer.DEFAULTS, "tok")


# ── The assistant ───────────────────────────────────────────────────────────

def test_the_reply_is_parsed_from_json_around_thinking_and_fences():
    from src.print_helper import assistant
    r = assistant.parse_reply('<think>{"x": 1} hmm</think>Sure.\n```json\n{"say": "Keep the sockets open?", '
                              '"options": {"keep_holes": true}, "run": false}\n```')
    assert r == {"say": "Keep the sockets open?", "options": {"keep_holes": True}, "run": False}
    assert assistant.parse_reply("just text")["say"] == "just text"


def test_totoro_is_skipped_on_monday_mornings_only():
    from src.print_helper import assistant
    mon_10 = datetime.datetime(2026, 10, 19, 10, 0)
    mon_13 = datetime.datetime(2026, 10, 19, 13, 0)
    tue_9 = datetime.datetime(2026, 10, 20, 9, 0)
    assert assistant.is_busy("totoro:11435", "http://100.1.2.3:11435/v1", mon_10)
    assert not assistant.is_busy("totoro:11435", "http://100.1.2.3:11435/v1", mon_13)
    assert not assistant.is_busy("totoro:11435", "http://100.1.2.3:11435/v1", tue_9)
    assert not assistant.is_busy("PC Ollama", "http://100.1.2.4:11434/v1", mon_10)


def test_only_local_endpoints_are_used_best_model_first(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as cdb
    from src.print_helper import assistant, jobs

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(jobs, "load_config", lambda: {k: "" for k in jobs.CONFIG_KEYS})
    with factory() as s:
        s.add(cdb.ModelEndpoint(id="cloud", name="OpenRouter", base_url="https://openrouter.ai/api/v1",
                                cached_models=json.dumps(["qwen3.8:27b"])))
        s.add(cdb.ModelEndpoint(id="totoro", name="totoro:11435", base_url="http://100.102.86.125:11435/v1",
                                cached_models=json.dumps(["gpt-oss:20b", "qwen3.8:27b", "nomic-embed-text"])))
        s.add(cdb.ModelEndpoint(id="pc", name="PC Ollama", base_url="http://192.168.1.20:11434/v1",
                                cached_models=json.dumps(["llama3.2:3b", "gemma4:26b"])))
        s.commit()
    tue = datetime.datetime(2026, 10, 20, 9, 0)
    cands, _ = assistant.candidates(now=tue)
    assert [(c["endpoint_id"], c["model"]) for c in cands] == [
        ("totoro", "qwen3.8:27b"), ("totoro", "gpt-oss:20b"), ("pc", "gemma4:26b"), ("pc", "llama3.2:3b")]
    mon = datetime.datetime(2026, 10, 19, 9, 0)
    cands, notes = assistant.candidates(now=mon)
    assert [c["endpoint_id"] for c in cands] == ["pc", "pc"]
    assert any("Monday" in n for n in notes)
