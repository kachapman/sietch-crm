#!/usr/bin/env python3
"""Guard the modern Invoice / COC PDF dependency (ReportLab).

The photo-report PDF dependency guard (tests/test_pdf_dependency.py) caught
fpdf2 missing from the Docker image's hardcoded pip line. The modern
(Sherwood-style) Invoice + Certificate of Completion PDFs in doc_pdf/ need
ReportLab, which had the same exposure: requirements.txt lists it, but the
image is built from a hardcoded `RUN pip install` line.

Run with: python3 tests/test_reportlab_dependency.py
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))


class TestReportLabDependency(unittest.TestCase):
    def test_dockerfile_installs_reportlab(self):
        src = (REPO / "Dockerfile").read_text(encoding="utf-8")
        pip_lines = [ln for ln in src.splitlines() if ln.strip().startswith("RUN pip install")]
        self.assertTrue(pip_lines, "no pip install line in Dockerfile")
        self.assertTrue(any("reportlab" in ln for ln in pip_lines),
                        "reportlab missing from the Docker image build")

    def test_requirements_lists_reportlab(self):
        src = (REPO / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(src, r"(?mi)^reportlab\b")

    def test_reportlab_pin_supports_python_312(self):
        """The 4.x line is the one that supports Python 3.12."""
        src = (REPO / "requirements.txt").read_text(encoding="utf-8")
        m = re.search(r"(?mi)^reportlab\s*(.*)$", src)
        self.assertIsNotNone(m, "reportlab pin not found in requirements.txt")
        spec = m.group(1).strip()
        self.assertTrue(spec, "reportlab pin is empty")

    def test_dockerfile_copies_doc_pdf_package(self):
        """doc_pdf/ is a package, so the `COPY *.py ./` glob does not copy it."""
        src = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("doc_pdf", src)

    def test_reportlab_imports_used_by_doc_pdf(self):
        src = (REPO / "doc_pdf" / "pdf_common.py").read_text(encoding="utf-8")
        self.assertGreater(len(re.findall(r"from reportlab", src)), 0)

    def test_reportlab_is_importable_in_this_environment(self):
        try:
            import reportlab
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"reportlab not installed here: {exc}")
        self.assertTrue(hasattr(reportlab, "Version"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
