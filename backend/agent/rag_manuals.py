"""RAG local con ChromaDB persistente (./data/chroma_data), sin Docker.

Colecciones:
- manuals:        fragmentos de los PDFs en data/manuals/ (W204, 4Runner, Pilot...)
- web_solutions:  fragmentos web que el agente encontró + arreglos verificados por ti

Tip: nombra los PDFs con el motor/modelo (ej. "w204_m271_workshop.pdf", "4runner_3rzfe_fsm.pdf")
para que la búsqueda priorice el manual correcto.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Dict, List, Optional

from backend.config import CHROMA_DIR, MANUALS_DIR

log = logging.getLogger("baymax.rag")
_lock = threading.Lock()
_client = None


def _get_client():
    global _client
    if _client is None:
        import chromadb  # import perezoso: tarda ~1-2 s

        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def _collection(name: str):
    return _get_client().get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def _chunks(text: str, size: int = 1000, overlap: int = 150) -> List[str]:
    text = " ".join(text.split())
    out, i = [], 0
    while i < len(text):
        out.append(text[i: i + size])
        i += size - overlap
    return [c for c in out if len(c) > 80]


def ingest_manuals() -> Dict:
    """Indexa PDFs nuevos o modificados de data/manuals/. Idempotente."""
    from pypdf import PdfReader

    with _lock:
        col = _collection("manuals")
        report = {"indexed": [], "skipped": [], "errors": []}
        for pdf in sorted(MANUALS_DIR.glob("**/*.pdf")):
            digest = hashlib.md5(pdf.read_bytes()).hexdigest()[:12]
            existing = col.get(where={"file_hash": digest}, limit=1)
            if existing and existing.get("ids"):
                report["skipped"].append(pdf.name)
                continue
            try:
                col.delete(where={"file": pdf.name})  # versión anterior del mismo archivo
                reader = PdfReader(str(pdf))
                ids, docs, metas = [], [], []
                for page_no, page in enumerate(reader.pages, start=1):
                    for j, chunk in enumerate(_chunks(page.extract_text() or "")):
                        ids.append(f"{digest}-{page_no}-{j}")
                        docs.append(chunk)
                        metas.append({"file": pdf.name, "page": page_no, "file_hash": digest})
                for k in range(0, len(ids), 256):
                    col.add(ids=ids[k:k + 256], documents=docs[k:k + 256], metadatas=metas[k:k + 256])
                report["indexed"].append({"file": pdf.name, "chunks": len(ids)})
            except Exception as e:
                log.exception("Error indexando %s", pdf.name)
                report["errors"].append({"file": pdf.name, "error": str(e)})
        return report


def search_manuals(query: str, k: int = 4) -> List[dict]:
    try:
        col = _collection("manuals")
        if col.count() == 0:
            return []
        res = col.query(query_texts=[query], n_results=min(k, col.count()))
    except Exception as e:
        log.warning("RAG manuals no disponible: %s", e)
        return []
    out = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        out.append({"text": doc, "file": meta.get("file"), "page": meta.get("page"), "score": round(1 - dist, 3)})
    return out


def store_web_solutions(vehicle: dict, dtc_code: str, sources: List[dict]) -> None:
    """Guarda lo encontrado en internet para no depender de la red la próxima vez."""
    try:
        col = _collection("web_solutions")
        ids, docs, metas = [], [], []
        for s in sources:
            for p in s.get("passages", []):
                ids.append(hashlib.md5((s["url"] + p).encode()).hexdigest())
                docs.append(p)
                metas.append({"url": s["url"], "title": s.get("title", "")[:200], "dtc": dtc_code,
                              "engine": vehicle.get("engine_code") or "", "kind": "web"})
        if ids:
            col.upsert(ids=ids, documents=docs, metadatas=metas)
    except Exception as e:
        log.warning("No se pudo guardar en web_solutions: %s", e)


def store_verified_fix(vehicle: dict, dtc_code: Optional[str], fix: str, hypothesis: str, log_id: int) -> None:
    try:
        _collection("web_solutions").upsert(
            ids=[f"fix-{log_id}"],
            documents=[f"ARREGLO VERIFICADO en {vehicle.get('year')} {vehicle.get('make')} {vehicle.get('model')} "
                       f"({vehicle.get('engine_code')}) para {dtc_code or 'síntoma'}: {fix}. Hipótesis previa: {hypothesis[:300]}"],
            metadatas=[{"url": "garage.db", "title": "Arreglo verificado en tu flota", "dtc": dtc_code or "",
                        "engine": vehicle.get("engine_code") or "", "kind": "verified"}],
        )
    except Exception as e:
        log.warning("No se pudo guardar arreglo verificado: %s", e)


def search_known_solutions(query: str, dtc_code: Optional[str] = None, k: int = 4) -> List[dict]:
    try:
        col = _collection("web_solutions")
        if col.count() == 0:
            return []
        kwargs = {"query_texts": [query], "n_results": min(k, col.count())}
        if dtc_code:
            kwargs["where"] = {"dtc": dtc_code}
        res = col.query(**kwargs)
    except Exception as e:
        log.debug("web_solutions query falló: %s", e)
        return []
    return [
        {"text": d, "url": m.get("url"), "title": m.get("title"), "kind": m.get("kind"), "score": round(1 - dist, 3)}
        for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]


def stats() -> Dict:
    try:
        return {"manuals_chunks": _collection("manuals").count(),
                "web_chunks": _collection("web_solutions").count(),
                "pdfs_in_folder": len(list(MANUALS_DIR.glob("**/*.pdf")))}
    except Exception as e:
        return {"error": str(e)}
