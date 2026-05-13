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
            'titles': '',                # 'A|B|C'
            'cookies_json': '',          # Cookie-Editor export JSON
            'download_path': '',
            'max_per_run': '1',
            'use_waitfree_only': 'True',
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

    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret': 'success'}
        try:
            if command == 'verify_cookies':
                from .client import KakaopageClient, AuthRequiredError
                try:
                    cli = KakaopageClient(P.ModelSetting.get('cookies_json'), logger=logger)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
        except Exception as e:
            logger.error('Exception: %s', e)
            logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        return jsonify(ret)

    def scheduler_function(self):
        logger.debug('scheduler_function IN')
        try:
            ret = self.do_action()
            logger.info('scheduler 종료: %s', ret)
        except Exception as e:
            logger.error('Exception: %s', e)
            logger.error(traceback.format_exc())

    def do_action(self):
        try:
            w = Worker()
            return w.run()
        except Exception as e:
            logger.error('Exception: %s', e)
            logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}
