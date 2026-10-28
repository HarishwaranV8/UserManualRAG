import io
import os
import re
import threading
from pathlib import Path

import fitz  # PyMuPDF
import joblib
import pytesseract
from PIL import Image
from pypdf import PdfReader
from django.conf import settings
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity



_lock = threading.RLock()
_index = None

INDEX_FILE = Path(__file__).resolve().parent / "rag_index.joblib"
INDEX_VERSION = 2


def manuals_directory():
    """
    Resolve MANUALS_DIR from the environment.

    Relative paths are resolved from settings.BASE_DIR (backend/).
    Default: smartphone-rag/manuals/ locally,
             /app/manuals in the Docker deployment.
    """
    configured = os.getenv("MANUALS_DIR")

    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
    else:
        # rag.py is in backend/chat/.
        # Go up three levels to the repository root.
        project_root = Path(__file__).resolve().parents[2]
        path = project_root / "manuals"

    return path.resolve()



def split_text(text, chunk_size=1100, overlap=180):
    """Split extracted/OCR text into overlapping chunks."""
    text = re.sub(r"\s+", " ", text or "").strip()

    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end == len(text):
            break

        start = end - overlap

    return chunks


def _ocr_image(image_bytes):
    """OCR an embedded image, returning an empty string on failure."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()

            # Skip tiny decorative images.
            if image.width < 100 or image.height < 60:
                return ""

            image = image.convert("RGB")

            # Upscale small diagrams to improve OCR.
            if image.width < 1000:
                scale = min(2, 1000 / image.width)
                image = image.resize(
                    (int(image.width * scale), int(image.height * scale))
                )

            return pytesseract.image_to_string(
                image, config="--psm 11"
            ).strip()

    except Exception as exc:
        print(f"Image OCR failed: {exc}")
        return ""


def _ocr_page(pdf_page):
    """Render a scanned PDF page and OCR the rendered page."""
    try:
        pix = pdf_page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            colorspace=fitz.csRGB,
            alpha=False,
        )

        image = Image.frombytes(
            "RGB", (pix.width, pix.height), pix.samples
        )

        return pytesseract.image_to_string(
            image, config="--psm 3"
        ).strip()

    except Exception as exc:
        print(f"Page OCR failed: {exc}")
        return ""


def _extract_page_text(pdf_page, pymupdf_page):
    """
    Extract native PDF text first.
    OCR the full page if it appears scanned; otherwise OCR embedded images
    to capture labels in diagrams.
    """
    native_text = (pdf_page.extract_text() or "").strip()
    extracted_parts = []

    if native_text:
        extracted_parts.append(native_text)

    if len(native_text) < 80:
        page_ocr = _ocr_page(pymupdf_page)
        if page_ocr:
            extracted_parts.append(
                "[OCR text from scanned page]\n" + page_ocr
            )
    else:
        seen_xrefs = set()

        for image_info in pymupdf_page.get_images(full=True):
            xref = image_info[0]

            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                extracted_image = pymupdf_page.parent.extract_image(xref)
                image_text = _ocr_image(extracted_image["image"])

                if image_text:
                    extracted_parts.append(
                        "[OCR text from embedded diagram/image]\n"
                        + image_text
                    )

            except Exception as exc:
                print(
                    f"Could not extract image {xref} "
                    f"on page {pymupdf_page.number + 1}: {exc}"
                )

    return "\n\n".join(extracted_parts).strip()


def _file_signature(pdf_path):
    """
    Metadata used to detect PDF changes quickly.
    A content hash is also stored, so changes with preserved timestamps
    can be detected when the file is checked.
    """
    stat = pdf_path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _file_hash(pdf_path):
    """Return a SHA-256 hash so PDF changes are detected reliably."""
    import hashlib

    digest = hashlib.sha256()
    with pdf_path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_pdf(pdf_path):
    """Extract and chunk one PDF. Called only for new/changed manuals."""
    phone = pdf_path.stem.replace("_", " ").strip()
    chunks = []

    print(f"Indexing manual: {pdf_path.name}")

    reader = PdfReader(str(pdf_path))

    with fitz.open(str(pdf_path)) as visual_doc:
        page_count = min(len(reader.pages), len(visual_doc))

        for page_index in range(page_count):
            text = _extract_page_text(
                reader.pages[page_index],
                visual_doc[page_index],
            )

            if not text:
                continue

            for part in split_text(text):
                chunks.append({
                    "phone": phone,
                    "page": page_index + 1,
                    "source": pdf_path.name,
                    "text": part,
                })

    return chunks


def _build_search_index(manual_chunks, phones, fingerprints):
    """Build one consistent TF-IDF matrix from all currently indexed manuals."""
    chunks = []

    # Keep chunks in stable PDF order.
    for pdf_name in sorted(manual_chunks):
        chunks.extend(manual_chunks[pdf_name])

    if not chunks:
        raise ValueError(
            "No text was extracted from the manuals. "
            "Check the PDFs and Tesseract installation."
        )

    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        max_features=100000,
    )
    matrix = vectorizer.fit_transform([chunk["text"] for chunk in chunks])

    return {
        "version": INDEX_VERSION,
        "phones": phones,
        "chunks": chunks,
        "vectorizer": vectorizer,
        "matrix": matrix,
        # Metadata enables future change detection and incremental extraction.
        "manual_chunks": manual_chunks,
        "fingerprints": fingerprints,
    }


def _save_index(index):
    """Atomically replace the old index file with the updated index."""
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = INDEX_FILE.with_name(INDEX_FILE.name + ".tmp")

    try:
        joblib.dump(index, temp_file)
        os.replace(temp_file, INDEX_FILE)
    finally:
        if temp_file.exists():
            temp_file.unlink()


def build_index():
    """
    Force a full rebuild of all PDFs and save the result.
    Use this when you intentionally want to re-extract every manual.
    """
    global _index

    with _lock:
        directory = manuals_directory()
        if not directory.exists():
            raise FileNotFoundError(f"Manuals directory not found: {directory}")

        pdf_files = sorted(directory.glob("*.pdf"))
        if not pdf_files:
            raise FileNotFoundError(f"No PDF manuals found in: {directory}")

        manual_chunks = {}
        fingerprints = {}
        phones = []

        for pdf_path in pdf_files:
            manual_chunks[pdf_path.name] = _extract_pdf(pdf_path)
            fingerprints[pdf_path.name] = {
                **_file_signature(pdf_path),
                "sha256": _file_hash(pdf_path),
            }
            phones.append(pdf_path.stem.replace("_", " ").strip())

        new_index = _build_search_index(
            manual_chunks, phones, fingerprints
        )
        _save_index(new_index)
        _index = new_index

        print(
            f"Index ready: {len(phones)} manuals, "
            f"{len(new_index['chunks'])} text chunks."
        )
        print(f"Index saved to: {INDEX_FILE}")
        return _index


def _load_saved_index():
    """Load a compatible saved index, or return None if it cannot be reused."""
    if not INDEX_FILE.exists():
        return None

    try:
        saved_index = joblib.load(INDEX_FILE)

        required_keys = {
            "version", "phones", "chunks", "vectorizer", "matrix",
            "manual_chunks", "fingerprints",
        }
        if (
            not isinstance(saved_index, dict)
            or not required_keys.issubset(saved_index)
            or saved_index.get("version") != INDEX_VERSION
        ):
            print("Saved index is outdated or incompatible; updating it.")
            return None

        return saved_index

    except Exception as exc:
        print(f"Could not load saved index: {exc}")
        print("Rebuilding the index...")
        return None


def _refresh_index():
    """
    Detect added, changed, and deleted PDFs.
    Extract only added/changed PDFs, then rebuild the shared TF-IDF matrix
    and persist the updated index. Returns the current index.
    """
    global _index

    directory = manuals_directory()
    if not directory.exists():
        raise FileNotFoundError(f"Manuals directory not found: {directory}")

    pdf_files = sorted(directory.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF manuals found in: {directory}")

    saved_index = _index if _index is not None else _load_saved_index()

    # Old or missing index: perform a full build.
    if saved_index is None:
        return build_index()

    current_names = {pdf.name for pdf in pdf_files}
    old_chunks = saved_index["manual_chunks"]
    old_fingerprints = saved_index["fingerprints"]
    old_names = set(old_chunks)

    added = current_names - old_names
    deleted = old_names - current_names
    changed = set()

    # Hash files to detect edits even when size/mtime metadata is preserved.
    current_fingerprints = {}
    for pdf_path in pdf_files:
        signature = _file_signature(pdf_path)
        sha256 = _file_hash(pdf_path)
        current_fingerprints[pdf_path.name] = {
            **signature,
            "sha256": sha256,
        }

        previous = old_fingerprints.get(pdf_path.name, {})
        if pdf_path.name in old_names and (
            previous.get("sha256") != sha256
        ):
            changed.add(pdf_path.name)

    if not added and not changed and not deleted:
        _index = saved_index
        print("Manuals unchanged; using saved TF-IDF index.")
        return _index

    print(
        "Manual changes detected — "
        f"added: {sorted(added)}, "
        f"modified: {sorted(changed)}, "
        f"deleted: {sorted(deleted)}"
    )

    # Copy the dictionary before updating it, leaving the previous index intact
    # if extraction or TF-IDF construction fails.
    manual_chunks = dict(old_chunks)

    for name in deleted:
        manual_chunks.pop(name, None)

    for pdf_path in pdf_files:
        if pdf_path.name in added or pdf_path.name in changed:
            manual_chunks[pdf_path.name] = _extract_pdf(pdf_path)

    # Only keep fingerprints for files currently present.
    fingerprints = {
        name: current_fingerprints[name]
        for name in current_names
    }
    phones = [
        pdf_path.stem.replace("_", " ").strip()
        for pdf_path in pdf_files
    ]

    new_index = _build_search_index(manual_chunks, phones, fingerprints)
    _save_index(new_index)
    _index = new_index

    print(
        f"Updated index: {len(phones)} manuals, "
        f"{len(new_index['chunks'])} text chunks."
    )
    print(f"Updated index saved to: {INDEX_FILE}")

    return _index


def get_index():
    """
    Return the index, checking for manual changes each time this function
    is called. The check hashes the PDFs but only re-extracts changed files.
    """
    global _index

    with _lock:
        return _refresh_index()


def retrieve(question, per_phone=4):
    """
    Retrieve up to per_phone relevant chunks for each phone.

    Returns:
        phones: list of manual/phone names
        evidence: list of chunks, including score and page references
    """
    index = get_index()
    phones = index["phones"]
    chunks = index["chunks"]

    question = (question or "").strip()
    if not question:
        return phones, []

    query_vector = index["vectorizer"].transform([question])
    scores = cosine_similarity(
        query_vector, index["matrix"]
    ).flatten()

    evidence = []

    for phone in phones:
        phone_indices = [
            i for i, chunk in enumerate(chunks)
            if chunk["phone"] == phone
        ]

        ranked_indices = sorted(
            phone_indices,
            key=lambda i: scores[i],
            reverse=True,
        )

        selected = [
            i for i in ranked_indices if scores[i] > 0
        ][:per_phone]

        if selected:
            for i in selected:
                item = dict(chunks[i])
                item["score"] = float(scores[i])
                evidence.append(item)
        else:
            evidence.append({
                "phone": phone,
                "page": None,
                "source": "retrieval",
                "text": "No relevant excerpt matched this question.",
                "score": 0.0,
            })

    return phones, evidence