"""discover_title_folders 디스크 스캔 로직 단위 테스트 (네트워크·DB 없이).

메타 동기화가 체크 목록이 아니라 다운로드 폴더({webtoon|novel}/작품)를 스캔해
작품 폴더를 찾는다. worker.py 는 상대 import 가 있어 standalone 로드가 안 되므로
의존 모듈을 sys.modules 스텁으로 넣고 'kp.worker' 로 로드한다.
실행: python -X utf8 test_sync_scan.py
"""
import importlib.util
import os
import sys
import tempfile
import types

_fail = 0


def check(name, got, want):
    global _fail
    ok = got == want
    if not ok:
        _fail += 1
    print(('OK  ' if ok else 'FAIL') + f' {name}: got={got!r} want={want!r}')


class _Dummy:
    def __getattr__(self, _):
        return self

    def __call__(self, *a, **k):
        return self


def _load_worker():
    here = os.path.dirname(os.path.abspath(__file__))
    pkg = types.ModuleType('kp')
    pkg.__path__ = [here]
    sys.modules['kp'] = pkg

    def stub(name, **attrs):
        m = types.ModuleType('kp.' + name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules['kp.' + name] = m

    stub('client', KakaopageClient=_Dummy, KakaopageError=Exception,
         AuthRequiredError=Exception, NotPurchasedError=Exception)
    stub('model', ModelKakaopageItem=_Dummy)
    stub('notify', send_webhook=_Dummy(), build_download_summary=_Dummy(),
         build_cookie_expired_message=_Dummy())
    stub('setup', P=_Dummy(), db=_Dummy(), logger=_Dummy())

    spec = importlib.util.spec_from_file_location(
        'kp.worker', os.path.join(here, 'worker.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules['kp.worker'] = m
    spec.loader.exec_module(m)
    return m


def main():
    W = _load_worker()
    disc = W.discover_title_folders

    root = tempfile.mkdtemp(prefix='scan_kp_')
    os.makedirs(os.path.join(root, 'webtoon', '웹툰A', '0001_1화'))
    os.makedirs(os.path.join(root, 'webtoon', '웹툰B'))
    os.makedirs(os.path.join(root, 'novel', '소설A'))
    open(os.path.join(root, 'webtoon', 'x.txt'), 'w').close()

    got = disc(root)
    check('webtoon+novel 스캔', sorted(got),
          sorted([(False, '웹툰A'), (False, '웹툰B'), (True, '소설A')]))
    check('파일은 작품으로 안 잡힘',
          any(n == 'x.txt' for _nv, n in got), False)

    # novel 폴더가 없으면 webtoon 만
    root2 = tempfile.mkdtemp(prefix='scan_kp2_')
    os.makedirs(os.path.join(root2, 'webtoon', 'only'))
    check('novel 없으면 webtoon 만', disc(root2), [(False, 'only')])

    check('없는 root → 빈 목록', disc(os.path.join(root2, 'nope')), [])

    print('\n' + ('ALL PASS' if _fail == 0 else f'{_fail} FAILED'))
    return 1 if _fail else 0


if __name__ == '__main__':
    sys.exit(main())
