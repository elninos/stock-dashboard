"""공통 파일 I/O 유틸리티 — JSON 읽기/쓰기 패턴 통합."""
import json
import os
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def load_json(path: str, default=None):
    """JSON 파일 읽기. 파일 없거나 오류 시 default 반환."""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] JSON 읽기 실패 {path}: {e}")
        return default


def save_json(path: str, data, indent: int = 2):
    """JSON 파일 쓰기. 디렉토리 없으면 생성."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def now_kst() -> str:
    """현재 KST 시각을 ISO 형식으로 반환. 예: 2026-04-13T14:30:00+09:00"""
    return datetime.now(KST).isoformat(timespec="seconds")


def load_api_key(env_var: str = "ANTHROPIC_API_KEY", base_dir: str = ".") -> str:
    """환경변수 또는 .env 파일에서 API 키 로드."""
    key = os.environ.get(env_var, "")
    if not key:
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith(f"{env_var}="):
                        key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    return key
