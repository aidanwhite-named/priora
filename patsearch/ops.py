"""EPO Open Patent Services(OPS) v3.2 검색 클라이언트 (표준 라이브러리만 사용)."""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
BASE_URL = "https://ops.epo.org/3.2/rest-services"
USER_AGENT = "patsearch-mvp/0.1"
MAX_RETRIEVABLE = 2000  # OPS 는 검색식 하나당 앞의 2000건까지만 내준다
MAX_PER_REQUEST = 100


class OpsError(Exception):
    pass


class OpsQueryError(OpsError):
    """검색식(CQL) 자체가 잘못된 경우. 메시지를 LLM 에 돌려줘 고치게 한다."""


class OpsQuotaError(OpsError):
    pass


class OpsThrottleError(OpsError):
    """분당 요청 제한에 걸려 wait_seconds 동안 검색이 차단된 경우."""

    def __init__(self, wait_seconds: float, detail: str = ""):
        self.wait_seconds = wait_seconds
        super().__init__(f"EPO 검색이 약 {max(1, round(wait_seconds / 60))}분간 차단되었습니다 ({detail})")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter(el, name: str):
    return (e for e in el.iter() if _local(e.tag) == name)


def _first(el, name: str):
    return next(_iter(el, name), None)


def _text(el) -> str:
    return " ".join(" ".join(el.itertext()).split()) if el is not None else ""


def _pick_lang(elements, lang: str = "en"):
    elements = list(elements)
    for e in elements:
        if e.get("lang") == lang:
            return e
    return elements[0] if elements else None


def _error_message(body: str) -> str:
    try:
        root = ET.fromstring(body)
        msg = _first(root, "message")
        details = _first(root, "details")
        if details is None:
            details = _first(root, "description")
        parts = [_text(msg), _text(details)]
        text = " / ".join(p for p in parts if p)
        if text:
            return text
    except ET.ParseError:
        pass
    return re.sub(r"\s+", " ", body)[:300]


def parse_document(doc) -> dict:
    """<exchange-document> 하나를 평평한 dict 로 바꾼다."""
    country = doc.get("country", "")
    number = doc.get("doc-number", "")
    kind = doc.get("kind", "")
    pub_date = ""
    pub_ref = _first(doc, "publication-reference")
    if pub_ref is not None:
        for did in _iter(pub_ref, "document-id"):
            date = _text(_first(did, "date"))
            if date and (did.get("document-id-type") == "docdb" or not pub_date):
                pub_date = date
    prio_dates = []
    prio = _first(doc, "priority-claims")
    if prio is not None:
        prio_dates = sorted(d for d in (_text(e) for e in _iter(prio, "date")) if d)
    applicants = []
    for app in _iter(doc, "applicant"):
        if app.get("data-format") in (None, "epodoc"):
            name = _text(_first(app, "name"))
            if name and name not in applicants:
                applicants.append(name)
    cpcs = []
    for pc in _iter(doc, "patent-classification"):
        scheme = _first(pc, "classification-scheme")
        if scheme is not None and not (scheme.get("scheme", "").upper().startswith("CPC")):
            continue
        parts = {n: _text(_first(pc, n)) for n in ("section", "class", "subclass", "main-group", "subgroup")}
        if parts["section"] and parts["main-group"]:
            sym = f'{parts["section"]}{parts["class"]}{parts["subclass"]}{parts["main-group"]}/{parts["subgroup"]}'
            if sym not in cpcs:
                cpcs.append(sym)
    title = _text(_pick_lang(_iter(doc, "invention-title")))
    abstract_el = _pick_lang(_iter(doc, "abstract"))
    return {
        "pn": f"{country}{number}{kind}",
        "country": country,
        "family_id": doc.get("family-id", "") or f"{country}{number}",
        "pub_date": pub_date,
        "priority_date": prio_dates[0] if prio_dates else "",
        "title": title,
        "abstract": _text(abstract_el),
        "abstract_lang": abstract_el.get("lang", "") if abstract_el is not None else "",
        "applicants": applicants[:4],
        "cpc": cpcs[:12],
    }


