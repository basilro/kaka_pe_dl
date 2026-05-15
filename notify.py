"""웹훅 알림 발송 유틸 — Discord/Slack/일반 자동 분기."""
from typing import List, Dict

import requests


def send_webhook(url: str, message: str, username: str = 'kakao_pe_dl',
                 timeout: int = 10) -> bool:
    """웹훅 URL 로 메시지 발송. URL 비어있으면 False 반환 (no-op).

    Discord / Slack / 기타 자동 분기:
      - discord.com/api/webhooks → {"content": msg, "username": ...}
      - hooks.slack.com         → {"text": msg}
      - 기타                     → {"content": msg, "text": msg}
    """
    if not url or not message:
        return False
    u = url.strip()
    try:
        if 'discord.com/api/webhooks' in u or 'discordapp.com/api/webhooks' in u:
            payload = {'content': message, 'username': username}
        elif 'hooks.slack.com' in u:
            payload = {'text': message}
        else:
            payload = {'content': message, 'text': message}
        r = requests.post(u, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def build_download_summary(completed_items: List[Dict], is_novel: bool) -> str:
    """완료된 다운로드 항목 list → 발송용 텍스트.

    completed_items: [{'series_title': str, 'episode_title': str,
                       'episode_no': int}, ...]
    is_novel: True 면 [카카오페이지 소설], False 면 [카카오페이지 웹툰] 헤더.
    """
    if not completed_items:
        return ''
    # series_title → list[episode]
    grouped: Dict[str, List[Dict]] = {}
    for it in completed_items:
        s = it.get('series_title') or '(unknown)'
        grouped.setdefault(s, []).append(it)

    total = len(completed_items)
    kind_label = '소설' if is_novel else '웹툰'
    lines: List[str] = [f'[카카오페이지 {kind_label}] 다운로드 완료 — 총 {total}회차']

    for series_title, eps in sorted(grouped.items()):
        eps_sorted = sorted(eps, key=lambda x: x.get('episode_no') or 0)
        cnt = len(eps_sorted)
        if cnt <= 5:
            titles = ', '.join((e.get('episode_title') or '?')
                               for e in eps_sorted)
        else:
            first = eps_sorted[0].get('episode_title') or '?'
            last = eps_sorted[-1].get('episode_title') or '?'
            titles = f'{first} ~ {last}'
        lines.append(f'- {series_title} ({cnt}): {titles}')
    return '\n'.join(lines)


def build_cookie_expired_message() -> str:
    return ('[카카오페이지] 쿠키 만료 감지\n'
            '설정 페이지에서 쿠키를 재주입해주세요.\n'
            '(자동 다운로드가 중단됩니다)')
