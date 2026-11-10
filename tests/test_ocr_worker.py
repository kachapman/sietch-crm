#!/usr/bin/env python3
"""Offline tests for the OCR worker (client + engine glue).

No real PaddleOCR, no model download, no GPU, no network: HTTP is a fake client
and the OCR pipeline is a fake object. The only real subprocess is `pdftoppm`
on a synthetic two-page PDF (poppler is part of the server/CI image).

Run with: python3 tests/test_ocr_worker.py
"""

import importlib.util
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKER_DIR = ROOT / "scripts" / "ocr_worker"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "fixtures" / "ocr"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(WORKER_DIR))

import network_guard  # noqa: E402

network_guard.install()

from pdfgen import write_fixtures  # noqa: E402

PADDLE_IMPORTED_AT_STARTUP = "paddleocr" in sys.modules

try:
    import worker  # noqa: E402
    import engine  # noqa: E402
    HAVE_WORKER = True
except Exception as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    HAVE_WORKER = False

FIXTURES: dict[str, Path] = {}
TMPDIR = None


def setUpModule():
    global TMPDIR
    TMPDIR = tempfile.mkdtemp(prefix="sietch-ocr-worker-")
    FIXTURES.update(write_fixtures(TMPDIR))


def tearDownModule():
    if TMPDIR:
        shutil.rmtree(TMPDIR, ignore_errors=True)


class TestConfig(unittest.TestCase):
    def test_requires_url_and_token_without_echoing_the_token(self):
        with self.assertRaises(worker.ConfigError):
            worker.load_config({})
        with self.assertRaises(worker.ConfigError):
            worker.load_config({"SIETCH_URL": "https://crm.example.com"})
        with self.assertRaises(worker.ConfigError):
            worker.load_config({"SIETCH_OCR_TOKEN": "sietch_secret"})

    def test_values_come_from_env(self):
        cfg = worker.load_config({
            "SIETCH_URL": "https://crm.example.com/",
            "SIETCH_OCR_TOKEN": "sietch_secret",
            "SIETCH_OCR_TMPDIR": "/dev/shm",
            "SIETCH_OCR_DEVICE": "cpu",
            "SIETCH_OCR_DPI": "300",
            "SIETCH_OCR_POLL_SECONDS": "5",
        })
        self.assertEqual(cfg.base_url, "https://crm.example.com")
        self.assertEqual(cfg.tmpdir, "/dev/shm")
        self.assertEqual(cfg.device, "cpu")
        self.assertEqual(cfg.page_dpi, 300)
        self.assertEqual(cfg.poll_seconds, 5)

    def test_repr_never_shows_the_token(self):
        cfg = worker.load_config({
            "SIETCH_URL": "https://crm.example.com",
            "SIETCH_OCR_TOKEN": "sietch_supersecret",
        })
        self.assertNotIn("sietch_supersecret", repr(cfg))
        self.assertIn("<redacted>", repr(cfg))

    def test_generic_defaults_are_not_host_specific(self):
        cfg = worker.load_config({
            "SIETCH_URL": "https://crm.example.com",
            "SIETCH_OCR_TOKEN": "sietch_secret",
        })
        self.assertEqual(cfg.device, engine.DEFAULT_DEVICE)
        self.assertIsNone(cfg.tmpdir)
        source = (WORKER_DIR / "worker.py").read_text(encoding="utf-8").lower()
        for marker in ("zionad", "4b6c0c35", "ken-proartlaptop", "/home/"):
            self.assertNotIn(marker, source)


class FakeClient:
    def __init__(self, jobs=None, payload=b"%PDF-1.4\nsynthetic", fail=None):
        self.jobs = jobs or []
        self.payload = payload
        self.fail = fail
        self.calls = []

    def list_pending(self, limit=1):
        self.calls.append(("list_pending", limit))
        return list(self.jobs)

    def claim(self, job_id):
        self.calls.append(("claim", job_id))
        return {"id": job_id, "status": "in_progress"}

    def download(self, job_id):
        self.calls.append(("download", job_id))
        if self.fail:
            raise self.fail
        return self.payload

    def upload_pages(self, job_id, pages, engine_name, engine_version):
        self.calls.append(("upload_pages", job_id, pages, engine_name, engine_version))

    def complete(self, job_id, status, error=None):
        self.calls.append(("complete", job_id, status, error))