def parse_search(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    search = _first(root, "biblio-search")
    total = int(search.get("total-result-count", "0")) if search is not None else 0
    docs = [parse_document(d) for d in _iter(root, "exchange-document")]
    return {"total": total, "docs": docs}


class OpsClient:
    def __init__(self, key: str, secret: str, cache=None, base_interval: float = 3.0, max_wait: float = 120.0,
                 on_wait=None):
        self.key = key
        self.secret = secret
        self.cache = cache
        self.base_interval = base_interval  # 검색 요청 사이 최소 간격(초)
        self.min_interval = base_interval
        self.max_wait = max_wait  # 차단됐을 때 자동으로 기다려 줄 최대 시간(초)
        self.on_wait = on_wait
        self.throttle_state = ""
        self._token = ""
        self._token_expiry = 0.0
        self._last_call = 0.0
        self.requests_made = 0

    # --- 인증 -------------------------------------------------------------
    def token(self, force: bool = False) -> str:
        if self._token and not force and time.time() < self._token_expiry:
            return self._token
        basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        req = urllib.request.Request(
            AUTH_URL,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise OpsError(f"OPS 인증 실패 (HTTP {e.code}): {_error_message(body)}") from None
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 1200)) - 120
        return self._token

    # --- 요청 -------------------------------------------------------------
    def _throttle(self, headers) -> None:
        """응답 헤더가 알려주는 분당 검색 허용 횟수에 요청 간격을 맞춘다.

        예: X-Throttling-Control: busy (images=green:50, ..., search=green:15)
        서버가 바쁠수록 숫자가 줄어든다. 이를 넘기면 로봇으로 판정되어 한동안 차단된다.
        """
        control = (headers.get("X-Throttling-Control") or "") if headers else ""
        m = re.search(r"search=(\w+):(\d+)", control)
        if not m:
            return
        self.throttle_state = control
        per_minute = int(m.group(2))
        if per_minute > 0:
            # 허용치의 70% 속도로만 보낸다
            self.min_interval = max(self.base_interval, 60.0 / (per_minute * 0.7))

    def _post(self, url: str, cql: str, rng: str) -> str:
        last_error = None
        for attempt in range(4):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode({"q": cql}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "Accept": "application/xml",
                    "Content-Type": "text/plain",
                    "X-OPS-Range": rng,
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            self._last_call = time.time()
            self.requests_made += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    self._throttle(resp.headers)
                    return body
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                message = _error_message(body)
                if e.code == 404:
                    return ""  # 결과 0건
                if e.code == 400 and "invalid_access_token" in body:
                    self.token(force=True)
                    continue
                if e.code == 400:
                    raise OpsQueryError(message) from None
                if e.code == 403:
                    reason = e.headers.get("X-Rejection-Reason", "")
                    self._throttle(e.headers)
                    if reason in ("IndividualQuotaPerHour", "RegisteredQuotaPerWeek"):
                        raise OpsQuotaError(f"OPS 데이터 쿼터 초과: {reason}") from None
                    # 분당 요청 제한(ThrottlingControlQuota / RobotDetected). Retry-After 는 밀리초로 온다.
                    raw = int(e.headers.get("Retry-After", "0") or 0)
                    wait_s = raw / 1000 if raw > 3600 else float(raw)
                    wait_s = max(wait_s, 20.0) + 5
                    if wait_s > self.max_wait or attempt == 3:
                        raise OpsThrottleError(wait_s, f"{reason} {message}".strip()) from None
                    if self.on_wait:
                        self.on_wait(wait_s)
                    time.sleep(wait_s)
                    last_error = OpsThrottleError(wait_s, reason)
                    continue
                if e.code in (429, 500, 502, 503, 504):
                    retry_after = int(e.headers.get("Retry-After", "0") or 0)
                    last_error = OpsError(f"HTTP {e.code}: {message}")
                    time.sleep(max(retry_after, 3 * (attempt + 1)))
                    continue
                raise OpsError(f"HTTP {e.code}: {message}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                last_error = OpsError(f"네트워크 오류: {e}")
                time.sleep(3 * (attempt + 1))
        raise last_error or OpsError("OPS 요청 실패")

    # --- 검색 -------------------------------------------------------------
    def count(self, cql: str) -> int:
        """검색식의 히트 수만 확인한다 (서지 없이 1건만 요청)."""
        key = f"count|{cql}"
        if self.cache is not None:
            hit = self.cache.get("ops", key)
            if hit is not None:
                return hit["total"]
        body = self._post(f"{BASE_URL}/published-data/search", cql, "1-1")
        total = parse_search(body)["total"] if body else 0
        if self.cache is not None:
            self.cache.put("ops", key, {"total": total})
        return total

    def search(self, cql: str, limit: int = 100) -> dict:
        """서지·초록과 함께 앞에서부터 limit 건을 가져온다."""
        limit = min(limit, MAX_RETRIEVABLE)
        key = f"biblio|{limit}|{cql}"
        if self.cache is not None:
            hit = self.cache.get("ops", key)
            if hit is not None:
                return hit
        docs, total, begin = [], 0, 1
        while begin <= limit:
            end = min(begin + MAX_PER_REQUEST - 1, limit)
            body = self._post(f"{BASE_URL}/published-data/search/biblio", cql, f"{begin}-{end}")
            if not body:
                break
            page = parse_search(body)
            total = page["total"]
            docs.extend(page["docs"])
            if end >= total or not page["docs"]:
                break
            begin = end + 1
        result = {"total": total, "docs": docs}
        if self.cache is not None:
            self.cache.put("ops", key, result)
        return result
