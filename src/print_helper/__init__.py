"""3D print helper: upload an STL or 3MF and get pieces that print without supports.

Asked for: "can we make in odysseus a way to upload a 3d model and ask it to
make it easier to print? ... ask questions using a local model?"

All geometry is deterministic code here; the local model only talks and picks
options (assistant.py).

  meshio.py      read STL and 3MF (Bambu components and build transforms), write STL
  geometry.py    overhang and plate contact per orientation, height bands, islands
  transforms.py  scale, orient, cut with pin holes, fill hollows, 45 degree roofs, posts
  render.py      PNG renders with overhangs in red
  pipeline.py    analyze a job, fix it with chosen options (runs in worker.py)
  slicer.py      Bambu Studio CLI on the PC over SSH: the real slice check
  jobs.py        job folders, a one-at-a-time background queue, niced worker processes
  assistant.py   the local model that asks the questions and picks the options

numpy, manifold3d and matplotlib are imported lazily, so the rest of Odysseus
runs without them and the page says what to install.
"""


def missing_packages():
    """The packages the helper needs that are not installed."""
    import importlib.util
    return [m for m in ("numpy", "manifold3d", "matplotlib") if importlib.util.find_spec(m) is None]
