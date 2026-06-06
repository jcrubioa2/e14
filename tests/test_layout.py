import hashlib
from unittest import TestCase

from e14detector.layout import (
    LAYOUT,
    NormalizedBox,
    PixelBox,
    all_rows,
    field_layouts_for_page,
    layout_for,
    rows_for_page,
    vote_column_box,
)


def _r1_geometry_signature() -> str:
    """A stable string over every R1 row's identity + normalized box, for the golden pin."""
    parts = []
    for r in all_rows("r1"):
        b = r.box
        parts.append(
            f"{r.page_number}|{r.row_type}|{r.row_number}|{r.candidate_number}|"
            f"{r.candidate_name}|{r.section}|{b.x0:.6f}|{b.y0:.6f}|{b.x1:.6f}|{b.y1:.6f}"
        )
    return ";".join(parts)


class RoundLayoutTests(TestCase):
    # If this hash changes, R1 geometry changed — which would silently move every R1 crop. Update
    # the constant ONLY with a deliberate, reviewed R1 re-calibration, never as a side effect.
    R1_GOLDEN = "9937ce6c245c928ab6661daa8a0075f43af79540973c3a71df75209b3d64de7f"

    def test_r1_geometry_is_pinned(self) -> None:
        got = hashlib.sha256(_r1_geometry_signature().encode()).hexdigest()
        self.assertEqual(got, self.R1_GOLDEN, "R1 layout geometry changed — see test comment")

    def test_r1_shape(self) -> None:
        lay = LAYOUT["r1"]
        self.assertTrue(lay.ready)
        self.assertEqual(lay.n_candidates, 13)
        self.assertEqual(lay.n_pages, 2)

    def test_default_round_is_r1(self) -> None:
        # With E14_ELECTION_ROUND unset, the active layout is R1 (back-compat: callers that pass
        # no round get exactly the old behavior).
        self.assertIs(layout_for(), LAYOUT["r1"])

    def test_r2_is_a_guarded_stub(self) -> None:
        lay = LAYOUT["r2"]
        self.assertFalse(lay.ready)
        self.assertEqual(lay.n_candidates, 2)
        self.assertEqual(lay.n_pages, 1)
        # Metadata is allowed on the stub, but any CROP attempt must raise (no silent garbage).
        self.assertEqual(len(rows_for_page(1, "r2")), 6)
        with self.assertRaises(RuntimeError):
            field_layouts_for_page(1, 2550, 3300, round="r2")
        with self.assertRaises(RuntimeError):
            vote_column_box(1, 2550, 3300, round="r2")

    def test_unknown_round_raises(self) -> None:
        with self.assertRaises(ValueError):
            layout_for("r99")


class LayoutTests(TestCase):
    def test_normalized_box_to_pixels(self) -> None:
        box = NormalizedBox(0.25, 0.10, 0.75, 0.90).to_pixels(1000, 2000)
        self.assertEqual(box, PixelBox(250, 200, 750, 1800))

    def test_pixel_box_split_columns(self) -> None:
        slots = PixelBox(10, 20, 310, 80).split_columns(3)
        self.assertEqual(slots[0], PixelBox(10, 20, 110, 80))
        self.assertEqual(slots[1], PixelBox(110, 20, 210, 80))
        self.assertEqual(slots[2], PixelBox(210, 20, 310, 80))

    def test_rows_for_relevant_pages(self) -> None:
        self.assertEqual(len(rows_for_page(1)), 7)
        self.assertEqual(len(rows_for_page(2)), 10)
        self.assertEqual(rows_for_page(1)[0].candidate_number, 1)
        self.assertEqual(rows_for_page(2)[0].candidate_number, 8)
        self.assertGreater(rows_for_page(2)[0].box.y0, 0.25)

    def test_vote_column_and_fields_stay_inside_page(self) -> None:
        width, height = 2550, 3300
        column = vote_column_box(1, width, height)
        self.assertGreater(column.width, 0)
        self.assertGreater(column.height, 0)
        self.assertGreater(column.width, 600)
        for layout in field_layouts_for_page(1, width, height):
            self.assertGreater(layout.field_box.width, 0)
            self.assertGreater(layout.field_box.height, 0)
            self.assertLess(layout.field_box.height, 190)
            self.assertEqual(len(layout.slot_boxes), 3)
            for slot in layout.slot_boxes:
                self.assertGreater(slot.width, 0)
                self.assertGreater(slot.height, 0)
                self.assertGreaterEqual(slot.x0, 0)
                self.assertLessEqual(slot.x1, width)
