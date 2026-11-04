"""Modern (Sherwood "v26.5"-style) PDF builders for Sietch CRM.

This package is a port of the PDF builders from the Sherwood Toolbox
(kachapman/sherwood-toolbox, master 3d5bf83, "v28.1"). Sietch CRM had its
own hand-rolled fpdf2 renderers for the Invoice and Certificate of
Completion; this package provides the ReportLab-based "modern" layouts
instead.

Modules
-------
pdf_common    Ported from toolbox/tools/documents/pdf_common.py
invoice_pdf    Ported from toolbox/tools/documents/invoice_pdf.py
coc_pdf       Ported from toolbox/tools/documents/coc_pdf.py
mapping       Pure mapping layer: template row + payload -> brand/job/items/opts

All builders are pure: dicts in, PDF file out. Nothing in here touches the
database, the HTTP layer, or the filesystem outside the given out_path, so a
future sync with Sherwood upstream stays mechanical.
"""

__all__ = ["pdf_common", "invoice_pdf", "coc_pdf", "mapping"]
