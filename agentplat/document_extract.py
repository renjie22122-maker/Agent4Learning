"""Bounded local document extraction. No document is uploaded to an external model."""
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
import tempfile

TEXT = {'.txt', '.md', '.rst', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.html', '.xml', '.log', '.sql'}
IMAGES = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
SUPPORTED = TEXT | IMAGES | {'.csv', '.tsv', '.docx', '.xlsx', '.pptx', '.pdf'}
MAX_TEXT = 4_000_000


def decode(raw):
    for encoding in (('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')):
        try:
            text = raw.decode(encoding)
            if '\x00' not in text:
                return text
        except UnicodeError:
            pass
    return raw.decode('utf-8', 'replace')


def ocr(path):
    if shutil.which('tesseract'):
        command = ['tesseract', str(path), 'stdout']
    elif os.name == 'nt':
        policy_path = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'ocr-policy.json'
        policy = json.loads(policy_path.read_text(encoding='utf-8')) if policy_path.exists() else {}
        command = ['powershell.exe', '-NoProfile', '-NonInteractive']
        if policy.get('allow_unsigned_local_ocr_script') is True:
            command += ['-ExecutionPolicy', 'Bypass']  # explicit host approval, child process only
        command += ['-File', str(Path(__file__).with_name('ocr_windows.ps1')), '-ImagePath', str(path)]
    else:
        raise RuntimeError('图片 OCR 需要 Windows OCR 语言包或本机 Tesseract')
    # Own the entire OCR subprocess tree; timeout cannot leave an OCR worker running.
    from .processes import ProcessSupervisor
    supervisor = ProcessSupervisor(max_output_bytes=MAX_TEXT)
    try:
        task = supervisor.start(command, path.parent, timeout_s=30,
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        result = supervisor.wait(task, 40)
        if result['status'] != 'exited' or result['exit_code'] != 0:
            raise RuntimeError('OCR 失败：' + result['output'][-1200:])
        text = result['output'].strip()
        return text
    finally:
        supervisor.close()


def office_zip(path):
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    if len(infos) > 10000 or sum(i.file_size for i in infos) > 100_000_000 or any(i.file_size > 20_000_000 for i in infos):
        archive.close()
        raise ValueError('Office 压缩包展开体积超限')
    return archive


def xml_part(archive, name):
    raw = archive.read(name)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('拒绝含 DTD/实体声明的文档')
    return ET.fromstring(raw)


def extract(path):
    path = Path(path).resolve()
    if path.stat().st_size > 25_000_000:
        raise ValueError('单文件超过 25 MB')
    suffix = path.suffix.lower()
    segments, warnings = [], []
    total_text = 0
    def add(text, location):
        nonlocal total_text
        text = text.strip()
        if text:
            segments.append({'text': text, 'location': location})
            total_text += len(text)
        if total_text > MAX_TEXT:
            raise ValueError('提取文本超过 400 万字符')
    if suffix in TEXT:
        lines = decode(path.read_bytes()).splitlines()
        for i in range(0, len(lines), 40):
            add('\n'.join(lines[i:i+40]), f'lines {i+1}-{min(i+40,len(lines))}')
    elif suffix in {'.csv', '.tsv'}:
        reader = csv.reader(io.StringIO(decode(path.read_bytes())), delimiter='\t' if suffix == '.tsv' else ',')
        header = next(reader, [])
        for i, row in enumerate(reader, 2):
            add(' | '.join(f'{header[n] if n < len(header) else n+1}: {value}' for n, value in enumerate(row)), f'row {i}')
        if not segments:
            add(' | '.join(header), 'row 1')
    elif suffix == '.docx':
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with office_zip(path) as archive:
            root = xml_part(archive, 'word/document.xml')
            for i, paragraph in enumerate(root.findall('.//w:p', ns), 1):
                add(''.join(n.text or '' for n in paragraph.findall('.//w:t', ns)), f'paragraph {i}')
            if any(n.startswith('word/media/') for n in archive.namelist()):
                media = [n for n in archive.namelist() if n.startswith('word/media/') and Path(n).suffix.lower() in IMAGES]
                for name in media[:8]:
                    try:
                        with tempfile.TemporaryDirectory(prefix='office-ocr-') as td:
                            picture = Path(td)/Path(name).name; picture.write_bytes(archive.read(name))
                            add(ocr(picture), 'embedded image ' + Path(name).name)
                    except Exception as exc: warnings.append(f'内嵌图片 {Path(name).name} OCR 失败：{exc}')
                if len(media)>8: warnings.append('内嵌图片超过 8 张，仅识别前 8 张')
    elif suffix == '.xlsx':
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with office_zip(path) as archive:
            shared = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                shared = [''.join(n.text or '' for n in item.findall('.//s:t', ns)) for item in xml_part(archive, 'xl/sharedStrings.xml').findall('s:si', ns)]
            for name in sorted(n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
                sheet = Path(name).stem
                for row in xml_part(archive, name).findall('.//s:row', ns):
                    values = []
                    for cell in row.findall('s:c', ns):
                        v = cell.find('s:v', ns)
                        value = v.text if v is not None and v.text else ''
                        if cell.get('t') == 's' and value:
                            value = shared[int(value)]
                        elif cell.get('t') == 'inlineStr':
                            value = ''.join(n.text or '' for n in cell.findall('.//s:t', ns))
                        formula = cell.find('s:f', ns)
                        if formula is not None:
                            value += ' [formula: ' + (formula.text or '') + ']'
                        values.append(cell.get('r', '?') + ': ' + value)
                    add(' | '.join(values), f'{sheet} row {row.get("r", "?")}')
            warnings.append('读取已有单元格值与公式，不执行宏、不重新计算公式')
    elif suffix == '.pptx':
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        with office_zip(path) as archive:
            for name in sorted(n for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')):
                add('\n'.join(n.text or '' for n in xml_part(archive, name).findall('.//a:t', ns)), Path(name).stem)
        warnings.append('提取幻灯片文字，不解释图形与内嵌图片')
    elif suffix == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError('PDF 提取需要本机 pypdf') from None
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('加密 PDF 需要先由用户解密')
        if len(reader.pages) > 1000:
            raise ValueError('PDF 超过 1000 页')
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if not text.strip():
                try:
                    images = list(page.images)
                    if len(images)>8: warnings.append(f'第 {i} 页图片超过 8 张，仅识别前 8 张')
                    for index, embedded in enumerate(images[:8], 1):
                        with tempfile.TemporaryDirectory(prefix='pdf-ocr-') as td:
                            picture = Path(td)/'scan.png'; embedded.image.save(picture)
                            add(ocr(picture), f'page {i} image {index} OCR')
                    if not images: warnings.append(f'第 {i} 页没有文本或可识别图片；尚不支持整页矢量渲染 OCR')
                except Exception as exc: warnings.append(f'第 {i} 页扫描图片 OCR 失败：{exc}')
            add(text, f'page {i}')
    elif suffix in IMAGES:
        add(ocr(path), 'image OCR')
        warnings.append('图片仅做 OCR；不推断图表数值、物体或场景，OCR 可能有误')
        if not segments:
            warnings.append('未识别到文字；原件可查看，但没有可检索分块')
    else:
        raise ValueError(f'不支持的文件类型：{suffix}')
    if not segments and suffix not in IMAGES:
        raise ValueError('未提取到可检索文本。' + '；'.join(warnings[:10]))
    return {'segments': segments, 'warnings': warnings, 'kind': suffix.lstrip('.')}


if __name__ == '__main__':
    # Parent owns timeout and output size. Result file avoids truncating large extraction.
    try:
        result = extract(sys.argv[1])
    except Exception as exc:
        result = {'error': f'{type(exc).__name__}: {exc}'}
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
