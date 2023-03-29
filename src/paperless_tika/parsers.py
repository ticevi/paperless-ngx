import os
from pathlib import Path

import dateutil.parser
import requests
from django.conf import settings
from documents.parsers import DocumentParser
from documents.parsers import make_thumbnail_from_pdf
from documents.parsers import ParseError
from tika import parser


class TikaDocumentParser(DocumentParser):
    """
    This parser sends documents to a local tika server
    """

    logging_name = "paperless.parsing.tika"

    def get_thumbnail(self, document_path, mime_type, file_name=None):
        if not self.archive_path:
            self.archive_path = self.convert_to_pdf(document_path, file_name)

        return make_thumbnail_from_pdf(
            self.archive_path,
            self.tempdir,
            self.logging_group,
        )

    def extract_metadata(self, document_path, mime_type):
        tika_server = settings.TIKA_ENDPOINT

        # tika does not support a PathLike, only strings
        # ensure this is a string
        document_path = str(document_path)

        try:
            parsed = parser.from_file(document_path, tika_server)
        except Exception as e:
            self.log(
                "warning",
                f"Error while fetching document metadata for {document_path}: {e}",
            )
            return []

        return [
            {
                "namespace": "",
                "prefix": "",
                "key": key,
                "value": parsed["metadata"][key],
            }
            for key in parsed["metadata"]
        ]

    def parse(self, document_path: Path, mime_type, file_name=None):
        self.log("info", f"Sending {document_path} to Tika server")
        tika_server = settings.TIKA_ENDPOINT

        # tika does not support a PathLike, only strings
        # ensure this is a string
        document_path = str(document_path)

        try:
            parsed = parser.from_file(document_path, tika_server)
        except Exception as err:
            raise ParseError(
                f"Could not parse {document_path} with tika server at "
                f"{tika_server}: {err}",
            ) from err

        self.text = parsed["content"].strip()

        try:
            self.date = dateutil.parser.isoparse(parsed["metadata"]["Creation-Date"])
        except Exception as e:
            self.log(
                "warning",
                f"Unable to extract date for document {document_path}: {e}",
            )

        self.archive_path = self.convert_to_pdf(document_path, file_name)

    def convert_to_pdf(self, document_path, file_name):
        pdf_path = os.path.join(self.tempdir, "convert.pdf")
        gotenberg_server = settings.TIKA_GOTENBERG_ENDPOINT
        url = gotenberg_server + "/forms/libreoffice/convert"

        self.log("info", f"Converting {document_path} to PDF as {pdf_path}")
        with open(document_path, "rb") as document_handle:
            files = {
                "files": (
                    "convert" + os.path.splitext(document_path)[-1],
                    document_handle,
                ),
            }
            headers = {}
            data = {}

            # Set the output format of the resulting PDF
            # Valid inputs: https://gotenberg.dev/docs/modules/pdf-engines#uno
            if settings.OCR_OUTPUT_TYPE in {"pdfa", "pdfa-2"}:
                data["pdfFormat"] = "PDF/A-2b"
            elif settings.OCR_OUTPUT_TYPE == "pdfa-1":
                data["pdfFormat"] = "PDF/A-1a"
            elif settings.OCR_OUTPUT_TYPE == "pdfa-3":
                data["pdfFormat"] = "PDF/A-3b"

            try:
                response = requests.post(url, files=files, headers=headers, data=data)
                response.raise_for_status()  # ensure we notice bad responses
            except Exception as err:
                raise ParseError(
                    f"Error while converting document to PDF: {err}",
                ) from err

        with open(pdf_path, "wb") as file:
            file.write(response.content)
            file.close()

        return pdf_path
