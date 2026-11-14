"""Fake Gemini, Tavily and shop-page answers for trying the app without keys
and for the tests. Turned on only with TRENDPACK_DEMO=1. Everything it
returns is labelled DEMO so it can never be mistaken for real research."""
from __future__ import annotations

import json
import os
import time

import httpx

DEMO_PRODUCTS = [
    ("DEMO Curtain fairy lights, 3m warm white", "£9.99", "low"),
    ("DEMO Rechargeable candle lamp", "£14.50", "medium"),
    ("DEMO Christmas door wreath with pine cones", "£18.00", "high"),
    ("DEMO Rotating spice rack, 20 jars", "£22.99", "medium"),
    ("DEMO Heated throw blanket", "£34.99", "high"),
    ("DEMO Boucle recliner desk chair", "£89.00", "medium"),
    ("DEMO Under-bed storage bags, set of 4", "£11.99", "low"),
    ("DEMO Sunset projection lamp", "£12.49", "medium"),
    ("DEMO Draught excluder for doors", "£7.99", "low"),
    ("DEMO Electric milk frother", "£8.49", "high"),
    ("DEMO Self-watering plant pots, set of 3", "£13.99", "low"),
]


def _gemini_text(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def _discover() -> str:
    products = []
    for index, (name, price, competition) in enumerate(DEMO_PRODUCTS):
        products.append({
            "name": name, "price_gbp": price,
            "why_now": "Demo reason: cosy winter evenings and Christmas gifting are peaking this month.",
            "evidence": "Demo evidence: listed in two demo best-seller round-ups with strong weekly sales.",
            "competition": competition,
            "video_angle": "Demo angle: lights off, then the product turns the room cosy in one cut.",
            "marketplace_search": name.replace("DEMO ", ""),
            "confidence": "high" if index < 4 else "medium",
            "source_ids": [1 + index % 3, 2 + index % 3],
        })
    # One product with no source on purpose: the app must drop it.
    products.insert(2, {"name": "DEMO Invented product with no source", "price_gbp": "£1", "source_ids": []})
    return json.dumps({"products": products})


def _pack(prompt: str) -> str:
    name = prompt.split("PRODUCT:", 1)[-1].split("\n", 1)[0].strip() or "DEMO product"
    thin = "thin" in name.lower()
    scenes = []
    for n, goal in enumerate(("Hook: show the problem", "Reveal the product", "Close-up of it working", "The finished cosy room"), 1):
        scenes.append({
            "n": n, "goal": f"Demo: {goal}",
            "flow_prompt": (f"Vertical 9:16 phone video. A dim UK living room in the evening. {name}, warm white, "
                            f"draped across a cream curtain. A hand reaches in and switches it on. Slow handheld push-in, "
                            f"natural light from a side lamp. Shot on a phone, handheld, natural light."),
            "on_screen_text": ["My living room felt cold", "Then I found these", "One switch", "Cosy in seconds"][n - 1],
            "voiceover": ["Dark evenings made this room feel flat.", "These changed it.",
                          "One switch and the whole wall glows.", "Buyers say they never take them down."][n - 1],
            "keep_seconds": 4,
        })
    return json.dumps({
        "product": {"name": name, "one_line": "Demo: a warm white light curtain that hangs behind a sheer curtain.",
                    "price_gbp": "around £10"},
        "data_quality": "thin" if thin else "good",
        "facts": [] if thin else [
            {"text": "Demo fact: 3 metres wide with 300 warm white LEDs.", "source_ids": [1]},
            {"text": "Demo fact: plugs into USB and has 8 light modes.", "source_ids": [1, 2]},
            {"text": "Demo fact with no source, the app must drop this.", "source_ids": []},
        ],
        "buyer_insights": [] if thin else [
            {"text": "Demo insight: buyers say it makes the room feel cosy straight away.", "source_ids": [2]},
            {"text": "Demo insight: some buyers found the hooks fiddly to attach.", "source_ids": [3]},
        ],
        "format": {"name": "Cosy room transformation", "why": "Demo: before-and-after room clips lead this category.",
                   "source_ids": [3]},
        "hooks": [
            {"label": "A", "on_screen_text": "My living room felt cold", "voiceover": "Dark evenings made this room feel flat.",
             "why": "Demo: opens on a problem viewers recognise."},
            {"label": "B", "on_screen_text": "£10 changed this room", "voiceover": "This is the cheapest glow-up I have seen.",
             "why": "Demo: opens on the result and the price."},
        ],
        "scenes": scenes,
        "caption": "Demo caption: the easiest way to make a room feel cosy this winter. Tap the product to see it.",
        "hashtags": ["tiktokmademebuyit", "#tiktokshopuk", "#cosyhome", "#fairylights", "#ad", "#christmasdecor"],
        "best_time_uk": {"window": "7pm to 9pm", "why": "Demo: home buyers scroll in the evening."},
        "sound": "Demo: a soft, cosy trending instrumental.",
        "reference_image_tip": "Demo: upload the main product photo on a plain background so the lights look the same in every clip.",
        "claims_to_avoid": ["Demo: do not say it is waterproof.", "Demo: do not promise a delivery date."],
    })


def _handler(request: httpx.Request) -> httpx.Response:
    delay = float(os.environ.get("TRENDPACK_DEMO_DELAY", "0") or 0)
    host, path = request.url.host, request.url.path
    if host == "generativelanguage.googleapis.com":
        if request.method == "GET":
            return httpx.Response(200, json={"models": [
                {"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                for n in ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                          "gemini-2.5-flash", "gemini-3.1-flash-image-preview", "gemini-4-argon")]})
        body = json.loads(request.content)
        prompt = body["contents"][0]["parts"][0]["text"]
        if body.get("tools"):
            if os.environ.get("TRENDPACK_DEMO_GROUNDING") != "1":
                return httpx.Response(400, json={"error": {"message": "Google Search tool is not available on the free tier."}})
            text = "DEMO fairy lights are a best seller. Buyers like the warm glow."
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": text}]},
                "groundingMetadata": {
                    "webSearchQueries": ["demo query"],
                    "groundingChunks": [{"web": {"uri": "https://example.com/demo-g1", "title": "example.com"}},
                                        {"web": {"uri": "https://example.com/demo-g2", "title": "example.org"}}],
                    "groundingSupports": [
                        {"segment": {"startIndex": 0, "endIndex": 37, "text": "DEMO fairy lights are a best seller."},
                         "groundingChunkIndices": [0]},
                        {"segment": {"startIndex": 38, "endIndex": 68, "text": "Buyers like the warm glow."},
                         "groundingChunkIndices": [0, 1]}]}}]})
        time.sleep(delay)
        if "TASK: DISCOVER" in prompt:
            return httpx.Response(200, json=_gemini_text(_discover()))
        if "TASK: PACK" in prompt:
            return httpx.Response(200, json=_gemini_text("```json\n" + _pack(prompt) + "\n```"))
        if "TASK: NAME" in prompt:
            return httpx.Response(200, json=_gemini_text(json.dumps({"name": "DEMO Curtain fairy lights"})))
        return httpx.Response(200, json=_gemini_text("OK"))
    if host == "api.tavily.com":
        time.sleep(delay / 4)
        if path == "/extract":
            return httpx.Response(200, json={"results": [{"url": "x", "raw_content": "DEMO extracted page text. " * 30}]})
        query = json.loads(request.content).get("query", "")
        slug = "".join(c if c.isalnum() else "-" for c in query.lower())[:40]
        return httpx.Response(200, json={"results": [
            {"url": f"https://example.com/demo/{slug}/{i}", "title": f"DEMO source {i} for: {query[:40]}",
             "content": f"DEMO page {i}. This is placeholder evidence about '{query}'. " * 4,
             "raw_content": "DEMO longer page text with ![img](x.png) and a [link](https://example.com). " * 6}
            for i in (1, 2, 3)]})
    # Any other address is treated as a shop product page.
    if "blocked" in str(request.url):
        return httpx.Response(403, text="blocked")
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=(
        '<html><head><title>DEMO Curtain fairy lights | Demo Shop</title>'
        '<meta property="og:title" content="DEMO Curtain fairy lights | Demo Shop">'
        '<meta property="og:description" content="Demo description: 3m warm white light curtain.">'
        '<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product",'
        '"name":"DEMO Curtain fairy lights 3m","description":"Demo: 300 LEDs, USB powered.",'
        '"offers":{"@type":"Offer","price":"9.99","priceCurrency":"GBP"},'
        '"aggregateRating":{"ratingValue":"4.6","reviewCount":"1280"}}]}</script>'
        '</head><body><h1>DEMO Curtain fairy lights</h1><p>Demo body text about the product.</p>'
        '<script>var x = 1;</script></body></html>'))


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handler)
