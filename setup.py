setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': None,
    'menu': {
        'uri': __package__,
        'name': '카카오페이지 다운',
        'list': [
            {'uri': 'basic/setting', 'name': '설정'},
            {'uri': 'basic/manual',  'name': '수동 다운로드'},
            {'uri': 'basic/status',  'name': '진행 상황'},
            {'uri': 'basic/list',    'name': '다운로드 이력'},
            {'uri': 'basic/guide',   'name': '매뉴얼'},
            {'uri': 'log',           'name': '로그'},
        ],
    },
    'setting_menu': None,
    'default_route': 'normal',
}

from plugin import *

P = create_plugin_instance(setting)

import os as _os
import sys as _sys
import traceback as _tb
try:
    from support import SupportSC
    _here = _os.path.dirname(_os.path.abspath(__file__))
    for _name in ('client', 'meta', 'trace', 'worker', 'manual_worker'):
        if (not _os.path.exists(_os.path.join(_here, _name + '.py'))) \
                and _os.path.exists(_os.path.join(_here, _name + '.pyf')):
            _mod = SupportSC.load_module_f(__file__, _name)
            if _mod is not None:
                _sys.modules[f'{__package__}.{_name}'] = _mod
except Exception:
    P.logger.error(_tb.format_exc())

try:
    from .mod_basic import ModuleBasic
except Exception:
    from support import SupportSC
    _mb = SupportSC.load_module_P(P, 'mod_basic')
    _sys.modules[f'{__package__}.mod_basic'] = _mb
    ModuleBasic = _mb.ModuleBasic
P.set_module_list([ModuleBasic])
