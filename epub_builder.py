"""소설 .txt 파일들을 하나의 epub2 으로 합치는 빌더 (stdlib only).

reading_info/text2epub.py 의 구현을 참고해 아래 기능을 적용:
- cover.jpg 표지 삽입
- CSS (한국 소설 본문 타이포그래피: p.basic-1)
- BOM / surrogate 제거 + cp949 인코딩 폴백
- 빈줄 처리 (연속 빈줄비율 조정)
- info.xml 에서 작가 메타데이터 추출 (stdlib xml.etree)
"""
import html
import os
import re
import zipfile
from typing import Dict, List, Optional, Tuple

_MIMETYPE = 'application/epub+zip'

_CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:schemas:container">\n'
    '  <rootfiles>\n'
    '    <rootfile full-path="OEBPS/content.opf"'
    ' media-type="application/oebps-package+xml"/>\n'
    '  </rootfiles>\n'
    '</container>'
)

_STYLESHEET = """\
p.basic-1 {
  font-size: 1.000em;
  line-height: 1.80em;
  text-align: justify;
  margin: 0em;
  text-indent: 1.00em;
}
p.basic-1 br { line-height: 1.80em; }
"""

# 빈줄 N개마다 <br/> 1개 삽입 (레퍼런스의 EPUB_빈줄비율과 동일 개념)
_BLANK_LINE_RATIO = 2


def _uid(title: str) -> str:
    return 'kaka-pe-dl-' + re.sub(r'[^a-z0-9]', '-', title.lower())


def _ch_id(i: int) -> str:
    return f'ch{i:04d}'


def _ch_file(i: int) -> str:
    return f'ch{i:04d}.xhtml'


# ---- 텍스트 파일 읽기 (인코딩 강건화) ----

def _read_txt(path: str) -> str:
    """BOM / lone-surrogate 제거 + 다중 인코딩 폴백으로 텍스트 읽기."""
    with open(path, 'rb') as f:
        raw = f.read()
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    # UTF-8 규격 위반 lone surrogate (0xED A0–BF 80–BF) 제거
    cleaned = re.sub(rb'\xed[\xa0-\xbf][\x80-\xbf]', b'', raw)
    for enc in ('utf-8', 'cp949', 'euc-kr', 'utf-16'):
        try:
            return cleaned.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return cleaned.decode('utf-8', errors='replace')


# ---- info.xml 메타데이터 ----

def _read_info_xml(series_dir: str) -> Dict[str, str]:
    """series_dir/info.xml (ComicInfo) → {title, author}. 실패 시 빈 dict."""
    path = os.path.join(series_dir, 'info.xml')
    if not os.path.isfile(path):
        return {}
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(path).getroot()
        def _get(tag: str) -> str:
            el = root.find(tag)
            return (el.text or '').strip() if el is not None else ''
        return {'title': _get('Title'), 'author': _get('Writer')}
    except Exception:
        return {}


# ---- 텍스트 → HTML 단락 변환 ----

def _text_to_html_paras(text: str) -> List[str]:
    """텍스트를 <p class="basic-1"> 리스트로 변환.

    - 단락 구분: \n\n (worker.py 저장 포맷과 동일)
    - 단락 내 개행: <br/>
    - 빈 단락: _BLANK_LINE_RATIO 개마다 <br/> 1개
    """
    parts = text.split('\n\n')
    result: List[str] = []
    blank_run = 0
    for part in parts:
        stripped = part.strip()
        if not stripped:
            blank_run += 1
            if blank_run % _BLANK_LINE_RATIO == 0:
                result.append('<p class="basic-1"><br/></p>')
            continue
        blank_run = 0
        lines = [html.escape(l) for l in stripped.splitlines() if l.strip()]
        if lines:
            result.append(f'<p class="basic-1">{"<br/>".join(lines)}</p>')
    return result


# ---- epub 구성 요소 빌더 ----

def _chapter_xhtml(ch_title: str, paras: List[str], series_title: str = '') -> str:
    esc = html.escape(ch_title)
    body = '\n'.join(paras)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
        '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head>\n'
        f'  <meta content="{html.escape(series_title)}" name="DC.Title"/>\n'
        '  <link href="../Styles/stylesheet.css" rel="stylesheet" type="text/css"/>\n'
        f'  <title>{esc}</title>\n'
        '</head>\n'
        '<body>\n'
        f'<p class="basic-1"><br/></p>\n'
        f'<h2>{esc}</h2>\n'
        f'{body}\n'
        '<p class="basic-1"><br/></p>\n'
        '</body>\n'
        '</html>'
    )


def _cover_xhtml(title: str) -> str:
    esc = html.escape(title)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
        '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head>\n'
        '  <link href="../Styles/stylesheet.css" rel="stylesheet" type="text/css"/>\n'
        f'  <title>{esc}</title>\n'
        '</head>\n'
        '<body>\n'
        '<div style="text-align:center">'
        '<img alt="" style="width:100%" src="../Images/cover.jpg"/>'
        '</div>\n'
        '</body>\n'
        '</html>'
    )


