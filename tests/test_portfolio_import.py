"""Format-dispatch tests for XLSX and PDF portfolio statements."""

from __future__ import annotations

import io
import unittest

from ystocker.portfolio_import import parse


class XlsxImportTests(unittest.TestCase):
    @staticmethod
    def _benefits_workbook() -> bytes:
        from openpyxl import Workbook

        workbook = Workbook()
        espp = workbook.active
        espp.title = "ESPP"
        espp.append(["Record Type", "Symbol", "Purchased Qty.",
                     "Sellable Qty.", "Est. Market Value"])
        espp.append(["Purchase", "AAPL", 54.395, 54.395, 17390.08])
        espp.append(["Totals", None, None, 54.395, 17390.08])

        rsu = workbook.create_sheet("Restricted Stock")
        rsu.append(["Record Type", "Symbol", "Granted Qty.", "Vested Qty.",
                    "Unvested Qty.", "Sellable Qty.", "Est. Market Value"])
        rsu.append(["Grant", "AAPL", 1146, 1003, 143, 0, 46289.10])
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_multisheet_benefit_statement_uses_value_matching_quantities(self) -> None:
        result = parse(self._benefits_workbook(), filename="benefits.xlsx")

        self.assertTrue(result.ok, result.error)
        self.assertEqual("Excel workbook", result.broker)
        self.assertEqual(2, len(result.rows))
        self.assertEqual([54.395, 143.0], [row.quantity for row in result.rows])
        self.assertEqual(["ESPP", "Restricted Stock"],
                         [row.account for row in result.rows])
        self.assertEqual("Sellable Qty. / Unvested Qty.",
                         result.mapping["quantity"])

    def test_damaged_xlsx_is_a_safe_parse_error(self) -> None:
        result = parse(b"not-a-zip", filename="positions.xlsx")
        self.assertFalse(result.ok)
        self.assertIn("XLSX", result.error)


class PdfImportTests(unittest.TestCase):
    @staticmethod
    def _statement_pdf() -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        output = io.BytesIO()
        document = SimpleDocTemplate(output, pagesize=letter)
        table = Table([
            ["Symbol", "Description", "Quantity", "Market Value"],
            ["AAPL", "Apple Inc.", "12", "$3,840.00"],
            ["VOO", "Vanguard S&P 500 ETF", "4", "$2,100.00"],
        ], colWidths=[70, 190, 80, 100])
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ]))
        document.build([
            Paragraph("Portfolio Statement", getSampleStyleSheet()["Title"]),
            Spacer(1, 12), table,
        ])
        return output.getvalue()

    def test_pdf_table_uses_canonical_mapping_and_preview_rows(self) -> None:
        result = parse(self._statement_pdf(), filename="statement.pdf")

        self.assertTrue(result.ok, result.error)
        self.assertEqual("PDF statement", result.broker)
        self.assertEqual(["AAPL", "VOO"], [row.symbol for row in result.rows])
        self.assertEqual([3840.0, 2100.0],
                         [row.market_value for row in result.rows])
        self.assertEqual("Market Value", result.mapping["market_value"])

    def test_image_or_empty_pdf_has_an_actionable_error(self) -> None:
        # A syntactically invalid PDF should never escape as a server exception.
        result = parse(b"%PDF-1.4\ninvalid", filename="scan.pdf")
        self.assertFalse(result.ok)
        self.assertIn("Could not read", result.error)


class DispatchTests(unittest.TestCase):
    def test_unsupported_extension_is_rejected_explicitly(self) -> None:
        result = parse(b"symbol,quantity\nAAPL,1\n", filename="positions.xls")
        self.assertFalse(result.ok)
        self.assertIn("Unsupported file type", result.error)


if __name__ == "__main__":
    unittest.main()
