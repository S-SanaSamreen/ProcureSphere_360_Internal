import io
import logging
import os
from decimal import Decimal
from typing import Optional

from django.db import models

from apps.receipts.models import GRNAcknowledgement, ReceiptLine

logger = logging.getLogger(__name__)


def generate_grn_acknowledgement_pdf(
    acknowledgement: GRNAcknowledgement, output_path: Optional[str] = None
) -> bytes:
    """
    Generates an official, printable/downloadable Goods Receipt Note (GRN) Acknowledgement PDF
    using ReportLab. Captures complete receipt, PO, vendor, line item quantities, inspection outcomes,
    and rejection reasons.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import (
            HRFlowable,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError:
        logger.error("ReportLab is not installed; cannot generate GRN Acknowledgement PDF.")
        return b""

    buffer = io.BytesIO()
    target = output_path if output_path else buffer

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    doc = SimpleDocTemplate(
        target,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    brand_style = ParagraphStyle(
        "BrandHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#002B49"),
    )
    doc_type_style = ParagraphStyle(
        "DocType",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#0284C7"),
    )
    meta_label = ParagraphStyle(
        "MetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )
    meta_val = ParagraphStyle(
        "MetaVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#0F172A"),
    )
    table_header = ParagraphStyle(
        "TH",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.white,
    )
    table_cell = ParagraphStyle(
        "TC",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B"),
    )
    table_cell_bold = ParagraphStyle(
        "TCB",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#0F172A"),
    )
    badge_style = ParagraphStyle(
        "BadgeText",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        alignment=1,  # Centered
    )

    story = []

    # 1. Header with branding
    story.append(Paragraph("ProcureSphere 360", brand_style))
    story.append(Paragraph("GOODS RECEIPT NOTE (GRN) ACKNOWLEDGEMENT", doc_type_style))
    story.append(
        Paragraph(
            f"Official Receipt & Quality Inspection Certificate &bull; Version {acknowledgement.version}",
            meta_label,
        )
    )
    story.append(Spacer(1, 10))
    story.append(
        HRFlowable(
            width="100%", thickness=1.5, color=colors.HexColor("#0284C7"), spaceAfter=12
        )
    )

    receipt = acknowledgement.receipt
    po = receipt.po
    vendor = po.vendor

    # 2. Outcome Status Banner
    outcome = acknowledgement.outcome
    if outcome == GRNAcknowledgement.OUTCOME_FULL:
        bg_col = colors.HexColor("#DCFCE7")
        txt_col = colors.HexColor("#166534")
        outcome_label = "STATUS: FULL ACCEPTANCE — All Inspected Quantities Passed Quality Verification"
    elif outcome == GRNAcknowledgement.OUTCOME_PARTIAL:
        bg_col = colors.HexColor("#FFEDD5")
        txt_col = colors.HexColor("#9A3412")
        outcome_label = "STATUS: PARTIAL ACCEPTANCE — Some Quantities Accepted; Quality Exceptions Recorded"
    else:
        bg_col = colors.HexColor("#FEE2E2")
        txt_col = colors.HexColor("#991B1B")
        outcome_label = "STATUS: QUALITY REJECTION — Material Rejected Due to Quality Non-Conformance"

    status_table = Table(
        [[Paragraph(f"<font color='{txt_col.hexval()}'><b>{outcome_label}</b></font>", badge_style)]],
        colWidths=[540],
    )
    status_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg_col),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOX", (0, 0), (-1, -1), 1, txt_col),
            ]
        )
    )
    story.append(status_table)
    story.append(Spacer(1, 14))

    # 3. Two-Column Metadata Summary
    primary_contact = vendor.contacts.filter(is_primary=True).first() or vendor.contacts.first()
    vendor_contact_str = (
        f"{primary_contact.first_name} {primary_contact.last_name} ({primary_contact.email})"
        if primary_contact
        else (vendor.email or "No contact on file")
    )

    inspector = acknowledgement.generated_by
    inspector_name = (
        f"{inspector.get_full_name()} ({inspector.email})"
        if inspector.get_full_name()
        else inspector.email
    )

    summary_data = [
        [
            Paragraph("<b>ACKNOWLEDGEMENT REF:</b>", meta_label),
            Paragraph(acknowledgement.acknowledgement_number, meta_val),
            Paragraph("<b>PURCHASE ORDER:</b>", meta_label),
            Paragraph(po.po_number, meta_val),
        ],
        [
            Paragraph("<b>GRN NUMBER:</b>", meta_label),
            Paragraph(receipt.grn_number, meta_val),
            Paragraph("<b>PO VERSION:</b>", meta_label),
            Paragraph(f"V{po.version}", meta_val),
        ],
        [
            Paragraph("<b>RECEIPT DATE:</b>", meta_label),
            Paragraph(receipt.received_date.strftime("%Y-%m-%d %H:%M"), meta_val),
            Paragraph("<b>DELIVERY NOTE REF:</b>", meta_label),
            Paragraph(receipt.delivery_note_number or "N/A", meta_val),
        ],
        [
            Paragraph("<b>VENDOR / SUPPLIER:</b>", meta_label),
            Paragraph(f"{vendor.legal_name} ({vendor.vendor_number})", meta_val),
            Paragraph("<b>INSPECTED BY:</b>", meta_label),
            Paragraph(inspector_name, meta_val),
        ],
        [
            Paragraph("<b>VENDOR CONTACT:</b>", meta_label),
            Paragraph(vendor_contact_str, meta_val),
            Paragraph("<b>ACKNOWLEDGEMENT DATE:</b>", meta_label),
            Paragraph(acknowledgement.generated_at.strftime("%Y-%m-%d %H:%M UTC"), meta_val),
        ],
    ]

    meta_table = Table(summary_data, colWidths=[130, 140, 130, 140])
    meta_table.setStyle(
        TableStyle(
            [
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # 4. Itemized Line Breakdown Table
    story.append(Paragraph("<b>ITEMIZED RECEIPT AND QUALITY INSPECTION BREAKDOWN</b>", meta_label))
    story.append(Spacer(1, 4))

    headers = [
        Paragraph("Item Description", table_header),
        Paragraph("Ordered", table_header),
        Paragraph("Prior Recv", table_header),
        Paragraph("Newly Recv", table_header),
        Paragraph("Accepted", table_header),
        Paragraph("Rejected", table_header),
        Paragraph("PO Pending", table_header),
        Paragraph("Result", table_header),
    ]

    table_rows = [headers]

    lines = (
        receipt.lines.select_related("po_line")
        .prefetch_related("inspection", "rejections")
        .order_by("created_at")
    )

    for line in lines:
        po_line = line.po_line

        # Prior received accepted on earlier receipts for this PO line
        prior_accepted = (
            ReceiptLine.objects.filter(
                po_line=po_line, receipt__created_at__lt=receipt.created_at
            ).aggregate(total=models.Sum("quantity_accepted"))["total"]
            or Decimal("0.00")
        )

        pending_on_po = max(
            Decimal("0.00"), po_line.quantity - (po_line.quantity_received or Decimal("0.00"))
        )

        insp = getattr(line, "inspection", None)
        line_passed = (insp.passed if insp else False) and line.quantity_rejected == Decimal("0.00")
        result_str = (
            "<font color='#16a34a'><b>PASSED</b></font>"
            if line_passed
            else "<font color='#dc2626'><b>REJECTED</b></font>"
        )

        desc_text = po_line.item_description
        if line.quantity_rejected > Decimal("0.00"):
            rej = line.latest_rejection
            reason = rej.rejection_reason if rej else (insp.inspection_notes if insp else "")
            if reason:
                desc_text += f"<br/><font color='#dc2626' size='7'><b>Reason:</b> {reason[:120]}</font>"

        row = [
            Paragraph(desc_text, table_cell),
            Paragraph(f"{po_line.quantity:.2f}", table_cell),
            Paragraph(f"{prior_accepted:.2f}", table_cell),
            Paragraph(f"{line.quantity_received:.2f}", table_cell_bold),
            Paragraph(f"{line.quantity_accepted:.2f}", table_cell),
            Paragraph(f"{line.quantity_rejected:.2f}", table_cell),
            Paragraph(f"{pending_on_po:.2f}", table_cell),
            Paragraph(result_str, table_cell),
        ]
        table_rows.append(row)

    # Totals row
    table_rows.append(
        [
            Paragraph("<b>TOTALS</b>", table_cell_bold),
            Paragraph("-", table_cell),
            Paragraph("-", table_cell),
            Paragraph(f"<b>{acknowledgement.total_received_qty:.2f}</b>", table_cell_bold),
            Paragraph(f"<b>{acknowledgement.total_accepted_qty:.2f}</b>", table_cell_bold),
            Paragraph(f"<b>{acknowledgement.total_rejected_qty:.2f}</b>", table_cell_bold),
            Paragraph("-", table_cell),
            Paragraph("-", table_cell),
        ]
    )

    col_widths = [160, 48, 52, 56, 52, 52, 56, 64]
    item_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#002B49")),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
            ]
        )
    )
    story.append(item_table)
    story.append(Spacer(1, 14))

    # 5. Summary and Next Steps Notes
    notes_lines = []
    if receipt.remarks:
        notes_lines.append(f"<b>Receipt Remarks:</b> {receipt.remarks}")
    if acknowledgement.document_notes:
        notes_lines.append(f"<b>Inspection Summary:</b> {acknowledgement.document_notes}")

    if acknowledgement.total_rejected_qty > Decimal("0.00"):
        notes_lines.append(
            "<b>Action Required for Rejected Items:</b> The rejected quantities noted above failed quality inspection and cannot be accepted into inventory. Please coordinate with the Stores & Procurement team regarding return dispatch or replacement delivery."
        )
    else:
        notes_lines.append(
            "<b>Accepted Inventory Note:</b> All received goods have passed inspection and are accepted for formal financial invoice processing and 3-way match reconciliation."
        )

    notes_p = Paragraph("<br/>".join(notes_lines), table_cell)
    notes_table = Table([[notes_p]], colWidths=[540])
    notes_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(notes_table)
    story.append(Spacer(1, 14))

    # 6. Verification & Sign-off footer
    fingerprint = acknowledgement.inspection_fingerprint[:16]
    footer_text = (
        f"Generated automatically by ProcureSphere 360 ERP &bull; "
        f"Verification Hash: {fingerprint}... &bull; "
        f"Document ID: {acknowledgement.id}<br/>"
        "This is an electronically verified Goods Receipt Note Acknowledgement. No signature is required."
    )
    story.append(
        HRFlowable(
            width="100%", thickness=0.5, color=colors.HexColor("#CBD5E1"), spaceAfter=6
        )
    )
    story.append(
        Paragraph(
            footer_text,
            ParagraphStyle(
                "Footer",
                parent=styles["Normal"],
                fontName="Helvetica",
                fontSize=7,
                leading=10,
                alignment=1,
                textColor=colors.HexColor("#64748B"),
            ),
        )
    )

    doc.build(story)

    if output_path:
        with open(output_path, "rb") as f:
            pdf_bytes = f.read()
    else:
        pdf_bytes = buffer.getvalue()
        buffer.close()

    return pdf_bytes
