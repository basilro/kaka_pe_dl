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
            self._log('error', 'kakao auth fail body: %s',
                      json.dumps(body, ensure_ascii=False)[:1500])
            raise AuthRequiredError(body.get('message') or '권한 인증 실패')
        if rc == -200 and 'not_purchased' in (body.get('message_key') or ''):
            raise NotPurchasedError(body.get('message') or '미구매')
        if rc != 0:
            # 실패 응답 전체를 덤프 — message_key/message 외 추가 단서가 있는 경우 확인용
            self._log('error', 'kakao api fail rc=%s body: %s',
                      rc, json.dumps(body, ensure_ascii=False)[:1500])
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

    def _fetch_product_list(self, series_id: int, cursor_index: int,
                            cursor_direction: str, window_size: int,
                            sort_type: Optional[str] = None,
                            phase: str = '?') -> Dict:
        s = self._session(referer=f'{PAGE}/')
        params = {'series_id': series_id, 'cursor_index': cursor_index,
                  'cursor_direction': cursor_direction, 'window_size': window_size}
        if sort_type:
            params['sort_type'] = sort_type
        url = f'{BFF}/api/gateway/api/v2/content/product/list'
        self._log('info', 'product/list[%s] params=%s', phase, params)
        r = s.get(url, params=params, timeout=15)
        self._log('info', 'product/list[%s] status=%d body[:200]=%s',
                  phase, r.status_code, r.text[:200])
        return self._check(self._json(r))

    def get_episodes_all(self, series_id: int, window_size: int = 25,
                         on_series_item=None) -> Dict[str, Any]:
        """회차 전체 수집.

        반환: {'series_item': {...title, ...}, 'list': [...episodes]}

        on_series_item: 첫 ANCHOR 응답에서 series_item 확보 즉시 호출되는 callback
                        (PREV/NEXT 페이징이 끝나기 전에 UI 갱신할 수 있게).

        카카오 BFF v2 패턴 (브라우저 트래픽 분석 결과):
          1) ANCHOR cursor_index=0 → last_view 주변 일부 반환
          2) PREV  (lst[0].cursor_index 기준 위쪽, 더 최신 회차)
          3) NEXT  (lst[-1].cursor_index 기준 아래쪽, 더 오래된 회차/트레일러)
        """
        all_items: List[Dict] = []
        seen_pid = set()
        series_item: Dict = {}

        def absorb(lst):
            new = 0
            for x in lst:
                it = x.get('item') or {}
                pid = it.get('product_id')
                if pid is None or pid in seen_pid:
                    continue
                seen_pid.add(pid)
                all_items.append(x)
                new += 1
            return new

        # 1) ANCHOR
        try:
            body = self._fetch_product_list(series_id, 0, 'ANCHOR', 6, phase='anchor')
        except KakaopageError:
            raise
        res = body.get('result', {})
        series_item = res.get('series_item') or {}
        lst = res.get('list') or []
        absorb(lst)
        # 첫 ANCHOR 응답 직후 즉시 콜백 (PREV/NEXT 끝나기 전에 UI 갱신용)
        if on_series_item and series_item:
            try:
                on_series_item(series_item)
            except Exception as cb_e:
                self._log('warning', 'on_series_item callback 예외: %s', cb_e)
        anchor_first = lst[0].get('cursor_index') if lst else 0
        anchor_last = lst[-1].get('cursor_index') if lst else 0
        has_prev = bool(res.get('has_prev'))
        has_next = bool(res.get('has_next'))

        # 2) PREV (최신 방향)
        cur = anchor_first
        for _ in range(50):
            if not has_prev:
                break
            try:
                body = self._fetch_product_list(series_id, cur, 'PREV', window_size,
                                                sort_type='desc', phase='prev')
            except KakaopageError as e:
                self._log('warning', 'PREV 중단 누적%d: %s', len(all_items), e)
                break
            res = body.get('result', {})
            lst = res.get('list') or []
            if not lst or absorb(lst) == 0:
                break
            has_prev = bool(res.get('has_prev'))
            new_cur = lst[0].get('cursor_index')
            if new_cur is None or new_cur >= cur:
                break
            cur = new_cur
            time.sleep(0.3)

        # 3) NEXT (오래된 방향)
        cur = anchor_last
        for _ in range(50):
            if not has_next:
                break
            try:
                body = self._fetch_product_list(series_id, cur, 'NEXT', window_size,
                                                sort_type='desc', phase='next')
            except KakaopageError as e:
                self._log('warning', 'NEXT 중단 누적%d: %s', len(all_items), e)
                break
            res = body.get('result', {})
            lst = res.get('list') or []
            if not lst or absorb(lst) == 0:
                break
            has_next = bool(res.get('has_next'))
            new_cur = lst[-1].get('cursor_index')
            if new_cur is None or new_cur <= cur:
                break
            cur = new_cur
            time.sleep(0.3)

        return {'series_item': series_item, 'list': all_items}

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
        m = re.search(r'content/(\d+)', s)   # leading slash 없는 'content/12345' 도 매칭
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

        주의: 'not_purchased' + is_free=True 동시인 케이스는 무료로 우선 판정.
        rent 의 경우 rent_expire_dt 가 과거면 'locked'.
        """
        # 무료 회차 우선 판정 (purchase_type=not_purchased 와 공존 가능)
        if item.get('is_free') is True:
            return 'free'

        sp = item.get('service_property') or {}
        pi = sp.get('purchase_info') or {}
        pt = (pi.get('purchase_type') or '').lower()

        if pt == 'own':
            return 'owned'

        if pt in ('rent', 'rental'):
            expire = pi.get('rent_expire_dt')
            if expire:
                try:
                    dt = datetime.fromisoformat(expire)
                    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                    if dt <= now:
                        return 'locked'  # 만료
                except Exception:
                    pass
            return 'rented'

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
        result = self._check(self._json(r)).get('result', {})
        # 일반 대여권 잔량 필드를 정확히 모르므로 응답 전체를 한 번 찍음 (필드 확인용)
        try:
            self._log('info', 'ticket/my result(series=%s): %s',
                      series_id, json.dumps(result, ensure_ascii=False)[:1500])
        except Exception:
            pass
        return result

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

    # ---- 소설 텍스트 ----
    @staticmethod
    def _extract_paragraphs(paragraphs) -> List[str]:
        """카카오 소설 paragraphList → 단락 string 리스트.

        구조: [{type, text, childParagraphList:[{type:'TEXT', text:'...'}]}]
        TEXT 노드의 text 만 추출 — &nbsp;, &lt; 등 HTML 엔티티는 그대로.
        """
        import html
        out: List[str] = []
        for p in paragraphs or []:
            ptype = (p.get('type') or '').upper()
            children = p.get('childParagraphList') or []
            if ptype in ('P', 'H1', 'H2', 'H3', 'DIV'):
                # 단락: 자식의 TEXT 합침
                buf = []
                for c in children:
                    if (c.get('type') or '').upper() == 'TEXT':
                        t = c.get('text') or ''
                        buf.append(html.unescape(t))
                line = ''.join(buf).strip()
                if line:
                    out.append(line)
            elif ptype == 'TEXT':
                t = (p.get('text') or '').strip()
                if t:
                    out.append(html.unescape(t))
            else:
                # IMG, BR 등은 자식 검사
                if children:
                    out.extend(KakaopageClient._extract_paragraphs(children))
        return out

    def download_novel_chapter(self, ats_server_url: str, secure_url: str) -> List[str]:
        """소설 chapter content json 다운 → 단락 리스트 반환."""
        url = ats_server_url + secure_url
        s = self._session()
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            raise KakaopageError(f'novel content fetch {r.status_code}')
        try:
            data = r.json()
        except Exception:
            raise KakaopageError(f'novel content invalid json: {r.text[:200]}')
        ci = data.get('contentInfo') or {}
        return self._extract_paragraphs(ci.get('paragraphList'))

    # ---- 다운로드 (이미지) ----
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
