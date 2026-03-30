from app.services.rag_ingestion import chunk_text, extract_text_from_bytes


def test_chunk_text_creates_overlapping_chunks():
    text = " ".join(f"token{i}" for i in range(1, 21))
    chunks = chunk_text(text, chunk_size=6, overlap=2)

    assert len(chunks) == 5
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1
    assert "token5 token6" in chunks[1]["content"]
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["token_count"] <= 6


def test_extract_text_from_html_bytes():
    html = b"<html><body><h1>Idempotency</h1><p>POST is not idempotent by default.</p></body></html>"
    extracted = extract_text_from_bytes("doc.html", html, "text/html")

    assert "Idempotency" in extracted
    assert "POST is not idempotent by default." in extracted
