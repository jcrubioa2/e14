from unittest import TestCase

from e14detector.schemas import FieldClassification
from e14detector.vlm.base import parse_vlm_json
from e14detector.vlm.mock_provider import MockVisionReviewer


class VLMJsonTests(TestCase):
    def test_parse_valid_vlm_json(self) -> None:
        result = parse_vlm_json(
            '{"classification":"UNCLEAR","confidence":0.72,"read_value":"42","reason":"unclear mark"}'
        )
        self.assertEqual(result.classification, FieldClassification.UNCLEAR)
        self.assertEqual(result.confidence, 0.72)
        self.assertEqual(result.read_value, "42")

    def test_parse_invalid_vlm_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_vlm_json('{"classification":"FRAUD"}')

    def test_parse_json_object_embedded_in_text(self) -> None:
        result = parse_vlm_json(
            'Result: {"classification":"CLEAN","confidence":0.8,"read_value":null,"reason":"normal filler"}'
        )
        self.assertEqual(result.classification, FieldClassification.CLEAN)
        self.assertEqual(result.confidence, 0.8)
        self.assertIsNone(result.read_value)

    def test_mock_provider_returns_review_result(self) -> None:
        reviewer = MockVisionReviewer(FieldClassification.CLEAN)
        result = reviewer.review_vote_field(["crop.png"], {"document_id": "doc"})
        self.assertEqual(result.classification, FieldClassification.CLEAN)
        self.assertEqual(result.raw_json["image_count"], 1)
