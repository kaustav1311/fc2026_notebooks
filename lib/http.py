from __future__ import annotations
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=1.5,            # waits 1.5, 3, 6, 12, 24, 48 s on retries
        backoff_jitter=0.5,            # avoid synchronized retries against the same host
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": UA, "Accept": "application/json, */*"})
    return s


session = build_session()


def polite_get(url: str, sleep: float = 0.0, **kwargs) -> requests.Response:
    r = session.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    if sleep:
        time.sleep(sleep)
    return r
