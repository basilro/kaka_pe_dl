"""ff(sjva) 재시작/종료 시 다운로드 때문에 프로세스가 좀비화되는 것을 막는다.

배경
----
프레임워크는 플러그인 작업을 **non-daemon 스레드**로 띄운다
(``lib/plugin/logic.py`` 의 ``one_execute`` / ``immediately_execute``).
웹 UI 재시작은 ``socketio.stop()`` 뒤에 ``sys.exit(1)`` 로 끝나는데,
인터프리터 종료 시퀀스가 그 non-daemon 스레드를 join 하면서 멈춘다.
결과는 "웹서버는 죽었는데 프로세스는 살아있는" 좀비이고, ``run.sh`` 의
재시작 루프가 영영 돌아오지 않는다.

여기서는 프레임워크를 고칠 수 없으므로 플러그인 쪽에서 두 겹으로 막는다.

1. **협조적 취소** — ``request()`` 가 켜지면 워커가 다음 체크포인트에서 스스로
   빠져나온다. 정상적으로 끝나면 아무것도 강제하지 않는다.
2. **강제 종료 백스톱** — 취소를 알아채지 못하는 구간(TLS 위장 curl 호출,
   zip 쓰기 같은 블로킹 C 확장)에 들어가 있으면 체크포인트에 영영 도달하지
   못한다. 유예 시간이 지나면 ``os._exit()`` 으로 프로세스를 확실히 끝낸다.
   종료 코드는 프레임워크가 의도한 값을 그대로 쓰므로 ``run.sh`` 는 재시작을
   원래대로 수행한다.

주의
----
* 백스톱 타이머는 반드시 **진짜 OS 스레드**여야 한다. gevent 로 monkey patch
  된 ``threading.Thread`` 는 greenlet 이라, 허브가 블로킹 호출에 잡혀 있으면
  타이머 자체가 돌지 않는다. 그래서 ``get_original`` 로 원본을 꺼내 쓴다.
* ``plugin_unload`` 는 재시작/종료 때만 호출된다. 평상시에는 이 모듈의
  어떤 코드도 동작하지 않는다.
"""

import os
import threading

_flag = threading.Event()
_armed = False
_arm_lock = threading.Lock()

DEFAULT_GRACE = 10.0


def request():
    """종료 요청. 워커들은 다음 체크포인트에서 중단한다."""
    _flag.set()


def requested() -> bool:
    return _flag.is_set()


def reset():
    """테스트/재로드용. 운영 경로에서는 쓰지 않는다."""
    global _armed
    _flag.clear()
    with _arm_lock:
        _armed = False


def _original(module_name, item_name, fallback):
    """gevent 가 monkey patch 하기 전의 원본을 꺼낸다. 없으면 fallback."""
    try:
        from gevent.monkey import get_original
        got = get_original(module_name, item_name)
        if got is not None:
            return got
    except Exception:
        pass
    return fallback


def _intended_exit_code(default=1):
    """프레임워크가 정하려던 종료 코드. 재시작=1, 종료=0.

    ``Framework.__exit_code`` 는 name mangling 된 private 이라 정식 접근자가
    없다. 못 읽으면 재시작(1)으로 본다 — 재시작 중 좀비가 되는 쪽이 원래
    문제였으므로 기본값은 재시작이 맞다.
    """
    try:
        from framework import F
        code = getattr(F, '_Framework__exit_code', None)
        if isinstance(code, int) and code >= 0:
            return code
    except Exception:
        pass
    return default


def arm_force_exit(grace: float = DEFAULT_GRACE, logger=None) -> bool:
    """유예 시간 뒤 프로세스를 강제 종료하는 백스톱을 건다.

    이미 걸려 있으면 아무것도 하지 않고 False. 실제로 걸었으면 True.
    """
    global _armed
    with _arm_lock:
        if _armed:
            return False
        _armed = True

    import _thread as _rawthread
    start_new_thread = _original('_thread', 'start_new_thread',
                                 _rawthread.start_new_thread)
    import time as _time
    sleep = _original('time', 'sleep', _time.sleep)

    def _watchdog():
        try:
            sleep(grace)
            code = _intended_exit_code()
            if logger:
                try:
                    logger.warning(
                        '[shutdown] 작업이 %.1f초 안에 멈추지 않음 — '
                        'os._exit(%d) 로 강제 종료', grace, code)
                except Exception:
                    pass
            os._exit(code)
        except Exception:
            os._exit(1)

    try:
        start_new_thread(_watchdog, ())
        return True
    except Exception:
        with _arm_lock:
            _armed = False
        return False
