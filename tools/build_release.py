"""릴리즈 빌드 — 핵심 로직 모듈을 flaskfarm .pyf 로 암호화.

SJVA(flaskfarm) 리눅스 컨테이너 안에서 실행. sc 네이티브 모듈 필요
(encrypt_pyf.py 가 lib/support/libsc 자동 탐색). 같은 tools/ 에 둔다.

사용:
    cd <플러그인 디렉토리>            # 예: /volume1/docker/ff/plugins/kaka_toon_dl
    python3 tools/build_release.py --pilot   # client.py 만 (최초 검증용)
    python3 tools/build_release.py           # 핵심 로직 전부

동작:
    1) 대상 .py → .pyf 암호화 (encrypt_pyf.encode_file, mode=1)
    2) .gitignore 에 평문 .py 비공개 패턴 보장 (이미 있으면 skip)
    3) 실행할 git 명령을 '출력' (자동 실행하지 않음 — 직접 검토 후 실행)

평문 .py 는 로컬 dev 용으로 그대로 둔다. 배포 repo 엔 .pyf 만 올라간다.
이미 .pyf 가 있으면 재실행 시 최신 소스로 다시 암호화(릴리즈마다 갱신).
"""
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)

# 암호화 대상(핵심 로직). 존재하는 것만 자동 선택.
CORE = ['client.py', 'worker.py', 'manual_worker.py', 'meta.py', 'trace.py', 'mod_basic.py']
PILOT = ['client.py']

GITIGNORE_MARK = '# === 암호화 대상 평문 .py (dev 전용, 배포는 .pyf) ==='


def _targets(pilot):
    names = PILOT if pilot else CORE
    return [n for n in names if os.path.isfile(os.path.join(ROOT, n))]


def _ensure_gitignore(names):
    gi = os.path.join(ROOT, '.gitignore')
    text = ''
    if os.path.isfile(gi):
        with open(gi, 'r', encoding='utf-8') as fp:
            text = fp.read()
    have = set(text.splitlines())
    to_add = [e for e in names if e not in have]
    if not to_add:
        print('.gitignore: 변경 없음')
        return
    if GITIGNORE_MARK in have:
        block = '\n'.join(to_add) + '\n'
    else:
        block = GITIGNORE_MARK + '\n' + '\n'.join(to_add) + '\n'
    with open(gi, 'a', encoding='utf-8') as fp:
        if text and not text.endswith('\n'):
            fp.write('\n')
        fp.write('\n' + block)
    print('.gitignore: %d개 항목 비공개 추가 (%s)' % (len(to_add), ', '.join(to_add)))


def main(argv):
    pilot = '--pilot' in argv
    targets = _targets(pilot)
    if not targets:
        print('암호화 대상 없음 (플러그인 루트에서 실행했는지 확인)')
        return 1
    import encrypt_pyf
    pyfs = []
    print('== %s 빌드: %d개 모듈 ==' % ('PILOT' if pilot else 'FULL', len(targets)))
    for n in targets:
        dst = encrypt_pyf.encode_file(os.path.join(ROOT, n))  # .py → .pyf
        pyfs.append(os.path.basename(dst))
        print('  암호화 OK: %s → %s' % (n, os.path.basename(dst)))
    _ensure_gitignore(targets)
    print('\n다음 git 명령을 검토 후 실행하세요 (배포 repo 에서 .py 제거 → .pyf 추가):')
    print('  git rm --cached ' + ' '.join(targets))
    print('  git add .gitignore ' + ' '.join(pyfs))
    print('  git commit -m "핵심 로직 .pyf 암호화 (평문 비공개)"')
    print('  git push')
    print('\n평문 .py 는 로컬에 그대로 남습니다(dev). '
          'SJVA 재시작 후 플러그인 정상 동작을 반드시 확인하세요.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
