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
            {'uri': 'basic/list',    'name': '다운로드 이력'},
            {'uri': 'log',           'name': '로그'},
        ],
    },
    'setting_menu': None,
    'default_route': 'normal',
}

from plugin import *

# 일부 환경에서 SQLALCHEMY_BINDS가 None인 채로 첫 플러그인이 로드될 때 init이 실패할 수 있어 안전망
try:
    from framework import F as _F
    if _F.app.config.get('SQLALCHEMY_BINDS') is None:
        _F.app.config['SQLALCHEMY_BINDS'] = {}
except Exception:
    pass

P = create_plugin_instance(setting)

try:
    from .mod_basic import ModuleBasic
    P.set_module_list([ModuleBasic])
except Exception as e:
    import traceback
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
