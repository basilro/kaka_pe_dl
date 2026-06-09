"""평문 .py → flaskfarm .pyf (Python 코드 암호화) 변환.

사용:
    # SJVA Linux 컨테이너 안에서 실행 (Python 3.10 또는 3.11)
    cd /volume1/docker/ff/plugins/toki_dl
    python3 tools/encrypt_pyf.py _novel_crypto.py
    # → _novel_crypto.pyf (같은 디렉토리)

내부적으로 flaskfarm 의 native `sc` 모듈의 `encode(text, mode=1)` 호출.
mode=1 은 `cli/code_encode.py` 가 사용하는 Python 코드 전용 암호화 모드.
mode=0 은 일반 텍스트용이며 의도된 보호 효과가 다름.

검증: 출력 첫 200자에 `import`/`def`/`class` 같은 키워드가 보이면
sc.encode 가 passthrough 한 것 → mode 가 잘못된 것으로 판단하여 에러.

평문 .py 는 손대지 않음. .pyf 만 commit, .py 는 .gitignore 로 비공개.
"""
import os
import sys


def _find_libsc() -> str:
    """flaskfarm libsc 디렉토리 자동 탐색.

    우선순위:
      1) 환경변수 FLASKFARM_LIBSC
      2) import flaskfarm 패키지 기준
      3) 플러그인 위치에서 상위로 올라가며 lib/support/libsc
      4) 흔한 고정 경로
      5) 제한적 파일시스템 검색(/data·/app·/opt·/sjva·/volume1)
    """
    import glob

    env = os.environ.get('FLASKFARM_LIBSC')
    if env and os.path.isdir(env):
        return env

    cands = []
    # (2) importable flaskfarm 패키지 기준
    try:
        import flaskfarm
        base = os.path.dirname(os.path.abspath(flaskfarm.__file__))
        cands += [os.path.join(base, 'lib', 'support', 'libsc'),
                  os.path.join(base, 'support', 'libsc')]
    except Exception:
        pass
    # (3) 플러그인 디렉토리에서 위로 올라가며 lib/support/libsc
    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = plugin_dir
    for _ in range(7):
        cands.append(os.path.join(p, 'lib', 'support', 'libsc'))
        np = os.path.dirname(p)
        if np == p:
            break
        p = np
    # (4) 흔한 고정 경로
    cands += [
        '/data/lib/support/libsc',
        '/app/flaskfarm/lib/support/libsc',
        '/opt/flaskfarm/lib/support/libsc',
        '/sjva/flaskfarm/lib/support/libsc',
        '/volume1/docker/ff/flaskfarm/lib/support/libsc',
        '/data/flaskfarm/lib/support/libsc',
    ]
    for c in cands:
        if os.path.isdir(c):
            return c
    # (5) 제한적 파일시스템 검색
    for root in ('/data', '/app', '/opt', '/sjva', '/volume1'):
        if os.path.isdir(root):
            hits = glob.glob(os.path.join(root, '**', 'support', 'libsc'),
                             recursive=True)
            if hits:
                return hits[0]
    raise RuntimeError(
        'flaskfarm libsc 디렉토리 못 찾음. '
        'FLASKFARM_LIBSC=/path/to/.../lib/support/libsc 로 지정')


def encode_file(src_path: str, mode: int = 1) -> str:
    """`.py` → `.pyf` (sc.encode mode=1 = Python 코드 암호화).

    flaskfarm 의 `cli/code_encode.py` 가 사용하는 표준 모드.
    mode=0 은 일반 텍스트용 (의도된 효과 다름).
    """
    if not os.path.isfile(src_path):
        raise FileNotFoundError(src_path)
    libsc = _find_libsc()
    sys.path.insert(0, libsc)
    import sc  # noqa
    with open(src_path, 'r', encoding='utf-8') as fp:
        src = fp.read()
    encoded = sc.encode(src, mode)
    if not encoded:
        raise RuntimeError('sc.encode() returned empty')
    out = encoded if isinstance(encoded, str) else encoded.decode('utf-8')
    # 검증: 평문 그대로면 mode 가 잘못된 것 (passthrough)
    head = out[:200]
    if 'import' in head or 'def ' in head or 'class ' in head:
        raise RuntimeError(
            f'sc.encode(mode={mode}) 가 평문 반환 — mode 가 잘못된 듯. '
            f'output head: {head[:100]!r}')
    dst_path = src_path + 'f' if src_path.endswith('.py') \
        else src_path + '.pyf'
    with open(dst_path, 'w', encoding='utf-8') as fp:
        fp.write(out)
    return dst_path


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    for src in argv[1:]:
        try:
            dst = encode_file(src)
            print(f'OK: {src} → {dst}')
        except Exception as e:
            print(f'FAIL: {src} — {type(e).__name__}: {e}')
            return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
