"""TLS 지문 둔갑(curl_cffi) 적용 검증 — 네트워크 불필요.

client.py 의 _new_session() impersonation/폴백, 예외 튜플, 그리고
KakaopageClient._session() 의 UA·쿠키 주입을 확인한다.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(modname):
    path = os.path.join(HERE, modname + '.py')
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


client = _load('client')
import requests as _rq

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f'  OK  {name}')
    else:
        _failed += 1
        print(f'FAIL  {name}')


# 1) curl_cffi 사용 시 impersonation 세션 반환
check('curl_cffi 로드됨', client._CFFI_OK is True)
s = client._new_session()
check('_new_session() = curl_cffi impersonated',
      type(s).__module__.startswith('curl_cffi'))

# 2) 폴백: curl_cffi 비활성 시 requests.Session
_orig = client._CFFI_OK
client._CFFI_OK = False
try:
    s2 = client._new_session()
    check('폴백 → requests.Session', isinstance(s2, _rq.Session))
finally:
    client._CFFI_OK = _orig

# 3) 예외 튜플에 양쪽 백엔드 포함 (verify() 프록시/연결 처리에 사용)
check('PROXY_ERRORS ⊇ requests.ProxyError',
      _rq.exceptions.ProxyError in client._PROXY_ERRORS)
check('CONN_ERRORS ⊇ requests.ConnectionError',
      _rq.exceptions.ConnectionError in client._CONN_ERRORS)
if client._CFFI_OK:
    from curl_cffi.requests.exceptions import (
        ProxyError as _CP, ConnectionError as _CC)
    check('PROXY_ERRORS ⊇ curl_cffi.ProxyError', _CP in client._PROXY_ERRORS)
    check('CONN_ERRORS ⊇ curl_cffi.ConnectionError', _CC in client._CONN_ERRORS)

# 4) 클라이언트 세션: UA 헤더 + 쿠키 주입 (impersonation 경로)
c = client.KakaopageClient(json.dumps(
    [{'name': '_kau', 'value': 'tok', 'domain': '.kakao.com', 'path': '/'}]))
sess = c._session()
check('UA 헤더 설정', 'Chrome' in (sess.headers.get('User-Agent') or ''))
check('Origin 헤더 설정', bool(sess.headers.get('Origin')))
check('쿠키 _kau 주입', sess.cookies.get('_kau') == 'tok')

print(f'\n{_passed} passed, {_failed} failed')
sys.exit(1 if _failed else 0)
