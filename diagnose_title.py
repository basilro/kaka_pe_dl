"""특정 제목이 왜 다운로드되지 않는지 진단.

사용:
  python -X utf8 diagnose_title.py "백작가의 망나니가 되었다" path/to/cookies.json
  python -X utf8 diagnose_title.py 55553244            path/to/cookies.json
  python -X utf8 diagnose_title.py https://page.kakao.com/content/55553244  path/to/cookies.json

cookies.json: ModelSetting 의 cookies_json 값 (Cookie-Editor JSON export 형식)을
              그대로 저장한 파일. 또는 raw JSON 문자열을 환경변수
              KP_COOKIES_JSON 으로 넘겨도 됨.

진단 항목:
  1) BFF 검색 매칭이 되는가
  2) 쿠키 verify
  3) get_series_item (작품 메타)
  4) get_ticket_my (기다무/대여권 상태)
  5) get_episodes_all (회차 목록)
  6) 회차별 availability 카운트 (free/owned/rented/locked)
  7) 결론
"""
import json
import os
import sys

# 패키지 경로 추가
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

# kp 패키지 stub — worker.py 의존 회피 위해 client 만 직접 로드
import importlib.util
import types

pkg = types.ModuleType('kp')
pkg.__path__ = [HERE]
sys.modules['kp'] = pkg

spec = importlib.util.spec_from_file_location('kp.client', os.path.join(HERE, 'client.py'))
client_mod = importlib.util.module_from_spec(spec)
sys.modules['kp.client'] = client_mod
spec.loader.exec_module(client_mod)

KakaopageClient = client_mod.KakaopageClient


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    raw = sys.argv[1]
    cookies_json = ''
    if len(sys.argv) >= 3:
        cookies_json = open(sys.argv[2], 'r', encoding='utf-8').read()
    else:
        cookies_json = os.environ.get('KP_COOKIES_JSON', '')
    if not cookies_json.strip():
        print('[!] cookies_json 미제공 — 2번째 인자로 파일 경로 또는 KP_COOKIES_JSON 환경변수')
        sys.exit(2)

    print(f'\n=== 진단: {raw!r} ===\n')
    c = KakaopageClient(cookies_json=cookies_json)

    # 1) series_id 추출 또는 검색
    sid = KakaopageClient.extract_series_id(raw)
    if sid:
        print(f'[1] series_id 직접: {sid}')
    else:
        print(f'[1] 제목 검색 → BFF /search/series')
        items = c.search_series(raw)
        print(f'    검색 결과 {len(items)}개:')
        for it in items[:10]:
            print(f'      - {it.get("title")!r}  category={it.get("category")!r}'
                  f'  sid={it.get("series_id")}')
        s = c.find_series(raw, category=KakaopageClient.COMIC_CATEGORIES)
        if not s:
            print('[X] find_series(웹툰) 매칭 실패 — '
                  '검색에 같은 제목 웹툰이 없거나 카테고리가 웹툰/만화가 아님.')
            return
        sid = s['series_id']
        print(f'    find_series → sid={sid} title={s.get("title")!r}'
              f' category={s.get("category")!r}')

    # 2) verify
    print(f'\n[2] 쿠키 verify')
    ok = c.verify_cookies()
    print(f'    verify={ok}  last_verify_error={c.last_verify_error}')
    if not ok:
        print('[X] 쿠키 만료/문제 — Worker.run 자체가 시작 직후 abort 됨 (알림 발송).')
        print('    Cookie-Editor 로 .kakao.com 쿠키 전체를 다시 export 해 주세요.')
        return

    # 3) series 메타
    print(f'\n[3] series 메타')
    meta = c.get_series_item(sid)
    print(f'    title={meta.get("title")!r}  category={meta.get("category")!r}'
          f'  on_issue={meta.get("on_issue")!r}  is_waitfree={meta.get("is_waitfree")}'
          f'  state={meta.get("state")!r}')

    # 4) ticket
    print(f'\n[4] get_ticket_my')
    tm = c.get_ticket_my(sid)
    wf = tm.get('waitfree') or {}
    my = tm.get('my') or {}
    print(f'    waitfree: charged_complete={wf.get("charged_complete")}'
          f'  charged_at={wf.get("charged_at")}')
    print(f'    my (보유 대여권 dict): {json.dumps(my, ensure_ascii=False)[:500]}')

    # 5) episodes
    print(f'\n[5] get_episodes_all')
    data = c.get_episodes_all(sid)
    eps = data.get('list') or []
    print(f'    총 회차 {len(eps)}개')

    # 6) availability 카운트
    from collections import Counter
    cnt = Counter()
    sample = {'free': [], 'owned': [], 'rented': [], 'locked': [], 'other': []}
    for x in eps:
        it = x.get('item') or {}
        a = KakaopageClient.episode_availability(it)
        cnt[a] += 1
        bucket = sample.get(a, sample['other'])
        if len(bucket) < 3:
            bucket.append(it.get('title'))
    print(f'    availability: {dict(cnt)}')
    for k, vs in sample.items():
        if vs:
            print(f'      {k}: {vs}')

    # 7) 결론
    print('\n[7] 결론')
    free_owned = cnt.get('free', 0) + cnt.get('owned', 0) + cnt.get('rented', 0)
    locked = cnt.get('locked', 0)
    if free_owned == 0 and locked == 0:
        print('    회차 0개 — 인증 또는 시리즈 상태 이상. 위 verify/에피소드 응답 확인.')
    elif free_owned == 0 and locked > 0:
        print(f'    무료/소장/대여 회차 = 0, 잠금 회차 = {locked}.')
        print('    → 설정에서 "기다무 사용" 또는 "보유 대여권 사용" 옵션 + 실제 잔량 필요.')
        print(f'    기다무 충전됨? {bool(wf.get("charged_complete"))}')
    else:
        print(f'    무료/소장 {free_owned}개 + 잠금 {locked}개 — 정상이라면 다음 실행에 다운로드 시도됨.')
        print('    이미 모두 받았으면 DB(ModelKakaopageItem)에 completed로 표시되어 스킵.')
        print('    실제 다운 안 되는 회차가 있다면 flaskfarm 로그에서')
        print('    "[웹툰] [<raw>] series_id 직접" 또는 "검색→ series_id=..." 로그 직후')
        print('    이어지는 메시지(스킵/에러 사유)를 보면 확실히 진단됩니다.')


if __name__ == '__main__':
    main()
