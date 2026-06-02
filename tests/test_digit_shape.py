from unittest import TestCase

import numpy as np

from e14detector.comparison import compare_digit_to_examples
from e14detector.digit_shape import digit_shape_score, extract_digit_shape_features


def vertical_one() -> np.ndarray:
    img = np.zeros((70, 45), dtype=np.uint8)
    img[10:60, 21:26] = 255
    return img


def slash_one() -> np.ndarray:
    img = np.zeros((70, 45), dtype=np.uint8)
    for i in range(50):
        x = 11 + i // 3
        y = 10 + i
        img[y:y + 2, x:x + 4] = 255
    return img


class DigitShapeTests(TestCase):
    def test_slash_like_digit_scores_above_vertical_digit(self) -> None:
        vertical = extract_digit_shape_features(vertical_one())
        slash = extract_digit_shape_features(slash_one())
        self.assertGreater(slash.slash_like_score, vertical.slash_like_score)
        self.assertGreaterEqual(digit_shape_score(slash), 0.65)

    def test_comparison_reports_outlier(self) -> None:
        target = extract_digit_shape_features(slash_one())
        examples = [extract_digit_shape_features(vertical_one()) for _ in range(3)]
        result = compare_digit_to_examples(target, examples)
        self.assertGreaterEqual(result.mismatch_score, 0.30)
        self.assertTrue("different" in result.notes or "outlier" in result.notes)
