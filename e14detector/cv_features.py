"""Feature extraction for vote-field and slot crops."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .preprocess import Component, _cv2, connected_components

# Components smaller than this fraction of the slot area are pen ticks / scanner
# specks, not meaningful marks. Dropping them stops a single written digit plus a
# stray speck from being read as a digit+placeholder overlap. Kept below the size
# of a genuine small mark (a thin "1" is ~0.019 of slot area).
NOISE_AREA_RATIO = 0.008


@dataclass(frozen=True)
class SlotFeatures:
    width: int
    height: int
    ink_density: float
    component_count: int
    largest_component_area: int
    component_bounding_boxes: list[tuple[int, int, int, int]]
    aspect_ratios: list[float]
    slot_density: float
    slot_component_count: int
    placeholder_like_component_count: int
    digit_like_component_count: int
    mixed_component_score: float
    spiky_component_score: float
    relative_darkness: float
    relative_size: float

    def to_json(self) -> dict:
        return asdict(self)


def _classify_component(component: Component, slot_width: int, slot_height: int) -> str:
    area_ratio = component.area / max(1, slot_width * slot_height)
    height_ratio = component.height / max(1, slot_height)
    width_ratio = component.width / max(1, slot_width)
    # Tiny pre-printed guide marks: small in both area and height.
    if area_ratio < 0.03 and height_ratio < 0.22 and width_ratio <= 0.45:
        return "placeholder"
    # Handwritten digits: tall enough OR enough ink area. Slots are intentionally
    # tall, so digits commonly fill only ~18-35% of slot height; a thin "1" has
    # little area but real height, a flat "0" has little height but real area.
    if height_ratio >= 0.22 or area_ratio >= 0.03:
        return "digit"
    return "unclear"


def extract_slot_features(binary: np.ndarray) -> SlotFeatures:
    if binary.ndim != 2:
        raise ValueError("slot feature extraction expects a single-channel binary image")
    height, width = binary.shape
    ink_pixels = int(np.count_nonzero(binary))
    area = max(1, width * height)
    components = [
        c for c in connected_components(binary)
        if c.area / area >= NOISE_AREA_RATIO
    ]
    boxes = [c.bbox for c in components]
    aspect_ratios = [round(c.width / max(1, c.height), 4) for c in components]
    largest = max((c.area for c in components), default=0)
    labels = [_classify_component(c, width, height) for c in components]
    placeholder_count = labels.count("placeholder")
    digit_count = labels.count("digit")
    spiky_score = 0.0
    if components:
        cv2 = _cv2()
        contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            contour = max(contours, key=cv2.contourArea)
            contour_area = cv2.contourArea(contour)
            hull_area = cv2.contourArea(cv2.convexHull(contour)) or 1.0
            x, y, w, h = cv2.boundingRect(contour)
            extent = contour_area / max(1, w * h)
            aspect_ratio = w / max(1, h)
            perimeter = cv2.arcLength(contour, True) or 1.0
            circularity = 4 * np.pi * contour_area / (perimeter * perimeter)
            solidity = contour_area / hull_area
            largest_ratio = largest / area
            if (
                largest_ratio >= 0.02
                and aspect_ratio >= 0.55
                and solidity < 0.58
                and extent < 0.48
                and circularity < 0.30
            ):
                spiky_score = min(
                    1.0,
                    0.45
                    + (0.58 - solidity) * 1.4
                    + (0.48 - extent) * 0.8
                    + (0.30 - circularity) * 0.5,
                )
    mixed_score = 0.0
    if placeholder_count and digit_count:
        mixed_score = min(1.0, 0.55 + 0.15 * min(3, placeholder_count + digit_count - 2))
    elif len(components) > 2 and ink_pixels / area > 0.08:
        mixed_score = 0.35

    density = round(ink_pixels / area, 6)
    return SlotFeatures(
        width=width,
        height=height,
        ink_density=density,
        component_count=len(components),
        largest_component_area=largest,
        component_bounding_boxes=boxes,
        aspect_ratios=aspect_ratios,
        slot_density=density,
        slot_component_count=len(components),
        placeholder_like_component_count=placeholder_count,
        digit_like_component_count=digit_count,
        mixed_component_score=round(mixed_score, 4),
        spiky_component_score=round(spiky_score, 4),
        relative_darkness=density,
        relative_size=round(largest / area, 6),
    )
