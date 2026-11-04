import io
import zipfile
import unittest

from openpyxl import Workbook

from mars_core.documents import extract_document, extract_docx, extract_pdf, extract_xlsx


def make_docx(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return buffer.getvalue()


def make_pdf(text):
    stream = b"BT /F1 12 Tf 20 80 Td (" + text.encode() + b") Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


class DocumentExtractionTests(unittest.TestCase):
    def test_extract_docx_paragraphs_and_tabs(self):
        content = make_docx("Mars report")

        self.assertIn("Mars report", extract_docx(content))

    def test_extract_xlsx_values_from_multiple_sheets(self):
        workbook = Workbook()
        workbook.active.title = "Summary"
        workbook.active.append(["Name", "Value"])
        workbook.active.append(["Mars", 42])
        extra = workbook.create_sheet("Notes")
        extra.append(["Confirmed"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        extracted = extract_xlsx(buffer.getvalue())

        self.assertIn("[シート: Summary]", extracted)
        self.assertIn("Mars\t42", extracted)
        self.assertIn("[シート: Notes]", extracted)
        self.assertIn("Confirmed", extracted)

    def test_extract_pdf_text(self):
        self.assertIn("Mars PDF", extract_pdf(make_pdf("Mars PDF")))

    def test_reject_invalid_or_unsupported_documents(self):
        for suffix, data in ((".docx", b"not a zip"), (".xlsx", b"not a zip"), (".pdf", b"not a PDF")):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                extract_document(suffix, data)
        with self.assertRaises(ValueError):
            extract_document(".png", b"image")

    def test_reject_duplicate_archive_members(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<root/>")
            archive.writestr("word/document.xml", "<root/>")

        with self.assertRaisesRegex(ValueError, "圧縮文書"):
            extract_docx(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
