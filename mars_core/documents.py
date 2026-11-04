import io
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

from openpyxl import load_workbook
from pypdf import PdfReader


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_ARCHIVE_ITEMS = 2000
MAX_EXTRACTED_CHARACTERS = 500_000
MAX_SPREADSHEET_CELLS = 100_000
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _check_file_size(data):
    if not isinstance(data, bytes) or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("文書は20 MB以内にしてください")


def _check_archive(data, required_member):
    _check_file_size(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("Office文書を読み取れません") from error
    try:
        members = archive.infolist()
        names = [member.filename for member in members]
        if (
            len(members) > MAX_ARCHIVE_ITEMS
            or len(names) != len(set(names))
            or sum(member.file_size for member in members) > MAX_ARCHIVE_BYTES
            or any(member.flag_bits & 1 for member in members)
        ):
            raise ValueError("圧縮文書の構造または展開後サイズが上限を超えています")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("圧縮文書に不正なパスが含まれています")
        if required_member not in names:
            raise ValueError("対応するOffice文書ではありません")
        return archive
    except Exception:
        archive.close()
        raise


def _truncate(text):
    text = text.strip()
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        return text[:MAX_EXTRACTED_CHARACTERS] + "\n[抽出結果は文字数上限で省略しました]"
    return text


def extract_docx(data):
    with _check_archive(data, "word/document.xml") as archive:
        try:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        except ElementTree.ParseError as error:
            raise ValueError("DOCX本文のXMLを読み取れません") from error
    paragraphs = []
    for paragraph in root.iter(WORD_NAMESPACE + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == WORD_NAMESPACE + "t" and node.text:
                parts.append(node.text)
            elif node.tag == WORD_NAMESPACE + "tab":
                parts.append("\t")
            elif node.tag == WORD_NAMESPACE + "br":
                parts.append("\n")
        value = "".join(parts).strip()
        if value:
            paragraphs.append(value)
    return _truncate("\n".join(paragraphs))


def extract_xlsx(data):
    _check_archive(data, "[Content_Types].xml").close()
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:
        raise ValueError("XLSXブックを読み取れません") from error
    rows = []
    cell_count = 0
    try:
        for sheet in workbook.worksheets:
            rows.append(f"[シート: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                cell_count += len(row)
                if cell_count > MAX_SPREADSHEET_CELLS:
                    rows.append("[セル数の上限で省略しました]")
                    return _truncate("\n".join(rows))
                values = ["" if value is None else str(value) for value in row]
                if any(values):
                    rows.append("\t".join(values))
                if sum(map(len, rows)) >= MAX_EXTRACTED_CHARACTERS:
                    return _truncate("\n".join(rows))
    finally:
        workbook.close()
    return _truncate("\n".join(rows))


def extract_pdf(data):
    _check_file_size(data)
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("暗号化されたPDFは読み取れません")
        page_count = len(reader.pages)
        if page_count > 1000:
            raise ValueError("PDFのページ数が上限（1000ページ）を超えています")
        pages = []
        for index, page in enumerate(reader.pages):
            if len(pages) >= 100:
                pages.append("[先頭100ページを抽出しました]")
                break
            text = page.extract_text(extraction_mode="layout") or ""
            if text.strip():
                pages.append(f"[ページ {index + 1}]\n{text}")
            if sum(map(len, pages)) >= MAX_EXTRACTED_CHARACTERS:
                break
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDFを読み取れません") from error
    return _truncate("\n\n".join(pages))


def extract_document(suffix, data):
    suffix = suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(data)
    if suffix == ".docx":
        return extract_docx(data)
    if suffix == ".xlsx":
        return extract_xlsx(data)
    raise ValueError("この文書形式は読み取れません")
