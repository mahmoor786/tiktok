"""The actual work: find products, build video packs, check connections.

The model's answers are never trusted as-is. Anything without a real source
behind it is dropped here, in code, before it reaches the page.
"""
from __future__ import annotations

import json
import re

from . import config, db, prompts
from .fetch import FetchError, fetch_product_page
from .llm import Gemini, LLMError, pick_models
from .search import Evidence, Researcher, SearchError, clean_text

PRODUCT_COUNT = 10
MIN_SOURCED = 3  # facts + buyer insights needed before a pack counts as trustworthy
FLOW_SUFFIX = "No on-screen text, no captions, no logos. Natural room sound only, no speech, no music."
GENERIC_TITLES = ("tiktok shop", "tiktok", "tiktok - make your day", "amazon.co.uk", "amazon", "just a moment",
                  "access denied", "robot check", "security check", "page not found")


class PipelineError(Exception):
    """A problem worth showing to her in plain words."""


def _demo_transport():
    if not config.DEMO:
        return None
    from . import demo
    return demo.transport()


def services(settings: dict) -> tuple[Gemini, Researcher]:
    transport = _demo_transport()
    if config.DEMO:
        settings = {**settings, "gemini_key": settings["gemini_key"] or "demo", "tavily_key": settings["tavily_key"] or "demo"}
    llm = Gemini(settings["gemini_key"], settings["model_main"], settings["model_lite"], transport=transport)
    return llm, Researcher(settings, llm, transport=transport)


def setup_state(settings: dict) -> dict:
    has_gemini = bool(settings["gemini_key"]) or config.DEMO
    has_search = bool(settings["tavily_key"] or settings["grounding_model"]) or config.DEMO
    return {"gemini": has_gemini, "search": has_search, "ready": has_gemini and has_search, "demo": config.DEMO}


# ---------------- small helpers ----------------

