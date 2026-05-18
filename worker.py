"""스케줄 1회 실행 단위 — 제목 리스트를 돌면서 다운로드 시도."""
import os
import re
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any
from urllib.parse import unquote as urlparse_unquote

from .client import KakaopageClient, KakaopageError, AuthRequiredError, NotPurchasedError
from .model import ModelKakaopageItem
from .notify import (send_webhook, build_download_summary,
                     build_cookie_expired_message)
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


def _xml_escape(s) -> str:
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;')
                  .replace('<', '"').replace('>', '"').strip())


_THUMB_HOST = 'https://page-images.kakaoentcdn.com/download/resource'


def _thumb_url(kid: str) -> str:
    if not kid:
        return ''
    if kid.startswith('http'):
        return kid
    return f'{_THUMB_HOST}?kid={kid}&filename=o1'


# Kavita/Komga 호환 ComicInfo XML — reading_info 의 포맷과 동일
_INFO_XML = '''<?xml version="1.0"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Summary>{desc}</Summary>
  <Writer>{author}</Writer>
  <Publisher>{publisher}</Publisher>
  <Genre>{genre}</Genre>
  <Tags>{tags}</Tags>
  <LanguageISO>ko</LanguageISO>
  <Notes>{notes}</Notes>
  <CoverArtist></CoverArtist>
  <Penciller></Penciller>
  <Inker></Inker>
  <Colorist></Colorist>
  <Letterer></Letterer>
  <Editor></Editor>
  <Characters></Characters>
  <Year>{year}</Year>
  <Month>{month}</Month>
  <Day>{day}</Day>
</ComicInfo>'''


# ---- 메타 헬퍼 (모듈 레벨 — auto/manual worker 모두에서 재사용) ----
def title_dir_for(download_root: str, title_name: str,
                  is_novel: bool = False) -> str:
    """kakaopage 다운로드 폴더 규칙: {download_root}/{webtoon|novel}/{title}/"""
    kind = 'novel' if is_novel else 'webtoon'
    return os.path.join(download_root, kind, _safe_filename(title_name))


def build_info_xml(title_name: str, series_meta: Dict[str, Any],
                   is_novel: bool = False) -> str:
    """kakaopage series_item → ComicInfo XML. 부족한 필드는 빈 값."""
    m = series_meta or {}
    title = m.get('title') or title_name or ''
    desc = m.get('description') or ''
    authors_raw = m.get('authors') or ''
    author = ', '.join(a.strip() for a in re.split(r'[,/·]', authors_raw) if a.strip())
    category = (m.get('category') or '').strip()
    sub_category = (m.get('sub_category') or '').strip()
    genres = [g for g in (category, sub_category) if g]
    tags = [t for t in ('카카오페이지', sub_category) if t]
    on_issue = (m.get('on_issue') or '').upper()
    if on_issue == 'Y':
        notes = '연재중'
    elif on_issue == 'N':
        notes = '완결'
    else:
        notes = ''

    year = month = day = ''
    sale_dt = (m.get('start_sale_dt') or '')[:10]  # 'YYYY-MM-DD...'
    mch = re.match(r'(\d{4})-(\d{2})-(\d{2})', sale_dt)
    if mch:
        year, month, day = mch.group(1), mch.group(2), mch.group(3)

    return _INFO_XML.format(
        title=_xml_escape(title),
        desc=_xml_escape(desc),
        author=_xml_escape(author),
        publisher=_xml_escape('카카오페이지'),
        genre=_xml_escape(', '.join(genres)),
        tags=_xml_escape(', '.join(tags)),
        notes=_xml_escape(notes),
        year=year, month=month, day=day,
    )


