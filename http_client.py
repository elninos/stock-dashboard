"""공통 HTTP 유틸리티 — urllib 반복 패턴 통합."""
import json
import urllib.request
import urllib.error
from config import USER_AGENT, TIMEOUT_MEDIUM

# Set to True to print HTTP errors (useful for debugging)
DEBUG_HTTP = False


def http_get(url: str, headers: dict | None = None, timeout: int = TIMEOUT_MEDIUM) -> bytes | None:
    """GET 요청 → bytes 반환. 실패 시 None."""
    _headers = {"User-Agent": USER_AGENT}
    if headers:
        _headers.update(headers)
    req = urllib.request.Request(url, headers=_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if DEBUG_HTTP:
            print(f"[http_client] HTTP {e.code} {url}")
        return None
    except Exception as e:
        if DEBUG_HTTP:
            print(f"[http_client] ERROR {type(e).__name__}: {url}")
        return None


def http_get_json(url: str, headers: dict | None = None, timeout: int = TIMEOUT_MEDIUM) -> dict | list | None:
    """GET 요청 → JSON 파싱. 실패 시 None."""
    data = http_get(url, headers=headers, timeout=timeout)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        if DEBUG_HTTP:
            print(f"[http_client] JSON parse error: {url}: {e}")
        return None


def http_get_text(url: str, headers: dict | None = None, timeout: int = TIMEOUT_MEDIUM) -> str | None:
    """GET 요청 → 텍스트 반환. 실패 시 None."""
    data = http_get(url, headers=headers, timeout=timeout)
    if data is None:
        return None
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