def _s(value, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _ids(value, valid: set[int]) -> list[int]:
    out: list[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            num = int(str(item).strip().lstrip("[Ss").rstrip("]"))
        except ValueError:
            continue
        if num in valid and num not in out:
            out.append(num)
    return out


def _pick(value, allowed: tuple[str, ...], default: str) -> str:
    value = _s(value, 20).lower()
    return value if value in allowed else default


def learning_summary() -> str:
    rows = db.packs_with_results()
    if not rows:
        return "No results recorded yet."

    def line(row: dict) -> str:
        fmt = ((row.get("pack") or {}).get("format") or {}).get("name", "") or "?"
        note = f" | note: {_s(row['notes'], 120)}" if row.get("notes") else ""
        return (f"- {row['product_name']} | format: {fmt} | hook {row['hook_used'] or '?'} | "
                f"{row['views'] or 0} views | {row['sales'] or 0} sales{note}")

    ranked = sorted(rows, key=lambda r: ((r["sales"] or 0), (r["views"] or 0)), reverse=True)
    out = ["Best so far:"] + [line(r) for r in ranked[:6]]
    if len(ranked) > 8:
        out += ["Weakest so far:"] + [line(r) for r in ranked[-4:]]
    return "\n".join(out)


def _category_line(cat_id: str) -> tuple[str, str]:
    """(line for prompts, search terms)"""
    cat = config.category(cat_id)
    season = config.season_hint()
    if cat["id"] == "seasonal":
        return f"Seasonal and gift products ({season})", season.split(":")[-1].split(",")[0].strip() + " gifts"
    if cat["id"] == "any":
        return "Any category, whatever has the strongest demand", ""
    return f"{cat['label']} ({cat['terms']})", cat["label"].replace("&", "and").lower()


# ---------------- discovery ----------------

def clean_products(raw, valid: set[int], count: int = PRODUCT_COUNT) -> list[dict]:
    items = raw.get("products") if isinstance(raw, dict) else raw
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = _s(item.get("name"), 120)
        ids = _ids(item.get("source_ids"), valid)
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not name or not ids or key in seen:
            continue  # no real source behind it: drop it
        seen.add(key)
        out.append({
            "name": name,
            "price_gbp": _s(item.get("price_gbp"), 30),
            "why_now": _s(item.get("why_now"), 300),
            "evidence": _s(item.get("evidence"), 400),
            "competition": _pick(item.get("competition"), ("low", "medium", "high", "unknown"), "unknown"),
            "video_angle": _s(item.get("video_angle"), 300),
            "marketplace_search": _s(item.get("marketplace_search"), 80) or name,
            "confidence": _pick(item.get("confidence"), ("high", "medium", "low"), "medium"),
            "source_ids": ids,
        })
        if len(out) >= count:
            break
    return out


def discover(settings: dict, research_id: int, progress) -> None:
    research = db.get_research(research_id)
    llm, researcher = services(settings)
    now = config.uk_now()
    month_year = now.strftime("%B %Y")
    season = config.season_hint(now)
    category_line, term = _category_line(research["category"])
    season_short = season.split(":")[-1].split(",")[0].strip()

    queries = [
        (f"TikTok Shop UK best selling {term} products {month_year}".replace("  ", " "), "month"),
        (f"TikTok Shop UK trending {term} products this week".replace("  ", " "), "week"),
        (f"TikTok made me buy it UK viral {term} products {month_year}".replace("  ", " "), "month"),
        (f"TikTok Shop UK top selling products {season_short} {now.year}", "month"),
        (f"best selling {term or 'products'} UK {month_year} viral on TikTok", "month"),
    ]
    if research["note"]:
        queries[-1] = (f"TikTok Shop UK {research['note']} {term} best selling {month_year}".replace("  ", " "), "month")
    brief = prompts.DISCOVER_BRIEF.format(
        month_year=month_year, category_line=category_line, season=season,
        queries="; ".join(q for q, _ in queries))

    db.update("research", research_id, {"status": "working"})
    progress(6, "Searching for what is selling in the UK")
    evidence = researcher.gather(
        queries, brief, per_query=5,
        on_step=lambda i, n, q: progress(6 + int(48 * i / max(n, 1)), "Searching for what is selling in the UK"))
    progress(60, f"Reading {len(evidence.sources)} sources and choosing products")

    raw = llm.generate_json(prompts.DISCOVER.format(
        today=now.strftime("%A %d %B %Y"), count=PRODUCT_COUNT, category_line=category_line, season=season,
        note_line=f"SHE ALSO ASKED FOR: {research['note']}\n" if research["note"] else "",
        account=settings["account_notes"] or "Nothing noted yet.",
        learning=learning_summary(),
        exclude=", ".join(db.recent_product_names()) or "none",
        evidence=evidence.text()), system=prompts.SYSTEM)

    products = clean_products(raw, evidence.valid_ids())
    if not products:
        raise PipelineError(
            "The search did not find enough real evidence of products selling in this category. "
            "Try another category, or paste a product link instead.")
    used = {i for p in products for i in p["source_ids"]}
    db.update("research", research_id, {
        "status": "ready",
        "products": json.dumps(products),
        "sources": json.dumps([s for s in evidence.sources if s["id"] in used]),
    })
    progress(100, f"Found {len(products)} products")


# ---------------- packs ----------------

def _sourced(items, valid: set[int], limit: int = 8) -> list[dict]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text, ids = _s(item.get("text"), 400), _ids(item.get("source_ids"), valid)
        if text and ids:
            out.append({"text": text, "source_ids": ids})
    return out[:limit]


def clean_pack(raw, valid: set[int], settings: dict, fallback_name: str) -> dict:
    if not isinstance(raw, dict):
        raise PipelineError("The pack came back in a form that could not be read. Tap Try again.")
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    best = raw.get("best_time_uk") if isinstance(raw.get("best_time_uk"), dict) else {}

    hooks = []
    for index, hook in enumerate(raw.get("hooks") if isinstance(raw.get("hooks"), list) else []):
        if isinstance(hook, dict) and (_s(hook.get("on_screen_text")) or _s(hook.get("voiceover"))):
            hooks.append({"label": "AB"[index] if index < 2 else str(index + 1),
                          "on_screen_text": _s(hook.get("on_screen_text"), 120),
                          "voiceover": _s(hook.get("voiceover"), 240),
                          "why": _s(hook.get("why"), 240)})
    hooks = hooks[:2]

    clip = int(settings.get("clip_seconds") or 8)
    wanted = max(1, min(int(settings.get("scenes") or 6), 6))
    scenes = []
    for scene in raw.get("scenes") if isinstance(raw.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        flow = _s(scene.get("flow_prompt"), 1800)
        if not flow:
            continue
        if "no on-screen text" not in flow.lower():
            flow = flow.rstrip(". ") + ". " + FLOW_SUFFIX
        try:
            keep = max(2, min(int(scene.get("keep_seconds") or 4), clip))
        except (TypeError, ValueError):
            keep = 4
        scenes.append({"n": len(scenes) + 1, "goal": _s(scene.get("goal"), 160), "flow_prompt": flow,
                       "on_screen_text": _s(scene.get("on_screen_text"), 80),
                       "voiceover": _s(scene.get("voiceover"), 240), "keep_seconds": keep})
    scenes = scenes[:wanted]  # never more clips than she asked for
    if wanted == 1 and scenes:
        scenes[0]["keep_seconds"] = clip  # one prompt, one clip: the clip is the whole video

    tags, seen = [], set()
    for tag in raw.get("hashtags") if isinstance(raw.get("hashtags"), list) else []:
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag))
        if len(tag) > 1 and tag.lower() not in seen and tag.lower() != "#ad":
            seen.add(tag.lower())
            tags.append(tag)
    tags = ["#ad"] + tags[:7]  # UK rule: affiliate posts must be clearly marked as ads

    caption = _s(raw.get("caption"), 400)
    facts = _sourced(raw.get("facts"), valid)
    insights = _sourced(raw.get("buyer_insights"), valid)
    return {
        "product": {"name": _s(product.get("name"), 120) or fallback_name,
                    "one_line": _s(product.get("one_line"), 240),
                    "price_gbp": _s(product.get("price_gbp"), 30)},
        "data_quality": _pick(raw.get("data_quality"), ("good", "thin"), "good"),
        "facts": facts,
        "buyer_insights": insights,
        "format": {"name": _s(fmt.get("name"), 60), "why": _s(fmt.get("why"), 300),
                   "source_ids": _ids(fmt.get("source_ids"), valid)},
        "hooks": hooks,
        "scenes": scenes,
        "voiceover_full": " ".join(s["voiceover"] for s in scenes if s["voiceover"]),
        "caption": caption,
        "hashtags": tags,
        "caption_full": (caption + "\n\n" + " ".join(tags)).strip(),
        "best_time_uk": {"window": _s(best.get("window"), 80), "why": _s(best.get("why"), 240)},
        "sound": _s(raw.get("sound"), 240),
        "reference_image_tip": _s(raw.get("reference_image_tip"), 300),
        "claims_to_avoid": [_s(c, 200) for c in (raw.get("claims_to_avoid") if isinstance(raw.get("claims_to_avoid"), list) else []) if _s(c)][:6],
        "total_seconds": sum(s["keep_seconds"] for s in scenes),
    }


def _tidy_page_name(name: str) -> str:
    name = re.split(r"\s+[|–—]\s+|\s+-\s+(?=TikTok|Amazon|eBay|Temu)", name)[0].strip()
    return "" if name.lower() in GENERIC_TITLES or len(name) < 4 else name


def _read_link(pack: dict, llm: Gemini, researcher: Researcher, evidence: Evidence) -> tuple[str, str]:
    """Returns (product name, context text for the prompt). Adds the page as a source when readable."""
    url, typed_name = pack["link_url"], _s((pack.get("product") or {}).get("typed_name"), 120)
    info, page_text = None, ""
    try:
        info = fetch_product_page(url, transport=_demo_transport())
    except FetchError:
        info = None
    if info:
        bits = [info["name"], info["description"]]
        if info["price"]:
            bits.append(f"Price shown: {info['currency']} {info['price']}".strip())
        if info["rating"]:
            bits.append(f"Rating shown: {info['rating']} from {info['review_count'] or '?'} reviews")
        page_text = "\n".join(b for b in bits if b) + "\n" + info["text"][:1500]
    if len(page_text) < 300 and researcher.tavily:
        try:
            page_text = (page_text + "\n" + clean_text(researcher.tavily.extract(url))).strip()
        except SearchError:
            pass
    name = typed_name or _tidy_page_name(info["name"] if info else "")
    if not name and len(page_text) > 200:
        try:
            got = llm.generate_json(prompts.NAME_FROM_PAGE.format(text=page_text[:4000]),
                                    system=prompts.SYSTEM, prefer="lite", temperature=0.1)
            name = _tidy_page_name(_s(got.get("name") if isinstance(got, dict) else "", 120))
        except LLMError:
            name = ""
    if not name:
        raise PipelineError(
            "The server could not read that shop page (many shops block this). "
            "Type the product name under the link and tap Try again.")
    if len(page_text) >= 80:
        evidence.add_doc(url, f"Product page: {name}", page_text)
    return name, f"She gave this product link: {url}\n"


def make_pack(settings: dict, pack_id: int, progress, *, force: bool = False) -> None:
    pack = db.get_pack(pack_id)
    llm, researcher = services(settings)
    db.update("packs", pack_id, {"status": "working", "error": ""})
    evidence = Evidence()
    card = pack.get("product") or {}

    if pack["link_url"]:
        progress(8, "Opening the product link")
        name, context = _read_link(pack, llm, researcher, evidence)
        db.update("packs", pack_id, {"product_name": name})
    else:
        name = pack["product_name"]
        context = "".join(
            f"{label}: {card[key]}\n" for key, label in
            (("why_now", "Why it was picked"), ("evidence", "Evidence of demand"),
             ("video_angle", "Suggested angle"), ("price_gbp", "Price seen")) if card.get(key))

    queries = [
        (f"{name} review UK", ""),
        (f"{name} reviews what buyers say pros and cons", ""),
        (f"{name} TikTok Shop UK", "year"),
        (f"{name} TikTok viral video hook", "year"),
    ]
    brief = prompts.PACK_BRIEF.format(name=name, extra=context)
    progress(15, f"Researching {name}")
    try:
        researcher.gather(queries, brief, per_query=4, evidence=evidence,
                          on_step=lambda i, n, q: progress(15 + int(40 * i / max(n, 1)), f"Researching {name}"))
    except SearchError:
        if not evidence.sources:
            raise
    progress(60, f"Writing the pack for {name}")

    raw = llm.generate_json(prompts.PACK.format(
        today=config.uk_now().strftime("%A %d %B %Y"), name=name, product_context=context,
        account=settings["account_notes"] or "Nothing noted yet.", learning=learning_summary(),
        evidence=evidence.text(), **prompts.video_rules(int(settings["scenes"]), int(settings["clip_seconds"]))),
        system=prompts.SYSTEM)
    result = clean_pack(raw, evidence.valid_ids(), settings, name)

    used = {i for group in (result["facts"], result["buyer_insights"]) for item in group for i in item["source_ids"]}
    used |= set(result["format"]["source_ids"])
    sourced = len(result["facts"]) + len(result["buyer_insights"])
    if not result["scenes"]:
        raise PipelineError("The pack came back without any scenes. Tap Try again.")
    thin = sourced < MIN_SOURCED or result["data_quality"] == "thin"
    status = "insufficient" if thin and not force else "ready"
    error = ""
    if thin:
        error = (f"Only {sourced} checked fact{'s' if sourced != 1 else ''} could be found for this product, "
                 "so the script may be generic. Skip it, or use it and check the details against the product page yourself.")
    result["forced"] = bool(thin and force)
    db.update("packs", pack_id, {
        "status": status, "error": error, "product_name": result["product"]["name"],
        "pack": json.dumps(result),
        "sources": json.dumps([s for s in evidence.sources if s["id"] in used]),
    })
    progress(100, f"Pack ready for {name}" if status == "ready" else f"Not enough data for {name}")


# ---------------- connection test ----------------

def self_test(settings: dict) -> dict:
    """Checks each connection for real and returns what works, in plain words."""
    checks: list[dict] = []
    updates: dict = {}
    llm, _ = services(settings)
    gemini_ok = False
    models: list[dict] = []

    if not (settings["gemini_key"] or config.DEMO):
        checks.append({"name": "Gemini key", "ok": False, "detail": "No key saved yet. Get a free one at aistudio.google.com."})
    else:
        try:
            models = llm.list_models()
            main, lite = pick_models(models)
            updates.update(model_main=main, model_lite=lite)
            llm.model_main, llm.model_lite = main, lite
            reply = llm.generate("Reply with the single word OK.", prefer="lite", temperature=0)
            gemini_ok = True
            checks.append({"name": "Gemini key", "ok": True,
                           "detail": f"Working. Writing with {main}, quick jobs with {lite} (answered using {reply.model})."})
        except LLMError as exc:
            checks.append({"name": "Gemini key", "ok": False, "detail": str(exc)})

    tavily_ok = False
    if settings["tavily_key"] or config.DEMO:
        _, researcher = services(settings)
        try:
            hits = researcher.tavily.search("TikTok Shop UK trending products", max_results=1)
            tavily_ok = bool(hits)
            checks.append({"name": "Tavily web search", "ok": tavily_ok,
                           "detail": "Working." if tavily_ok else "Connected, but the test search returned nothing."})
        except SearchError as exc:
            checks.append({"name": "Tavily web search", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "Tavily web search", "ok": False,
                       "detail": "No key saved yet. Get a free one at tavily.com (1,000 searches a month, no card)."})

    grounding = ""
    if gemini_ok:
        listed = {m.get("name", "").split("/", 1)[-1] for m in models}
        candidates = [m for m in ("gemini-2.5-flash", "gemini-2.5-flash-lite") if m in listed]
        candidates += [llm.model_lite, llm.model_main]
        tried = []
        for name in candidates[:3]:
            if name in tried:
                continue
            tried.append(name)
            try:
                reply = llm.generate("What is the top story on BBC News today? One sentence.", model=name,
                                     search=True, temperature=0)
                if reply.sources:
                    grounding = name
                    break
            except LLMError:
                continue
        updates["grounding_model"] = grounding
        checks.append({
            "name": "Google Search through Gemini", "ok": bool(grounding), "optional": True,
            "detail": (f"Available on this key (using {grounding})." if grounding else
                       "Not available on this key. Google only allows it on keys with billing turned on. "
                       "This is fine as long as Tavily works."),
        })

    ready = gemini_ok and (tavily_ok or bool(grounding))
    return {"checks": checks, "ready": ready, "updates": updates}
