"""Fixed normalized layout model for E-14 vote-count crops.

Coordinates are normalized as fractions of rendered page width/height. The
initial values are intentionally easy to tune: the first MVP target is
inspectable crop/debug output, then iterative coordinate refinement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from . import config


@dataclass(frozen=True)
class NormalizedBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def to_pixels(self, width: int, height: int) -> "PixelBox":
        return PixelBox(
            x0=max(0, min(width, round(self.x0 * width))),
            y0=max(0, min(height, round(self.y0 * height))),
            x1=max(0, min(width, round(self.x1 * width))),
            y1=max(0, min(height, round(self.y1 * height))),
        ).normalized()


@dataclass(frozen=True)
class PixelBox:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return max(0, self.x1 - self.x0)

    @property
    def height(self) -> int:
        return max(0, self.y1 - self.y0)

    def normalized(self) -> "PixelBox":
        x0, x1 = sorted((self.x0, self.x1))
        y0, y1 = sorted((self.y0, self.y1))
        return PixelBox(x0=x0, y0=y0, x1=x1, y1=y1)

    def inset(self, frac_x: float, frac_y: float) -> "PixelBox":
        """Shrink the box by a fraction of its own size on each side.

        Used to drop printed table border/divider lines that hug a slot's
        edges, so blank cells are not read as digit-like marks.
        """
        dx = round(self.width * frac_x)
        dy = round(self.height * frac_y)
        return PixelBox(
            x0=self.x0 + dx,
            y0=self.y0 + dy,
            x1=self.x1 - dx,
            y1=self.y1 - dy,
        ).normalized()

    def split_columns(self, count: int) -> list["PixelBox"]:
        if count <= 0:
            raise ValueError("count must be positive")
        step = self.width / count
        out = []
        for i in range(count):
            out.append(PixelBox(
                x0=round(self.x0 + step * i),
                y0=self.y0,
                x1=round(self.x0 + step * (i + 1)),
                y1=self.y1,
            ))
        return out


@dataclass(frozen=True)
class RowLayout:
    page_number: int
    row_type: str
    row_number: int
    candidate_number: int | None
    candidate_name: str | None
    section: str
    box: NormalizedBox


@dataclass(frozen=True)
class FieldLayout:
    row: RowLayout
    field_box: PixelBox
    slot_boxes: tuple[PixelBox, PixelBox, PixelBox]
    layout_confidence: float = 0.5


@dataclass(frozen=True)
class RoundLayout:
    """All geometry for one election round, keyed into ``LAYOUT`` by round name.

    The 3-digit slot model, inset logic and the CV feature path are geometry-agnostic and live
    outside this struct; only the per-round coordinate TABLES differ. ``ready`` gates whether the
    round may actually be cropped: the R1 layout is fully calibrated (``ready=True``), while the
    R2 (runoff) layout ships as a structural STUB with placeholder coordinates (``ready=False``)
    so any attempt to process R2 actas fails loudly until real coords are filled in — never silent
    garbage crops. See ``_require_ready``.
    """
    rows_by_page: dict[int, tuple[RowLayout, ...]]
    page_vote_columns: dict[int, NormalizedBox]
    candidate_names: dict[int, str]
    n_slots: int = 3
    slot_inset_x: float = 0.06
    slot_inset_y: float = 0.04
    ready: bool = False

    @property
    def n_pages(self) -> int:
        return len(self.rows_by_page)

    @property
    def n_candidates(self) -> int:
        return sum(
            1 for rows in self.rows_by_page.values() for r in rows if r.row_type == "candidate"
        )


# Conservative first-pass crop columns. X starts at the printed divider between
# the candidate area and VOTACION cells; the previous value started near slot 2.
VOTE_X0 = 0.690
VOTE_X1 = 0.942

# Inner inset applied to each slot box before feature extraction, as a fraction
# of the slot's own width/height. This trims the printed table border and the
# vertical dividers that hug slot edges, which would otherwise be read as
# digit-like or placeholder-like marks in blank cells. Borders observed at the
# rightmost ~3% of a slot, so ~6% horizontally clears them with margin.
SLOT_INSET_X = 0.06
SLOT_INSET_Y = 0.04

PAGE_VOTE_COLUMNS = {
    1: NormalizedBox(VOTE_X0, 0.205, VOTE_X1, 0.825),
    2: NormalizedBox(VOTE_X0, 0.135, VOTE_X1, 0.875),
}

# Fixed ballot order for this E-14 (PRE) form: candidate_number -> name. The
# printed order is identical on every acta, so each row's identity is purely
# positional. Rows 1-7 are on page 1, rows 8-13 on page 2.
CANDIDATE_NAMES = {
    1: "Ivan Cepeda",
    2: "Claudia Lopez",
    3: "Santiago Botero",
    4: "Abelardo de la Espriella",
    5: "Mauricio Lizcano",
    6: "Miguel Uribe",
    7: "Sondra Macollins",
    8: "Roy Barreras",
    9: "Eduardo Caicedo",
    10: "Gustavo Matamoros",
    11: "Paloma Valencia",
    12: "Sergio Fajardo",
    13: "Gilberto Murillo",
}


def _candidate_rows(
    page_number: int,
    first: int,
    last: int,
    cell_y0: float,
    cell_y1: float,
    band_top: float,
    band_bottom: float,
) -> list[RowLayout]:
    count = last - first + 1
    step = (cell_y1 - cell_y0) / count
    rows = []
    for idx, candidate_number in enumerate(range(first, last + 1), start=0):
        row_top = cell_y0 + idx * step
        rows.append(RowLayout(
            page_number=page_number,
            row_type="candidate",
            row_number=candidate_number,
            candidate_number=candidate_number,
            candidate_name=CANDIDATE_NAMES.get(candidate_number),
            section="votacion",
            box=NormalizedBox(
                VOTE_X0,
                row_top + band_top * step,
                VOTE_X1,
                row_top + band_bottom * step,
            ),
        ))
    return rows


def _candidate_rows_from_edges(
    page_number: int,
    first: int,
    cell_edges: tuple[tuple[float, float], ...],
    band_top: float,
    band_bottom: float,
) -> list[RowLayout]:
    rows = []
    for idx, (top, bottom) in enumerate(cell_edges):
        candidate_number = first + idx
        height = bottom - top
        rows.append(RowLayout(
            page_number=page_number,
            row_type="candidate",
            row_number=candidate_number,
            candidate_number=candidate_number,
            candidate_name=CANDIDATE_NAMES.get(candidate_number),
            section="votacion",
            box=NormalizedBox(VOTE_X0, top + band_top * height, VOTE_X1, top + band_bottom * height),
        ))
    return rows


# Cell edges differ by page. The row crop itself intentionally targets only the
# handwritten number band inside each cell, leaving out most horizontal borders.
PAGE1_ROWS = tuple(_candidate_rows_from_edges(1, 1, (
    (0.384, 0.468),
    (0.475, 0.547),
    (0.554, 0.626),
    (0.633, 0.706),
    (0.713, 0.784),
    (0.791, 0.865),
    (0.872, 0.944),
), 0.08, 0.70))
PAGE2_CANDIDATE_ROWS = tuple(_candidate_rows_from_edges(2, 8, (
    (0.255, 0.335),
    (0.342, 0.421),
    (0.428, 0.506),
    (0.513, 0.592),
    (0.599, 0.678),
    (0.685, 0.764),
), 0.28, 0.76))
PAGE2_SUMMARY_ROWS = (
    RowLayout(2, "summary", 14, None, "votos_en_blanco", "summary", NormalizedBox(VOTE_X0, 0.780, VOTE_X1, 0.803)),
    RowLayout(2, "summary", 15, None, "votos_nulos", "summary", NormalizedBox(VOTE_X0, 0.811, VOTE_X1, 0.833)),
    RowLayout(2, "summary", 16, None, "votos_no_marcados", "summary", NormalizedBox(VOTE_X0, 0.844, VOTE_X1, 0.866)),
    RowLayout(2, "summary", 17, None, "total_votos", "summary", NormalizedBox(VOTE_X0, 0.878, VOTE_X1, 0.900)),
)

ROWS_BY_PAGE = {
    1: PAGE1_ROWS,
    2: PAGE2_CANDIDATE_ROWS + PAGE2_SUMMARY_ROWS,
}


# --- R2 (runoff) layout STUB -------------------------------------------------
# The runoff is a 2-candidate, almost-certainly single-page form. The 3-digit slot model, inset
# logic and CV feature path are unchanged; only the coordinate TABLES below differ — and they are
# PLACEHOLDERS. ready=False (in LAYOUT["r2"]) makes any crop attempt raise, so R2 can never produce
# silent garbage until a real blank/simulacro R2 acta is measured and these are filled in.
R2_CANDIDATE_NAMES: dict[int, str] = {
    1: "TODO candidato 1 (segunda vuelta)",
    2: "TODO candidato 2 (segunda vuelta)",
}
# TODO(form): every box below is a structural placeholder, NOT a measured R2 coordinate. Replace
# with real cell edges from an actual runoff acta, then flip LAYOUT["r2"].ready = True.
_R2_PAGE1_ROWS: tuple[RowLayout, ...] = (
    RowLayout(1, "candidate", 1, 1, R2_CANDIDATE_NAMES[1], "votacion", NormalizedBox(VOTE_X0, 0.40, VOTE_X1, 0.45)),
    RowLayout(1, "candidate", 2, 2, R2_CANDIDATE_NAMES[2], "votacion", NormalizedBox(VOTE_X0, 0.47, VOTE_X1, 0.52)),
    RowLayout(1, "summary", 3, None, "votos_en_blanco", "summary", NormalizedBox(VOTE_X0, 0.60, VOTE_X1, 0.63)),
    RowLayout(1, "summary", 4, None, "votos_nulos", "summary", NormalizedBox(VOTE_X0, 0.64, VOTE_X1, 0.67)),
    RowLayout(1, "summary", 5, None, "votos_no_marcados", "summary", NormalizedBox(VOTE_X0, 0.68, VOTE_X1, 0.71)),
    RowLayout(1, "summary", 6, None, "total_votos", "summary", NormalizedBox(VOTE_X0, 0.72, VOTE_X1, 0.75)),
)


# The round registry. LAYOUT["r1"] is the live, fully-calibrated first-round geometry (pinned
# byte-for-byte by a golden test); LAYOUT["r2"] is the guarded stub above.
LAYOUT: dict[str, RoundLayout] = {
    "r1": RoundLayout(
        rows_by_page=ROWS_BY_PAGE,
        page_vote_columns=PAGE_VOTE_COLUMNS,
        candidate_names=CANDIDATE_NAMES,
        n_slots=3, slot_inset_x=SLOT_INSET_X, slot_inset_y=SLOT_INSET_Y,
        ready=True,
    ),
    "r2": RoundLayout(
        rows_by_page={1: _R2_PAGE1_ROWS},
        page_vote_columns={1: NormalizedBox(VOTE_X0, 0.38, VOTE_X1, 0.76)},  # TODO(form)
        candidate_names=R2_CANDIDATE_NAMES,
        n_slots=3, slot_inset_x=SLOT_INSET_X, slot_inset_y=SLOT_INSET_Y,
        ready=False,
    ),
}


def _resolve_round(round: str | None) -> str:
    return (round or config.ELECTION_ROUND or "r1").strip().lower()


def layout_for(round: str | None = None) -> RoundLayout:
    """The RoundLayout for ``round`` (default: the active ``config.ELECTION_ROUND``)."""
    r = _resolve_round(round)
    try:
        return LAYOUT[r]
    except KeyError as exc:
        raise ValueError(f"unknown election round: {r!r} (known: {sorted(LAYOUT)})") from exc


def _require_ready(lay: RoundLayout, round: str) -> None:
    """Refuse to produce crops for a round whose coordinates are still placeholders."""
    if not lay.ready:
        raise RuntimeError(
            f"layout for round {round!r} is a STUB with placeholder coordinates — refusing to "
            f"crop (would be silent garbage). Calibrate LAYOUT[{round!r}] from a real {round} "
            f"acta and set ready=True first.")


def rows_for_page(page_number: int, round: str | None = None) -> tuple[RowLayout, ...]:
    # Metadata only (no cropping), so this is allowed even on an un-calibrated stub round.
    return layout_for(round).rows_by_page.get(page_number, ())


def vote_column_box(page_number: int, width: int, height: int, round: str | None = None) -> PixelBox:
    r = _resolve_round(round)
    lay = layout_for(r)
    _require_ready(lay, r)
    try:
        box = lay.page_vote_columns[page_number]
    except KeyError as exc:
        raise ValueError(f"unsupported page for {r} layout: {page_number}") from exc
    return box.to_pixels(width, height)


def field_layouts_for_page(
    page_number: int, width: int, height: int, round: str | None = None
) -> list[FieldLayout]:
    r = _resolve_round(round)
    lay = layout_for(r)
    _require_ready(lay, r)
    layouts: list[FieldLayout] = []
    for row in lay.rows_by_page.get(page_number, ()):
        field_box = row.box.to_pixels(width, height)
        slots = tuple(
            slot.inset(lay.slot_inset_x, lay.slot_inset_y)
            for slot in field_box.split_columns(lay.n_slots)
        )
        layouts.append(FieldLayout(row=row, field_box=field_box, slot_boxes=slots))  # type: ignore[arg-type]
    return layouts


def all_rows(round: str | None = None) -> Iterable[RowLayout]:
    lay = layout_for(round)
    for page in sorted(lay.rows_by_page):
        yield from lay.rows_by_page[page]
