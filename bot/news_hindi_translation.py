"""Add Hindi headline text to the existing news-monitor response.

This module is display-only. It never changes news scoring, trade blocking,
strategy decisions, quantities, exits, or broker orders.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping

TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_HEADLINES = 5
_HINDI_RE = re.compile(r"[\u0900-\u097F]")


def _contains_hindi(value: Any) -> bool:
    return bool(_HINDI_RE.search(str(value or "")))


@lru_cache(maxsize=512)
def translate_headline_to_hindi(title: str) -> str:
    """Translate one headline and return an empty string on any failure."""
    original = str(title or "").strip()
    if not original or _contains_hindi(original):
        return original

    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": "hi",
            "dt": "t",
            "q": original,
        }
    )
    request = urllib.request.Request(
        f"{TRANSLATE_URL}?{query}",
        headers={
            "User-Agent": "OptionKingAI-NewsHindi/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=7) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        parts = payload[0] if isinstance(payload, list) and payload else []
        translated = "".join(
            str(part[0] or "")
            for part in parts
            if isinstance(part, list) and part
        ).strip()
        return translated if translated and _contains_hindi(translated) else ""
    except Exception:
        return ""


def add_hindi_headlines(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a response copy with ``title_hi`` on the first five headlines."""
    if not isinstance(report, Mapping):
        return dict(report or {})

    output: Dict[str, Any] = dict(report)
    current = output.get("current_news")
    if not isinstance(current, Mapping):
        return output

    current_copy: Dict[str, Any] = dict(current)
    raw_headlines = current_copy.get("top_headlines")
    if not isinstance(raw_headlines, list) or not raw_headlines:
        output["current_news"] = current_copy
        return output

    headlines = [dict(item) if isinstance(item, Mapping) else item for item in raw_headlines]
    pending: Dict[Any, int] = {}

    with ThreadPoolExecutor(max_workers=min(MAX_HEADLINES, len(headlines))) as pool:
        for index, item in enumerate(headlines[:MAX_HEADLINES]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            existing = str(
                item.get("title_hi")
                or item.get("hindi_title")
                or item.get("translated_title_hi")
                or ""
            ).strip()
            if existing and _contains_hindi(existing):
                item["title_hi"] = existing
                continue
            if not title:
                continue
            pending[pool.submit(translate_headline_to_hindi, title)] = index

        for future in as_completed(pending):
            index = pending[future]
            try:
                translated = str(future.result() or "").strip()
            except Exception:
                translated = ""
            if translated and _contains_hindi(translated) and isinstance(headlines[index], dict):
                headlines[index]["title_hi"] = translated

    current_copy["top_headlines"] = headlines
    current_copy["headline_language_support"] = {
        "english": True,
        "hindi": True,
        "translated_count": sum(
            1
            for item in headlines[:MAX_HEADLINES]
            if isinstance(item, dict) and _contains_hindi(item.get("title_hi"))
        ),
        "display_only": True,
    }
    output["current_news"] = current_copy
    return output
