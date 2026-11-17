"""Investigador web 100% gratis: DuckDuckGo (ddgs) + extracción con BeautifulSoup / Jina Reader.

Busca fallas por [Marca + Motor + Código], descarga las páginas top en paralelo y extrae
los párrafos más relevantes (los que hablan del código, del motor y de arreglos reales).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from backend.config import USE_JINA

log = logging.getLogger("baymax.web")

try:
    from ddgs import DDGS
except ImportError:  # nombre anterior del paquete
    from duckduckgo_search import DDGS  # type: ignore

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_BLOCKED = ("youtube.com", "facebook.com", "pinterest.", "tiktok.com", "instagram.com", "amazon.", "ebay.")
_FIX_WORDS = ("fix", "fixed", "replaced", "replace", "cause", "solution", "solved", "leak", "sensor",
              "hose", "gasket", "clean", "vacuum", "injector", "coil", "plug", "valve", "turbo", "boost",
              "arreglo", "cambié", "solución", "causa", "fuga", "manguera", "empaque", "sensor")
_cache: Dict[str, tuple] = {}
_CACHE_TTL = 60 * 60 * 6


def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def _search_sync(query: str, max_results: int) -> List[dict]:
    with DDGS() as ddg:
        return list(ddg.text(query, max_results=max_results) or [])


async def web_search(query: str, max_results: int = 8) -> List[dict]:
    """Resultados crudos: [{title, url, snippet, domain}]"""
    key = f"s::{query}::{max_results}"
    if key in _cache and time.time() - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1]
    try:
        raw = await asyncio.to_thread(_search_sync, query, max_results)
    except Exception as e:  # rate limit / sin internet
        log.warning("DuckDuckGo falló para %r: %s", query, e)
        return []
    out = []
    for r in raw:
        url = r.get("href") or r.get("url") or ""
        if not url or any(b in url for b in _BLOCKED):
            continue
        out.append({"title": r.get("title", ""), "url": url, "snippet": r.get("body", ""), "domain": _domain(url)})
    _cache[key] = (time.time(), out)
    return out


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    blocks = []
    for el in soup.find_all(["p", "li", "blockquote", "td", "div"]):
        if el.name == "div" and el.find(["p", "div", "li"]):
            continue  # solo divs "hoja" (posts de foros)
        txt = " ".join(el.get_text(" ", strip=True).split())
        if 60 <= len(txt) <= 1500:
            blocks.append(txt)
    return "\n".join(dict.fromkeys(blocks))  # dedup conservando orden


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        if USE_JINA:
            r = await client.get(f"https://r.jina.ai/{url}", headers={"Accept": "text/plain"})
            return r.text if r.status_code == 200 else ""
        r = await client.get(url)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return ""
        return await asyncio.to_thread(_extract_text, r.text)
    except Exception as e:
        log.debug("fetch %s falló: %s", url, e)
        return ""


def _best_passages(text: str, keywords: List[str], n: int = 2, max_len: int = 550) -> List[str]:
    kws = [k.lower() for k in keywords if k]
    scored = []
    for para in re.split(r"\n+", text):
        low = para.lower()
        score = sum(3 for k in kws if k in low) + sum(1 for w in _FIX_WORDS if w in low)
        if score >= 4:
            scored.append((score, para[:max_len]))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:n]]


async def research(query: str, keywords: List[str], max_sources: int = 4) -> List[dict]:
    """Busca + descarga + extrae. Devuelve fuentes con fragmentos relevantes."""
    results = await web_search(query, max_results=max_sources * 2 + 2)
    if not results:
        return []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        pages = await asyncio.gather(*(_fetch(client, r["url"]) for r in results[: max_sources * 2]))
    sources = []
    for r, text in zip(results, pages):
        passages = _best_passages(text, keywords) if text else []
        if not passages and r["snippet"]:
            passages = [r["snippet"]]
        if passages:
            sources.append({**r, "passages": passages})
        if len(sources) >= max_sources:
            break
    return sources


def build_dtc_query(vehicle: dict, dtc_code: str, symptoms: Optional[str] = None) -> str:
    engine = (vehicle.get("engine_code") or "").split(" ")[0]
    model = (vehicle.get("model") or "").split(" ")[0]
    q = f"{vehicle.get('make', '')} {model} {engine} {dtc_code} common causes fix"
    if symptoms:
        q += " " + " ".join(symptoms.split()[:6])
    return " ".join(q.split())


async def research_dtc(vehicle: dict, dtc_code: str, symptoms: Optional[str] = None, max_sources: int = 4) -> Dict:
    query = build_dtc_query(vehicle, dtc_code, symptoms)
    engine = (vehicle.get("engine_code") or "").split(" ")[0]
    keywords = [dtc_code, engine, vehicle.get("make", ""), (vehicle.get("model") or "").split(" ")[0]]
    sources = await research(query, keywords, max_sources)
    if len(sources) < 2:  # búsqueda más amplia si el motor es raro
        broader = f"{dtc_code} {vehicle.get('make', '')} causes fix forum"
        extra = await research(broader, keywords, max_sources - len(sources))
        seen = {s["url"] for s in sources}
        sources += [s for s in extra if s["url"] not in seen]
        query = f"{query} | {broader}"
    return {"query": query, "sources": sources}


async def check_internet(timeout: float = 3.0) -> bool:
    import socket

    def _probe_dns() -> bool:
        for host in ("1.1.1.1", "8.8.8.8"):
            try:
                with socket.create_connection((host, 53), timeout=1.5):
                    return True
            except OSError:
                continue
        return False

    if await asyncio.to_thread(_probe_dns):
        return True
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = await c.get("https://duckduckgo.com")
            return r.status_code < 500
    except Exception:
        return False
