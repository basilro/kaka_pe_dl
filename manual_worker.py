"""수동 다운로드 워커 — 작품 URL 하나에 대해 보유한 회차 전체 직렬 다운로드.

state는 모듈 레벨 싱글톤. UI는 폴링으로 progress 확인.
"""
import os
import re
import threading
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import unquote as urlparse_unquote

from .client import KakaopageClient, KakaopageError, AuthRequiredError, NotPurchasedError
from .model import ModelKakaopageItem
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


# 동시 한 작업만 허용 — 카카오 부하 + 단순화
_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    'status': 'idle',   # idle | analyzing | running | done | error | canceled
    'message': '',
    'series_id': None,
    'series_title': '',
    'started_at': None,
    'finished_at': None,
    'episodes': [],     # [{product_id, episode_no, title, availability, state, pages_done, pages_total, save_dir, error}]
    'current_index': -1,
    'total_to_download': 0,
    'completed': 0,
    'skipped': 0,
    'failed': 0,
}
_cancel_flag = threading.Event()
_thread: Optional[threading.Thread] = None


# ---- state helpers ----
def get_state() -> Dict[str, Any]:
    with _state_lock:
        # shallow copy + episodes 리스트 복사 (UI에 안전한 스냅샷)
        snap = {k: v for k, v in _state.items() if k != 'episodes'}
        snap['episodes'] = [dict(e) for e in _state['episodes']]
        return snap


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _reset_state():
    with _state_lock:
        _state.update({
            'status': 'idle',
            'message': '',
            'series_id': None,
            'series_title': '',
            'started_at': None,
            'finished_at': None,
            'episodes': [],
            'current_index': -1,
            'total_to_download': 0,
            'completed': 0,
            'skipped': 0,
            'failed': 0,
        })


def is_running() -> bool:
    with _state_lock:
        return _state['status'] in ('analyzing', 'running')


def cancel():
    _cancel_flag.set()
    _set(message='취소 요청됨')


# ---- analyze (동기) ----
def analyze(url_or_id: str) -> Dict[str, Any]:
    """URL → series 메타 + 회차 목록. 다운로드는 안 함."""
    P.logger.info('[manual] analyze BEGIN url_or_id=%r', url_or_id)
    series_id = KakaopageClient.extract_series_id(url_or_id)
    P.logger.info('[manual] extract_series_id → %r', series_id)
    if not series_id:
        return {'ret': 'fail', 'msg': f'URL에서 series_id 추출 실패: {url_or_id!r}'}

    cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
    if not cookies_json:
        P.logger.error('[manual] cookies_json 비어있음')
        return {'ret': 'fail', 'msg': '쿠키 미설정 — 설정 페이지에서 쿠키 주입 후 다시 시도'}

    try:
        cli = KakaopageClient(cookies_json, logger=logger)
    except AuthRequiredError as e:
        P.logger.error('[manual] 쿠키 파싱 실패: %s', e)
        return {'ret': 'fail', 'msg': f'쿠키 인증 실패: {e}'}
    except Exception as e:
        P.logger.error('[manual] KakaopageClient 생성 예외: %s', e)
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'클라이언트 생성 실패: {e}'}

    try:
        eps = cli.get_episodes_all(series_id)
        P.logger.info('[manual] get_episodes_all → %d개', len(eps) if eps else 0)
    except AuthRequiredError as e:
        P.logger.error('[manual] 회차 조회 권한 실패: %s', e)
        return {'ret': 'fail', 'msg': f'권한 만료 — 쿠키 재주입 필요: {e}'}
    except Exception as e:
        P.logger.error('[manual] 회차 조회 예외: %s', e)
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'회차 목록 조회 실패: {e}'}

    if not eps:
        return {'ret': 'fail', 'msg': '회차가 없습니다'}

    # 시리즈 제목 추출 (회차 메타 첫 항목의 series_title 또는 series_simple_info)
    first_item = eps[0]['item']
    series_title = (first_item.get('series_title')
                    or first_item.get('series_simple_info', {}).get('title')
                    or f'series_{series_id}')

    episodes = []
    for x in eps:
        it = x['item']
        ep_no = KakaopageClient.episode_no_from_title(it.get('title', '')) or 0
        avail = KakaopageClient.episode_availability(it)
        episodes.append({
            'product_id': it.get('product_id'),
            'episode_no': ep_no,
            'title': it.get('title', ''),
            'availability': avail,
            'state': 'pending',  # pending | skipped | downloading | completed | failed
            'pages_done': 0,
            'pages_total': 0,
            'save_dir': '',
            'error': '',
        })
    # 회차순 정렬
    episodes.sort(key=lambda e: (e['episode_no'], e['product_id'] or 0))

    will_download = sum(1 for e in episodes if e['availability'] in ('free', 'owned', 'rented'))

    _reset_state()
    _set(status='idle',
         message=f'분석 완료 — {len(episodes)}개 회차 중 다운로드 가능 추정 {will_download}개',
         series_id=series_id, series_title=series_title,
         episodes=episodes, total_to_download=will_download)

    P.logger.info('[manual] analyze END series=%r total=%d will_download=%d',
                  series_title, len(episodes), will_download)
    return {
        'ret': 'success',
        'series_id': series_id,
        'series_title': series_title,
        'episodes': episodes,
        'will_download': will_download,
        'total': len(episodes),
    }


