from unittest import TestCase

from e14detector.layout import (
    NormalizedBox,
    PixelBox,
    field_layouts_for_page,
    rows_for_page,
    vote_column_box,
)


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
