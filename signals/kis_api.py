"""한국투자증권 OpenAPI 클라이언트.

토큰 자동 갱신 + 캐싱 + rate limit 준수.

호출 제한 (실전계좌):
  시세: 초당 20건 (우린 4.5건만 사용 — 25%)
  주문: 초당 5건 (사용 안 함)
  토큰: 1분당 1회

캐싱 전략:
  토큰 — 24시간
  거래원 — 5분 (실시간 변화)
  투자자/공매도 — 1시간 (일별 데이터)
  마감 후 — 24시간
"""
import os, json, time
from datetime import datetime, timedelta
from pathlib import Path

# .env.kis 경로 (프로젝트 루트)
ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".env.kis"
)

# 토큰 캐시 위치
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "kis_cache"
)
TOKEN_FILE = os.path.join(CACHE_DIR, "token.json")
os.makedirs(CACHE_DIR, exist_ok=True)


# 환경변수 로드
def _load_env():
    from dotenv import load_dotenv
    if os.path.exists(ENV_FILE):
        load_dotenv(ENV_FILE)
    appkey = os.environ.get("KIS_APPKEY")
    appsecret = os.environ.get("KIS_APPSECRET")
    if not appkey or not appsecret:
        raise RuntimeError(f"KIS_APPKEY/SECRET 없음. {ENV_FILE} 확인")
    return appkey, appsecret


# API 도메인 (실전/모의 자동 감지)
PROD_HOST = "https://openapi.koreainvestment.com:9443"
VTS_HOST  = "https://openapivts.koreainvestment.com:29443"


class KISClient:
    """KIS OpenAPI 클라이언트."""

    def __init__(self, mode: str = "auto"):
        """
        mode: 'real' | 'vts' | 'auto'
              auto = 토큰 발급 시도해서 작동하는 쪽 자동 선택
        """
        self.appkey, self.appsecret = _load_env()
        self._token = None
        self._token_expires = None
        self.host = None

        if mode == "real":
            self.host = PROD_HOST
        elif mode == "vts":
            self.host = VTS_HOST
        else:
            self.host = self._detect_host()

    def _detect_host(self) -> str:
        """캐시된 토큰이 있으면 그 호스트 사용, 없으면 실전 → 모의 순으로 시도."""
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE) as f:
                    cache = json.load(f)
                return cache.get("host", PROD_HOST)
            except Exception:
                pass
        # 실전 먼저 시도
        for host in (PROD_HOST, VTS_HOST):
            try:
                self.host = host
                self._issue_token()
                return host
            except Exception:
                continue
        raise RuntimeError("KIS API 토큰 발급 실패 (실전/모의 모두)")

    @property
    def token(self) -> str:
        """유효한 토큰 반환. 만료됐으면 자동 갱신."""
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        # 캐시 확인
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE) as f:
                    cache = json.load(f)
                exp = datetime.fromisoformat(cache["expires"])
                if datetime.now() < exp - timedelta(minutes=10):  # 10분 여유
                    self._token = cache["token"]
                    self._token_expires = exp
                    self.host = cache.get("host", self.host)
                    return self._token
            except Exception:
                pass

        # 새로 발급
        self._issue_token()
        return self._token

    def _issue_token(self):
        """토큰 신규 발급 + 캐시."""
        import requests
        url = f"{self.host}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.appkey,
            "appsecret": self.appsecret,
        }
        res = requests.post(url, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()

        self._token = data["access_token"]
        # KIS 토큰: 보통 24시간 (86400초)
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires = datetime.now() + timedelta(seconds=expires_in)

        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "token": self._token,
                "expires": self._token_expires.isoformat(),
                "host": self.host,
            }, f)

    def get(self, path: str, tr_id: str, params: dict, retry: int = 2) -> dict:
        """GET 요청 (시세 조회용).

        path:    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        tr_id:   "FHKST03010100" 같은 거래 ID
        params:  쿼리 파라미터
        """
        import requests
        url = f"{self.host}{path}"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": self.appkey,
            "appsecret": self.appsecret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }
        last_err = None
        for i in range(retry + 1):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=15)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("rt_cd") == "0":
                        return data
                    # 토큰 만료
                    if data.get("rt_cd") == "1" and "토큰" in data.get("msg1", ""):
                        self._token = None
                        os.remove(TOKEN_FILE) if os.path.exists(TOKEN_FILE) else None
                        continue
                    return data  # 비정상이지만 응답은 있음
                last_err = f"HTTP {res.status_code}: {res.text[:200]}"
            except Exception as e:
                last_err = str(e)
            time.sleep(0.5 * (i + 1))
        raise RuntimeError(f"KIS API 요청 실패: {last_err}")


# 싱글턴 클라이언트
_client = None
def get_client() -> KISClient:
    global _client
    if _client is None:
        _client = KISClient(mode="auto")
    return _client


# Rate limiter (1초당 5건 — 실전 한도 20건의 25%)
_last_call = 0.0
_MIN_INTERVAL = 0.22  # 초당 4.5건 안전 마진

def rate_limit():
    """API 호출 간 최소 간격 보장."""
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.time()


# ────────────────────────────────────────
# 데이터 캐싱 (호출 절약)
# ────────────────────────────────────────
DATA_CACHE_DIR = os.path.join(CACHE_DIR, "data")
os.makedirs(DATA_CACHE_DIR, exist_ok=True)


def _cache_key(name: str, code: str, **params) -> str:
    """캐시 키: name + code + params 해시"""
    import hashlib
    p_str = "_".join(f"{k}={v}" for k, v in sorted(params.items()))
    h = hashlib.md5(p_str.encode()).hexdigest()[:8]
    return f"{name}_{code}_{h}.json"


def cached_call(cache_name: str, code: str, ttl_seconds: int, fn, **params):
    """API 결과 캐싱.

    cache_name: "short" / "investor" / "broker" 등
    code:       종목코드
    ttl_seconds: 유효 시간 (장중 1시간, 마감 후 24시간 등)
    fn:         실제 호출 함수 (인자 없이 결과 반환)
    """
    path = os.path.join(DATA_CACHE_DIR, _cache_key(cache_name, code, **params))
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl_seconds:
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass

    result = fn()
    if result and (not isinstance(result, dict) or result.get("available", True)):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, default=str)
        except Exception:
            pass
    return result


def is_market_open() -> bool:
    """한국 시장 개장 시간 (월~금 09:00~15:30)."""
    now = datetime.now()
    if now.weekday() >= 5: return False
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    return 9 * 60 <= minutes <= 15 * 60 + 30


def smart_ttl(cache_type: str = "default") -> int:
    """상황별 TTL.

    장중: 짧게 (10분 ~ 1시간)
    장 마감 후: 길게 (24시간)
    """
    if not is_market_open():
        return 24 * 3600  # 마감 후 24시간

    ttl_map = {
        "broker":    300,    # 거래원 — 5분 (실시간 변화)
        "investor":  3600,   # 투자자 매매 — 1시간 (일별 데이터)
        "short":     3600,   # 공매도 — 1시간
        "minutes":   300,    # 분봉 — 5분
        "default":   1800,
    }
    return ttl_map.get(cache_type, 1800)
