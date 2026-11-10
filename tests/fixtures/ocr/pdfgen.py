"""Deterministic SYNTHETIC fixtures for the offline OCR/document-read tests.

No real customer document is used or needed. Everything here is generated from
literal bytes at test time; the PDF writer is a tiny hand-rolled one (no
third-party library), so the fixtures work on any machine.

The "scanned" fixture is a valid PDF whose only page content is an 8x8 RGB
image — there is no text layer, which is exactly what ``pdftotext`` sees for a
real scan. The "digital" fixture draws real text with Helvetica.

Run ``python3 tests/fixtures/ocr/pdfgen.py <dir>`` to materialise the files.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

DIGITAL_PAGES = (
    "Synthetic digital page one. Claim 0000-000000. Roof replacement scope.",
    "Synthetic digital page two. Line items and measurements only.",
)


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(pages: list[str] | None = None, *, image_only: bool = False,
              width: int = 612, height: int = 792) -> bytes:
    """Build a minimal but valid PDF.

    ``pages`` supplies the text of each page. With ``image_only=True`` every
    page instead draws an 8x8 solid image and carries no text layer (a
    synthetic "scan").
    """
    texts = list(pages or DIGITAL_PAGES)
    n = len(texts)
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_obj_pos = add(b"")  # placeholder, filled after kids are known
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    image_ids: list[int] = []
    for _ in range(n):
        content_ids.append(add(b""))   # placeholder
        image_ids.append(add(b"") if image_only else 0)
        page_ids.append(add(b""))      # placeholder

    # Image XObject: 8x8 RGB, mid-grey pixels, no filter (raw bytes).
    if image_only:
        pixels = bytes([128, 128, 128]) * 8 * 8
        for idx, img_id in enumerate(image_ids):
            objects[img_id - 1] = (
                b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
                + str(len(pixels)).encode() + b" >>\nstream\n" + pixels + b"\nendstream"
            )

    for i, text in enumerate(texts):
        if image_only:
            stream = b"q 400 0 0 400 106 296 cm /Im0 Do Q\n"
            resources = (b"<< /XObject << /Im0 " + str(image_ids[i]).encode()
                         + b" 0 R >> >>")
        else:
            stream = (
                b"BT /F1 12 Tf 72 720 Td ("
                + _escape_pdf_text(text).encode("latin-1", errors="replace")
                + b") Tj ET\n"
            )
            resources = b"<< /Font << /F1 " + str(font_id).encode() + b" 0 R >> >>"
        objects[content_ids[i] - 1] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"endstream"
        )
        objects[page_ids[i] - 1] = (
            b"<< /Type /Page /Parent " + str(pages_obj_pos).encode()
            + b" 0 R /MediaBox [0 0 " + str(width).encode() + b" "
            + str(height).encode() + b"] /Resources " + resources
            + b" /Contents " + str(content_ids[i]).encode() + b" 0 R >>"
        )

    kids = b" ".join(str(pid).encode() + b" 0 R" for pid in page_ids)
    objects[pages_obj_pos - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(n).encode() + b" >>"
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root " + str(catalog_id).encode() + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


def build_png() -> bytes:
    """A 1x1 RGB PNG (stdlib zlib only) — used for ``unsupported_type``."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


TXT = (
    "Synthetic plain text attachment.\n"
    "Claim 0000-000000 — interior water damage.\n"
)

HTML = """<!doctype html>
<html><head><style>p{color:red}</style><title>ignored</title></head>
<body><p>Hello <b>synthetic</b> HTML body.</p><script>alert(1)</script></body></html>
"""

EML = (
    "From: adjuster@example.invalid\r\n"
    "To: crm@example.invalid\r\n"
    "Subject: Synthetic attachment message\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/alternative; boundary="BOUND"\r\n'
    "\r\n"
    "--BOUND\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    "Plain synthetic body text.\r\n"
    "--BOUND\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n"
    "<html><body><p>HTML <i>synthetic</i> body text.</p>"
    "<script>alert('x')</script></body></html>\r\n"
    "--BOUND--\r\n"
)


def write_fixtures(directory: str | Path) -> dict[str, Path]:
    """Write every synthetic fixture into ``directory`` and return the paths."""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "digital_pdf": out_dir / "synthetic-digital.pdf",
        "scanned_pdf": out_dir / "synthetic-scanned.pdf",
        "txt": out_dir / "synthetic-note.txt",
        "html": out_dir / "synthetic-page.html",
        "eml": out_dir / "synthetic-mail.eml",
        "png": out_dir / "synthetic-image.png",
    }
    files["digital_pdf"].write_bytes(build_pdf())
    files["scanned_pdf"].write_bytes(build_pdf(image_only=True))
    files["txt"].write_text(TXT, encoding="utf-8")
    files["html"].write_text(HTML, encoding="utf-8")
    files["eml"].write_bytes(EML.encode("utf-8"))
    files["png"].write_bytes(build_png())
    return files


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
    for name, path in write_fixtures(target).items():
        print(f"{name}: {path}")