def _download_cover(client, url: str, dest_path: str) -> bool:
    """client._session() 으로 cover 받아 JPG 로 저장 (Pillow 변환)."""
    if not url or client is None:
        return False
    try:
        s = client._session()
        s.headers['Accept'] = 'image/avif,image/webp,*/*'
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            P.logger.warning('cover HTTP=%d url=%s', r.status_code, url[:120])
            return False
        data = r.content
        if data[:3] == b'\xff\xd8\xff':
            with open(dest_path, 'wb') as fp:
                fp.write(data)
            return True
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            if img.mode in ('RGBA', 'LA'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode == 'P':
                img = img.convert('RGBA')
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(dest_path, format='JPEG', quality=92, optimize=False)
            return True
        except Exception as e:
            P.logger.warning('cover JPG 변환 실패 — 원본 저장: %s', e)
            with open(dest_path, 'wb') as fp:
                fp.write(data)
            return True
    except Exception as e:
        P.logger.warning('cover 다운로드 예외: %s url=%s', e, url[:120])
        return False


def ensure_title_metadata(client, download_root: str,
                          title_name: str, series_id: int,
                          series_meta: Dict[str, Any],
                          is_novel: bool = False) -> Dict[str, Any]:
    """작품 폴더에 info.xml / cover.jpg 가 없으면 생성. 멱등."""
    result = {'info': False, 'cover': False, 'dir': ''}
    title_dir = title_dir_for(download_root, title_name, is_novel)
    result['dir'] = title_dir
    try:
        os.makedirs(title_dir, exist_ok=True)
    except Exception as e:
        P.logger.warning('[%s] 작품 폴더 생성 실패: %s', title_name, e)
        return result

    info_path = os.path.join(title_dir, 'info.xml')
    if not os.path.exists(info_path):
        try:
            xml = build_info_xml(title_name, series_meta or {}, is_novel)
            with open(info_path, 'w', encoding='utf-8') as fp:
                fp.write(xml)
            P.logger.info('[%s] info.xml 생성', title_name)
            result['info'] = True
        except Exception as e:
            P.logger.warning('[%s] info.xml 생성 실패: %s', title_name, e)

    cover_path = os.path.join(title_dir, 'cover.jpg')
    if not os.path.exists(cover_path):
        kid = (series_meta or {}).get('thumbnail') or ''
        url = _thumb_url(kid)
        if _download_cover(client, url, cover_path):
            P.logger.info('[%s] cover.jpg 생성', title_name)
            result['cover'] = True
    return result


def _extract_own_ticket_count(tm: Dict[str, Any]) -> int:
    """ticket/my 응답에서 일반(보유) 대여권 잔량 추출.

    카카오 BFF 응답 필드를 정확히 모르므로 알려진 후보 키들을 차례로 검사.
    """
    if not isinstance(tm, dict):
        return 0
    candidates: List[Any] = []

    # 1) result.my.* — 카카오 실제 필드: my.ticket_rental_count (일반 대여권)
    #    참고: my.ticket_own_count = 소장권(영구), my.cash_amount = 캐시 — 둘 다 일반 대여권 아님
    my = tm.get('my') or {}
    if isinstance(my, dict):
        for k in ('ticket_rental_count',  # 카카오 실제 필드 (확인됨)
                  'ticket_rent_count', 'rental_ticket_count', 'rent_ticket_count',
                  'rental_count'):
            v = my.get(k)
            if v is not None:
                candidates.append((f'my.{k}', v))

    # 2) result.* (top-level) 후보
    for k in ('rental_ticket_count', 'rent_ticket_count', 'own_ticket_count',
              'ticket_rent_count', 'ticket_own_count'):
        v = tm.get(k)
        if v is not None:
            candidates.append((k, v))

    # 3) tickets/rental_tickets 같은 리스트
    for outer_key in ('tickets', 'rental_tickets', 'rental', 'my_tickets',
                      'rental_ticket'):
        outer = tm.get(outer_key)
        if isinstance(outer, list):
            candidates.append((f'{outer_key}[len]', len(outer)))
        elif isinstance(outer, dict):
            for k in ('count', 'total', 'remain', 'total_count'):
                v = outer.get(k)
                if v is not None:
                    candidates.append((f'{outer_key}.{k}', v))

    # 양수 후보 우선, 없으면 0
    for src, v in candidates:
        try:
            n = int(v)
            if n > 0:
                return n
        except Exception:
            continue
    return 0


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # '2026-05-14T14:33:31+09:00' 같은 형식 — 우리는 로컬화 안 함
        return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


# ---- 자동 다운로드 진행 상태 (싱글톤) ----
_auto_state_lock = threading.Lock()
_auto_state: Dict[str, Any] = {
    'status': 'idle',          # idle | running | done | error
    'started_at': None,
    'finished_at': None,
    'message': '',
    'titles_total': 0,
    'titles_done': 0,
    'current_title': '',
    'current_phase': '',       # 'searching'|'check_ticket'|'fetch_episodes'|'downloading'
    'current_episode': '',
    'current_pages_done': 0,
    'current_pages_total': 0,
    'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
}


def get_auto_state() -> Dict[str, Any]:
    with _auto_state_lock:
        snap = dict(_auto_state)
        snap['summary'] = dict(_auto_state['summary'])
        return snap


def _auto_set(**kw):
    with _auto_state_lock:
        _auto_state.update(kw)


def _auto_reset():
    with _auto_state_lock:
        _auto_state.update({
            'status': 'idle', 'started_at': None, 'finished_at': None,
            'message': '', 'titles_total': 0, 'titles_done': 0,
            'current_title': '', 'current_phase': '',
            'current_episode': '', 'current_pages_done': 0, 'current_pages_total': 0,
            'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
        })


def _auto_summary_inc(key: str, delta: int = 1):
    with _auto_state_lock:
        _auto_state['summary'][key] = _auto_state['summary'].get(key, 0) + delta


class Worker:

    def __init__(self):
        self.cfg = P.ModelSetting.to_dict()
        self.download_root = (self.cfg.get('download_path') or '').strip()
        self.cookies_json = (self.cfg.get('cookies_json') or '').strip()
        # 입력: textarea(newline) + |  둘 다 split. 각 항목은 제목/URL/숫자/path 모두 가능.
        self.items: List[Dict[str, Any]] = []
        for raw in self._split_items(self.cfg.get('titles') or ''):
            self.items.append({'raw': raw, 'is_novel': False})
        for raw in self._split_items(self.cfg.get('titles_novel') or ''):
            self.items.append({'raw': raw, 'is_novel': True})
        self.max_per_run = int(self.cfg.get('max_per_run') or '1')
        self.use_waitfree = (self.cfg.get('use_waitfree') or 'True') == 'True'
        self.use_owned_rental = (self.cfg.get('use_owned_rental') or 'False') == 'True'
        self.notify_cookie_url = (self.cfg.get('notify_webhook_cookie') or '').strip()
        self.notify_download_url = (self.cfg.get('notify_webhook_download') or '').strip()
        self.notify_download_novel_url = (self.cfg.get('notify_webhook_download_novel') or '').strip()
        self.proxy_url = KakaopageClient.resolve_proxy(
            self.cfg.get('use_proxy'), self.cfg.get('proxy_url'))
        self.client: Optional[KakaopageClient] = None
        # 알림용 누적 — 웹툰/소설 분리
        self.completed_webtoon: List[Dict[str, Any]] = []
        self.completed_novel: List[Dict[str, Any]] = []

    @staticmethod
    def _split_items(raw: str) -> List[str]:
        out = []
        for chunk in (raw or '').replace('\r', '').replace('|', '\n').split('\n'):
            s = chunk.strip()
            if s:
                out.append(s)
        return out

    # ---- public ----
    def run(self) -> dict:
        P.logger.info('[basic] Worker.run BEGIN items=%s use_waitfree=%s use_owned_rental=%s max_per_run=%s',
                      [i['raw'] + (' (소설)' if i['is_novel'] else '') for i in self.items],
                      self.use_waitfree, self.use_owned_rental, self.max_per_run)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='시작', titles_total=len(self.items))
        if not self.download_root:
            P.logger.error('download_path 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            P.logger.error('cookies_json 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.items:
            P.logger.error('체크할 작품 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = KakaopageClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            P.logger.error('쿠키 인증 실패: %s', e)
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        if not self.client.verify():
            P.logger.error('쿠키 만료 — 재주입 필요')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='쿠키 만료 — 재주입 필요')
            # 만료 알림 — 1회만 발송 (스팸 방지)
            try:
                already = (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True'
                if not already and self.notify_cookie_url:
                    if send_webhook(self.notify_cookie_url,
                                    build_cookie_expired_message()):
                        P.ModelSetting.set('cookie_expired_notified', 'True')
            except Exception as e:
                P.logger.warning('쿠키 만료 알림 발송 실패: %s', e)
            return {'ret': 'fail', 'reason': 'cookie_expired'}

        # 정상 verify → 만료 플래그 리셋 (다음 만료 때 다시 1회 알림 가능)
        try:
            if (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True':
                P.ModelSetting.set('cookie_expired_notified', 'False')
        except Exception:
            pass

        summary = {'titles': len(self.items), 'downloaded': 0, 'skipped': 0, 'failed': 0}
        for item in self.items:
            _auto_set(current_title=item['raw'] + (' [소설]' if item['is_novel'] else ''),
                      current_phase='searching',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_item(item)
                if got == 'downloaded':
                    summary['downloaded'] += 1
                    _auto_summary_inc('downloaded')
                elif got == 'skipped':
                    summary['skipped'] += 1
                    _auto_summary_inc('skipped')
                else:
                    summary['failed'] += 1
                    _auto_summary_inc('failed')
            except Exception as e:
                import traceback
                P.logger.error('process item %r exception: %s', item, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
                _auto_summary_inc('failed')
            _auto_set(titles_done=summary['downloaded'] + summary['skipped'] + summary['failed'])

        # ---- 다운로드 완료 요약 알림 (웹툰/소설 분리, 받은 게 있을 때만) ----
        if self.completed_webtoon and self.notify_download_url:
            try:
                msg = build_download_summary(self.completed_webtoon, is_novel=False)
                if msg:
                    ok = send_webhook(self.notify_download_url, msg)
                    P.logger.info('웹툰 다운로드 요약 알림 발송: %s (%d건)',
                                  'OK' if ok else 'FAIL', len(self.completed_webtoon))
            except Exception as e:
                P.logger.warning('웹툰 다운로드 요약 알림 예외: %s', e)
        if self.completed_novel and self.notify_download_novel_url:
            try:
                msg = build_download_summary(self.completed_novel, is_novel=True)
                if msg:
                    ok = send_webhook(self.notify_download_novel_url, msg)
                    P.logger.info('소설 다운로드 요약 알림 발송: %s (%d건)',
                                  'OK' if ok else 'FAIL', len(self.completed_novel))
            except Exception as e:
                P.logger.warning('소설 다운로드 요약 알림 예외: %s', e)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"완료 — 다운 {summary['downloaded']}, 스킵 {summary['skipped']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- per item (제목/URL/숫자 어느 형태든 처리) ----
    def _process_item(self, item: Dict[str, Any]) -> str:
        raw = item['raw']
        is_novel = item['is_novel']
        kind_label = '소설' if is_novel else '웹툰'

        # 1) URL/숫자/path → series_id 직접
        sid = KakaopageClient.extract_series_id(raw)
        if sid:
            series_id = sid
            display_title = raw  # 임시 — _process_series에서 실제 제목으로 덮어씀
            P.logger.info('[%s] [%s] series_id 직접: %s', kind_label, raw, series_id)
        else:
            # 2) 제목 → 검색
            category = '웹소설' if is_novel else '웹툰'
            series = self.client.find_series(raw, category=category)
            if not series:
                series = self.client.find_series(raw, category='')
            if not series:
                P.logger.warning('[%s] [%s] 검색 결과 매칭 실패', kind_label, raw)
                return 'failed'
            series_id = series['series_id']
            display_title = series.get('title') or raw
            P.logger.info('[%s] [%s] 검색→ series_id=%s title=%r',
                          kind_label, raw, series_id, display_title)

        return self._process_series(display_title, series_id, is_novel)

    def _process_series(self, title: str, series_id: int, is_novel: bool) -> str:

        # 회차 목록 — 첫 ANCHOR 응답 직후 즉시 series 제목으로 화면 갱신 (PREV/NEXT 페이징은 길 수 있음)
        _auto_set(current_phase='fetch_episodes')

        title_holder = {'t': title}

        def _on_series(meta):
            nm = (meta or {}).get('title')
            if nm and nm != title_holder['t']:
                P.logger.info('[%s] series 제목 확보 → %r (즉시 갱신)', title_holder['t'], nm)
                title_holder['t'] = nm
                _auto_set(current_title=nm + (' [소설]' if is_novel else ''))

        data = self.client.get_episodes_all(series_id, on_series_item=_on_series)
        eps = (data.get('list') if isinstance(data, dict) else data) or []
        if not eps:
            P.logger.warning('[%s] 회차 목록 비어있음', title_holder['t'])
            return 'failed'

        # 폴백: callback 못 탔거나 series_item 비어있을 때 episode item 안에서도 찾아봄
        if title_holder['t'] == title:
            series_meta = data.get('series_item') if isinstance(data, dict) else None
            meta_title = (series_meta or {}).get('title')
            if not meta_title:
                # episode item에 series_title 들어있는 경우도 있음
                first_it = eps[0].get('item') if eps else None
                if first_it:
                    meta_title = first_it.get('series_title') or first_it.get('seriesTitle')
            if meta_title and meta_title != title_holder['t']:
                P.logger.info('[%s] series 제목 폴백 → %r', title_holder['t'], meta_title)
                title_holder['t'] = meta_title
                _auto_set(current_title=meta_title + (' [소설]' if is_novel else ''))

        title = title_holder['t']

        # info.xml / cover.jpg — 작품 폴더에 없으면 자동 생성 (다운로드 여부 무관)
        series_meta = data.get('series_item') if isinstance(data, dict) else None
        self._ensure_title_metadata(title, series_id, series_meta or {}, is_novel)

        # 분류: 받지 않은 회차들 → free/owned/rented(직접) vs locked(기다무 ticket 필요)
        free_owned: List = []   # (item, availability)
        locked: List = []
        for x in eps:
            it = x['item']
            pid = it.get('product_id')
            if not pid:
                continue
            rec = db.session.query(ModelKakaopageItem).filter_by(product_id=pid).first()
            if rec and rec.status == 'completed':
                continue
            avail = KakaopageClient.episode_availability(it)
            if avail in ('free', 'owned', 'rented'):
                free_owned.append((it, avail))
            elif avail == 'locked':
                locked.append((it, avail))
        ep_key = lambda t: KakaopageClient.episode_no_from_title(t[0].get('title', '')) or 0
        free_owned.sort(key=ep_key)
        locked.sort(key=ep_key)
        P.logger.info('[%s] 미수신 — 무료/보유 %d개, 잠금 %d개',
                      title, len(free_owned), len(locked))

        downloaded_count = 0
        _auto_set(current_phase='downloading')

        # 1) 무료/보유 직접 다운 (제한 없음)
        for it, avail in free_owned:
            _auto_set(current_episode=it.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            result = self._download_one(title, series_id, it, avail, is_novel=is_novel)
            if result == 'downloaded':
                downloaded_count += 1

        # 2) 잠금 회차 — 기다무/일반 대여권 옵션에 따라 처리
        #    기다무: 보통 1장씩 충전되므로 max_per_run 한도 적용
        #    일반(보유) 대여권: 잔량(ticket_own_count)까지 모두 사용
        #    두 옵션 모두 Off면 잠금 회차 자체를 스킵
        if locked and not (self.use_waitfree or self.use_owned_rental):
            P.logger.info('[%s] 대여권 사용 모두 Off — 잠금 회차 %d개 스킵 (무료/소장만 다운)',
                          title, len(locked))
        elif locked:
            _auto_set(current_phase='check_ticket')
            tm = self.client.get_ticket_my(series_id)
            wf = tm.get('waitfree') or {}
            my = tm.get('my') or {}

            waitfree_used = 0
            own_used = 0
            for it, _ in locked:
                wf_ready = bool(wf.get('charged_complete'))
                own_left = _extract_own_ticket_count(tm)

                can_use_wf = self.use_waitfree and wf_ready and (waitfree_used < self.max_per_run)
                can_use_own = self.use_owned_rental and (own_left > 0)

                if not (can_use_wf or can_use_own):
                    # 사유 분기 로깅
                    reasons = []
                    if self.use_waitfree:
                        if not wf_ready:
                            reasons.append(f'기다무 미충전(예정 {wf.get("charged_at")})')
                        elif waitfree_used >= self.max_per_run:
                            reasons.append(f'기다무 max_per_run({self.max_per_run}) 도달')
                    else:
                        reasons.append('기다무 사용 Off')
                    if self.use_owned_rental:
                        if own_left <= 0:
                            reasons.append('일반 잔량 0')
                    else:
                        reasons.append('일반 사용 Off')
                    P.logger.info('[%s] 사용 가능 이용권 없음 — 종료 (%s)',
                                  title, ' / '.join(reasons))
                    break

                _auto_set(current_phase='downloading',
                          current_episode=it.get('title', ''),
                          current_pages_done=0, current_pages_total=0)
                result = self._download_one(title, series_id, it, 'locked',
                                            wf_charged=wf_ready, is_novel=is_novel)
                if result != 'downloaded':
                    P.logger.info('[%s] 잠금 회차 다운 실패/스킵 — 종료', title)
                    break

                downloaded_count += 1
                # ticket 상태 갱신 + 어느 쪽 ticket이 줄었는지 추적
                _auto_set(current_phase='check_ticket')
                tm2 = self.client.get_ticket_my(series_id)
                wf2 = tm2.get('waitfree') or {}
                my2 = tm2.get('my') or {}
                new_wf_ready = bool(wf2.get('charged_complete'))
                new_own = _extract_own_ticket_count(tm2)

                if wf_ready and not new_wf_ready:
                    waitfree_used += 1
                    P.logger.info('[%s] 기다무 1장 사용 (누적 %d/%d, 일반 잔량 %d)',
                                  title, waitfree_used, self.max_per_run, new_own)
                elif new_own < own_left:
                    own_used += 1
                    P.logger.info('[%s] 일반 대여권 1장 사용 (누적 %d, 일반 잔량 %d, 기다무 충전 %s)',
                                  title, own_used, new_own, new_wf_ready)
                else:
                    # 잔량 변화 못 잡힘 — 기다무 우선 추정
                    if wf_ready:
                        waitfree_used += 1
                    else:
                        own_used += 1
                    P.logger.info('[%s] ticket 사용 (추정) — 기다무 누적 %d, 일반 누적 %d',
                                  title, waitfree_used, own_used)

                wf = wf2
                my = my2

        return 'downloaded' if downloaded_count else 'skipped'

    # ---- 작품 폴더 메타 (info.xml / cover.jpg) wrapper ----
    def _ensure_title_metadata(self, title_name: str, series_id: int,
                               series_meta: Dict[str, Any],
                               is_novel: bool = False) -> Dict[str, Any]:
        return ensure_title_metadata(self.client, self.download_root,
                                     title_name, series_id, series_meta,
                                     is_novel=is_novel)

    # ---- 전 작품 메타 일괄 동기화 (UI 버튼) ----
    def sync_metadata_all(self) -> dict:
        """titles + titles_novel 의 모든 작품에 대해 info.xml/cover.jpg 누락분 생성.

        다운로드 폴더에 작품 폴더가 이미 있는 항목만 처리 — 없으면 만들지 않고 스킵.
        """
        P.logger.info('[basic] sync_metadata_all BEGIN items=%d', len(self.items))
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='메타 동기화 시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = KakaopageClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        summary = {'titles': len(self.items), 'info': 0, 'cover': 0,
                   'skipped_no_folder': 0, 'failed': 0}
        for item in self.items:
            raw = item['raw']
            is_novel = item['is_novel']
            kind_label = '소설' if is_novel else '웹툰'
            _auto_set(current_title=f'[{kind_label}] {raw}',
                      current_phase='sync_metadata',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                sid = KakaopageClient.extract_series_id(raw)
                if sid is None:
                    category = '웹소설' if is_novel else '웹툰'
                    series = self.client.find_series(raw, category=category)
                    if not series:
                        series = self.client.find_series(raw, category='')
                    if not series:
                        summary['failed'] += 1
                        continue
                    sid = series['series_id']
                    title_guess = series.get('title') or raw
                else:
                    title_guess = raw

                # 폴더 존재 여부 먼저 확인 — 없으면 메타 API 호출 없이 스킵
                folder = title_dir_for(self.download_root, title_guess, is_novel)
                series_meta = {}
                if not os.path.isdir(folder):
                    # 검색 결과 title로도 한 번 더 점검 (입력이 ID인 경우)
                    series_meta = self.client.get_series_item(sid)
                    new_title = (series_meta.get('title') or '').strip()
                    if new_title:
                        alt_folder = title_dir_for(self.download_root,
                                                   new_title, is_novel)
                        if os.path.isdir(alt_folder):
                            folder = alt_folder
                            title_guess = new_title
                        else:
                            summary['skipped_no_folder'] += 1
                            continue
                    else:
                        summary['skipped_no_folder'] += 1
                        continue

                _auto_set(current_title=f'[{kind_label}] {title_guess}')

                # 둘 다 이미 있으면 API 호출 안 함
                info_p = os.path.join(folder, 'info.xml')
                cover_p = os.path.join(folder, 'cover.jpg')
                if os.path.isfile(info_p) and os.path.isfile(cover_p):
                    continue

                if not series_meta:
                    series_meta = self.client.get_series_item(sid) or {}

                r = self._ensure_title_metadata(title_guess, sid, series_meta,
                                                is_novel=is_novel)
                if r.get('info'):
                    summary['info'] += 1
                if r.get('cover'):
                    summary['cover'] += 1
            except Exception as e:
                import traceback
                P.logger.error('[sync_metadata] %r 예외: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
            _auto_set(titles_done=(summary['info'] + summary['cover']
                                   + summary['skipped_no_folder']
                                   + summary['failed']))

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"메타 동기화 완료 — info {summary['info']}, "
                           f"cover {summary['cover']}, "
                           f"폴더없음 {summary['skipped_no_folder']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- one episode ----
    def _download_one(self, series_title: str, series_id: int, ep_item: dict,
                      availability: str = 'locked', wf_charged: Optional[bool] = None,
                      is_novel: bool = False) -> str:
        """availability='locked' 면 ticket 차감 후 다운, 그 외(free/owned/rented)는
        viewer/data 직접 호출 (ticket 단계 건너뜀).

        wf_charged: 외부 루프에서 알고 있는 기다무 충전 여부.
          True  → RT05 우선, 실패 시 추천 type fallback
          False → 추천 type 우선 (RT05 건너뜀), 실패 시 RT05도 시도
          None  → 보수적으로 RT05 → 추천 type 순서
        """
        ep_no = KakaopageClient.episode_no_from_title(ep_item.get('title', '')) or 0
        product_id = ep_item['product_id']
        episode_title = ep_item.get('title', '')

        # DB 레코드 확보
        rec = db.session.query(ModelKakaopageItem).filter_by(product_id=product_id).first()
        if rec and rec.status == 'completed':
            P.logger.info('[%s] %s 이미 다운로드 완료 — 스킵', series_title, episode_title)
            return 'skipped'
        if rec is None:
            rec = ModelKakaopageItem()
            rec.series_id = series_id
            rec.series_title = series_title
            rec.product_id = product_id
            rec.episode_no = ep_no
            rec.episode_title = episode_title
            db.session.add(rec)
            db.session.commit()

        rec.updated_time = datetime.now()

        # ---- ticket 단계 (잠금 회차만) ----
        if availability == 'locked':
            rec.status = 'using_ticket'; db.session.commit()
            try:
                ready = self.client.ready_to_use(product_id)
            except KakaopageError as e:
                rec.status = 'failed'; rec.error_msg = f'ready_to_use: {e}'
                db.session.commit(); return 'failed'
            available = ready.get('available') or {}
            rec_type = available.get('ticket_rental_type')
            # ready_to_use 응답 전체를 한 번 로깅 (어떤 후보 ticket이 있는지 확인용)
            try:
                import json as _j
                P.logger.info('[%s] %s ready_to_use=%s',
                              series_title, episode_title,
                              _j.dumps(ready, ensure_ascii=False)[:800])
            except Exception:
                P.logger.info('[%s] %s ready_to_use available=%s',
                              series_title, episode_title, available)

            # ticket_type 우선순위 결정 (use_waitfree / use_owned_rental 옵션 기반)
            tries: List[str] = []
            # 1. 기다무 사용 가능 + 충전됨 → RT05 우선
            if self.use_waitfree and wf_charged is not False:
                tries.append('RT05')
            # 2. 일반 대여권 사용 가능 → 카카오 추천 type
            if self.use_owned_rental and rec_type and rec_type not in tries:
                tries.append(rec_type)
            # 3. 보완: use_waitfree=True 인데 wf 미충전 등으로 위에서 RT05 빠진 경우
            #    fallback으로라도 한 번 시도 (use_owned_rental이 다 실패했을 때 마지막 보루)
            if self.use_waitfree and 'RT05' not in tries:
                tries.append('RT05')
            if not tries:
                # 두 옵션 모두 Off — 호출자가 미리 걸러야 함 (방어적)
                P.logger.info('[%s] %s 대여권 사용 옵션 모두 Off — 스킵',
                              series_title, episode_title)
                rec.status = 'skipped_no_ticket'; db.session.commit()
                return 'skipped'
            P.logger.info('[%s] %s ticket 시도 순서: %s (use_wf=%s, use_own=%s, wf_charged=%s)',
                          series_title, episode_title, tries,
                          self.use_waitfree, self.use_owned_rental, wf_charged)

            used = None
            last_err = None
            tried_types = []
            for tt in tries:
                tried_types.append(tt)
                try:
                    used = self.client.use_ticket(product_id, ticket_type=tt)
                    P.logger.info('[%s] %s use_ticket(%s) 성공',
                                  series_title, episode_title, tt)
                    break
                except KakaopageError as e:
                    last_err = e
                    P.logger.info('[%s] %s use_ticket(%s) 실패: %s',
                                  series_title, episode_title, tt, e)

            if used is None:
                # 사용 가능 ticket 없음 — 스킵 (use_waitfree_only면 의도대로 기다무 미충전)
                rec.status = 'skipped_no_ticket'
                rec.error_msg = f'use_ticket {tried_types} 모두 실패: {last_err}'
                db.session.commit()
                P.logger.info('[%s] %s ticket 사용 불가 — 스킵 (tried=%s)',
                              series_title, episode_title, tried_types)
                return 'skipped'

            # 마지막으로 성공한 type (tries 순서 그대로 시도하므로 break 시점의 마지막 element)
            ticket_used_type = tried_types[-1]
            rec.ticket_uid = used.get('ticket_uid')
            rec.rent_expire_dt = _parse_dt(used.get('rent_expire_dt'))
            db.session.commit()
            P.logger.info('[%s] %s ticket 차감 OK (type=%s uid=%s expire=%s)',
                          series_title, episode_title, ticket_used_type,
                          rec.ticket_uid, rec.rent_expire_dt)
            try:
                self.client.open_page(series_id, product_id, rec.ticket_uid or '')
            except KakaopageError as e:
                P.logger.warning('open_page 실패 (계속): %s', e)
        else:
            P.logger.info('[%s] %s 직접 다운 (availability=%s)',
                          series_title, episode_title, availability)

        # ---- viewer/data ----
        try:
            vd = self.client.viewer_data(series_id, product_id)
        except KakaopageError as e:
            rec.status = 'failed'; rec.error_msg = f'viewer_data: {e}'
            db.session.commit(); return 'failed'
        viewer_data = vd.get('viewerData') or {}
        viewer_type = viewer_data.get('type') or ''

        # 저장 경로 — download_root/<webtoon|novel>/<작품>/...
        s_folder = _safe_filename(series_title)
        kind_dir = 'novel' if viewer_type == 'TextViewerData' else 'webtoon'
        series_dir = os.path.join(self.download_root, kind_dir, s_folder)

        # === 소설 (TextViewerData) — 회차 폴더 없이 NNNN_제목.txt ===
        if viewer_type == 'TextViewerData':
            os.makedirs(series_dir, exist_ok=True)
            rec.save_dir = series_dir
            rec.status = 'downloading'
            db.session.commit()

            ats = viewer_data.get('atsServerUrl') or ''
            contents = viewer_data.get('contentsList') or []
            if not contents:
                rec.status = 'failed'; rec.error_msg = 'no contentsList'
                db.session.commit(); return 'failed'
            rec.page_count = len(contents)
            _auto_set(current_pages_total=len(contents), current_pages_done=0)

            paragraphs: List[str] = []
            done = 0
            for c in contents:
                secure = c.get('secureUrl')
                if not secure:
                    continue
                try:
                    ps = self.client.download_novel_chapter(ats, secure)
                    paragraphs.extend(ps)
                    done += 1
                    _auto_set(current_pages_done=done)
                except Exception as e:
                    P.logger.warning('[%s] %s content %s 다운 실패: %s',
                                     series_title, episode_title, c.get('chapterId'), e)
            if not paragraphs:
                rec.status = 'failed'; rec.error_msg = 'no text extracted'
                db.session.commit(); return 'failed'

            fname = f'{ep_no:04d}_{_safe_filename(episode_title)}.txt'
            save_path = os.path.join(series_dir, fname)
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(paragraphs))
            total_bytes = os.path.getsize(save_path)
            downloaded = done
            files_count = len(contents)
            failed = []
        # === 웹툰 (이미지) — 회차폴더 안에 페이지별 이미지 ===
        else:
            e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
            save_dir = os.path.join(series_dir, e_folder)
            os.makedirs(save_dir, exist_ok=True)
            rec.save_dir = save_dir
            rec.status = 'downloading'
            db.session.commit()

            files = (viewer_data.get('imageDownloadData') or {}).get('files') or []
            if not files:
                rec.status = 'failed'; rec.error_msg = f'no image files in viewer_data (type={viewer_type})'
                db.session.commit(); return 'failed'
            rec.page_count = len(files)
            _auto_set(current_pages_total=len(files), current_pages_done=0)

            downloaded = 0; total_bytes = 0
            failed = []
            for f in files:
                no = f.get('no')
                url = f.get('secureUrl')
                ext = '.jpg'
                try:
                    fname_param = re.search(r'filename=([^&]+)', url).group(1)
                    fname_param = urlparse_unquote(fname_param)
                    if '.' in fname_param:
                        ext = '.' + fname_param.rsplit('.', 1)[-1]
                except Exception:
                    pass
                local = os.path.join(save_dir, f'{no:03d}{ext}')
                try:
                    got = self.client.download_image(url, local)
                    downloaded += 1; total_bytes += got
                    _auto_set(current_pages_done=downloaded)
                except Exception as e:
                    failed.append((no, str(e)))
                    P.logger.warning('[%s] %s page %s 다운 실패: %s',
                                     series_title, episode_title, no, e)
            files_count = len(files)
        rec.downloaded_count = downloaded
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at

        if downloaded == files_count:
            rec.status = 'completed'
            P.logger.info('[%s] %s 다운로드 완료 (%d개, %.1fKB)',
                        series_title, episode_title, downloaded, total_bytes / 1024)
            # 알림 누적 — viewer_type 으로 정확히 분기 (사용자 분류와 무관)
            entry = {
                'series_title': series_title,
                'episode_title': episode_title,
                'episode_no': ep_no,
            }
            if viewer_type == 'TextViewerData':
                self.completed_novel.append(entry)
            else:
                self.completed_webtoon.append(entry)
        else:
            rec.status = 'partial'
            rec.error_msg = f'failed {len(failed)}/{files_count}'
            P.logger.warning('[%s] %s 일부 실패 (%d/%d)',
                           series_title, episode_title, downloaded, files_count)
        db.session.commit()

        # 7) 진행 보고
        self.client.report_last_page(series_id, product_id, is_done=(rec.status == 'completed'))
        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'
