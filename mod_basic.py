import traceback

from .model import ModelKakaopageItem
from .setup import *
from .worker import Worker


class ModuleBasic(PluginModuleBase):

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic', first_menu='setting',
            scheduler_desc='카카오페이지 자동 다운로드',
        )
        self.db_default = {
            f'db_version': '1',
            f'{self.name}_auto_start': 'False',
            f'{self.name}_interval': '0 */3 * * *',  # 3시간마다
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
            f'{P.package_name}_item_last_list_option': '',

            # 사용자 설정
            'titles': '',                # 웹툰 — 한 줄에 하나 (또는 |). 제목/URL/숫자/path 모두 가능
            'titles_novel': '',          # 소설 — 같은 형식
            'cookies_json': '',          # Cookie-Editor export JSON
            'download_path': '',
            'max_per_run': '1',
            'use_waitfree': 'True',       # 기다무 대여권 사용
            'use_owned_rental': 'False',  # 일반(보유) 대여권 사용
            'notify_webhook_cookie': '',          # 쿠키 만료 시 발송할 웹훅
            'notify_webhook_download': '',        # 웹툰 다운로드 완료 요약 발송 웹훅
            'notify_webhook_download_novel': '',  # 소설 다운로드 완료 요약 발송 웹훅
            'cookie_expired_notified': 'False',   # 쿠키 만료 알림 1회 발송 플래그
            'use_proxy': 'False',                 # 프록시 사용 여부
            'proxy_url': '',                      # warproxy 등. use_proxy=True + 값 있을 때만 사용
            'use_compress': 'False',              # 정상 다운 완료 시 회차 폴더 ZIP 압축 + 원본 삭제
            'auto_start': 'False',
        }
        self.web_list_model = ModelKakaopageItem

    def process_menu(self, sub, req):
        logger.debug(f'process_menu IN: %s', sub)
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
        return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        try:
            P.logger.info('[basic.process_command] cmd=%r arg1=%r arg2=%r arg3=%r',
                          command, arg1, arg2, arg3)
        except Exception:
            pass
        ret = {'ret': 'success'}
        try:
            if command == 'verify_cookies':
                from .client import KakaopageClient, AuthRequiredError
                try:
                    proxy_url = KakaopageClient.resolve_proxy(
                        P.ModelSetting.get('use_proxy'),
                        P.ModelSetting.get('proxy_url'))
                    cli = KakaopageClient(P.ModelSetting.get('cookies_json'),
                                          logger=P.logger, proxy_url=proxy_url)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
            elif command == 'sync_metadata':
                ret = self.do_action_sync_metadata()
            elif command == 'compress_all':
                ret = self.do_action_compress_all()
            # ---- 수동 다운로드 (명령 이름에서 manual_ 접두사 제거 — 라우터 충돌 회피) ----
            elif command == 'mrun':
                from . import manual_worker
                url = (arg1 or '').strip()
                if not url and req is not None:
                    try:
                        url = (req.form.get('url') or req.values.get('url')
                               or req.args.get('url') or '').strip()
                    except Exception:
                        pass
                P.logger.info('[mrun] url=%r arg1=%r', url, arg1)
                ret = manual_worker.run_with_url(url)
            elif command == 'mcancel':
                from . import manual_worker
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}
            elif command == 'mprogress':
                from . import manual_worker
                ret = {'ret': 'success', 'state': manual_worker.get_state()}
            elif command == 'status_progress':
                # 자동 + 수동 진행 상황 통합
                from . import manual_worker, worker as auto_worker
                ret = {
                    'ret': 'success',
                    'auto': auto_worker.get_auto_state(),
                    'manual': manual_worker.get_state(),
                }
            elif command == 'notify_test':
                # arg1 = 'cookie' | 'download' | 'download_novel'
                from .notify import send_webhook
                kind = (arg1 or 'cookie').strip().lower()
                if kind == 'download_novel':
                    url_key = 'notify_webhook_download_novel'
                    label = '소설 다운로드'
                elif kind == 'download':
                    url_key = 'notify_webhook_download'
                    label = '웹툰 다운로드'
                else:
                    kind = 'cookie'
                    url_key = 'notify_webhook_cookie'
                    label = '쿠키 만료'
                url = (P.ModelSetting.get(url_key) or '').strip()
                if not url:
                    ret = {'ret': 'fail', 'msg': f'{label} URL 미설정'}
                else:
                    msg = f'[카카오페이지] 테스트 알림 ({label}) — 정상 수신 확인용'
                    ok = send_webhook(url, msg)
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '발송 성공' if ok else '발송 실패 (URL/형식 확인)'}
            elif command == 'db_delete_items':
                # arg1 = 콤마구분 id 문자열
                ids = []
                for x in (arg1 or '').split(','):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
                if not ids:
                    ret = {'ret': 'fail', 'msg': '삭제할 ID 없음', 'count': 0}
                else:
                    cnt = (db.session.query(ModelKakaopageItem)
                           .filter(ModelKakaopageItem.id.in_(ids))
                           .delete(synchronize_session=False))
                    db.session.commit()
                    P.logger.info('[basic] db_delete_items: %d개 삭제 (요청 %d개)',
                                  cnt, len(ids))
                    ret = {'ret': 'success', 'count': cnt}
        except Exception as e:
            P.logger.error('[basic.process_command] inner Exception: %s', e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        # jsonify 자체가 직렬화 실패할 수 있어 안전망
        try:
            return jsonify(ret)
        except Exception as e:
            P.logger.error('[basic.process_command] jsonify 실패: %s ret=%r', e, ret)
            P.logger.error(traceback.format_exc())
            return jsonify({'ret': 'fail', 'msg': f'jsonify 실패: {e}'})

    def scheduler_function(self):
        P.logger.info('[basic] scheduler_function CALLED')
        try:
            ret = self.do_action()
            P.logger.info('[basic] scheduler 종료: %s', ret)
        except Exception as e:
            P.logger.error('[basic] scheduler Exception: %s', e)
            P.logger.error(traceback.format_exc())

    def do_action(self):
        P.logger.info('[basic] do_action BEGIN')
        try:
            with F.app.app_context():
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}

    def do_action_sync_metadata(self):
        """체크할 작품 전체의 info.xml / cover.jpg 누락분 백그라운드 동기화."""
        import threading
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().sync_metadata_all()
            except Exception as e:
                P.logger.error('[basic] sync_metadata run Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '메타 동기화 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_compress_all(self):
        """download_path 아래 모든 회차 폴더 ZIP 압축 + 원본 폴더 삭제 (백그라운드)."""
        import threading
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 다른 작업 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().compress_all()
            except Exception as e:
                P.logger.error('[basic] compress_all Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '압축 시작됨 — "진행 상황" 메뉴에서 확인'}
