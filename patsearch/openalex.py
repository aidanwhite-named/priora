"""OpenAlex 논문(비특허문헌) 검색."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.openalex.org/works"
USER_AGENT = "patsearch-mvp/0.1"
MIN_INTERVAL = 1.5
_last_call = 0.0
SELECT = "id,doi,display_name,publication_year,publication_date,primary_location,cited_by_count,abstract_inverted_index,type"


class OpenAlexError(Exception):
    pass


def _abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = []
    for word, idxs in inverted.items():
        positions.extend((i, word) for i in idxs)
    return " ".join(word for _, word in sorted(positions))


def search_works(query: str, per_page: int = 25, api_key: str = "", before: str = "", cache=None) -> dict:
    """before 는 YYYYMMDD. 해당 일자 이전에 공개된 문헌만 남긴다."""
    params = {"search": query, "per-page": str(per_page), "select": SELECT}
    if before:
        params["filter"] = f"to_publication_date:{before[:4]}-{before[4:6]}-{before[6:8]}"
    cache_key = json.dumps(params, sort_keys=True)
    if cache is not None:
        hit = cache.get("openalex", cache_key)
        if hit is not None:
            return hit
    if api_key:
        params["api_key"] = api_key
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    global _last_call
    data = None
    for attempt in range(3):
        # 불린 연산자가 많은 검색은 초당 1회로 제한되므로 호출 간격을 둔다
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise OpenAlexError(f"HTTP {e.code}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise OpenAlexError(f"네트워크 오류: {e}") from None
    works = []
    for w in data.get("results", []):
        source = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        works.append(
            {
                "id": (w.get("id") or "").rsplit("/", 1)[-1],
                "url": w.get("doi") or w.get("id") or "",
                "title": w.get("display_name") or "",
                "year": w.get("publication_year"),
                "pub_date": (w.get("publication_date") or "").replace("-", ""),
                "venue": source,
                "type": w.get("type") or "",
                "cited_by": w.get("cited_by_count", 0),
                "abstract": _abstract(w.get("abstract_inverted_index")),
            }
        )
    result = {"total": (data.get("meta") or {}).get("count", 0), "works": works}
    if cache is not None:
        cache.put("openalex", cache_key, result)
    return result
