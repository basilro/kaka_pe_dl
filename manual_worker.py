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
        # _ 접두사 키는 내부용 (메타 캐시 등) — UI에 노출하지 않음
        snap = {k: v for k, v in _state.items()
                if k != 'episodes' and not k.startswith('_')}
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

    proxy_url = KakaopageClient.resolve_proxy(
        P.ModelSetting.get('use_proxy'), P.ModelSetting.get('proxy_url'))
    try:
        cli = KakaopageClient(cookies_json, logger=P.logger, proxy_url=proxy_url)
    except AuthRequiredError as e:
        P.logger.error('[manual] 쿠키 파싱 실패: %s', e)
        return {'ret': 'fail', 'msg': f'쿠키 인증 실패: {e}'}
    except Exception as e:
        P.logger.error('[manual] KakaopageClient 생성 예외: %s', e)
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'클라이언트 생성 실패: {e}'}

    try:
        data = cli.get_episodes_all(series_id)
        eps = data.get('list') or []
        series_item = data.get('series_item') or {}
        P.logger.info('[manual] get_episodes_all → %d개', len(eps))
    except AuthRequiredError as e:
        P.logger.error('[manual] 회차 조회 권한 실패: %s', e)
        return {'ret': 'fail', 'msg': f'권한 만료 — 쿠키 재주입 필요: {e}'}
    except Exception as e:
        P.logger.error('[manual] 회차 조회 예외: %s', e)
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'회차 목록 조회 실패: {e}'}

    if not eps:
        return {'ret': 'fail', 'msg': '회차가 없습니다'}

    series_title = series_item.get('title') or f'series_{series_id}'
    is_novel = (series_item.get('category') or '') in ('웹소설', '소설')

    # 이미 받은(completed) 회차 — 기본 체크 제외 + 체크 불가 표시용 (product_id 기준)
    completed_pids = set()
    try:
        pids = [it['item'].get('product_id') for it in eps]
        pids = [p for p in pids if p is not None]
        if pids:
            for r in (db.session.query(ModelKakaopageItem)
                      .filter(ModelKakaopageItem.product_id.in_(pids)).all()):
                if r.status == 'completed' and r.product_id is not None:
                    completed_pids.add(r.product_id)
    except Exception as e:
        P.logger.warning('[manual] completed 조회 실패(계속): %s', e)

    # 전체 회차를 표시용으로 모두 반환. selectable = 보유(무료/소장/대여) & 미수신.
    all_eps = []
    for x in eps:
        it = x['item']
        pid = it.get('product_id')
        ep_no = KakaopageClient.episode_no_from_title(it.get('title', '')) or 0
        avail = KakaopageClient.episode_availability(it)
        completed = pid in completed_pids
        all_eps.append({
            'product_id': pid,
            'episode_no': ep_no,
            'title': it.get('title', ''),
            'availability': avail,
            'completed': completed,
            'selectable': (avail in ('free', 'owned', 'rented')) and not completed,
            'state': 'have' if completed else 'pending',  # have=이미받음(이력에 completed)
            'pages_done': 0,
            'pages_total': 0,
            'save_dir': '',
            'error': '',
        })
    all_eps.sort(key=lambda e: (e['episode_no'], e['product_id'] or 0))
    will_download = sum(1 for e in all_eps if e['selectable'])

    # 뷰어 URL(/viewer/{product_id})이면 그 회차만 자동 선택(focus). 무효면 폴백.
    focus_pid = KakaopageClient.extract_product_id(url_or_id)
    focus_note = ''
    if focus_pid is not None:
        match = next((e for e in all_eps if e['product_id'] == focus_pid), None)
        if match is None:
            focus_note = f'지정한 회차(pid {focus_pid}) 없음 — 받기 가능 전체 선택'
            focus_pid = None
        elif not match['selectable']:
            focus_note = ('지정한 회차는 이미 받음 — 받기 가능 전체 선택'
                          if match['completed']
                          else '지정한 회차는 미구매/잠금 — 받을 수 없음')
            focus_pid = None

    _reset_state()
    _set(status='idle',
         message=(f'분석 완료 — 전체 {len(all_eps)}개, '
                  f'받기 가능(보유·미수신) {will_download}개'),
         series_id=series_id, series_title=series_title,
         episodes=all_eps, total_to_download=0,
         _series_meta=series_item, _is_novel=is_novel)

    P.logger.info('[manual] analyze END series=%r total=%d selectable=%d focus_pid=%s',
                  series_title, len(all_eps), will_download, focus_pid)
    return {
        'ret': 'success',
        'series_id': series_id,
        'series_title': series_title,
        'episodes': all_eps,
        'will_download': will_download,
        'total': len(all_eps),
        'focus_pid': focus_pid,
        'focus_note': focus_note,
    }


