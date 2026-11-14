import json

import httpx
import pytest

from app import pipeline
from app.fetch import FetchError, check_public_url, parse_product_html
from app.llm import Gemini, LLMError, parse_json, pick_models
from app.search import Evidence, Researcher, SearchError, mark_sources


def _models(*names):
    return [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]


def test_pick_models_takes_newest_stable_flash():
    main, lite = pick_models(_models("gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.10-flash-lite",
                                     "gemini-3.5-flash-lite", "gemini-3.9-flash-image-preview",
                                     "gemini-4-argon", "gemini-2.5-flash"))
    assert (main, lite) == ("gemini-3.8-flash", "gemini-3.10-flash-lite")


def test_pick_models_falls_back_to_aliases():
    assert pick_models(_models("gemini-4-argon")) == ("gemini-flash-latest", "gemini-flash-lite-latest")
    assert pick_models(_models("gemini-5-flash-preview"))[0] == "gemini-5-flash-preview"


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(json.JSONDecodeError):
        parse_json("no json here")


def _gemini(handler, **kw):
    return Gemini("key", "main-model", "lite-model", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def _ok(text="hi"):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "x", "thought": True}, {"text": text}]}}]})


def test_generate_waits_out_a_minute_limit_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down", "details": [{"retryDelay": "7s"}]}})
        return _ok("done")

    reply = _gemini(handler).generate("hello")
    assert reply.text == "done" and reply.model == "main-model" and len(calls) == 2


