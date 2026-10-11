"""Safe PDF report generation for business-file workflow results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from xml.sax.saxutils import escape as xml_escape

from app.engine.contracts import ExecutionContext, WorkflowInputError

MAX_REPORT_TITLE_LENGTH = 120
MAX_REPORT_FINDINGS = 1000


def validate_pdf_report_config(config: Mapping[str, object]) -> tuple[str, bool]:
    """Require an explicit safe title and an optional include_findings switch."""
    if set(config) - {"title", "include_findings"}:
        raise ValueError("pdf_report config accepts only title and include_findings.")
    title = config.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_REPORT_TITLE_LENGTH:
        raise ValueError("PDF report title must contain 1 to 120 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValueError("PDF report title cannot contain control characters.")
    include_findings = config.get("include_findings", True)
    if type(include_findings) is not bool:
        raise ValueError("include_findings must be a boolean.")
    return title.strip(), include_findings


def _text(value: object, limit: int = 500) -> str:
    raw = str(value)[:limit]
    # PDF text is intentionally limited to printable characters and common whitespace.
    cleaned = "".join(character if character in "\n\t" or ord(character) >= 32 else " " for character in raw)
    # ReportLab's built-in Helvetica uses WinAnsi; replace unsupported glyphs predictably.
    cleaned = cleaned.encode("cp1252", errors="replace").decode("cp1252")
    return xml_escape(cleaned).replace("\n", "<br/>").replace("\t", " &#160;&#160; ")


def _row_number(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _collect_findings(result: Mapping[str, object]) -> tuple[list[tuple[str, str, str, str]], bool]:
    findings: list[tuple[str, str, str, str]] = []
    truncated = bool(result.get("issues_truncated") or result.get("rule_issues_truncated"))
    structured_rules: list[tuple[str, str, str, str]] = []
    generic = result.get("issues", [])
    rule_findings = result.get("rule_issues", [])
    rule_messages: set[tuple[str, str]] = set()
    if isinstance(rule_findings, list):
        for item in rule_findings:
            if not isinstance(item, dict):
                continue
            row = _row_number(item.get("source_row_number"))
            column, rule, message = item.get("column_name"), item.get("rule_id"), item.get("message")
            if row is None or not all(isinstance(value, str) for value in (column, rule, message)):
                continue
            structured_rules.append((str(row), column, rule, message))
            rule_messages.add((str(row), f"Rule '{rule}' on column '{column}': {message}"))
    if isinstance(generic, list):
        for item in generic:
            if not isinstance(item, dict) or _row_number(item.get("row_number")) is None:
                continue
            messages = item.get("issues")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if isinstance(message, str):
                    row_number = str(item["row_number"])
                    if (row_number, message) not in rule_messages:
                        findings.append((row_number, "-", "File integrity", message))
    findings.extend(structured_rules)
    if len(findings) > MAX_REPORT_FINDINGS:
        findings = findings[:MAX_REPORT_FINDINGS]
        truncated = True
    return findings, truncated


def _report_styles():
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="FlowdeskTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=21, leading=26, textColor=colors.HexColor("#233454"), alignment=TA_CENTER,
        spaceAfter=4 * mm,
    ))
    styles.add(ParagraphStyle(
        name="FlowdeskMeta", parent=styles["Normal"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#60708a"), spaceAfter=1.5 * mm,
    ))
    styles.add(ParagraphStyle(
        name="FlowdeskSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=15, textColor=colors.HexColor("#263a5b"), spaceBefore=4 * mm,
        spaceAfter=2 * mm,
    ))
    styles.add(ParagraphStyle(
        name="FlowdeskCell", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=10, textColor=colors.HexColor("#344054"), splitLongWords=1,
    ))
    styles.add(ParagraphStyle(
        name="FlowdeskHeader", parent=styles["FlowdeskCell"], fontName="Helvetica-Bold",
        textColor=colors.white,
    ))
    return styles


def generate_pdf_report(
    stream,
    *,
    title: str,
    include_findings: bool,
    result: Mapping[str, object],
    context: ExecutionContext,
) -> dict[str, object]:
    """Write a summary-only PDF and return bounded metadata for the workflow step."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    counts = {key: result.get(key) for key in ("total_rows", "valid_rows", "invalid_rows")}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise WorkflowInputError("PDF report input is missing valid processing counts.", "invalid_pdf_input")
    if result.get("status") != "completed":
        raise WorkflowInputError("PDF reports require a completed business-file result.", "invalid_pdf_input")
    findings, truncated = _collect_findings(result)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    styles = _report_styles()
    story = [
        Paragraph(_text(title, MAX_REPORT_TITLE_LENGTH), styles["FlowdeskTitle"]),
        Paragraph(f"Generated: {_text(generated_at, 40)} UTC", styles["FlowdeskMeta"]),
        Paragraph(f"Workflow: {_text(context.workflow_id or 'unknown', 64)} - version {_text(context.workflow_version or 'unknown', 64)}", styles["FlowdeskMeta"]),
        Paragraph(f"Execution: {_text(context.execution_id, 64)}", styles["FlowdeskMeta"]),
        Spacer(1, 3 * mm),
        Paragraph("Processing summary", styles["FlowdeskSection"]),
    ]
    summary = [
        [Paragraph("Total rows", styles["FlowdeskHeader"]), Paragraph("Valid rows", styles["FlowdeskHeader"]), Paragraph("Rows with issues", styles["FlowdeskHeader"])],
        [str(counts["total_rows"]), str(counts["valid_rows"]), str(counts["invalid_rows"])],
    ]
    story.append(LongTable(summary, colWidths=[58 * mm, 58 * mm, 58 * mm], repeatRows=1, hAlign="LEFT", style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#344f80")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f3f6fb")),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#233454")),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, -1), 14),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#dce3ee")),
    ])))
    story.append(Paragraph("Validation findings", styles["FlowdeskSection"]))
    if not include_findings:
        story.append(Paragraph("Finding details were excluded by the report options.", styles["Normal"]))
    elif not findings:
        story.append(Paragraph("No validation findings were reported for this file.", styles["Normal"]))
    else:
        rows = [[Paragraph(label, styles["FlowdeskHeader"]) for label in ("Source row", "Column", "Rule / category", "Finding")]]
        rows.extend([
            Paragraph(_text(row, 30), styles["FlowdeskCell"]),
            Paragraph(_text(column, 180), styles["FlowdeskCell"]),
            Paragraph(_text(rule, 100), styles["FlowdeskCell"]),
            Paragraph(_text(message, 700), styles["FlowdeskCell"]),
        ] for row, column, rule, message in findings)
        story.append(LongTable(rows, colWidths=[20 * mm, 35 * mm, 35 * mm, 84 * mm], repeatRows=1, hAlign="LEFT", style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#344f80")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fb")]),
            ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#dce3ee")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])))
    if include_findings and truncated:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("This report contains the available validation preview; additional findings were omitted.", styles["FlowdeskMeta"]))

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dce3ee"))
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#7b8798"))
        canvas.drawString(18 * mm, 9 * mm, "Flowdesk - Business processing report")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        stream, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=21 * mm,
        title=title, author="Flowdesk",
    )
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return {
        "title": title,
        "generated_at": generated_at,
        "finding_count": len(findings),
        "findings_included": bool(include_findings),
        "findings_truncated": truncated if include_findings else False,
    }
