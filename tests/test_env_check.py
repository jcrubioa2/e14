from pathlib import Path
from unittest import TestCase

from e14detector.env_check import collect_env_report, format_env_report


class EnvCheckTests(TestCase):
    def test_env_check_cpu_fallback(self) -> None:
        tmp_path = Path("/tmp/e14detector-test-env")
        report = collect_env_report(gpu_mode="off", output_dir=tmp_path)
        self.assertEqual(report.gpu_mode_requested, "off")
        self.assertEqual(report.gpu_mode_used, "cpu")
        self.assertTrue(report.output_dir_write_test)

    def test_env_report_format_mentions_cpu_pipeline(self) -> None:
        tmp_path = Path("/tmp/e14detector-test-env-format")
        report = collect_env_report(gpu_mode="off", output_dir=tmp_path)
        text = format_env_report(report)
        self.assertIn("Python version:", text)
        self.assertIn("GPU acceleration requested: off", text)
        self.assertIn("Using CPU pipeline.", text)
