"""카카오페이지 BFF API 클라이언트.

PoC에서 확보한 endpoint들을 한 곳에 모아 둠.

쿠키는 Cookie-Editor 등으로 export한 JSON 문자열을 그대로 받음.
필수 쿠키: _kau, _kpwtkn, _T_ANO, _karmt, _kahai, _kawlt, _kpdid 등
"""
import json
import re
import time
import urllib.parse as urlparse
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests


BFF = 'https://bff-page.kakao.com'
PAGE = 'https://page.kakao.com'

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36')


class KakaopageError(Exception):
    pass


class AuthRequiredError(KakaopageError):
    """쿠키 없음/만료 — 사용자에게 재주입 요청 신호."""


class NotPurchasedError(KakaopageError):
    pass


class KakaopageClient:

    def __init__(self, cookies_json: str, logger=None):
        """
        cookies_json: Cookie-Editor export JSON 문자열 (또는 list).
        logger: SJVA 로거 (optional). 없으면 stdlib logging fallback.
        """
        self.logger = logger
        self._parse_cookies(cookies_json)
        self._kpdid = next((c['value'] for c in self.cookies if c['name'] == '_kpdid'), None)

    # ---- 내부 ----
    def _log(self, level: str, msg: str, *args):
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        else:
            print(f'[{level.upper()}] ' + (msg % args if args else msg))

    def _parse_cookies(self, cookies_json):
        if isinstance(cookies_json, list):
            data = cookies_json
        else:
            s = (cookies_json or '').strip()
            if not s:
                raise AuthRequiredError('cookies_json 비어있음')
            data = json.loads(s)
        self.cookies = []
        for c in data:
            if not c.get('name'):
                continue
            self.cookies.append({
                'name': c['name'], 'value': c.get('value', ''),
                'domain': c.get('domain', '.kakao.com'),
                'path': c.get('path', '/'),
            })
        if not any(c['name'] == '_kau' for c in self.cookies):
            raise AuthRequiredError('필수 쿠키 _kau 없음 — 재주입 필요')

    def _session(self, referer: str = PAGE + '/') -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': DEFAULT_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ko-KR,ko;q=0.9',
            'Origin': PAGE,
            'Referer': referer,
        })
        for c in self.cookies:
            try:
                s.cookies.set(c['name'], c['value'],
                              domain=c['domain'].lstrip('.'),
                              path=c['path'])
            except Exception:
                pass
        return s

    def _json(self, r: requests.Response) -> Dict[str, Any]:
        try:
            return r.json()
        except Exception:
            raise KakaopageError(f'invalid JSON ({r.status_code}): {r.text[:200]}')

    def _check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        rc = body.get('result_code', 0)
        if rc == -100:
            raise AuthRequiredError(body.get('message') or '권한 인증 실패')
        if rc == -200 and 'not_purchased' in (body.get('message_key') or ''):
            raise NotPurchasedError(body.get('message') or '미구매')
        if rc != 0:
            raise KakaopageError(f'{body.get("message_key")}: {body.get("message")}')
        return body

    # ---- 로그인 확인 ----
    def verify(self) -> bool:
        """쿠키 유효한지 빠르게 확인 (메인 페이지 HTML에 '로그아웃' 있는지)."""
        try:
            r = self._session().get(PAGE + '/', timeout=15)
            return '로그아웃' in r.text
        except Exception:
            return False

    # ---- 검색 / 메타 ----
    def search_series(self, keyword: str, size: int = 25) -> List[Dict]:
        s = self._session()
        r = s.get(f'{BFF}/api/gateway/api/v1/search/series',
                  params={'keyword': keyword, 'category_uid': 0,
                          'is_complete': 'false', 'sort_type': 'ACCURACY',
                          'page': 0, 'size': size}, timeout=15)
        body = self._check(self._json(r))
        return body.get('result', {}).get('list', []) or []

    def find_series(self, title: str, category: str = '웹툰') -> Optional[Dict]:
        """검색 결과 중 정확히 일치하는 작품 1개 선택."""
        items = self.search_series(title)
        for it in items:
            if it.get('title') == title and (not category or it.get('category') == category):
                return it
        for it in items:
            if it.get('title') == title:
                return it
        return None

    def get_episodes_all(self, series_id: int, window_size: int = 20) -> List[Dict]:
        """회차 전체 (페이지네이션 합쳐서). last_view/purchase_info 보존."""
        all_items = []
        cursor = None
        seen_pid = set()
        for page_no in range(100):  # 최대 100페이지 안전장치
            s = self._session(referer=f'{PAGE}/content/{series_id}')
            params = {'series_id': series_id, 'cursor_direction': 'AFTER',
                      'window_size': window_size}
            if cursor is not None:
                params['cursor_index'] = cursor
            r = s.get(f'{BFF}/api/gateway/api/v2/content/product/list',
                      params=params, timeout=15)
            try:
                body = self._check(self._json(r))
            except KakaopageError as e:
                # 이미 한 페이지 이상 받았으면 부분 성공으로 진행
                if all_items:
                    self._log('warning',
                              'get_episodes_all page=%d 중단, 누적 %d개로 진행: %s',
                              page_no, len(all_items), e)
                    break
                raise
            res = body.get('result', {})
            lst = res.get('list') or []
            if not lst:
                break
            new_count = 0
            for x in lst:
                it = x['item']
                pid = it.get('product_id')
                if pid in seen_pid:
                    continue
                seen_pid.add(pid)
                all_items.append(x)
                new_count += 1
            if new_count == 0:
                break  # 진전 없음
            if not res.get('has_next'):
                break
            # 카카오는 마지막 item의 cursor_index 를 다음 호출 cursor 로 그대로 사용
            next_cursor = lst[-1].get('cursor_index')
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(0.3)  # rate limit 회피
        return all_items

    @staticmethod
    def extract_series_id(url_or_id: str) -> Optional[int]:
        """카카오페이지 작품/뷰어 URL 또는 숫자에서 series_id 추출.

        지원 형태:
          - https://page.kakao.com/content/{series_id}
          - https://page.kakao.com/content/{series_id}/viewer/{product_id}
          - https://m.page.kakao.com/...?seriesid=12345
          - kakaopage://...?series_id=12345
          - 숫자 그 자체 ('67479044')
        """
        s = (url_or_id or '').strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.search(r'/content/(\d+)', s)
        if m:
            return int(m.group(1))
        m = re.search(r'[?&](?:series_id|seriesid)=(\d+)', s, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r'(\d{6,})', s)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def episode_availability(item: Dict) -> str:
        """회차 메타에서 보유/무료/잠금 추정.

        returns: 'owned' | 'rented' | 'free' | 'locked' | 'unknown'
        """
        sp = item.get('service_property') or {}
        pi = sp.get('purchase_info') or {}
        pt = (pi.get('purchase_type') or '').lower()
        if pt == 'own':
            return 'owned'
        if pt in ('rent', 'rental'):
            return 'rented'
        # 무료 마커들
        for src in (sp, pi, item):
            for k in ('is_free', 'free', 'free_episode'):
                if src.get(k) is True:
                    return 'free'
        # 뱃지에서 무료 표기
        badges = ((item.get('badge') or {}).get('badge_list')) or []
        for b in badges:
            t = (b.get('badge_type') or '').lower()
            if 'free' in t or '무료' in (b.get('text') or ''):
                return 'free'
        if pt == 'not_purchased':
            return 'locked'
        return 'unknown'

    @staticmethod
    def episode_no_from_title(title: str) -> Optional[int]:
        """'늙은 죄수는 고독에 산다 6화' → 6. '트레일러' 같은 건 None."""
        m = re.search(r'(\d+)\s*화\b', title or '')
        return int(m.group(1)) if m else None

    @staticmethod
    def find_last_viewed(episodes: List[Dict]) -> Optional[Dict]:
        """service_property.last_view 마커가 있는 회차."""
        for x in episodes:
            sp = x['item'].get('service_property', {})
            if 'last_view' in sp:
                return x['item']
        return None

    @staticmethod
    def find_next_episode(episodes: List[Dict], after_product_id: Optional[int] = None,
                          after_ep_no: Optional[int] = None) -> Optional[Dict]:
        """주어진 회차 다음의 회차(아직 안 본/안 산) 중 가장 빠른 것."""
        norm = []
        for x in episodes:
            it = x['item']
            ep = KakaopageClient.episode_no_from_title(it.get('title', ''))
            if ep is None:
                continue
            norm.append((ep, it))
        norm.sort(key=lambda t: t[0])
        target = (after_ep_no + 1) if after_ep_no is not None else None
        for ep, it in norm:
            if target is not None and ep < target:
                continue
            sp = it.get('service_property', {})
            pi = sp.get('purchase_info', {})
            ptype = pi.get('purchase_type')
            if ptype in (None, 'not_purchased'):
                return it
        return None

    # ---- 이용권 ----
    def get_ticket_my(self, series_id: int) -> Dict:
        s = self._session()
        r = s.get(f'{BFF}/api/gateway/api/v1/ticket/my',
                  params={'series_id': series_id, 'include_waitfree': 'true'}, timeout=15)
        return self._check(self._json(r)).get('result', {})

    def ready_to_use(self, product_id: int) -> Dict:
        s = self._session()
        r = s.get(f'{BFF}/api/gateway/api/v1/ticket/ready_to_use',
                  params={'product_id': product_id, 'include_series': 'true'}, timeout=15)
        return self._check(self._json(r)).get('result', {})

    def use_ticket(self, product_id: int, ticket_type: str = 'RT05') -> Dict:
        """기다무 대여권(RT05) 사용. 응답: ticket_uid, rent_expire_dt."""
        s = self._session()
        r = s.post(f'{BFF}/api/gateway/api/v1/ticket/use',
                   data={'product_id': product_id, 'ticket_type': ticket_type},
                   headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
        return self._check(self._json(r)).get('result', {})

    def open_page(self, series_id: int, product_id: int, ticket_uid: str) -> Dict:
        s = self._session(referer=f'{PAGE}/content/{series_id}/viewer/{product_id}')
        r = s.post(f'{BFF}/api/gateway/api/v5/inven/open_page',
                   data={'seriesId': series_id, 'productId': product_id,
                         'transactionId': self._kpdid or '',
                         'ticket_uid': ticket_uid},
                   headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
        return self._check(self._json(r))

    # ---- 뷰어 ----
    def viewer_data(self, series_id: int, product_id: int) -> Dict:
        s = self._session(referer=f'{PAGE}/content/{series_id}/viewer/{product_id}')
        r = s.get(f'{BFF}/api/gateway/api/v1/viewer/data',
                  params={'series_id': series_id, 'product_id': product_id}, timeout=20)
        body = self._json(r)
        return self._check(body)

    def report_last_page(self, series_id: int, product_id: int,
                         spine_index: int = 0, is_done: bool = False) -> None:
        try:
            s = self._session(referer=f'{PAGE}/content/{series_id}/viewer/{product_id}')
            s.post(f'{BFF}/api/gateway/api/v1/viewer/last_page',
                   data={'series_id': series_id, 'product_id': product_id,
                         'rate': 0, 'spine_index': spine_index,
                         'is_done': 'true' if is_done else 'false'},
                   headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
        except Exception as e:
            self._log('warning', 'report_last_page failed: %s', e)

    # ---- 다운로드 ----
    def download_image(self, secure_url: str, save_path: str) -> int:
        """secureUrl 다운로드 → 파일 저장 → bytes 반환."""
        s = self._session()
        # 이미지 요청은 Accept를 image/*로
        s.headers.update({'Accept': 'image/avif,image/webp,*/*'})
        r = s.get(secure_url, timeout=30, stream=True)
        if r.status_code != 200:
            raise KakaopageError(f'image fetch {r.status_code} {secure_url[:120]}')
        total = 0
        with open(save_path, 'wb') as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        return total
