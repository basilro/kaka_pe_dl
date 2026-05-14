import traceback

from .setup import *
from . import manual_worker


class ModuleManual(PluginModuleBase):

    def __init__(self, P):
        P.logger.info('[manual] __init__ BEGIN package=%s', P.package_name)
        try:
            super(ModuleManual, self).__init__(
                P, name='manual', first_menu='setting',
                scheduler_desc='카카오페이지 수동 다운로드',
            )
        except Exception as e:
            P.logger.error('[manual] super().__init__ 실패: %s', e)
            P.logger.error(traceback.format_exc())
            raise
        P.logger.info('[manual] super OK self.name=%s', getattr(self, 'name', '<no name>'))
        self.db_default = {}
        P.logger.info('[manual] __init__ END')

    def scheduler_function(self):
        pass

    def process_menu(self, sub, req):
        P.logger.info('[manual] process_menu CALLED sub=%r', sub)
        if not sub:
            sub = 'setting'
        arg = P.ModelSetting.to_dict()
        try:
            return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
        except Exception as e:
            P.logger.error('[manual] render_template 실패 sub=%r: %s', sub, e)
            P.logger.error(traceback.format_exc())
            return f'manual render failed: {e}'

    def process_normal(self, sub, req):
        P.logger.info('[manual] process_normal CALLED sub=%r', sub)
        return self.process_menu(sub, req)

    def process_command(self, command, arg1, arg2, arg3, req):
        P.logger.info('[manual] process_command CALLED cmd=%r', command)
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
            P.logger.error('[manual] command %s exception: %s', command, e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        return jsonify(ret)
