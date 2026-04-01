from __future__ import annotations

from app import models


def _build_pdf_bytes(text: str) -> bytes:
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 18 Tf\n72 120 Td\n({escaped_text}) Tj\nET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    body = bytearray()
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body) + len(b"%PDF-1.4\n"))
        body.extend(f"{index} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_start = len(b"%PDF-1.4\n") + len(body)
    xref_entries = [b"0000000000 65535 f \n"]
    xref_entries.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])

    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n")
    pdf.extend(body)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"".join(xref_entries))
    pdf.extend(f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\n".encode("ascii"))
    pdf.extend(f"startxref\n{xref_start}\n%%EOF\n".encode("ascii"))
    return bytes(pdf)


def test_pdf_upload_creates_ready_document_and_chunks(client, db_session):
    corpus_resp = client.post("/rag/corpora", json={"name": "PDF docs", "description": "pdf material"})
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    pdf_bytes = _build_pdf_bytes("HTTP status 200 means success")
    upload_resp = client.post(
        f"/rag/corpora/{corpus_id}/documents/upload",
        files={"file": ("http-guide.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload_resp.status_code == 201
    payload = upload_resp.json()
    assert payload["status"] == "ready"
    assert payload["content_type"] == "application/pdf"
    assert payload["filename"] == "http-guide.pdf"

    document = db_session.get(models.Document, payload["id"])
    assert document is not None
    assert "http status 200" in document.content.lower()
    assert document.object_key
    assert document.storage_bucket
    assert len(document.chunks) >= 1
