"""PNG renders of a part from a few angles with the overhangs (more than 45 degrees, not on the plate) in red.
From print-dad/render.py."""
import numpy as np

from src.print_helper.geometry import face_data, overhang_mask

VIEWS = ((25, -60), (25, 120), (-30, -60))
MAX_RENDER_TRIS = 150_000


def draw(V: np.ndarray, T: np.ndarray, out: str, title: str = "", views=VIEWS, dpi: int = 80, red=None) -> None:
    """`red`: a mask of the faces to show red (default: the face-angle overhang test)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    over = overhang_mask(V, T) if red is None else np.asarray(red, bool)
    if len(T) > MAX_RENDER_TRIS:      # thin out huge meshes; the shape still reads
        keep = np.sort(np.random.default_rng(0).choice(len(T), MAX_RENDER_TRIS, replace=False))
        T, over = T[keep], over[keep]
    n, _, _ = face_data(V, T)
    light = np.clip(n @ np.array([0.3, -0.4, 0.85]), 0, 1) * 0.6 + 0.35
    col = np.c_[light * 0.35, light * 0.62, light * 0.95, np.ones(len(T))]
    col[over] = [0.95, 0.2, 0.2, 1]
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    fig = plt.figure(figsize=(4 * len(views), 4.2), dpi=dpi)
    lo, hi = V.min(0), V.max(0)
    r = max(float((hi - lo).max()) / 2, 1e-3)
    mid = (hi + lo) / 2
    for i, (el, az) in enumerate(views):
        ax = fig.add_subplot(1, len(views), i + 1, projection="3d")
        # back-face culling: matplotlib sorts polygons by depth only, so the far side of a closed mesh
        # (long thin layer slivers especially) would show through
        e, z = np.radians(el), np.radians(az)
        eye = np.array([np.cos(e) * np.cos(z), np.cos(e) * np.sin(z), np.sin(e)])
        front = n @ eye > -1e-6
        # edges in the face color close the hairline gaps between long thin triangles
        ax.add_collection3d(Poly3DCollection(np.stack([a[front], b[front], c[front]], 1), facecolors=col[front],
                                             edgecolors=col[front], linewidths=0.3))
        ax.set_box_aspect((1, 1, 1))
        for k, s in enumerate("xyz"):
            getattr(ax, f"set_{s}lim")(mid[k] - r, mid[k] + r)
        ax.view_init(el, az)
        ax.set_axis_off()
        ax.set_title("from below" if el < 0 else f"view {i + 1}", fontsize=9)
    fig.suptitle((title + "   " if title else "") + "(red: overhang that would need support)", fontsize=11)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