# ---- run (분석 + 자동 시작 통합) ----
def run_with_url(url_or_id: str) -> Dict[str, Any]:
    """URL 하나로 분석 + 다운로드 시작까지."""
    P.logger.info('[manual] run_with_url BEGIN url=%r', url_or_id)
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    ar = analyze(url_or_id)
    if ar.get('ret') != 'success':
        return ar
    sr = start()
    return {
        'ret': sr.get('ret', 'fail'),
        'msg': sr.get('msg', ''),
        'series_id': ar.get('series_id'),
        'series_title': ar.get('series_title'),
        'will_download': ar.get('will_download'),
        'total': ar.get('total'),
    }


# ---- start (백그라운드) ----
def start() -> Dict[str, Any]:
    global _thread
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    with _state_lock:
        if not _state['series_id'] or not _state['episodes']:
            return {'ret': 'fail', 'msg': '먼저 작품을 분석하세요'}
    download_root = (P.ModelSetting.get('download_path') or '').strip()
    if not download_root:
        return {'ret': 'fail', 'msg': 'download_path 미설정 (설정 페이지에서 지정)'}

    _cancel_flag.clear()
    _set(status='running', message='다운로드 시작', started_at=datetime.now().isoformat(),
         finished_at=None, current_index=-1, completed=0, skipped=0, failed=0)

    _thread = threading.Thread(target=_run, args=(download_root,), daemon=True)
    _thread.start()
    return {'ret': 'success', 'msg': '시작됨'}


def _run(download_root: str):
    try:
        cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
        cli = KakaopageClient(cookies_json, logger=logger)
        with _state_lock:
            series_id = _state['series_id']
            series_title = _state['series_title']
            # working copy
            episodes = _state['episodes']

        for idx, ep in enumerate(episodes):
            if _cancel_flag.is_set():
                _set(status='canceled', finished_at=datetime.now().isoformat(),
                     message='취소됨')
                return

            _set(current_index=idx)

            # 보유 추정 외엔 스킵
            if ep['availability'] not in ('free', 'owned', 'rented'):
                with _state_lock:
                    _state['episodes'][idx]['state'] = 'skipped'
                    _state['skipped'] += 1
                continue

            ok = _download_episode(cli, series_id, series_title, idx, ep, download_root)
            with _state_lock:
                if ok == 'completed':
                    _state['completed'] += 1
                elif ok == 'skipped':
                    _state['skipped'] += 1
                else:
                    _state['failed'] += 1

        _set(status='done', finished_at=datetime.now().isoformat(),
             current_index=-1, message='완료')
    except AuthRequiredError as e:
        _set(status='error', finished_at=datetime.now().isoformat(),
             message=f'쿠키 만료/무효: {e}')
    except Exception as e:
        logger.error('manual worker exception: %s', e)
        logger.error(traceback.format_exc())
        _set(status='error', finished_at=datetime.now().isoformat(),
             message=f'에러: {e}')


