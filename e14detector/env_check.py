"""Runtime environment and optional acceleration detection."""
from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class EnvReport:
    python_version: str
    os: str
    wsl_detected: bool
    cpu_count: int | None
    ram_total_gb: float | None
    opencv_version: str | None
    opencl_available: bool
    opencl_enabled: bool
    gpu_mode_requested: str
    gpu_mode_used: str
    pdf_rendering_backend_available: bool
    output_dir_write_test: bool
    warnings: tuple[str, ...] = ()


def detect_wsl() -> bool:
    release = platform.release().lower()
    if "microsoft" in release or "wsl" in release:
        return True
    version_path = Path("/proc/version")
    try:
        return "microsoft" in version_path.read_text(encoding="utf-8").lower()
    except OSError:
        return False


def detect_ram_gb() -> float | None:
    meminfo = Path("/proc/meminfo")
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return round(kb / 1024 / 1024, 2)
    except (OSError, ValueError, IndexError):
        return None
    return None


def detect_opencv_opencl(requested_mode: str) -> tuple[str | None, bool, bool, str, tuple[str, ...]]:
    warnings: list[str] = []
    requested_mode = requested_mode.lower()
    if requested_mode not in config.GPU_MODES:
        warnings.append(f"Unknown GPU mode '{requested_mode}', using CPU pipeline.")
        requested_mode = "off"

    try:
        import cv2  # type: ignore
    except Exception as exc:
        warnings.append(f"OpenCV unavailable: {exc}. Using CPU pipeline.")
        return None, False, False, "cpu", tuple(warnings)

    version = getattr(cv2, "__version__", None)
    available = False
    enabled = False

    if requested_mode == "off":
        try:
            cv2.ocl.setUseOpenCL(False)
        except Exception:
            pass
        return version, False, False, "cpu", tuple(warnings)

    if requested_mode in {"directml", "rocm"}:
        warnings.append(f"GPU mode '{requested_mode}' is reserved for later experiments. Using CPU pipeline.")
        return version, False, False, "cpu", tuple(warnings)

    try:
        available = bool(cv2.ocl.haveOpenCL())
        if available and requested_mode in {"auto", "opencl"}:
            cv2.ocl.setUseOpenCL(True)
            enabled = bool(cv2.ocl.useOpenCL())
    except Exception as exc:
        warnings.append(f"OpenCV OpenCL detection failed: {exc}. Using CPU pipeline.")
        available = False
        enabled = False

    used = "opencl" if enabled else "cpu"
    if requested_mode in {"auto", "opencl"} and not enabled:
        warnings.append("OpenCV OpenCL not enabled. Using CPU pipeline.")
    return version, available, enabled, used, tuple(warnings)


def detect_pdf_backend() -> bool:
    try:
        import fitz  # type: ignore # noqa: F401
    except Exception:
        return False
    return True


def write_test(output_dir: Path) -> bool:
    try:
        config.ensure_output_dirs(output_dir)
        probe = Path(output_dir) / "results" / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def collect_env_report(gpu_mode: str | None = None, output_dir: Path | None = None) -> EnvReport:
    requested = (gpu_mode or os.environ.get("E14_USE_GPU") or config.DEFAULT_GPU_MODE).lower()
    output_dir = output_dir or config.DEFAULT_OUTPUT_DIR
    cv_version, opencl_available, opencl_enabled, gpu_used, cv_warnings = detect_opencv_opencl(requested)
    warnings = list(cv_warnings)
    pdf_backend = detect_pdf_backend()
    if not pdf_backend:
        warnings.append("PyMuPDF/fitz unavailable. PDF rendering commands will not work until dependencies are installed.")

    return EnvReport(
        python_version=sys.version.split()[0],
        os=f"{platform.system()} {platform.release()}",
        wsl_detected=detect_wsl(),
        cpu_count=os.cpu_count(),
        ram_total_gb=detect_ram_gb(),
        opencv_version=cv_version,
        opencl_available=opencl_available,
        opencl_enabled=opencl_enabled,
        gpu_mode_requested=requested,
        gpu_mode_used=gpu_used,
        pdf_rendering_backend_available=pdf_backend,
        output_dir_write_test=write_test(output_dir),
        warnings=tuple(warnings),
    )


def format_env_report(report: EnvReport) -> str:
    lines = [
        f"Python version: {report.python_version}",
        f"OS: {report.os}",
        f"WSL detected: {str(report.wsl_detected).lower()}",
        f"CPU count: {report.cpu_count}",
        f"Available RAM: {report.ram_total_gb if report.ram_total_gb is not None else 'unknown'} GB",
        f"OpenCV version: {report.opencv_version or 'unavailable'}",
        f"OpenCV OpenCL available: {str(report.opencl_available).lower()}",
        f"OpenCV OpenCL enabled: {str(report.opencl_enabled).lower()}",
        f"GPU acceleration requested: {report.gpu_mode_requested}",
        f"GPU mode actually used: {report.gpu_mode_used}",
        f"PDF rendering backend available: {str(report.pdf_rendering_backend_available).lower()}",
        f"Output directory write test: {str(report.output_dir_write_test).lower()}",
    ]
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    if report.gpu_mode_used == "cpu":
        lines.append("Using CPU pipeline.")
    return "\n".join(lines)
