#!/usr/bin/env python3
"""Guard the photo-report PDF dependency (2026-10-26).

The Photo Report generator 500'd with "fpdf2 not installed": requirements.txt
lists fpdf2, but the Docker image is built from a hardcoded pip line in the
Dockerfile that never included it (no `pip install -r requirements.txt`
anywhere). Every PDF path (photo report, invoices, etc.) was broken in every
built image.

Run with: python3 tests/test_pdf_dependency.py
"""
import sys
import unittest
from pathlib import Path

# Resolve every path relative to this file so the suite runs from any
# checkout / worktree (it used to hardcode the main checkout path).
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))


class TestPdfDependency(unittest.TestCase):
    def test_dockerfile_installs_fpdf2(self):
        src = (REPO / "Dockerfile").read_text(encoding="utf-8")
        pip_lines = [ln for ln in src.splitlines() if ln.strip().startswith("RUN pip install")]
        self.assertTrue(pip_lines, "no pip install line in Dockerfile")
        self.assertTrue(any("fpdf2" in ln for ln in pip_lines),
                        "fpdf2 missing from the Docker image build")

    def test_fpdf_imports_used_by_server(self):
        import re
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertGreater(len(re.findall(r"from fpdf import FPDF", src)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