def _content_opf(title: str, author: str,
                 chapters: List[Tuple[int, str]],
                 has_cover: bool) -> str:
    esc_title = html.escape(title)
    esc_author = html.escape(author) if author else ''
    uid = _uid(title)
    author_el = f'    <dc:creator opf:role="aut">{esc_author}</dc:creator>\n' if esc_author else ''
    cover_manifest = (
        '    <item id="cover.xhtml" href="Text/cover.xhtml"'
        ' media-type="application/xhtml+xml"/>\n'
        '    <item id="cover.jpg" href="Images/cover.jpg" media-type="image/jpeg"/>\n'
    ) if has_cover else ''
    cover_spine = '    <itemref idref="cover.xhtml"/>\n' if has_cover else ''
    cover_guide = (
        '  <guide>\n'
        '    <reference type="cover" title="표지" href="Text/cover.xhtml"/>\n'
        '  </guide>\n'
    ) if has_cover else ''
    ch_manifest = '\n'.join(
        f'    <item id="{_ch_id(i)}" href="Text/{_ch_file(i)}"'
        ' media-type="application/xhtml+xml"/>'
        for i, _ in chapters
    )
    ch_spine = '\n'.join(
        f'    <itemref idref="{_ch_id(i)}"/>'
        for i, _ in chapters
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package version="2.0" unique-identifier="BookId"'
        ' xmlns="http://www.idpf.org/2007/opf">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f'    <dc:title>{esc_title}</dc:title>\n'
        f'{author_el}'
        '    <dc:language>ko</dc:language>\n'
        f'    <dc:identifier id="BookId">{uid}</dc:identifier>\n'
        + ('    <meta name="cover" content="cover.jpg"/>\n' if has_cover else '')
        + '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="stylesheet.css" href="Styles/stylesheet.css"'
        ' media-type="text/css"/>\n'
        + cover_manifest
        + f'{ch_manifest}\n'
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        + cover_spine
        + f'{ch_spine}\n'
        '  </spine>\n'
        + cover_guide
        + '</package>'
    )


def _toc_ncx(title: str, chapters: List[Tuple[int, str]]) -> str:
    esc = html.escape(title)
    uid = _uid(title)
    nav_points = []
    for idx, (i, ch_title) in enumerate(chapters, 1):
        nav_points.append(
            f'  <navPoint id="nav{idx}" playOrder="{idx}">\n'
            f'    <navLabel><text>{html.escape(ch_title)}</text></navLabel>\n'
            f'    <content src="Text/{_ch_file(i)}"/>\n'
            '  </navPoint>'
        )
    nav_block = '\n'.join(nav_points)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"\n'
        '  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '  <head>\n'
        f'    <meta name="dtb:uid" content="{uid}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        '  </head>\n'
        f'  <docTitle><text>{esc}</text></docTitle>\n'
        '  <navMap>\n'
        f'{nav_block}\n'
        '  </navMap>\n'
        '</ncx>'
    )


# ---- 공개 API ----

def build_epub(series_dir: str, series_title: str) -> str:
    """series_dir 안의 .txt 파일들을 모아 합본 epub 을 만든다.

    출력: series_dir/{series_title}.epub
    반환: 생성된 epub 절대 경로
    """
    txts = sorted(f for f in os.listdir(series_dir) if f.endswith('.txt'))
    if not txts:
        raise ValueError(f'no .txt files in {series_dir}')

    meta = _read_info_xml(series_dir)
    author = meta.get('author') or ''

    cover_path = os.path.join(series_dir, 'cover.jpg')
    has_cover = os.path.isfile(cover_path)

    chapters: List[Tuple[int, str]] = []
    contents = []
    for i, fname in enumerate(txts):
        # NNNN_제목.txt → 제목
        ch_title = re.sub(r'^\d+_', '', fname[:-4])
        text = _read_txt(os.path.join(series_dir, fname))
        paras = _text_to_html_paras(text)
        chapters.append((i, ch_title))
        contents.append((ch_title, paras))

    out_path = os.path.join(series_dir, series_title + '.epub')
    tmp_path = out_path + '.tmp'

    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype: 반드시 첫 번째, 비압축
        mi = zipfile.ZipInfo('mimetype')
        mi.compress_type = zipfile.ZIP_STORED
        zf.writestr(mi, _MIMETYPE)

        zf.writestr('META-INF/container.xml', _CONTAINER_XML)
        zf.writestr('OEBPS/Styles/stylesheet.css', _STYLESHEET)
        zf.writestr('OEBPS/content.opf',
                    _content_opf(series_title, author, chapters, has_cover))
        zf.writestr('OEBPS/toc.ncx', _toc_ncx(series_title, chapters))

        if has_cover:
            with open(cover_path, 'rb') as f:
                zf.writestr('OEBPS/Images/cover.jpg', f.read())
            zf.writestr('OEBPS/Text/cover.xhtml', _cover_xhtml(series_title))

        for i, (ch_title, paras) in enumerate(contents):
            zf.writestr(f'OEBPS/Text/{_ch_file(i)}',
                        _chapter_xhtml(ch_title, paras, series_title))

    os.replace(tmp_path, out_path)
    return out_path