class FakeOcr:
    def __init__(self, pages=None, exc=None):
        self.pages = pages if pages is not None else [
            {"page": 1, "text": "WORKER OCR PAGE ONE"},
            {"page": 2, "text": "WORKER OCR PAGE TWO"},
        ]
        self.exc = exc
        self.calls = []

    def extract_pages(self, path, workdir, dpi=None):
        self.calls.append((path, workdir, dpi))
        if self.exc:
            raise self.exc
        return self.pages


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="sietch-ocr-work-")
        self.job = {"id": 42, "file_sha256": "a" * 64, "status": "pending"}

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_no_jobs_is_a_noop(self):
        client = FakeClient()
        self.assertFalse(worker.run_once(client, FakeOcr(), workdir=self.workdir))
        self.assertEqual(client.calls, [("list_pending", 1)])

    def test_success_uploads_pages_and_cleans_up(self):
        client = FakeClient([self.job])
        ocr = FakeOcr()
        self.assertTrue(worker.run_once(client, ocr, workdir=self.workdir))
        names = [c[0] for c in client.calls]
        self.assertEqual(names, ["list_pending", "claim", "download",
                                 "upload_pages", "complete"])
        upload = next(c for c in client.calls if c[0] == "upload_pages")
        self.assertEqual(upload[3], worker.ENGINE_NAME)
        self.assertEqual(upload[4], worker.ENGINE_VERSION)
        self.assertEqual([p["page"] for p in upload[2]], [1, 2])
        self.assertEqual(client.calls[-1], ("complete", 42, "done", None))
        # The job temp directory (download + page images) is gone.
        self.assertEqual(list(Path(self.workdir).glob("sietch-ocr-*")), [])

    def test_engine_failure_marks_failed_with_a_code_not_text(self):
        client = FakeClient([self.job])
        ocr = FakeOcr(exc=engine.EngineError("unsupported_for_ocr"))
        self.assertFalse(worker.run_once(client, ocr, workdir=self.workdir))
        final = client.calls[-1]
        self.assertEqual(final[0], "complete")
        self.assertEqual(final[2], "failed")
        self.assertEqual(final[3], "unsupported_for_ocr")
        self.assertNotIn("WORKER OCR", str(final[3]))
        self.assertEqual(list(Path(self.workdir).glob("sietch-ocr-*")), [])

    def test_generic_exception_uses_only_the_type_name(self):
        client = FakeClient([self.job])
        ocr = FakeOcr(exc=RuntimeError("patient zero leaked into a message"))
        self.assertFalse(worker.run_once(client, ocr, workdir=self.workdir))
        final = client.calls[-1]
        self.assertEqual(final[3], "RuntimeError")
        self.assertNotIn("patient zero", str(final))

    def test_worker_logs_no_token_and_no_text(self):
        token = "sietch_supersecret_token"
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        worker.logger.addHandler(handler)
        previous = worker.logger.level
        worker.logger.setLevel(logging.INFO)
        try:
            client = FakeClient([self.job])
            worker.run_once(client, FakeOcr(), workdir=self.workdir)
            # Exercise the config repr path too.
            worker.load_config({"SIETCH_URL": "https://crm.example.com",
                                "SIETCH_OCR_TOKEN": token})
        finally:
            worker.logger.removeHandler(handler)
            worker.logger.setLevel(previous)
        joined = "\n".join(records)
        self.assertNotIn(token, joined)
        self.assertNotIn("WORKER OCR PAGE", joined)
        self.assertIn("job 42 done", joined)

    def test_empty_ocr_result_is_a_failure(self):
        client = FakeClient([self.job])
        self.assertFalse(worker.run_once(client, FakeOcr(pages=[]), workdir=self.workdir))
        self.assertEqual(client.calls[-1][2], "failed")
        self.assertEqual(client.calls[-1][3], "no_pages_extracted")


class TestClientSafety(unittest.TestCase):
    def test_client_repr_hides_the_token(self):
        client = worker.SietchClient("https://crm.example.com", "sietch_supersecret")
        try:
            self.assertNotIn("sietch_supersecret", repr(client))
            self.assertIn("<redacted>", repr(client))
        finally:
            client.close()


class TestEngineGlue(unittest.TestCase):
    def test_result_text_parsing_res_list(self):
        result = {"parsing_res_list": [
            {"block_content": "First block"},
            {"block_content": "Table | 1 | 2"},
        ]}
        self.assertEqual(engine.result_text(result), "First block\nTable | 1 | 2")

    def test_result_text_rec_texts(self):
        self.assertEqual(engine.result_text({"rec_texts": ["a", "b"]}), "a\nb")

    def test_result_text_markdown_attribute(self):
        class Result:
            markdown = "# Heading\ntext"

        self.assertEqual(engine.result_text(Result()), "# Heading\ntext")

    def test_extract_pages_uses_pdftoppm_then_deletes_images(self):
        class FakePipeline:
            def __init__(self):
                self.inputs = []

            def predict(self, input):
                self.inputs.append(input)
                return [{"parsing_res_list": [{"block_content": "page text"}]}]

        workdir = tempfile.mkdtemp(prefix="sietch-ocr-engine-")
        try:
            ocr = engine.OcrEngine(device="cpu")
            ocr._pipeline = FakePipeline()
            pages = ocr.extract_pages(str(FIXTURES["scanned_pdf"]), workdir, dpi=100)
            self.assertEqual([p["page"] for p in pages], [1, 2])
            self.assertIn("page text", pages[0]["text"])
            self.assertEqual(len(ocr._pipeline.inputs), 2)
            self.assertEqual(list(Path(workdir).glob("page-*.png")), [])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_extract_pages_rejects_non_pdf_without_loading_paddle(self):
        workdir = tempfile.mkdtemp(prefix="sietch-ocr-engine-")
        other = Path(workdir) / "synthetic.docx"
        other.write_bytes(b"PK\x03\x04 not an image")
        try:
            ocr = engine.OcrEngine(device="cpu")
            with self.assertRaises(engine.EngineError) as ctx:
                ocr.extract_pages(str(other), workdir)
            self.assertEqual(str(ctx.exception), "unsupported_for_ocr")
            self.assertEqual(ctx.exception.code, "unsupported_for_ocr")
            self.assertIsNone(ocr._pipeline)  # load_pipeline never called
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_engine_does_not_import_paddle_at_module_import(self):
        self.assertFalse(PADDLE_IMPORTED_AT_STARTUP)
        source = (WORKER_DIR / "engine.py").read_text(encoding="utf-8")
        # The only paddleocr import is inside load_pipeline.
        for line in source.splitlines():
            if "import PPStructureV3" in line:
                self.assertTrue(line.startswith("        "), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