# ---- start_selected (선택 회차만 백그라운드 다운로드) ----
def start_selected(selected_pids: List[int]) -> Dict[str, Any]:
    """analyze 로 만든 목록에서 선택된 보유 회차(product_id)만 다운로드."""
    global _thread
    P.logger.info('[manual] start_selected BEGIN pids=%s', selected_pids)
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    with _state_lock:
        if not _state['series_id'] or not _state['episodes']:
            return {'ret': 'fail', 'msg': '먼저 작품을 분석하세요'}
    download_root = (P.ModelSetting.get('download_path') or '').strip()
    if not download_root:
        return {'ret': 'fail', 'msg': 'download_path 미설정 (설정 페이지에서 지정)'}

    sel = set(int(p) for p in (selected_pids or []))
    with _state_lock:
        targets = [idx for idx, ep in enumerate(_state['episodes'])
                   if ep.get('product_id') in sel and ep.get('selectable')]
    if not targets:
        return {'ret': 'fail',
                'msg': '선택된 받기 가능 회차 없음 (보유·미수신만 선택 가능)'}

    # 전역 락 — 자동/압축/메타 작업과 절대 겹치지 않게 (회차 폴더 zip+삭제와
    # 다운로드가 겹쳐 폴더가 사라지는 ENOENT 사고 방지). _run 의 finally 에서 해제.
    from . import worker as _wkr
    if not _wkr.try_acquire_run_lock():
        return {'ret': 'fail',
                'msg': '자동 다운로드/압축 등 다른 작업이 실행 중 — 끝난 뒤 다시'}

    target_set = set(targets)
    with _state_lock:
        for idx, ep in enumerate(_state['episodes']):
            if idx in target_set:
                ep['state'] = 'pending'; ep['error'] = ''
                ep['pages_done'] = 0; ep['pages_total'] = 0
            else:
                ep['state'] = 'excluded'

    _cancel_flag.clear()
    _set(status='running', message='선택 다운로드 시작',
         started_at=datetime.now().isoformat(), finished_at=None,
         current_index=-1, completed=0, skipped=0, failed=0,
         total_to_download=len(targets))
    _thread = threading.Thread(target=_run, args=(download_root, targets),
                               daemon=True)
    _thread.start()
    return {'ret': 'success', 'msg': f'{len(targets)}개 회차 다운로드 시작'}


