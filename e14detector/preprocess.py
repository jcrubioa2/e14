"""OpenCV preprocessing helpers for vote-field crops."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Component:
    label: int
    x: int
    y: int
    width: int
    height: int
    area: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


def _cv2():
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError("OpenCV is required for preprocessing") from exc
    return cv2


def to_grayscale(image: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
    raise ValueError(f"unsupported image shape: {image.shape}")


def normalize_contrast(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    return cv2.equalizeHist(gray)


def denoise(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    return cv2.medianBlur(gray, 3)


def adaptive_threshold(gray: np.ndarray, invert: bool = True) -> np.ndarray:
    cv2 = _cv2()
    mode = cv2.THRESH_BINARY_INV if invert else cv2.THRESH_BINARY
    return cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        mode,
        31,
        11,
    )


def morph_open(binary: np.ndarray, kernel_size: int = 2) -> np.ndarray:
    cv2 = _cv2()
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)


def morph_close(binary: np.ndarray, kernel_size: int = 2) -> np.ndarray:
    cv2 = _cv2()
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)


def preprocess_for_features(image: np.ndarray) -> np.ndarray:
    gray = to_grayscale(image)
    gray = normalize_contrast(gray)
    gray = denoise(gray)
    return adaptive_threshold(gray, invert=True)


def connected_components(binary: np.ndarray, min_area: int = 3) -> list[Component]:
    cv2 = _cv2()
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    components: list[Component] = []
    for label in range(1, count):
        x, y, width, height, area = [int(v) for v in stats[label]]
        if area >= min_area:
            components.append(Component(label, x, y, width, height, area))
    return components
