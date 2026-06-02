"""Shared detector schemas and classification enums.

The runtime code keeps these models lightweight so early commands can run even
before optional CV dependencies are installed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FieldClassification(StrEnum):
    CLEAN = "CLEAN"
    SUSPICIOUS_OVERLAP = "SUSPICIOUS_OVERLAP"
    DIGIT_SHAPE_ANOMALY = "DIGIT_SHAPE_ANOMALY"
    UNCLEAR = "UNCLEAR"
    CROP_FAILED = "CROP_FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SlotClass(StrEnum):
    DIGIT = "DIGIT"
    PLACEHOLDER = "PLACEHOLDER"
    BLANK = "BLANK"
    MIXED = "MIXED"
    UNCLEAR = "UNCLEAR"


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: str
    source_path: str
    source_sha256: str | None = None
    filename: str | None = None
    department_code: str | None = None
    department_name: str | None = None
    municipality_code: str | None = None
    municipality_name: str | None = None
    zone: str | None = None
    puesto: str | None = None
    mesa: str | None = None
    place_name: str | None = None
    official_lookup_url: str | None = None
    metadata_confidence: float = 0.0
    metadata_source: str = "unknown"
    processing_timestamp: str | None = None


@dataclass
class VoteField:
    document_id: str
    page_number: int
    row_type: str
    row_number: int
    candidate_number: int | None = None
    candidate_name: str | None = None
    section: str | None = None
    raw_crop_path: str | None = None
    enhanced_crop_path: str | None = None
    debug_crop_path: str | None = None
    slot_1_crop_path: str | None = None
    slot_2_crop_path: str | None = None
    slot_3_crop_path: str | None = None
    read_value: str | None = None
    slot_1_class: SlotClass = SlotClass.UNCLEAR
    slot_2_class: SlotClass = SlotClass.UNCLEAR
    slot_3_class: SlotClass = SlotClass.UNCLEAR
    cv_classification: FieldClassification = FieldClassification.UNCLEAR
    cv_score: float = 0.0
    placeholder_overlap_score: float = 0.0
    digit_shape_score: float = 0.0
    shape_anomaly_slot: int | None = None
    shape_anomaly_digit: str | None = None
    comparison_crop_path: str | None = None
    comparison_notes: str | None = None
    vlm_classification: FieldClassification | None = None
    vlm_confidence: float | None = None
    vlm_raw_json: dict[str, Any] | None = None
    final_classification: FieldClassification = FieldClassification.UNCLEAR
    final_reason: str = "needs human review"
    anomaly_tags: list[str] = field(default_factory=list)
    needs_human_review: bool = True


@dataclass(frozen=True)
class RuntimeInfo:
    run_id: str
    start_timestamp: str
    end_timestamp: str | None
    input_dir: str
    output_dir: str
    dpi: int
    workers: int
    vlm_mode: str
    gpu_mode_requested: str
    gpu_mode_used: str
    opencv_version: str | None
    opencl_available: bool
    opencl_enabled: bool
    wsl_detected: bool
    python_version: str
    status: str
