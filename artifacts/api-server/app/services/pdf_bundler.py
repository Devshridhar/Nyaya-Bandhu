"""Thread-safe compact NCH-ready PDF bundle generation (strictly under 2 MB)."""
from __future__ import annotations

import io
import re
from html import escape

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Image as PdfImage, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

MAX_PDF_BYTES = 2_000_000


def _pdf_text(value: str) -> str:
    # ReportLab's built-in Helvetica is Latin-1 only. Replace unsupported glyphs
    # rather than allowing a corrupt or oversized PDF; facts remain in the API.
    return escape(str(value)).encode("latin-1", "replace").decode("latin-1").replace("\n", "<br/>")


def _compressed_jpeg(data: bytes, quality: int, max_dimension: int) -> bytes:
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        return out.getvalue()


def _build_once(case_reference: str, facts: dict, legal_draft: str, evidence: list[bytes], quality: int, dimension: int) -> bytes:
    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Nyaya Bandhu complaint {case_reference}",
        author="Nyaya Bandhu",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SmallBody", parent=styles["BodyText"], fontSize=9, leading=12, spaceAfter=6))
    styles.add(ParagraphStyle(name="CenteredTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=17, leading=21))
    story = [
        Paragraph("Consumer Grievance &amp; Notice", styles["CenteredTitle"]),
        Paragraph(f"Case reference: {_pdf_text(case_reference)}", styles["SmallBody"]),
        Paragraph("Consumer Protection Act, 2019 — evidence bundle", styles["SmallBody"]),
        Spacer(1, 8),
    ]
    fact_rows = [
        ["Merchant", facts.get("merchant_name", "")],
        ["Claim amount", f"INR {float(facts.get('claim_amount_inr', 0)):,.2f}"],
        ["Transaction date", facts.get("transaction_date", "")],
        ["UTR / order ID", facts.get("utr_or_order_id", "")],
        ["Problem and remedy requested", facts.get("grievance_description", "")],
    ]
    table = Table([[Paragraph(f"<b>{_pdf_text(str(k))}</b>", styles["SmallBody"]), Paragraph(_pdf_text(str(v)), styles["SmallBody"])] for k, v in fact_rows], colWidths=[48 * mm, 112 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef3f7")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#bcc8d2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [table, Spacer(1, 12), Paragraph("<b>Formal complaint and notice</b>", styles["Heading2"])]
    for paragraph in re.split(r"\n\s*\n", legal_draft.strip()):
        story.append(Paragraph(_pdf_text(paragraph), styles["SmallBody"]))
    story += [
        Spacer(1, 12),
        Paragraph("<b>Verification</b>", styles["Heading2"]),
        Paragraph(
            "The particulars in this document are transcribed from information supplied by the consumer. "
            "The consumer should review the facts and supporting evidence before filing.",
            styles["SmallBody"],
        ),
        Paragraph("National Consumer Helpline: 1915 | WhatsApp: 8800001915 | consumerhelpline.gov.in", styles["SmallBody"]),
    ]
    if evidence:
        story += [PageBreak(), Paragraph("Annexure A — supplied payment evidence", styles["Heading2"])]
        for index, raw in enumerate(evidence, 1):
            if index > 1:
                story.append(Spacer(1, 10))
            compressed = _compressed_jpeg(raw, quality, dimension)
            image = PdfImage(io.BytesIO(compressed), width=160 * mm, height=220 * mm, kind="proportional")
            image._restrictSize(160 * mm, 220 * mm)
            story.append(KeepTogether([Paragraph(f"Evidence image {index}", styles["SmallBody"]), image]))
    doc.build(story)
    return output.getvalue()


def build_pdf_bundle(case_reference: str, facts: dict, legal_draft: str, evidence: list[bytes]) -> bytes:
    for dimension, quality in ((1200, 70), (1000, 55), (800, 45), (640, 35), (480, 28), (320, 22)):
        pdf = _build_once(case_reference, facts, legal_draft, evidence, quality, dimension)
        if len(pdf) < MAX_PDF_BYTES:
            return pdf
    raise ValueError("The PDF bundle could not be reduced below 2 MB. Remove a screenshot and retry.")