def _ep_update(idx: int, **kw):
    with _state_lock:
        _state['episodes'][idx].update(kw)


def _download_episode(cli: KakaopageClient, series_id: int, series_title: str,
                      idx: int, ep: Dict[str, Any], download_root: str) -> str:
    product_id = ep['product_id']
    episode_title = ep['title']
    ep_no = ep['episode_no']

    # 이미 DB에 completed면 스킵
    rec = db.session.query(ModelKakaopageItem).filter_by(product_id=product_id).first()
    if rec and rec.status == 'completed':
        _ep_update(idx, state='completed', save_dir=rec.save_dir or '',
                   pages_done=rec.downloaded_count or 0,
                   pages_total=rec.page_count or 0)
        return 'completed'

    if rec is None:
        rec = ModelKakaopageItem()
        rec.series_id = series_id
        rec.series_title = series_title
        rec.product_id = product_id
        rec.episode_no = ep_no
        rec.episode_title = episode_title
        db.session.add(rec)
        db.session.commit()

    _ep_update(idx, state='downloading', error='')
    rec.status = 'downloading'; rec.updated_time = datetime.now(); db.session.commit()

    # viewer/data 직접 시도 (보유 회차만 200 OK; 잠금이면 실패 → 스킵)
    try:
        vd = cli.viewer_data(series_id, product_id)
    except NotPurchasedError:
        _ep_update(idx, state='skipped', error='미구매(잠금)')
        rec.status = 'skipped_no_ticket'; db.session.commit()
        return 'skipped'
    except KakaopageError as e:
        _ep_update(idx, state='failed', error=f'viewer_data: {e}')
        rec.status = 'failed'; rec.error_msg = f'viewer_data: {e}'; db.session.commit()
        return 'failed'

    files = ((vd.get('viewerData') or {}).get('imageDownloadData') or {}).get('files') or []
    if not files:
        _ep_update(idx, state='skipped', error='이미지 목록 없음(잠금 가능성)')
        rec.status = 'skipped_no_ticket'; db.session.commit()
        return 'skipped'

    rec.page_count = len(files)
    _ep_update(idx, pages_total=len(files), pages_done=0)

    s_folder = _safe_filename(series_title)
    e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
    save_dir = os.path.join(download_root, s_folder, e_folder)
    os.makedirs(save_dir, exist_ok=True)
    rec.save_dir = save_dir
    _ep_update(idx, save_dir=save_dir)
    db.session.commit()

    downloaded = 0; total_bytes = 0; failed = 0
    for f in files:
        if _cancel_flag.is_set():
            break
        no = f.get('no') or (downloaded + 1)
        url = f.get('secureUrl')
        ext = '.jpg'
        try:
            m = re.search(r'filename=([^&]+)', url or '')
            if m:
                name = urlparse_unquote(m.group(1))
                if '.' in name:
                    ext = '.' + name.rsplit('.', 1)[-1]
        except Exception:
            pass
        local = os.path.join(save_dir, f'{no:03d}{ext}')
        try:
            got = cli.download_image(url, local)
            downloaded += 1; total_bytes += got
            _ep_update(idx, pages_done=downloaded)
        except Exception as e:
            failed += 1
            logger.warning('manual %s p%s 실패: %s', episode_title, no, e)

    rec.downloaded_count = downloaded
    rec.total_bytes = total_bytes
    rec.downloaded_at = datetime.now()
    rec.updated_time = rec.downloaded_at
    if downloaded == len(files):
        rec.status = 'completed'
        _ep_update(idx, state='completed')
        db.session.commit()
        return 'completed'
    elif downloaded > 0:
        rec.status = 'partial'
        rec.error_msg = f'failed {failed}/{len(files)}'
        _ep_update(idx, state='failed', error=f'부분실패 {failed}/{len(files)}')
        db.session.commit()
        return 'failed'
    else:
        rec.status = 'failed'
        rec.error_msg = f'all failed ({len(files)})'
        _ep_update(idx, state='failed', error='전부 실패')
        db.session.commit()
        return 'failed'
