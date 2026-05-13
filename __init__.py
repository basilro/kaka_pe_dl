import os

# 외부 의존 (SJVA 환경엔 보통 있지만 없는 경우 자동 설치)
try:
    import requests  # noqa
except Exception:
    os.system("pip install requests")
