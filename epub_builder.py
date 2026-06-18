"""소설 .txt 파일들을 하나의 epub2 으로 합치는 빌더 (stdlib only)."""
import html
import os
import re
import zipfile
from typing import List, Tuple

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


def _uid(title: str) -> str:
    return 'kaka-pe-dl-' + re.sub(r'[^a-z0-9]', '-', title.lower())


def _ch_id(i: int) -> str:
    return f'ch{i:04d}'


def _ch_file(i: int) -> str:
    return f'ch{i:04d}.xhtml'


def _chapter_xhtml(title: str, paragraphs: List[str]) -> str:
    esc = html.escape(title)
    body = '\n'.join(f'<p>{html.escape(p)}</p>' for p in paragraphs if p.strip())
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f'<head><meta charset="utf-8"/><title>{esc}</title></head>\n'
        '<body>\n'
        f'<h1>{esc}</h1>\n'
        f'{body}\n'
        '</body>\n'
        '</html>'
    )


def _content_opf(title: str, chapters: List[Tuple[int, str]]) -> str:
    esc = html.escape(title)
    uid = _uid(title)
    manifest = '\n'.join(
        f'    <item id="{_ch_id(i)}" href="{_ch_file(i)}"'
        ' media-type="application/xhtml+xml"/>'
        for i, _ in chapters
    )
    spine = '\n'.join(
        f'    <itemref idref="{_ch_id(i)}"/>'
        for i, _ in chapters
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"'
        ' unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:title>{esc}</dc:title>\n'
        '    <dc:language>ko</dc:language>\n'
        f'    <dc:identifier id="uid">{uid}</dc:identifier>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="ncx" href="toc.ncx"'
        ' media-type="application/x-dtbncx+xml"/>\n'
        f'{manifest}\n'
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        f'{spine}\n'
        '  </spine>\n'
        '</package>'
    )


def _toc_ncx(title: str, chapters: List[Tuple[int, str]]) -> str:
    esc = html.escape(title)
    uid = _uid(title)
    nav_points = []
    for idx, (i, ch_title) in enumerate(chapters, 1):
        nav_points.append(
            f'  <navPoint id="nav{idx}" playOrder="{idx}">\n'
            f'    <navLabel><text>{html.escape(ch_title)}</text></navLabel>\n'
            f'    <content src="{_ch_file(i)}"/>\n'
            '  </navPoint>'
        )
    nav_block = '\n'.join(nav_points)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"'
        ' "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
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


def build_epub(series_dir: str, series_title: str) -> str:
    """series_dir 안의 .txt 파일들을 모아 합본 epub 을 만든다.

    출력: series_dir/{series_title}.epub
    반환: 생성된 epub 절대 경로
    """
    txts = sorted(f for f in os.listdir(series_dir) if f.endswith('.txt'))
    if not txts:
        raise ValueError(f'no .txt files in {series_dir}')

    chapters: List[Tuple[int, str]] = []
    contents = []
    for i, fname in enumerate(txts):
        # NNNN_제목.txt → 제목 (앞의 숫자+언더스코어 제거, .txt 제거)
        ch_title = re.sub(r'^\d+_', '', fname[:-4])
        path = os.path.join(series_dir, fname)
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
        paragraphs = [p for p in text.split('\n\n') if p.strip()]
        chapters.append((i, ch_title))
        contents.append((ch_title, paragraphs))

    out_path = os.path.join(series_dir, series_title + '.epub')
    tmp_path = out_path + '.tmp'

    with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype: 반드시 첫 번째, 비압축
        mi = zipfile.ZipInfo('mimetype')
        mi.compress_type = zipfile.ZIP_STORED
        zf.writestr(mi, _MIMETYPE)
        zf.writestr('META-INF/container.xml', _CONTAINER_XML)
        zf.writestr('OEBPS/content.opf', _content_opf(series_title, chapters))
        zf.writestr('OEBPS/toc.ncx', _toc_ncx(series_title, chapters))
        for i, (ch_title, paragraphs) in enumerate(contents):
            zf.writestr(f'OEBPS/{_ch_file(i)}', _chapter_xhtml(ch_title, paragraphs))

    os.replace(tmp_path, out_path)
    return out_path
