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

import logging as _logging
_logging.getLogger().warning('[kakaopage_dl] setup.py ENTER')

from plugin import *

# 일부 환경에서 SQLALCHEMY_BINDS가 None인 채로 첫 플러그인이 로드될 때 init이 실패할 수 있어 안전망
try:
    from framework import F as _F
    _binds_before = _F.app.config.get('SQLALCHEMY_BINDS')
    _logging.getLogger().warning('[kakaopage_dl] SQLALCHEMY_BINDS before create=%r', _binds_before)
    if _binds_before is None:
        _F.app.config['SQLALCHEMY_BINDS'] = {}
except Exception as _e:
    _logging.getLogger().error('[kakaopage_dl] pre-init guard exception: %s', _e)

P = create_plugin_instance(setting)

# 진단: create_plugin_instance 직후 상태 출력
try:
    from framework import F as _F
    _binds_after = (_F.app.config.get('SQLALCHEMY_BINDS') or {})
    P.logger.warning('[kakaopage_dl] P.status=%r, bind_keys=%s, my_key_in_binds=%s',
                     getattr(P, 'status', '?'),
                     list(_binds_after.keys()),
                     P.package_name in _binds_after)
except Exception as _e:
    _logging.getLogger().error('[kakaopage_dl] post-init diag exception: %s', _e)

try:
    from .mod_basic import ModuleBasic
    P.set_module_list([ModuleBasic])
except Exception as e:
    import traceback
    P.logger.error(f'Exception:{str(e)}')
    P.logger.error(traceback.format_exc())
