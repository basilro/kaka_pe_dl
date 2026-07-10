"""소설 .txt → epub2 빌더 (stdlib only).

두 가지 모드:
- build_epub(): 시리즈의 모든 .txt 를 하나의 합본 epub 으로.
- build_epub_per_txt(): .txt 회차마다 개별 epub 하나씩.
"""
import html
import os
import re
import zipfile
from typing import Dict, List, Optional, Tuple

_MIMETYPE = 'application/epub+zip'

_CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0"'
    ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
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

_BLANK_LINE_RATIO = 2


def _uid(title: str) -> str:
    return 'kaka-pe-dl-' + re.sub(r'[^a-z0-9]', '-', title.lower())


def _ch_id(i: int) -> str:
    return f'ch{i:04d}'


def _ch_file(i: int) -> str:
    return f'ch{i:04d}.xhtml'


def _read_txt(path: str) -> str:
    """BOM / lone-surrogate 제거 + 다중 인코딩 폴백으로 텍스트 읽기."""
    with open(path, 'rb') as f:
        raw = f.read()
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    cleaned = re.sub(rb'\xed[\xa0-\xbf][\x80-\xbf]', b'', raw)
    for enc in ('utf-8', 'cp949', 'euc-kr', 'utf-16'):
        try:
            return cleaned.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return cleaned.decode('utf-8', errors='replace')


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


def _text_to_html_paras(text: str) -> List[str]:
    """텍스트를 <p class="basic-1"> 리스트로 변환."""
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


def _jpeg_size(path: str) -> Optional[Tuple[int, int]]:
    """JPEG 파일의 (width, height) 를 stdlib 만으로 파싱. 실패 시 None."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if not data.startswith(b'\xff\xd8'):
            return None
        i = 2
        n = len(data)
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in sof:
                h = (data[i + 5] << 8) + data[i + 6]
                w = (data[i + 7] << 8) + data[i + 8]
                if w > 0 and h > 0:
                    return w, h
                return None
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = (data[i + 2] << 8) + data[i + 3]
            if seg_len < 2:
                return None
            i += 2 + seg_len
        return None
    except Exception:
        return None


def _cover_xhtml(title: str, size: Optional[Tuple[int, int]]) -> str:
    esc = html.escape(title)
    if size:
        w, h = size
        img = (
            '<svg xmlns="http://www.w3.org/2000/svg"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1"'
            ' width="100%" height="100%"'
            f' viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet">\n'
            f'  <image width="{w}" height="{h}"'
            ' xlink:href="../Images/cover.jpg"/>\n'
            '</svg>'
        )
    else:
        img = ('<img alt="" style="max-width:100%;max-height:100%;"'
               ' src="../Images/cover.jpg"/>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
        '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head>\n'
        '  <link href="../Styles/stylesheet.css" rel="stylesheet" type="text/css"/>\n'
        f'  <title>{esc}</title>\n'
        '  <style type="text/css">\n'
        '    html,body{margin:0;padding:0;height:100%;text-align:center;}\n'
        '  </style>\n'
        '</head>\n'
        '<body>\n'
        f'{img}\n'
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


def _write_epub(out_path: str, book_title: str, author: str,
                cover_path: str, has_cover: bool,
                chapters: List[Tuple[int, str]],
                contents: List[Tuple[str, List[str]]]) -> str:
    """준비된 chapters/contents 로 하나의 epub 파일을 실제로 쓴다.

    chapters[k][0] 는 contents[k] 의 챕터 인덱스와 일치해야 한다(0..n-1).
    """
    tmp_path = out_path + '.tmp'

    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        mi = zipfile.ZipInfo('mimetype')
        mi.compress_type = zipfile.ZIP_STORED
        zf.writestr(mi, _MIMETYPE)

        zf.writestr('META-INF/container.xml', _CONTAINER_XML)
        zf.writestr('OEBPS/Styles/stylesheet.css', _STYLESHEET)
        zf.writestr('OEBPS/content.opf',
                    _content_opf(book_title, author, chapters, has_cover))
        zf.writestr('OEBPS/toc.ncx', _toc_ncx(book_title, chapters))

        if has_cover:
            with open(cover_path, 'rb') as f:
                zf.writestr('OEBPS/Images/cover.jpg', f.read())
            zf.writestr('OEBPS/Text/cover.xhtml',
                        _cover_xhtml(book_title, _jpeg_size(cover_path)))

        for i, (ch_title, paras) in enumerate(contents):
            zf.writestr(f'OEBPS/Text/{_ch_file(i)}',
                        _chapter_xhtml(ch_title, paras, book_title))

    os.replace(tmp_path, out_path)
    return out_path


def build_epub(series_dir: str, series_title: str) -> str:
    """series_dir 안의 .txt 파일들을 모아 합본 epub 을 만든다."""
    txts = sorted(f for f in os.listdir(series_dir) if f.endswith('.txt'))
    if not txts:
        raise ValueError(f'no .txt files in {series_dir}')

    meta = _read_info_xml(series_dir)
    author = meta.get('author') or ''

    cover_path = os.path.join(series_dir, 'cover.jpg')
    has_cover = os.path.isfile(cover_path)

    chapters: List[Tuple[int, str]] = []
    contents: List[Tuple[str, List[str]]] = []
    for i, fname in enumerate(txts):
        ch_title = re.sub(r'^\d+_', '', fname[:-4])
        text = _read_txt(os.path.join(series_dir, fname))
        paras = _text_to_html_paras(text)
        chapters.append((i, ch_title))
        contents.append((ch_title, paras))

    out_path = os.path.join(series_dir, series_title + '.epub')
    return _write_epub(out_path, series_title, author,
                       cover_path, has_cover, chapters, contents)


def build_epub_per_txt(series_dir: str, series_title: str) -> List[str]:
    """series_dir 안의 .txt 파일 각각을 개별 epub 으로 만든다.

    파일명은 원본 .txt 와 동일(확장자만 .epub), 책 제목은 회차 제목.
    생성된 epub 경로 리스트를 반환한다.
    """
    txts = sorted(f for f in os.listdir(series_dir) if f.endswith('.txt'))
    if not txts:
        raise ValueError(f'no .txt files in {series_dir}')

    meta = _read_info_xml(series_dir)
    author = meta.get('author') or ''

    cover_path = os.path.join(series_dir, 'cover.jpg')
    has_cover = os.path.isfile(cover_path)

    out_paths: List[str] = []
    for fname in txts:
        ch_title = re.sub(r'^\d+_', '', fname[:-4])
        text = _read_txt(os.path.join(series_dir, fname))
        paras = _text_to_html_paras(text)
        chapters: List[Tuple[int, str]] = [(0, ch_title)]
        contents: List[Tuple[str, List[str]]] = [(ch_title, paras)]
        out_path = os.path.join(series_dir, fname[:-4] + '.epub')
        _write_epub(out_path, ch_title, author,
                    cover_path, has_cover, chapters, contents)
        out_paths.append(out_path)
    return out_paths
