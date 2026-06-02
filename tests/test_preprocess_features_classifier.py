from dataclasses import replace
from unittest import TestCase

import numpy as np

from e14detector.classifier import classify_field, classify_slot
from e14detector.cv_features import extract_slot_features
from e14detector.preprocess import connected_components
from e14detector.schemas import FieldClassification, SlotClass


def blank_slot() -> np.ndarray:
    return np.zeros((60, 45), dtype=np.uint8)


def placeholder_slot() -> np.ndarray:
    img = blank_slot()
    img[29:34, 20:25] = 255
    return img


def digit_slot() -> np.ndarray:
    img = blank_slot()
    img[8:52, 22:27] = 255
    img[45:52, 18:32] = 255
    return img


def mixed_slot() -> np.ndarray:
    img = digit_slot()
    img[28:33, 8:13] = 255
    return img


def crossed_mark_slot() -> np.ndarray:
    img = blank_slot()
    for i in range(14, 46):
        img[i, i - 2:i + 3] = 255
        img[i, 44 - i:49 - i] = 255
    img[28:34, 12:36] = 255
    return img


class PreprocessFeatureClassifierTests(TestCase):
    def test_connected_components(self) -> None:
        components = connected_components(mixed_slot())
        self.assertEqual(len(components), 2)

    def test_extract_slot_features_for_blank(self) -> None:
        features = extract_slot_features(blank_slot())
        self.assertEqual(features.component_count, 0)
        self.assertEqual(features.ink_density, 0.0)

    def test_classify_placeholder_digit_and_blank(self) -> None:
        self.assertEqual(classify_slot(extract_slot_features(blank_slot())).slot_class, SlotClass.BLANK)
        self.assertEqual(classify_slot(extract_slot_features(placeholder_slot())).slot_class, SlotClass.PLACEHOLDER)
        self.assertEqual(classify_slot(extract_slot_features(digit_slot())).slot_class, SlotClass.DIGIT)

    def test_mixed_slot_flags_possible_overlap(self) -> None:
        result = classify_field([
            extract_slot_features(mixed_slot()),
            extract_slot_features(blank_slot()),
            extract_slot_features(blank_slot()),
        ])
        self.assertEqual(result.final_classification, FieldClassification.SUSPICIOUS_OVERLAP)
        self.assertTrue(result.needs_human_review)
        self.assertIn("placeholder_overlap", result.anomaly_tags)

    def test_spiky_crossed_mark_is_unclear_not_clean(self) -> None:
        features = extract_slot_features(crossed_mark_slot())
        middle = replace(extract_slot_features(digit_slot()), spiky_component_score=0.60)
        last = replace(extract_slot_features(digit_slot()), spiky_component_score=0.0)
        self.assertGreaterEqual(features.spiky_component_score, 0.55)
        result = classify_field([
            features,
            middle,
            last,
        ])
        self.assertEqual(result.final_classification, FieldClassification.UNCLEAR)
        self.assertTrue(result.needs_human_review)

    def test_all_spiky_marks_are_clean_filler(self) -> None:
        features = extract_slot_features(crossed_mark_slot())
        result = classify_field([features, features, features])
        self.assertEqual(result.final_classification, FieldClassification.CLEAN)
        self.assertFalse(result.needs_human_review)

    def test_crop_failed_priority(self) -> None:
        result = classify_field([], crop_failed=True)
        self.assertEqual(result.final_classification, FieldClassification.CROP_FAILED)
        self.assertTrue(result.needs_human_review)

    def test_digit_shape_tag_for_leading_digit(self) -> None:
        result = classify_field(
            [extract_slot_features(blank_slot())],
            digit_shape_score=0.80,
            shape_anomaly_slot=1,
        )
        self.assertEqual(result.final_classification, FieldClassification.DIGIT_SHAPE_ANOMALY)
        self.assertIn("digit_shape_inconsistency", result.anomaly_tags)
        self.assertIn("possible_leading_digit_alteration", result.anomaly_tags)
