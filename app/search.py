"""Web research. Everything found is kept as numbered sources ([S1], [S2]...)
so every later claim can be traced back to a real page.

Two providers:
  * Tavily search API (free plan, works with a free Gemini key)
  * Gemini's Google Search tool (needs a Gemini key with billing turned on)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .llm import Gemini, LLMError

TAVILY = "https://api.tavily.com"
DOC_CHARS = 2600      # text kept per page
TOTAL_CHARS = 70000   # overall evidence budget sent to the model


class SearchError(Exception):
    pass


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def clean_text(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")       # markdown images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)           # markdown links -> label
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


@dataclass
class Evidence:
    """Numbered sources plus the text gathered from them."""
    sources: list[dict] = field(default_factory=list)   # {"id","url","title"}
    blocks: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    _by_url: dict = field(default_factory=dict)

    def source_id(self, url: str, title: str = "") -> int:
        key = url.strip()
        if key in self._by_url:
            return self._by_url[key]
        new_id = len(self.sources) + 1
        self.sources.append({"id": new_id, "url": key, "title": (title or domain(key) or "Source").strip()[:140]})
        self._by_url[key] = new_id
        return new_id

    def add_doc(self, url: str, title: str, text: str) -> int | None:
        text = clean_text(text)
        if not url or len(text) < 40:
            return None
        if url.strip() in self._by_url:
            return self._by_url[url.strip()]
        sid = self.source_id(url, title)
        self.blocks.append(f"[S{sid}] {self.sources[sid - 1]['title']} ({domain(url)})\n{text[:DOC_CHARS]}")
        return sid

    def add_notes(self, text: str) -> None:
        if text.strip():
            self.blocks.append("RESEARCH NOTES (each [S#] marks the source of the sentence before it)\n" + text.strip())

    def text(self) -> str:
        out, used = [], 0
        for block in self.blocks:
            if used + len(block) > TOTAL_CHARS:
                break
            out.append(block)
            used += len(block)
        return "\n\n---\n\n".join(out)

    def valid_ids(self) -> set[int]:
        return {s["id"] for s in self.sources}


# ---------------- Tavily ----------------

class Tavily:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key.strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(40.0, connect=15.0), transport=transport,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )

    def _post(self, path: str, body: dict) -> dict:
        try:
            res = self._client.post(f"{TAVILY}{path}", json=body)
        except httpx.HTTPError as exc:
            raise SearchError(f"Could not reach Tavily ({exc.__class__.__name__}).") from exc
        if res.status_code in (401, 403):
            raise SearchError("Tavily did not accept this key. Check it in Settings.")
        if res.status_code in (429, 432, 433):
            raise SearchError("The Tavily search allowance for this month is used up (or it is rate-limited).")
        if res.status_code != 200:
            raise SearchError(f"Tavily answered with error {res.status_code}.")
        return res.json()

    def search(self, query: str, *, max_results: int = 5, time_range: str = "") -> list[dict]:
        body = {"query": query, "search_depth": "basic", "max_results": max_results,
                "include_raw_content": True, "include_answer": False}
        if time_range:
            body["time_range"] = time_range
        try:
            data = self._post("/search", body)
        except SearchError as exc:
            # Older/newer API versions may reject an optional field; retry with the bare minimum.
            if "error 4" not in str(exc):
                raise
            data = self._post("/search", {"query": query, "max_results": max_results})
        out = []
        for item in data.get("results") or []:
            text = (item.get("content") or "")
            raw = item.get("raw_content") or ""
            if raw:
                text = text + "\n" + raw
            out.append({"url": item.get("url", ""), "title": item.get("title", ""), "text": text})
        return out

    def extract(self, url: str) -> str:
        data = self._post("/extract", {"urls": [url]})
        for item in data.get("results") or []:
            if item.get("raw_content"):
                return item["raw_content"]
        return ""


# ---------------- Gemini + Google Search ----------------

def mark_sources(text: str, supports: list[dict], chunk_to_sid: dict[int, int]) -> str:
    """Insert [S#] markers after each sentence Google says a source supports."""
    inserts: list[tuple[int, str]] = []
    data = text.encode("utf-8")
    for sup in supports:
        ids = sorted({chunk_to_sid[c] for c in sup.get("chunks", []) if c in chunk_to_sid})
        if not ids:
            continue
        marker = "".join(f"[S{i}]" for i in ids)
        seg = sup.get("text") or ""
        pos = text.find(seg) if seg else -1
        if pos != -1:
            inserts.append((pos + len(seg), marker))
            continue
        # Fall back to the offsets, which the API gives in UTF-8 bytes.
        end = min(max(sup.get("end", 0), 0), len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end += 1
        inserts.append((len(data[:end].decode("utf-8", errors="ignore")), marker))
    for pos, marker in sorted(inserts, key=lambda x: x[0], reverse=True):
        text = text[:pos] + marker + text[pos:]
    return text


def google_research(llm: Gemini, model: str, brief: str, evidence: Evidence) -> None:
    reply = llm.generate(brief, model=model, search=True, temperature=0.3,
                         system="You are a meticulous researcher. Report only what the search results say. "
                                "Be specific: names, prices in GBP, numbers, dates. Say 'not found' when nothing is found.")
    if not reply.sources:
        raise SearchError("Google Search returned no sources for this.")
    chunk_to_sid = {}
    for index, src in enumerate(reply.sources):
        if src.get("url"):
            chunk_to_sid[index] = evidence.source_id(src["url"], src.get("title", ""))
    evidence.add_notes(mark_sources(reply.text, reply.supports, chunk_to_sid))
    evidence.queries.extend(reply.queries)


# ---------------- one entry point ----------------

class Researcher:
    """Runs web research with whichever provider this install has."""

    def __init__(self, settings: dict, llm: Gemini, *, transport: httpx.BaseTransport | None = None):
        self.llm = llm
        self.tavily = Tavily(settings["tavily_key"], transport=transport) if settings.get("tavily_key") else None
        self.grounding_model = settings.get("grounding_model", "")
        choice = settings.get("search_provider", "auto")
        if choice == "tavily" and self.tavily:
            self.order = ["tavily"]
        elif choice == "gemini" and self.grounding_model:
            self.order = ["gemini"]
        else:
            self.order = [p for p, ok in (("tavily", self.tavily), ("gemini", self.grounding_model)) if ok]

    @property
    def ready(self) -> bool:
        return bool(self.order)

    def gather(self, queries: list[tuple[str, str]], brief: str, *, per_query: int = 5,
               evidence: Evidence | None = None, on_step=None) -> Evidence:
        """queries: [(query, time_range)] for Tavily. brief: the same job in words for Google Search."""
        evidence = evidence or Evidence()
        if not self.order:
            raise SearchError(
                "No web search is set up. Add a free Tavily key in Settings "
                "(or use a Gemini key with billing turned on).")
        errors = []
        for provider in self.order:
            before = len(evidence.sources)
            try:
                if provider == "tavily":
                    for index, (query, time_range) in enumerate(queries):
                        if on_step:
                            on_step(index, len(queries), query)
                        for hit in self.tavily.search(query, max_results=per_query, time_range=time_range):
                            evidence.add_doc(hit["url"], hit["title"], hit["text"])
                        evidence.queries.append(query)
                else:
                    if on_step:
                        on_step(0, 1, "Google Search")
                    google_research(self.llm, self.grounding_model, brief, evidence)
            except (SearchError, LLMError) as exc:
                errors.append(str(exc))
                if len(evidence.sources) == before:
                    continue
            if len(evidence.sources) > before:
                evidence.providers.append(provider)
                return evidence
        raise SearchError(errors[0] if errors else "The web search found nothing.")
