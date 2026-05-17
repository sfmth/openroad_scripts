from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

from vastu.core.block import (
    Block,
    HardBlock,
    HierarchicalBlock,
    Placement,
    SoftBlock,
    StructuredBlock,
)
from vastu.core.hypergraph import Net, pin_position


_KIND_COLOR = {
    SoftBlock: ("#aedfff", "#3187c4"),
    HardBlock: ("#ffd5a8", "#cc6e1f"),
    StructuredBlock: ("#d2c4e9", "#6e4ba8"),
    HierarchicalBlock: ("#cfe9d2", "#3a8a4e"),
}

# Distinct colors for the *boundary overlay* layer (drawn on top of leaves,
# stroked, no fill). Each kind gets a high-contrast outline so the user can
# see which container a leaf belongs to even when the leaves fill the box.
_OUTER_BOUNDARY = {
    StructuredBlock: ("#4a2378", "-"),     # dark purple, solid
    HierarchicalBlock: ("#0e8b46", "-"),   # darker green, solid
    SoftBlock: ("#1a548d", "--"),          # dark blue, dashed
    HardBlock: ("#a05010", "--"),          # dark orange, dashed
}


def plot_floorplan(
    blocks: dict[str, Block],
    placements: dict[str, Placement],
    nets: Iterable[Net] = (),
    *,
    outer_blocks: dict[str, Block] | None = None,
    outer_placements: dict[str, Placement] | None = None,
    out_path: str | Path | None = None,
    title: str | None = None,
    show_pins: bool = False,
    show_nets: bool = False,
    show_leaf_labels: bool = True,
    figsize: tuple[float, float] = (8.0, 8.0),
):
    """Render a floorplan to a matplotlib figure.

    `placements` is the primary layer (drawn solid). For hierarchical results
    you can also pass `outer_placements` (e.g. top-level block bboxes) which
    are drawn first as faint dashed outlines so you can see how the leaves
    aggregate into their containing blocks. `nets` are drawn against
    `outer_placements` if provided (since that's where top-level pins live),
    otherwise against `placements`.
    """
    fig, ax = plt.subplots(figsize=figsize)
    if not placements:
        ax.set_title(title or "(no placements)")
        return fig
    xs = [p.x for p in placements.values()]
    ys = [p.y for p in placements.values()]
    x2s = [p.x2 for p in placements.values()]
    y2s = [p.y2 for p in placements.values()]
    if outer_placements:
        xs.extend(p.x for p in outer_placements.values())
        ys.extend(p.y for p in outer_placements.values())
        x2s.extend(p.x2 for p in outer_placements.values())
        y2s.extend(p.y2 for p in outer_placements.values())
    margin = 0.05 * max(max(x2s) - min(xs), max(y2s) - min(ys), 1.0)
    ax.set_xlim(min(xs) - margin, max(x2s) + margin)
    ax.set_ylim(min(ys) - margin, max(y2s) + margin)
    ax.set_aspect("equal")

    # Outer (containing) blocks: faint tinted backdrop drawn behind leaves,
    # so the leaves clearly belong to a container. Crisp colored outline gets
    # added at the end (after leaves) so it sits on top.
    if outer_placements:
        for name, pl in outer_placements.items():
            blk = (outer_blocks or {}).get(name)
            face, edge = "#eeeeee", "#888888"
            for kind, (f, e) in _KIND_COLOR.items():
                if isinstance(blk, kind):
                    face, edge = f, e
                    break
            rect = mpatches.Rectangle(
                (pl.x, pl.y), pl.w, pl.h,
                linewidth=0, edgecolor="none", facecolor=face, alpha=0.18,
            )
            ax.add_patch(rect)

    for name, pl in placements.items():
        blk = blocks.get(name)
        face, edge = "#dddddd", "#444444"
        for kind, (f, e) in _KIND_COLOR.items():
            if isinstance(blk, kind):
                face, edge = f, e
                break
        rect = mpatches.Rectangle(
            (pl.x, pl.y), pl.w, pl.h,
            linewidth=1.2, edgecolor=edge, facecolor=face, alpha=0.7,
        )
        ax.add_patch(rect)
        # Skip text for very small rectangles (illegible anyway), or when the
        # caller has asked to suppress leaf labels entirely (e.g. cluster
        # floorplans where leaf names are huge hierarchical paths).
        if show_leaf_labels and pl.w * pl.h > 25:
            ax.text(
                pl.x + pl.w / 2, pl.y + pl.h / 2, name,
                ha="center", va="center", fontsize=8, color="#222",
            )
        if show_pins and blk:
            for pin in blk.pins:
                px, py = pin_position(pin, pl)
                ax.plot(px, py, "o", markersize=3, color="black")

    # Crisp boundary overlay for outer blocks — drawn on top of the leaves so
    # the user can see which container each leaf belongs to.
    if outer_placements:
        for name, pl in outer_placements.items():
            blk = (outer_blocks or {}).get(name)
            edge, ls = "#444", "-"
            for kind, (e, l) in _OUTER_BOUNDARY.items():
                if isinstance(blk, kind):
                    edge, ls = e, l
                    break
            rect = mpatches.Rectangle(
                (pl.x, pl.y), pl.w, pl.h,
                linewidth=2.2, edgecolor=edge, facecolor="none",
                linestyle=ls,
            )
            ax.add_patch(rect)
            # Label hugs the top-left interior corner so it doesn't clash with
            # leaf labels in the centre of the box.
            ax.text(
                pl.x + pl.w * 0.02, pl.y + pl.h * 0.98, name,
                ha="left", va="top", fontsize=9, color=edge,
                weight="bold",
                bbox=dict(boxstyle="round,pad=0.15", fc="white",
                          ec=edge, alpha=0.85, lw=0.8),
            )

    if show_nets:
        # Nets are typically defined against outer_placements (top-level
        # instances) when provided, since that's where pin metadata lives.
        net_pls = outer_placements or placements
        net_blks = outer_blocks or blocks
        for net in nets:
            pts = []
            for pin in net.pins:
                pl = net_pls.get(pin.block)
                if pl:
                    pts.append(pin_position(pin, pl))
            if len(pts) < 2:
                continue
            # Star topology: draw lines from each pin to the net's centroid.
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            # Heavier nets get thicker lines.
            lw = max(0.4, min(1.5, 0.4 + 0.05 * len(pts) * net.weight))
            for px, py in pts:
                ax.plot(
                    [px, cx], [py, cy],
                    color="#d4604c", linewidth=lw, alpha=0.55,
                    solid_capstyle="round",
                )
            ax.plot(cx, cy, "x", markersize=3, color="#d4604c", alpha=0.6)

    ax.set_title(title or f"floorplan ({len(placements)} blocks)")
    # tight_layout emits a noisy UserWarning when label boxes overflow the
    # margin (common with long outer labels). The layout still renders fine;
    # silence it so Tcl exec doesn't see stderr noise from a successful run.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        fig.tight_layout()
    if out_path:
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    return fig