def _run(download_root: str, target_indices: List[int]):
    P.logger.info('[manual] _run BEGIN download_root=%r targets=%d',
                  download_root, len(target_indices))
    # 백그라운드 thread 에서 db.session 쓰려면 Flask app context 필요
    with F.app.app_context():
        try:
            cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
            proxy_url = KakaopageClient.resolve_proxy(
                P.ModelSetting.get('use_proxy'),
                P.ModelSetting.get('proxy_url'))
            cli = KakaopageClient(cookies_json, logger=P.logger,
                                  proxy_url=proxy_url)
            with _state_lock:
                series_id = _state['series_id']
                series_title = _state['series_title']
                episodes = list(_state['episodes'])
                series_meta = dict(_state.get('_series_meta') or {})
                is_novel = bool(_state.get('_is_novel'))
            P.logger.info('[manual] _run series=%r episodes=%d',
                          series_title, len(episodes))

            # info.xml / cover.jpg — 첫 다운로드 전에 생성
            try:
                from . import worker as _wkr
                _wkr.ensure_title_metadata(cli, download_root, series_title,
                                           series_id, series_meta,
                                           is_novel=is_novel)
            except Exception as e:
                P.logger.warning('[manual] ensure_title_metadata 실패: %s', e)

            total = len(target_indices)
            for n, idx in enumerate(target_indices, start=1):
                if _cancel_flag.is_set():
                    _set(status='canceled',
                         finished_at=datetime.now().isoformat(),
                         message='취소됨')
                    P.logger.info('[manual] _run CANCELED at idx=%d', idx)
                    return

                ep = episodes[idx]
                _set(current_index=idx)
                P.logger.info('[manual] _run [%d/%d] %s avail=%s pid=%s',
                              n, total, ep.get('title'),
                              ep.get('availability'), ep.get('product_id'))

                ok = _download_episode(cli, series_id, series_title,
                                       idx, ep, download_root)
                P.logger.info('[manual] _run [%d/%d] result=%s', n, total, ok)
                with _state_lock:
                    if ok == 'completed':
                        _state['completed'] += 1
                    elif ok == 'skipped':
                        _state['skipped'] += 1
                    else:
                        _state['failed'] += 1

            _set(status='done', finished_at=datetime.now().isoformat(),
                 current_index=-1, message='완료')
            P.logger.info('[manual] _run END')
        except AuthRequiredError as e:
            P.logger.error('[manual] _run AuthRequired: %s', e)
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'쿠키 만료/무효: {e}')
        except Exception as e:
            P.logger.error('[manual] _run exception: %s', e)
            P.logger.error(traceback.format_exc())
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'에러: {e}')
        finally:
            from . import worker as _wkr
            _wkr.release_run_lock()


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

    viewer_data = vd.get('viewerData') or {}
    viewer_type = viewer_data.get('type') or ''

    s_folder = _safe_filename(series_title)
    kind_dir = 'novel' if viewer_type == 'TextViewerData' else 'webtoon'
    series_dir = os.path.join(download_root, kind_dir, s_folder)

    # === 소설 (TextViewerData) — 회차 폴더 없이 작품폴더 직속 NNNN_제목.txt ===
    if viewer_type == 'TextViewerData':
        os.makedirs(series_dir, exist_ok=True)
        rec.save_dir = series_dir
        _ep_update(idx, save_dir=series_dir)
        db.session.commit()

        ats = viewer_data.get('atsServerUrl') or ''
        contents = viewer_data.get('contentsList') or []
        if not contents:
            _ep_update(idx, state='skipped', error='contentsList 없음')
            rec.status = 'skipped_no_ticket'; db.session.commit()
            return 'skipped'
        rec.page_count = len(contents)
        _ep_update(idx, pages_total=len(contents), pages_done=0)

        paragraphs = []
        done = 0
        for c in contents:
            if _cancel_flag.is_set():
                break
            secure = c.get('secureUrl')
            if not secure:
                continue
            try:
                ps = cli.download_novel_chapter(ats, secure)
                paragraphs.extend(ps)
                done += 1
                _ep_update(idx, pages_done=done)
            except Exception as e:
                P.logger.warning('novel %s c%s 실패: %s', episode_title, c.get('chapterId'), e)

        if not paragraphs:
            _ep_update(idx, state='failed', error='텍스트 추출 실패')
            rec.status = 'failed'; rec.error_msg = 'no text extracted'
            db.session.commit(); return 'failed'

        fname = f'{ep_no:04d}_{_safe_filename(episode_title)}.txt'
        save_path = os.path.join(series_dir, fname)
        with open(save_path, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(paragraphs))
        total_bytes = os.path.getsize(save_path)
        rec.downloaded_count = done
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        if done == len(contents):
            rec.status = 'completed'
            _ep_update(idx, state='completed')
        else:
            rec.status = 'partial'
            _ep_update(idx, state='failed',
                       error=f'부분실패 {len(contents)-done}/{len(contents)}')
        db.session.commit()
        return 'completed' if rec.status == 'completed' else 'failed'

    # === 웹툰 (이미지) — 회차폴더 안에 페이지 이미지들 ===
    e_folder = f'{ep_no:04d}_{_safe_filename(episode_title)}'
    save_dir = os.path.join(series_dir, e_folder)
    os.makedirs(save_dir, exist_ok=True)
    rec.save_dir = save_dir
    _ep_update(idx, save_dir=save_dir)
    db.session.commit()

    files = (viewer_data.get('imageDownloadData') or {}).get('files') or []
    if not files:
        _ep_update(idx, state='skipped', error=f'다운로드 데이터 없음 (type={viewer_type})')
        rec.status = 'skipped_no_ticket'; db.session.commit()
        return 'skipped'

    rec.page_count = len(files)
    _ep_update(idx, pages_total=len(files), pages_done=0)

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
            P.logger.warning('manual %s p%s 실패: %s', episode_title, no, e)

    rec.downloaded_count = downloaded
    rec.total_bytes = total_bytes
    rec.downloaded_at = datetime.now()
    rec.updated_time = rec.downloaded_at
    if downloaded == len(files):
        rec.status = 'completed'
        _ep_update(idx, state='completed')
        db.session.commit()
        # 정상 완료 + 압축 옵션 On → 회차 폴더 ZIP 압축 (웹툰만)
        if (P.ModelSetting.get('use_compress') or 'False') == 'True':
            from . import worker as _wkr
            zip_path = _wkr.compress_episode_folder(save_dir)
            if zip_path:
                clean = _wkr._strip_pagecount(zip_path)  # DB/UI 는 #N 없이
                rec.save_dir = clean
                db.session.commit()
                _ep_update(idx, save_dir=clean)
                P.logger.info('[manual] %s 압축 완료 → %s',
                              episode_title, zip_path)
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
