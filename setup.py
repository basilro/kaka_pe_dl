setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '카카오페이지 다운',
        'list': [
            {'uri': 'basic/setting',  'name': '설정'},
            {'uri': 'manual/setting', 'name': '수동 다운로드'},
            {'uri': 'basic/list',     'name': '다운로드 이력'},
            {'uri': 'log',            'name': '로그'},
        ],
    },
    'setting_menu': None,
    'default_route': 'normal',
}

from plugin import *

P = create_plugin_instance(setting)

import traceback as _tb

modules = []
try:
    from .mod_basic import ModuleBasic
    modules.append(ModuleBasic)
    P.logger.info('ModuleBasic import OK')
except Exception as e:
    P.logger.error('ModuleBasic import 실패: %s', e)
    P.logger.error(_tb.format_exc())

try:
    from .mod_manual import ModuleManual
    modules.append(ModuleManual)
    P.logger.info('ModuleManual import OK')
except Exception as e:
    P.logger.error('ModuleManual import 실패: %s', e)
    P.logger.error(_tb.format_exc())

try:
    P.set_module_list(modules)
    P.logger.info('plugin set_module_list 완료: %s', [m.__name__ for m in modules])
except Exception as e:
    P.logger.error('set_module_list 실패: %s', e)
    P.logger.error(_tb.format_exc())
