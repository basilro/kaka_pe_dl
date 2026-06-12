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

# 핵심 모듈을 .pyf 로만 배포한 환경(평문 .py 부재, 예: 실서버) 지원.
# flaskfarm 은 .pyf 를 자동 import 하지 않으므로, 평문이 없으면 의존성 순서대로
# .pyf 를 미리 로드해 sys.modules 에 등록한다. (sjva 플러그인과 동일 방식)
# P 생성 이후에 수행해야 .pyf 내부의 `from .setup import *` 가 P 를 참조할 수 있다.
import os as _os
import traceback as _tb
try:
    from support import SupportSC
    _here = _os.path.dirname(_os.path.abspath(__file__))
    for _name in ('client', 'meta', 'trace', 'worker', 'manual_worker'):
        if (not _os.path.exists(_os.path.join(_here, _name + '.py'))) \
                and _os.path.exists(_os.path.join(_here, _name + '.pyf')):
            SupportSC.load_module_f(__file__, _name)
except Exception:
    P.logger.error(_tb.format_exc())

try:
    from .mod_basic import ModuleBasic
except Exception:
    from support import SupportSC
    ModuleBasic = SupportSC.load_module_P(P, 'mod_basic').ModuleBasic
P.set_module_list([ModuleBasic])
