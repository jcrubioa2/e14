"""Crop and debug-image generation for fixed E-14 layouts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .layout import FieldLayout, PixelBox, field_layouts_for_page, vote_column_box
from .utils import parse_document_metadata


@dataclass(frozen=True)
class CropPaths:
    raw_crop_path: Path
    enhanced_crop_path: Path
    debug_crop_path: Path
    slot_paths: tuple[Path, Path, Path]


def crop_image(image: object, box: PixelBox) -> object:
    if not hasattr(image, "crop"):
        raise TypeError("image object must provide a Pillow-compatible crop method")
    return image.crop((box.x0, box.y0, box.x1, box.y1))


def draw_debug_overlay(image: object, field: PixelBox, slots: tuple[PixelBox, PixelBox, PixelBox]) -> object:
    try:
        from PIL import ImageDraw  # type: ignore
    except Exception as exc:
        raise RuntimeError("Pillow is required for debug overlays") from exc
    if not hasattr(image, "copy"):
        raise TypeError("image object must provide a Pillow-compatible copy method")
    out = image.copy()
    draw = ImageDraw.Draw(out)
    draw.rectangle((field.x0, field.y0, field.x1, field.y1), outline="red", width=4)
    for slot in slots:
        draw.rectangle((slot.x0, slot.y0, slot.x1, slot.y1), outline="blue", width=2)
    return out


def field_debug_crop(image: object, field: PixelBox, slots: tuple[PixelBox, PixelBox, PixelBox], pad: int = 8) -> object:
    """A small debug image: the field region with slot boundaries drawn.

    Unlike :func:`draw_debug_overlay` this crops to the field first, so we never
    copy/save the full (multi-megapixel) page per field — the dominant cost in
    bulk processing.
    """
    try:
        from PIL import ImageDraw  # type: ignore
    except Exception as exc:
        raise RuntimeError("Pillow is required for debug overlays") from exc
    width, height = image.size if hasattr(image, "size") else (field.x1, field.y1)
    x0 = max(0, field.x0 - pad)
    y0 = max(0, field.y0 - pad)
    x1 = min(width, field.x1 + pad)
    y1 = min(height, field.y1 + pad)
    out = image.crop((x0, y0, x1, y1)).copy()
    draw = ImageDraw.Draw(out)
    draw.rectangle((field.x0 - x0, field.y0 - y0, field.x1 - x0, field.y1 - y0), outline="red", width=3)
    for slot in slots:
        draw.rectangle((slot.x0 - x0, slot.y0 - y0, slot.x1 - x0, slot.y1 - y0), outline="blue", width=2)
    return out


def save_field_crops(
    image: object,
    layout: FieldLayout,
    document_id: str,
    output_dir: Path,
    slot_boxes: tuple[PixelBox, PixelBox, PixelBox] | None = None,
) -> CropPaths:
    slots = slot_boxes if slot_boxes is not None else layout.slot_boxes
    crops_dir = Path(output_dir) / "crops"
    slots_dir = Path(output_dir) / "slots"
    debug_dir = Path(output_dir) / "debug"
    for path in (crops_dir, slots_dir, debug_dir):
        path.mkdir(parents=True, exist_ok=True)

    safe_type = layout.row.row_type.replace("/", "_")
    stem = f"{document_id}_p{layout.row.page_number}_row{layout.row.row_number}_{safe_type}"
    raw_path = crops_dir / f"{stem}_field.png"
    enhanced_path = crops_dir / f"{stem}_field_enhanced.png"
    debug_path = debug_dir / f"{stem}_debug.png"
    slot_paths = tuple(slots_dir / f"{stem}_slot{i}.png" for i in range(1, 4))

    crop = crop_image(image, layout.field_box)
    crop.save(raw_path)
    try:
        import numpy as np
        from PIL import Image  # type: ignore

        from .preprocess import preprocess_for_features

        enhanced = preprocess_for_features(np.array(crop))
        Image.fromarray(enhanced).save(enhanced_path)
    except Exception:
        crop.save(enhanced_path)
    for slot, slot_path in zip(slots, slot_paths):
        crop_image(image, slot).save(slot_path)
    field_debug_crop(image, layout.field_box, slots).save(debug_path)
    return CropPaths(raw_crop_path=raw_path, enhanced_crop_path=enhanced_path, debug_crop_path=debug_path, slot_paths=slot_paths)  # type: ignore[arg-type]


def save_page_debug_overlay(image: object, page_number: int, output_path: Path) -> None:
    try:
        from PIL import ImageDraw  # type: ignore
    except Exception as exc:
        raise RuntimeError("Pillow is required for debug overlays") from exc
    out = image.copy()
    draw = ImageDraw.Draw(out)
    width, height = out.size
    column = vote_column_box(page_number, width, height)
    draw.rectangle((column.x0, column.y0, column.x1, column.y1), outline="green", width=5)
    for layout in field_layouts_for_page(page_number, width, height):
        field = layout.field_box
        draw.rectangle((field.x0, field.y0, field.x1, field.y1), outline="red", width=3)
        for slot in layout.slot_boxes:
            draw.rectangle((slot.x0, slot.y0, slot.x1, slot.y1), outline="blue", width=1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path)


def inspect_pdf_layout(pdf_path: Path, output_dir: Path, dpi: int = 300) -> list[Path]:
    from .pdf_render import render_pdf_pages

    meta = parse_document_metadata(pdf_path)
    output_dir = Path(output_dir)
    written: list[Path] = []
    for page in render_pdf_pages(pdf_path, pages=(1, 2), dpi=dpi):
        page_overlay = output_dir / f"{meta.document_id}_p{page.page_number}_layout.png"
        save_page_debug_overlay(page.image, page.page_number, page_overlay)
        written.append(page_overlay)
    return written
