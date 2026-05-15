"""스케줄 1회 실행 단위 — 제목 리스트를 돌면서 다운로드 시도."""
import os
import re
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any
from urllib.parse import unquote as urlparse_unquote

from .client import KakaopageClient, KakaopageError, AuthRequiredError, NotPurchasedError
from .model import ModelKakaopageItem
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


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
        titles_raw = (self.cfg.get('titles') or '').strip()
        self.titles = [t.strip() for t in titles_raw.split('|') if t.strip()]
        self.max_per_run = int(self.cfg.get('max_per_run') or '1')
        self.use_waitfree_only = (self.cfg.get('use_waitfree_only') or 'True') == 'True'
        self.client: Optional[KakaopageClient] = None

    # ---- public ----
    def run(self) -> dict:
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='시작', titles_total=len(self.titles))
        if not self.download_root:
            logger.error('download_path 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            logger.error('cookies_json 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.titles:
            logger.error('titles 미설정')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='titles 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = KakaopageClient(self.cookies_json, logger=P.logger)
        except AuthRequiredError as e:
            logger.error('쿠키 인증 실패: %s', e)
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        if not self.client.verify():
            logger.error('쿠키 만료 — 재주입 필요')
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='쿠키 만료 — 재주입 필요')
            return {'ret': 'fail', 'reason': 'cookie_expired'}

        summary = {'titles': len(self.titles), 'downloaded': 0, 'skipped': 0, 'failed': 0}
        for title in self.titles:
            _auto_set(current_title=title, current_phase='searching',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_title(title)
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
                logger.error('process title %r exception: %s', title, e)
                logger.error(traceback.format_exc())
                summary['failed'] += 1
                _auto_summary_inc('failed')
            _auto_set(titles_done=summary['downloaded'] + summary['skipped'] + summary['failed'])

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"완료 — 다운 {summary['downloaded']}, 스킵 {summary['skipped']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- per title ----
    def _process_title(self, title: str) -> str:
        logger.info('[%s] 처리 시작', title)
        series = self.client.find_series(title, category='웹툰')
        if not series:
            series = self.client.find_series(title, category='')
        if not series:
            logger.warning('[%s] 검색 결과에서 매칭 실패', title)
            return 'failed'

        series_id = series['series_id']
        logger.info('[%s] series_id=%s', title, series_id)

        # 이용권 보유 + 기다무 충전 상태
        _auto_set(current_phase='check_ticket')
        tm = self.client.get_ticket_my(series_id)
        wf = tm.get('waitfree') or {}
        my = tm.get('my') or {}
        if self.use_waitfree_only:
            if not wf.get('charged_complete'):
                logger.info('[%s] 기다무 미충전 — 스킵 (충전 예정: %s)',
                            title, wf.get('charged_at'))
                return 'skipped'
        else:
            if not (wf.get('charged_complete') or my.get('ticket_own_count', 0) > 0):
                logger.info('[%s] 사용 가능한 이용권 없음 — 스킵', title)
                return 'skipped'

        # 회차 목록 + 마지막 본 회차
        _auto_set(current_phase='fetch_episodes')
        data = self.client.get_episodes_all(series_id)
        eps = (data.get('list') if isinstance(data, dict) else data) or []
        if not eps:
            logger.warning('[%s] 회차 목록 비어있음', title)
            return 'failed'

        last_viewed = self.client.find_last_viewed(eps)
        last_ep_no = self.client.episode_no_from_title(last_viewed['title']) if last_viewed else 0
        logger.info('[%s] 마지막 본 회차: %s화', title, last_ep_no)

        next_ep = self.client.find_next_episode(eps, after_ep_no=last_ep_no)
        if not next_ep:
            logger.info('[%s] 다음 화 없음 (최신화 도달 or 모두 구매됨)', title)
            return 'skipped'

        _auto_set(current_phase='downloading')
        downloaded = 0
        for _ in range(self.max_per_run):
            ep_no = self.client.episode_no_from_title(next_ep['title'])
            _auto_set(current_episode=next_ep.get('title', ''),
                      current_pages_done=0, current_pages_total=0)
            result = self._download_one(title, series_id, next_ep)
            if result == 'downloaded':
                downloaded += 1
                # 다음 회차로 진행 (기다무는 보통 한 번에 1장만 충전돼서 break)
                tm2 = self.client.get_ticket_my(series_id)
                wf2 = tm2.get('waitfree') or {}
                if self.use_waitfree_only and not wf2.get('charged_complete'):
                    logger.info('[%s] 기다무 소진 — 다음 실행 대기', title)
                    break
                data2 = self.client.get_episodes_all(series_id)
                eps2 = (data2.get('list') if isinstance(data2, dict) else data2) or []
                next_ep = self.client.find_next_episode(eps2, after_ep_no=ep_no)
                if not next_ep:
                    break
            else:
                break
        return 'downloaded' if downloaded else 'failed'

    # ---- one episode ----
    def _download_one(self, series_title: str, series_id: int, ep_item: dict) -> str:
        ep_no = KakaopageClient.episode_no_from_title(ep_item.get('title', '')) or 0
        product_id = ep_item['product_id']
        episode_title = ep_item.get('title', '')

        # DB 레코드 확보 (product_id 유일 인덱스)
        rec = db.session.query(ModelKakaopageItem).filter_by(product_id=product_id).first()
        if rec and rec.status == 'completed':
            logger.info('[%s] %s 이미 다운로드 완료 — 스킵', series_title, episode_title)
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

        rec.status = 'using_ticket'
        rec.updated_time = datetime.now()
        db.session.commit()

        # 1) 사용 가능 여부 확인
        try:
            ready = self.client.ready_to_use(product_id)
        except KakaopageError as e:
            rec.status = 'failed'; rec.error_msg = f'ready_to_use: {e}'
            db.session.commit(); return 'failed'
        ticket_rental_type = (ready.get('available') or {}).get('ticket_rental_type')
        if not ticket_rental_type:
            logger.info('[%s] %s 무료/유료 외 케이스 — 스킵', series_title, episode_title)
            rec.status = 'skipped_no_ticket'; db.session.commit(); return 'skipped'

        # 2) 차감
        try:
            used = self.client.use_ticket(product_id, ticket_type=ticket_rental_type)
        except KakaopageError as e:
            rec.status = 'failed'; rec.error_msg = f'use_ticket: {e}'
            db.session.commit(); return 'failed'
        rec.ticket_uid = used.get('ticket_uid')
        rec.rent_expire_dt = _parse_dt(used.get('rent_expire_dt'))
        db.session.commit()
        logger.info('[%s] %s 차감 OK (ticket_uid=%s, expire=%s)',
                    series_title, episode_title, rec.ticket_uid, rec.rent_expire_dt)

        # 3) 열람 등록
        try:
            self.client.open_page(series_id, product_id, rec.ticket_uid or '')
        except KakaopageError as e:
            logger.warning('open_page 실패 (계속 진행): %s', e)

        # 4) viewer/data
        try:
            vd = self.client.viewer_data(series_id, product_id)
        except KakaopageError as e:
            rec.status = 'failed'; rec.error_msg = f'viewer_data: {e}'
            db.session.commit(); return 'failed'
        files = ((vd.get('viewerData') or {}).get('imageDownloadData') or {}).get('files') or []
        if not files:
            rec.status = 'failed'; rec.error_msg = 'no files in viewer_data'
            db.session.commit(); return 'failed'
        rec.page_count = len(files)
        _auto_set(current_pages_total=len(files), current_pages_done=0)

        # 5) 저장 폴더
        s_folder = _safe_filename(series_title)
        e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
        save_dir = os.path.join(self.download_root, s_folder, e_folder)
        os.makedirs(save_dir, exist_ok=True)
        rec.save_dir = save_dir
        rec.status = 'downloading'
        db.session.commit()

        # 6) 이미지 다운로드
        downloaded = 0; total_bytes = 0
        failed = []
        for f in files:
            no = f.get('no')
            url = f.get('secureUrl')
            ext = '.jpg'
            # 파일명에서 확장자 추출 시도
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
                logger.warning('[%s] %s page %s 다운 실패: %s', series_title, episode_title, no, e)
        rec.downloaded_count = downloaded
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at

        if downloaded == len(files):
            rec.status = 'completed'
            logger.info('[%s] %s 다운로드 완료 (%d장, %.1fMB)',
                        series_title, episode_title, downloaded, total_bytes/1024/1024)
        else:
            rec.status = 'partial'
            rec.error_msg = f'failed {len(failed)}/{len(files)}'
            logger.warning('[%s] %s 일부 실패 (%d/%d)',
                           series_title, episode_title, downloaded, len(files))
        db.session.commit()

        # 7) 진행 보고
        self.client.report_last_page(series_id, product_id, is_done=(rec.status == 'completed'))
        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'
