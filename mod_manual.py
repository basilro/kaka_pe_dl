import traceback

from .setup import *
from . import manual_worker


class ModuleManual(PluginModuleBase):

    def __init__(self, P):
        logger.info('ModuleManual __init__ BEGIN package=%s', P.package_name)
        try:
            super(ModuleManual, self).__init__(
                P, name='manual', first_menu='setting',
                scheduler_desc='카카오페이지 수동 다운로드',
            )
        except Exception as e:
            logger.error('ModuleManual super().__init__ 실패: %s', e)
            logger.error(traceback.format_exc())
            raise
        logger.info('ModuleManual super().__init__ OK self.name=%s',
                    getattr(self, 'name', '<no name>'))
        # mod_basic과 ModelSetting 키 충돌 / list 라우트 충돌 피하려 비움
        self.db_default = {}
        # web_list_model 미설정 — mod_basic이 이미 동일 모델로 list 라우트 사용 중
        logger.info('ModuleManual __init__ END')

    def scheduler_function(self):
        # 수동 다운로드 모듈은 스케줄러 사용 안 함
        pass

    def process_menu(self, sub, req):
        logger.info('manual.process_menu CALLED sub=%r', sub)
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
        logger.info('manual.process_command CALLED cmd=%r', command)
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
