"""Gemini client (plain REST, no SDK).

Only the parts of the API that are stable are used: models.list and
models.generateContent with optional JSON output or the google_search tool.
Model names change every few months, so they are picked from the key's own
model list instead of being hard-coded.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MAIN = "gemini-flash-latest"
FALLBACK_LITE = "gemini-flash-lite-latest"

_FLASH = re.compile(r"^models/gemini-(\d+)(?:\.(\d+))?-flash(-lite)?$")
_FLASH_PREVIEW = re.compile(r"^models/gemini-(\d+)(?:\.(\d+))?-flash(-lite)?-preview$")


class LLMError(Exception):
    """An error with a message that is safe and useful to show on the page."""

    def __init__(self, message: str, *, kind: str = "error", status: int = 0):
        super().__init__(message)
        self.kind = kind  # auth | quota | daily_quota | unavailable | blocked | bad_json | error
        self.status = status


@dataclass
class Reply:
    text: str
    model: str
    sources: list[dict] = field(default_factory=list)   # [{"url","title"}] from Google Search
    supports: list[dict] = field(default_factory=list)  # [{"text","start","end","chunks":[int]}]
    queries: list[str] = field(default_factory=list)


def pick_models(models: list[dict]) -> tuple[str, str]:
    """Newest stable Flash and Flash-Lite that support generateContent."""

    def best(pattern: re.Pattern, lite: bool) -> str:
        found = []
        for m in models:
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            hit = pattern.match(m.get("name", ""))
            if hit and bool(hit.group(3)) == lite:
                found.append(((int(hit.group(1)), int(hit.group(2) or 0)), m["name"].split("/", 1)[1]))
        return max(found)[1] if found else ""

    main = best(_FLASH, False) or best(_FLASH_PREVIEW, False) or FALLBACK_MAIN
    lite = best(_FLASH, True) or best(_FLASH_PREVIEW, True) or FALLBACK_LITE
    return main, lite


def parse_json(text: str):
    """Parse model output as JSON, tolerating code fences and stray prose."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", cleaned, 0)


def _retry_delay(body: dict) -> float:
    for detail in (body.get("error") or {}).get("details") or []:
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                pass
    return 0.0


def _is_daily_quota(body: dict) -> bool:
    blob = json.dumps(body).lower()
    return "perday" in blob or "per day" in blob or "daily" in blob


class Gemini:
    def __init__(self, api_key: str, model_main: str = "", model_lite: str = "", *,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.api_key = api_key.strip()
        self.model_main = model_main or FALLBACK_MAIN
        self.model_lite = model_lite or FALLBACK_LITE
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=20.0),
            transport=transport,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
        )

    # ----- models -----

    def list_models(self) -> list[dict]:
        if not self.api_key:
            raise LLMError("Add your Gemini key in Settings first.", kind="auth")
        try:
            res = self._client.get(f"{BASE}/models", params={"pageSize": 1000})
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Gemini: {exc.__class__.__name__}.", kind="unavailable") from exc
        if res.status_code in (400, 401, 403):
            raise LLMError("Gemini did not accept this key. Check it was copied in full.", kind="auth",
                           status=res.status_code)
        if res.status_code != 200:
            raise LLMError(f"Gemini answered with error {res.status_code} when listing models.",
                           status=res.status_code)
        return res.json().get("models") or []

    # ----- generate -----

    def _chain(self, prefer: str) -> list[str]:
        order = [self.model_main, self.model_lite] if prefer == "main" else [self.model_lite, self.model_main]
        seen, out = set(), []
        for name in order:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def generate(self, prompt: str, *, system: str = "", prefer: str = "main", model: str = "",
                 json_mode: bool = False, search: bool = False, temperature: float = 0.6) -> Reply:
        """One generateContent call with retries and a fallback model.

        Waits out per-minute limits; when a model's daily limit is used up it
        moves on to the other model.
        """
        if not self.api_key:
            raise LLMError("Add your Gemini key in Settings first.", kind="auth")
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 16384},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if search:
            body["tools"] = [{"google_search": {}}]

        last: LLMError | None = None
        for name in ([model] if model else self._chain(prefer)):
            for attempt in range(3):
                try:
                    res = self._client.post(f"{BASE}/models/{name}:generateContent", json=body)
                except httpx.HTTPError as exc:
                    last = LLMError(f"Could not reach Gemini ({exc.__class__.__name__}).", kind="unavailable")
                    self._sleep(3 * (attempt + 1))
                    continue
                if res.status_code == 200:
                    return self._read(res.json(), name)
                try:
                    err_body = res.json()
                except ValueError:
                    err_body = {}
                detail = ((err_body.get("error") or {}).get("message") or "").strip()[:300]
                if res.status_code == 429:
                    if _is_daily_quota(err_body):
                        last = LLMError(
                            "Today's free Gemini limit is used up. It resets overnight (US Pacific time).",
                            kind="daily_quota", status=429)
                        break  # try the other model
                    last = LLMError("Gemini is rate-limiting this key. Wait a minute and try again.",
                                    kind="quota", status=429)
                    self._sleep(min(max(_retry_delay(err_body), 5.0), 60.0))
                    continue
                if res.status_code in (500, 502, 503, 504):
                    last = LLMError("Gemini is busy right now. Try again in a few minutes.",
                                    kind="unavailable", status=res.status_code)
                    self._sleep(4 * (attempt + 1))
                    continue
                if res.status_code in (401, 403) or "api key" in detail.lower():
                    raise LLMError("Gemini did not accept this key. Check it in Settings.", kind="auth",
                                   status=res.status_code)
                # 400 / 404: wrong model name or unsupported option. Try the next model.
                last = LLMError(f"Gemini refused the request ({res.status_code}): {detail or 'no detail given'}",
                                status=res.status_code)
                break
        raise last or LLMError("Gemini did not answer.")

    @staticmethod
    def _read(data: dict, model: str) -> Reply:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "")
            raise LLMError(f"Gemini returned no answer{' (' + reason + ')' if reason else ''}.", kind="blocked")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            reason = cand.get("finishReason", "")
            raise LLMError(f"Gemini returned an empty answer{' (' + reason + ')' if reason else ''}.", kind="blocked")
        meta = cand.get("groundingMetadata") or {}
        sources = []
        for chunk in meta.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            sources.append({"url": web.get("uri", ""), "title": web.get("title", "")})
        supports = []
        for sup in meta.get("groundingSupports") or []:
            seg = sup.get("segment") or {}
            supports.append({
                "text": seg.get("text", ""),
                "start": int(seg.get("startIndex") or 0),
                "end": int(seg.get("endIndex") or 0),
                "chunks": [int(i) for i in sup.get("groundingChunkIndices") or []],
            })
        return Reply(text=text, model=model, sources=sources, supports=supports,
                     queries=list(meta.get("webSearchQueries") or []))

    def generate_json(self, prompt: str, *, system: str = "", prefer: str = "main", temperature: float = 0.6):
        """Ask for JSON; if the answer does not parse, ask once more."""
        reply = self.generate(prompt, system=system, prefer=prefer, json_mode=True, temperature=temperature)
        try:
            return parse_json(reply.text)
        except json.JSONDecodeError:
            pass
        reply = self.generate(
            prompt + "\n\nYour last answer was not valid JSON. Return only one valid JSON object, nothing else.",
            system=system, prefer=prefer, json_mode=True, temperature=0.2)
        try:
            return parse_json(reply.text)
        except json.JSONDecodeError as exc:
            raise LLMError("Gemini's answer could not be read. Try again.", kind="bad_json") from exc
