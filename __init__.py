import os

# 외부 의존 (SJVA 환경엔 보통 있지만 없는 경우 자동 설치)
try:
    import requests  # noqa
except Exception:
    os.system("pip install requests")

# TLS 지문 둔갑용. 설치 실패(미지원 플랫폼 등)해도 client.py 가 requests 로 폴백.
try:
    from curl_cffi import requests as _cffi  # noqa
except Exception:
    os.system("pip install curl_cffi")

try:
    import nacl  # noqa
except Exception:
    os.system("pip install pynacl")
