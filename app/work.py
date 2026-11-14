"""Glue between the web routes, the scheduler and the pipeline: creates the
database rows, then queues the job that fills them in."""
from __future__ import annotations

import json
import threading
import time

from . import config, db, jobs, pipeline


def start_research(category: str, note: str = "", origin: str = "manual") -> dict:
    cat = config.category(category)
    research_id = db.insert("research", {
        "created_at": db.now_iso(), "category": cat["id"], "note": note[:120], "origin": origin, "status": "queued"})

    def run(ctx: jobs.JobContext) -> None:
        try:
            pipeline.discover(db.get_settings(), research_id, ctx.progress)
        except Exception as exc:
            db.update("research", research_id, {"status": "error", "error": jobs.friendly_error(exc)})
            raise

    job_id = jobs.submit("research", run, {"research_id": research_id}, "Getting ready to search")
    return {"job_id": job_id, "research_id": research_id}


def _queue_packs(pack_ids: list[int], force: bool = False) -> int:
    def run(ctx: jobs.JobContext) -> None:
        failures = []
        total = len(pack_ids)
        for index, pack_id in enumerate(pack_ids):
            def step(percent: int, message: str = "", _i=index) -> None:
                label = f"Pack {_i + 1} of {total}: {message}" if total > 1 else message
                ctx.progress(int((_i * 100 + percent) / total), label)
            try:
                pipeline.make_pack(db.get_settings(), pack_id, step, force=force)
            except Exception as exc:
                failures.append(exc)
                db.update("packs", pack_id, {"status": "error", "error": jobs.friendly_error(exc)})
        if failures and len(failures) == total:
            raise failures[0]

    return jobs.submit("packs", run, {"pack_ids": pack_ids}, "Getting ready")


def start_packs_from_research(research_id: int, indexes: list[int]) -> dict:
    research = db.get_research(research_id)
    products = research["products"] or []
    pack_ids = []
    for index in indexes:
        if 0 <= index < len(products):
            product = products[index]
            pack_ids.append(db.insert("packs", {
                "research_id": research_id, "created_at": db.now_iso(), "product_name": product["name"],
                "category": research["category"], "product": json.dumps(product), "status": "queued"}))
    if not pack_ids:
        return {"job_id": None, "pack_ids": []}
    return {"job_id": _queue_packs(pack_ids), "pack_ids": pack_ids}


def start_pack_from_link(url: str, typed_name: str = "") -> dict:
    pack_id = db.insert("packs", {
        "created_at": db.now_iso(), "product_name": typed_name[:120] or "Product from your link",
        "link_url": url, "product": json.dumps({"typed_name": typed_name[:120]}), "status": "queued"})
    return {"job_id": _queue_packs([pack_id]), "pack_ids": [pack_id]}


def retry_pack(pack_id: int, force: bool = False, typed_name: str = "") -> dict:
    pack = db.get_pack(pack_id)
    values = {"status": "queued", "error": ""}
    if typed_name and pack["link_url"]:
        values["product"] = json.dumps({**(pack.get("product") or {}), "typed_name": typed_name[:120]})
        values["product_name"] = typed_name[:120]
    db.update("packs", pack_id, values)
    return {"job_id": _queue_packs([pack_id], force=force), "pack_ids": [pack_id]}


# ---------------- daily automatic search ----------------

def auto_tick() -> bool:
    """Starts today's automatic search once, after the chosen UK time."""
    settings = db.get_settings()
    if settings["auto_daily"] != "1" or not pipeline.setup_state(settings)["ready"]:
        return False
    now = config.uk_now()
    today = now.strftime("%Y-%m-%d")
    if settings["last_auto_date"] == today or now.strftime("%H:%M") < settings["auto_time"]:
        return False
    db.set_settings({"last_auto_date": today})
    start_research(settings["default_category"], origin="auto")
    return True


def start_scheduler() -> None:
    def loop() -> None:
        while True:
            try:
                auto_tick()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=loop, name="trendpack-scheduler", daemon=True).start()