def test_generate_moves_to_other_model_when_daily_limit_hit():
    def handler(request):
        if "main-model" in request.url.path:
            return httpx.Response(429, json={"error": {"message": "Quota exceeded",
                                  "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
        return _ok("from lite")

    reply = _gemini(handler).generate("hello")
    assert reply.text == "from lite" and reply.model == "lite-model"


def test_generate_reports_bad_key_plainly():
    with pytest.raises(LLMError) as err:
        _gemini(lambda r: httpx.Response(400, json={"error": {"message": "API key not valid."}})).generate("x")
    assert err.value.kind == "auth"


def test_generate_json_asks_again_after_bad_json():
    answers = iter(["not json", '{"ok": true}'])
    assert _gemini(lambda r: _ok(next(answers))).generate_json("x") == {"ok": True}


def test_generate_sends_key_in_header_not_url():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers.get("x-goog-api-key")
        return _ok()

    _gemini(handler).generate("x", json_mode=True)
    assert seen["key"] == "key" and "key=" not in seen["url"]


def test_mark_sources_by_text_and_by_byte_offset():
    text = "Café lights sell well. Buyers love them."
    marked = mark_sources(text, [
        {"text": "Café lights sell well.", "start": 0, "end": 23, "chunks": [0]},
        {"text": "", "start": 24, "end": len(text.encode()), "chunks": [0, 1]},
        {"text": "ignored", "start": 0, "end": 5, "chunks": [9]},
    ], {0: 1, 1: 2})
    assert marked == "Café lights sell well.[S1] Buyers love them.[S1][S2]"


def test_evidence_numbers_sources_once_and_cleans_markdown():
    ev = Evidence()
    a = ev.add_doc("https://a.co.uk/x", "A", "Read ![pic](p.png) the [guide](https://x.y) " + "word " * 20)
    assert a == 1 and ev.add_doc("https://a.co.uk/x", "A again", "x" * 100) == 1
    assert ev.add_doc("https://b.com", "B", "too short") is None
    assert "p.png" not in ev.text() and "guide" in ev.text() and ev.valid_ids() == {1}


def test_researcher_without_any_provider_explains_what_to_do():
    r = Researcher({"tavily_key": "", "grounding_model": "", "search_provider": "auto"}, None)
    assert not r.ready
    with pytest.raises(SearchError, match="Tavily"):
        r.gather([("q", "")], "brief")


def test_check_public_url_blocks_private_addresses():
    for bad in ("http://127.0.0.1/x", "http://10.0.0.5/", "http://169.254.169.254/latest", "ftp://example.com", "notalink"):
        with pytest.raises(FetchError):
            check_public_url(bad)


def test_parse_product_html_reads_json_ld_and_ignores_scripts():
    html = ('<html><head><title>T</title><meta property="og:title" content="OG name">'
            '<script type="application/ld+json">[{"@type":"BreadcrumbList"},{"@type":["Product"],"name":"Real name",'
            '"brand":{"name":"Brandy"},"offers":[{"price":12.5,"priceCurrency":"GBP"}],'
            '"aggregateRating":{"ratingValue":4.7,"ratingCount":88}}]</script></head>'
            '<body><script>secret()</script><p>Visible words</p></body></html>')
    info = parse_product_html(html)
    assert info["name"] == "Real name" and info["price"] == "12.5" and info["currency"] == "GBP"
    assert info["brand"] == "Brandy" and info["rating"] == "4.7" and info["review_count"] == "88"
    assert "Visible words" in info["text"] and "secret" not in info["text"]


def test_clean_products_drops_anything_without_a_real_source():
    raw = {"products": [
        {"name": "Real lamp", "source_ids": [1, "S2", 99], "competition": "LOW", "confidence": "sure"},
        {"name": "Made up", "source_ids": []},
        {"name": "Wrong ids", "source_ids": [42]},
        {"name": "real  lamp", "source_ids": [1]},
        "junk",
    ]}
    out = pipeline.clean_products(raw, {1, 2})
    assert [p["name"] for p in out] == ["Real lamp"]
    assert out[0]["source_ids"] == [1, 2] and out[0]["competition"] == "low" and out[0]["confidence"] == "medium"


def test_clean_pack_enforces_sources_ad_tag_and_flow_rules():
    raw = {
        "product": {"name": "Lamp"},
        "facts": [{"text": "Has a USB plug", "source_ids": [1]}, {"text": "Invented", "source_ids": []}],
        "buyer_insights": [{"text": "Cosy", "source_ids": [7]}],
        "hooks": [{"on_screen_text": "Look"}, {"voiceover": "Listen"}, {"on_screen_text": "Third"}],
        "scenes": [{"flow_prompt": "A lamp on a table"}, {"flow_prompt": ""}, {"flow_prompt": "Close up. No on-screen text.", "keep_seconds": 99}],
        "hashtags": ["cosy home", "#AD", "#cosyhome", "#Lamp!"],
        "caption": "Nice lamp",
    }
    pack = pipeline.clean_pack(raw, {1}, {"clip_seconds": "8"}, "Fallback")
    assert [f["text"] for f in pack["facts"]] == ["Has a USB plug"] and pack["buyer_insights"] == []
    assert [h["label"] for h in pack["hooks"]] == ["A", "B"]
    assert len(pack["scenes"]) == 2
    assert pack["scenes"][0]["flow_prompt"] == "A lamp on a table. " + pipeline.FLOW_SUFFIX + " " + pipeline.AUDIO_SILENT
    assert pack["scenes"][1]["flow_prompt"] == "Close up. No on-screen text. " + pipeline.AUDIO_SILENT and not pack["voice_in_video"]
    assert pack["scenes"][1]["keep_seconds"] == 8 and pack["scenes"][1]["n"] == 2
    assert pack["hashtags"] == ["#ad", "#cosyhome", "#Lamp"]
    assert pack["caption_full"] == "Nice lamp\n\n#ad #cosyhome #Lamp"


def test_one_prompt_mode_keeps_a_single_full_length_scene():
    raw = {"scenes": [{"flow_prompt": "One shot", "keep_seconds": 3}, {"flow_prompt": "Extra the model added"}]}
    pack = pipeline.clean_pack(raw, set(), {"clip_seconds": "8", "scenes": "1"}, "Lamp")
    assert len(pack["scenes"]) == 1 and pack["scenes"][0]["keep_seconds"] == 8 and pack["total_seconds"] == 8


def test_pack_prompt_wording_matches_the_number_of_prompts():
    from app import prompts
    one, many = prompts.video_rules(1, 8), prompts.video_rules(4, 8)
    assert "ONE prompt" in one["how_made"] and "no cuts" in one["prompt_rule"] and "20 words" in one["voice_rule"]
    assert "Write 4 scenes" in many["how_made"] and "every scene" in many["prompt_rule"]
    text = prompts.PACK.format(today="t", name="n", product_context="", account="", learning="", evidence="", **one)
    assert "{" not in text.split("Return one JSON object")[0]


def test_voiceover_is_written_into_the_flow_prompt():
    raw = {"scenes": [{
        "flow_prompt": "0-2 seconds: a press on a tray. No on-screen text, no captions, no logos. Natural room sound only, no speech, no music.",
        "voiceover": 'Make "holiday" cookies in seconds.'}]}
    settings = {"clip_seconds": "8", "scenes": "1", "voice_in_video": "1", "voice_style": "A calm Scottish male voice."}
    pack = pipeline.clean_pack(raw, set(), settings, "Press")
    prompt = pack["scenes"][0]["flow_prompt"]
    assert prompt == ("0-2 seconds: a press on a tray. No on-screen text, no captions, no logos. Natural room sound, no music. "
                      "A calm Scottish male voice says off-screen: \"Make 'holiday' cookies in seconds.\"")
    assert pack["voice_in_video"] is True and prompt.count("Natural room sound") == 1 and "no speech" not in prompt


def test_voice_setting_falls_back_to_a_silent_clip():
    scene = {"flow_prompt": "A lamp on a table", "voiceover": "It glows."}
    off = pipeline.clean_pack({"scenes": [dict(scene)]}, set(), {"scenes": "1", "voice_in_video": "0"}, "Lamp")
    no_line = pipeline.clean_pack({"scenes": [{"flow_prompt": "A lamp"}]}, set(), {"scenes": "1", "voice_in_video": "1"}, "Lamp")
    many = pipeline.clean_pack({"scenes": [dict(scene), dict(scene)]}, set(), {"scenes": "2", "voice_in_video": "1"}, "Lamp")
    default_voice = pipeline.clean_pack({"scenes": [dict(scene)]}, set(), {"scenes": "1", "voice_in_video": "1"}, "Lamp")
    for pack in (off, no_line, many):
        assert not pack["voice_in_video"] and all(s["flow_prompt"].endswith(pipeline.AUDIO_SILENT) for s in pack["scenes"])
    assert default_voice["scenes"][0]["flow_prompt"].endswith('A warm British female voice says off-screen: "It glows."')


def test_pack_prompt_tells_the_model_when_flow_speaks_the_voiceover():
    from app import prompts
    spoken, silent = prompts.video_rules(1, 8, True), prompts.video_rules(1, 8, False)
    assert "spoken inside" in spoken["voice_rule"] and "20 words" in spoken["voice_rule"]
    assert "voice already on it" in spoken["how_made"] and "voiceover in TikTok" in silent["how_made"]
    assert "added to it automatically" in spoken["prompt_rule"]
