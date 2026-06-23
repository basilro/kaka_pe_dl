"""웹훅 알림 발송 유틸 — Discord/Slack/일반 자동 분기."""
from typing import List, Dict

import requests


def send_webhook(url: str, message: str, username: str = 'kakao_pe_dl',
                 timeout: int = 10) -> bool:
    """웹훅 URL 로 메시지 발송. URL 비어있으면 False 반환 (no-op)."""
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


_KIND_LABEL = {'waitfree': '기다무', 'ticket': '대여권'}


def _ticket_tag(items: List[Dict]) -> str:
    """회차 목록에서 티켓 사용 종류별 카운트 → '[기다무 ×2, 대여권 ×1]' 같은 태그."""
    counts: Dict[str, int] = {}
    for it in items:
        k = it.get('kind') or 'free'
        if k in _KIND_LABEL:
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        return ''
    parts = [f'{_KIND_LABEL[k]} ×{counts[k]}'
             for k in ('waitfree', 'ticket') if k in counts]
    return f'  [{", ".join(parts)}]'


def build_download_summary(completed_items: List[Dict], is_novel: bool) -> str:
    """완료된 다운로드 항목 list → 발송용 텍스트."""
    if not completed_items:
        return ''
    grouped: Dict[str, List[Dict]] = {}
    for it in completed_items:
        s = it.get('series_title') or '(unknown)'
        grouped.setdefault(s, []).append(it)

    total = len(completed_items)
    kind_label = '소설' if is_novel else '웹툰'
    header_tag = _ticket_tag(completed_items)
    header = f'[카카오페이지 {kind_label}] 다운로드 완료 — 총 {total}회차'
    if header_tag:
        body = header_tag.strip().lstrip('[').rstrip(']')
        header += f' (티켓 사용: {body})'
    lines: List[str] = [header]

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
        lines.append(f'- {series_title} ({cnt}): {titles}{_ticket_tag(eps_sorted)}')
    return '\n'.join(lines)


def build_cookie_expired_message() -> str:
    return ('[카카오페이지] 쿠키 만료 감지\n'
            '설정 페이지에서 쿠키를 재주입해주세요.\n'
            '(자동 다운로드가 중단됩니다)')


def build_completed_removed_message(removed_titles: List[str]) -> str:
    """완결+전회차완료 감지로 체크 목록에서 자동 제거된 작품 목록."""
    if not removed_titles:
        return ''
    total = len(removed_titles)
    lines = [f'[카카오페이지] 완결+전회차 완료 — 체크 목록에서 자동 제거 {total}개']
    for t in removed_titles:
        lines.append(f'- {t}')
    return '\n'.join(lines)
