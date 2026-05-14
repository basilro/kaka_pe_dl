import traceback

from .model import ModelKakaopageItem
from .setup import *
from . import manual_worker


class ModuleManual(PluginModuleBase):

    def __init__(self, P):
        super(ModuleManual, self).__init__(
            P, name='manual', first_menu='setting',
            scheduler_desc='카카오페이지 수동 다운로드',
        )
        # mod_basic과 동일 패턴 — 라우트/세팅 자동 등록에 필요할 수 있음
        self.db_default = {
            f'{self.name}_db_version': '1',
            f'{self.name}_auto_start': 'False',
            f'{self.name}_interval': '0 */6 * * *',
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
        }
        self.web_list_model = ModelKakaopageItem
        logger.info('ModuleManual __init__ done: name=%s package=%s',
                    self.name, P.package_name)

    def scheduler_function(self):
        # 수동 다운로드 모듈은 스케줄러 사용 안 함
        pass

    def process_menu(self, sub, req):
        logger.debug('manual.process_menu IN sub=%r', sub)
        if not sub:
            sub = 'setting'
        arg = P.ModelSetting.to_dict()
        try:
            return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
        except Exception as e:
            logger.error('manual render_template 실패 sub=%r: %s', sub, e)
            logger.error(traceback.format_exc())
            return f'manual render failed: {e}'

    def process_command(self, command, arg1, arg2, arg3, req):
        ret = {'ret': 'success'}
        try:
            if command == 'analyze':
                url = (arg1 or '').strip()
                if not url and req is not None:
                    url = (req.form.get('url') or req.values.get('url') or '').strip()
                ret = manual_worker.analyze(url)

            elif command == 'start':
                ret = manual_worker.start()

            elif command == 'cancel':
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}

            elif command == 'progress':
                ret = {'ret': 'success', 'state': manual_worker.get_state()}

            else:
                ret = {'ret': 'fail', 'msg': f'unknown command: {command}'}
        except Exception as e:
            logger.error('manual command %s exception: %s', command, e)
            logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        return jsonify(ret)
