#!/usr/bin/env python3
"""Create a new Wrike delivery copy with only the requested report columns.

Offline; Python standard library only. Reads the latest SIMPLE.xlsx by default.
Edits four report tabs. Keeps the frozen snapshot, guide and supporting sheets.
"""
import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
ET.register_namespace('', NS)
ET.register_namespace('r', REL)
BLOCK = 1024 * 1024
PROJECTS = ('Accessible Projects', 'Missing Projects')
TARGETS = PROJECTS + ('Reconciliation', 'Extra Project Effort')


def q(name):
    return '{' + NS + '}' + name


def sha_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(BLOCK), b''):
            h.update(block)
    return h.hexdigest()


def cell_column(reference):
    match = re.fullmatch(r'([A-Z]+)([0-9]+)', reference)
    if not match:
        raise ValueError('Unexpected cell address: ' + reference)
    return match.group(1), int(match.group(2))


def text_cell(cell, text):
    for child in list(cell):
        cell.remove(child)
    cell.set('t', 'inlineStr')
    ET.SubElement(ET.SubElement(cell, q('is')), q('t')).text = text


def header_values(roots, archive):
    needed = set()
    for root in roots.values():
        for cell in root.findall('./' + q('sheetData') + '/' + q('row') + '[@r="5"]/' + q('c')):
            if cell.get('t') == 's':
                needed.add(int(cell.find(q('v')).text))
    strings = {}
    if needed:
        with archive.open('xl/sharedStrings.xml') as stream:
            index = 0
            for event, node in ET.iterparse(stream, events=('end',)):
                if node.tag == q('si'):
                    if index in needed:
                        strings[index] = ''.join(t.text or '' for t in node.iter(q('t')))
                    index += 1
                    node.clear()
    result = {}
    for name, root in roots.items():
        row = root.find('./' + q('sheetData') + '/' + q('row') + '[@r="5"]')
        if row is None:
            raise ValueError('Column header row 5 not found in ' + name)
        values = {}
        for cell in row.findall(q('c')):
            col, _ = cell_column(cell.get('r'))
            if cell.get('t') == 's':
                values[col] = strings[int(cell.find(q('v')).text)]
            else:
                values[col] = ''.join(t.text or '' for t in cell.iter(q('t')))
        result[name] = values
    return result


def validate_headers(values):
    for name in PROJECTS:
        expected = {'A': 'Project ID', 'B': 'Project name', 'E': 'Planned effort hours', 'N': 'Planned effort minutes'}
        if any(values[name].get(k) != v for k, v in expected.items()):
            raise ValueError('Unexpected source columns in ' + name + '. Use the original SIMPLE.xlsx.')
    expected = {'A': 'Measure', 'B': 'Unique tasks', 'C': 'Planned effort hours',
                'D': 'Unknown effort tasks', 'E': 'Ambiguous rows'}
    if any(values['Reconciliation'].get(k) != v for k, v in expected.items()):
        raise ValueError('Unexpected Reconciliation columns. Use the original SIMPLE.xlsx.')
    expected = {'A': 'Project id', 'B': 'Project name', 'E': 'Known current subtotal hours'}
    if any(values['Extra Project Effort'].get(k) != v for k, v in expected.items()):
        raise ValueError('Unexpected Extra Project Effort columns. Use the original SIMPLE.xlsx.')


def trim_sheet(name, root):
    mapping = {'A': 'A', 'B': 'B', 'C': 'C'} if name == 'Reconciliation' else {'A': 'A', 'B': 'B', 'E': 'C'}
    sheet_data = root.find(q('sheetData'))
    data_rows = 0
    for row in sheet_data:
        row_number = int(row.get('r'))
        cells = {cell_column(c.get('r'))[0]: c for c in row.findall(q('c'))}
        if row_number >= 6 and name in PROJECTS and 'E' in cells:
            formula = cells['E'].find(q('f'))
            if formula is not None:
                if formula.text != 'N' + str(row_number) + '/60':
                    raise ValueError('Unexpected effort formula at ' + name + '!E' + str(row_number))
                minute_formula = cells.get('N').find(q('f')) if 'N' in cells else None
                expression = minute_formula.text if minute_formula is not None else ''
                if not re.fullmatch(r"(?:0|SUM\('Project Task Links'!C\d+:C\d+\))", expression):
                    raise ValueError('Unexpected minute calculation at ' + name + '!N' + str(row_number))
                # Preserve editable calculations after removing the helper column.
                formula.text = '(' + expression + ')/60'
        if row_number >= 6 and name == 'Reconciliation' and 'C' in cells:
            formula = cells['C'].find(q('f'))
            if formula is not None and formula.text:
                for project in PROJECTS:
                    formula.text = re.sub("'" + re.escape(project) + r"'!E(\d+):E(\d+)",
                                          lambda m, p=project: "'" + p + "'!C" + m.group(1) + ':C' + m.group(2), formula.text)
        for cell in list(row.findall(q('c'))):
            old_column, number = cell_column(cell.get('r'))
            if old_column not in mapping:
                row.remove(cell)
            else:
                cell.set('r', mapping[old_column] + str(number))
        row.attrib.pop('spans', None)
        if row_number >= 6 and 'A' in cells and name != 'Reconciliation':
            data_rows += 1
        if row_number == 5 and name != 'Reconciliation':
            new_cells = {cell_column(c.get('r'))[0]: c for c in row.findall(q('c'))}
            for column, label in [('A', 'Project ID'), ('B', 'Project name'),
                                  ('C', 'Current recorded effort hours' if name == 'Extra Project Effort' else 'Effort hours')]:
                text_cell(new_cells[column], label)
        if row_number == 3 and name == 'Extra Project Effort' and 'A' in cells:
            text_cell(cells['A'], 'Later Wrike capture. Recorded numeric effort only; tasks without numeric effort are excluded. Separate from snapshot totals.')
    dimension = root.find(q('dimension'))
    if dimension is not None:
        last_row = max(int(r.get('r')) for r in sheet_data)
        dimension.set('ref', 'A1:C' + str(last_row))
    columns = root.find(q('cols'))
    if columns is not None:
        old_columns = list(columns)
        for node in old_columns:
            columns.remove(node)
        for old, new in mapping.items():
            old_index = ord(old) - ord('A') + 1
            definition = next((c for c in old_columns if int(c.get('min')) <= old_index <= int(c.get('max'))), None)
            node = copy.deepcopy(definition) if definition is not None else ET.Element(q('col'), {'width': '24', 'customWidth': '1'})
            new_index = str(ord(new) - ord('A') + 1)
            node.set('min', new_index)
            node.set('max', new_index)
            node.attrib.pop('hidden', None)
            if name == 'Extra Project Effort' and new == 'C':
                node.set('width', '31')
                node.set('customWidth', '1')
            columns.append(node)
    auto_filter = root.find(q('autoFilter'))
    if auto_filter is not None:
        auto_filter.set('ref', re.sub(r':[A-Z]+(\d+)$', r':C\1', auto_filter.get('ref')))
        for node in list(auto_filter):
            if node.tag == q('filterColumn'):
                old_letter = chr(int(node.get('colId')) + ord('A'))
                if old_letter not in mapping:
                    raise ValueError('An active filter uses a removed column in ' + name + '. Clear that filter first.')
                node.set('colId', str(ord(mapping[old_letter]) - ord('A')))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), data_rows


def select_source(base, explicit):
    if explicit:
        source = Path(explicit).expanduser().resolve()
    else:
        candidates = sorted(p for p in (base / 'output' / 'simple_workbook').glob('*/*_SIMPLE.xlsx')
                            if p.is_file() and not p.name.startswith(('~$', '._')))
        if not candidates:
            raise ValueError('No SIMPLE.xlsx found under output/simple_workbook. Finish make_wrike_simple.py first.')
        source = candidates[-1].resolve()
    if not source.is_file() or source.name.startswith(('~$', '._')) or not zipfile.is_zipfile(source):
        raise ValueError('Input is not a readable workbook: ' + str(source))
    return source


def make_copy(source, destination):
    source_sha = sha_file(source)
    expected, counts = {}, {}
    with zipfile.ZipFile(source) as original:
        members = original.infolist()
        if len({m.filename for m in members}) != len(members):
            raise ValueError('Duplicate workbook package entries.')
        wb = ET.fromstring(original.read('xl/workbook.xml'))
        tabs = wb.find(q('sheets'))
        relationships = ET.fromstring(original.read('xl/_rels/workbook.xml.rels'))
        targets = {r.get('Id'): r.get('Target') for r in relationships}
        paths = {}
        for tab in tabs:
            if tab.get('name') in TARGETS:
                target = targets[tab.get('{' + REL + '}id')]
                paths[tab.get('name')] = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        if set(paths) != set(TARGETS):
            raise ValueError('The four required report tabs were not found.')
        roots = {}
        for name, path in paths.items():
            if original.getinfo(path).file_size > 64 * 1024 ** 2:
                raise ValueError('Unexpectedly large report tab: ' + name)
            roots[name] = ET.fromstring(original.read(path))
        validate_headers(header_values(roots, original))
        overrides = {}
        for name in TARGETS:
            overrides[paths[name]], counts[name] = trim_sheet(name, roots[name])
        for defined in wb.findall('./' + q('definedNames') + '/' + q('definedName')):
            if defined.get('name') == '_xlnm._FilterDatabase':
                scope = defined.get('localSheetId')
                if scope is not None and tabs[int(scope)].get('name') in TARGETS:
                    defined.text = re.sub(r':\$[A-Z]+(\$\d+)$', r':$C\1', defined.text or '')
        overrides['xl/workbook.xml'] = ET.tostring(wb, encoding='utf-8', xml_declaration=True)
        with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as output:
            for item in members:
                digest = hashlib.sha256()
                with output.open(item.filename, 'w', force_zip64=True) as target:
                    if item.filename in overrides:
                        content = overrides[item.filename]
                        target.write(content)
                        digest.update(content)
                    else:
                        with original.open(item) as stream:
                            for block in iter(lambda: stream.read(BLOCK), b''):
                                target.write(block)
                                digest.update(block)
                expected[item.filename] = digest.hexdigest()
    print('Checking saved workbook...', flush=True)
    with zipfile.ZipFile(destination) as saved:
        if set(saved.namelist()) != set(expected):
            raise ValueError('Workbook parts changed unexpectedly.')
        for member, digest in expected.items():
            h = hashlib.sha256()
            with saved.open(member) as stream:
                for block in iter(lambda: stream.read(BLOCK), b''):
                    h.update(block)
            if h.hexdigest() != digest:
                raise ValueError('Saved workbook verification failed: ' + member)
    if sha_file(source) != source_sha:
        raise ValueError('Source workbook changed during copying. Close it and retry.')
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'source_file': str(source),
            'source_sha256': source_sha, 'output_file': str(destination), 'output_sha256': sha_file(destination),
            'edited_tabs': list(TARGETS), 'project_rows_preserved': {k: v for k, v in counts.items() if k != 'Reconciliation'},
            'columns': {'Accessible Projects': ['Project ID', 'Project name', 'Effort hours'],
                        'Missing Projects': ['Project ID', 'Project name', 'Effort hours'],
                        'Reconciliation': ['Measure', 'Unique tasks', 'Planned effort hours'],
                        'Extra Project Effort': ['Project ID', 'Project name', 'Current recorded effort hours']},
            'extra_effort_definition': 'Existing known numeric subtotal, not a complete all-task total. Missing effort remains unknown.',
            'unchanged': 'Every worksheet except the four named tabs; all styles and supporting data. Source workbook unchanged.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', nargs='?', help='Optional explicit SIMPLE.xlsx path')
    args = parser.parse_args()
    base = Path.cwd()
    if not (base / 'output' / 'simple_workbook').exists() and (Path(__file__).resolve().parent / 'output' / 'simple_workbook').exists():
        base = Path(__file__).resolve().parent
    temporary = None
    try:
        source = select_source(base, args.workbook)
        print('Using workbook: ' + str(source), flush=True)
        if shutil.disk_usage(base).free < max(1024 ** 3, source.stat().st_size * 5):
            raise ValueError('At least 1 GB of free space is needed for a new copy.')
        folder = base / 'output' / 'concise_workbook' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        folder.mkdir(parents=True, exist_ok=False)
        stem = source.stem[:-7] if source.stem.endswith('_SIMPLE') else source.stem
        destination = folder / (stem + '_CONCISE.xlsx')
        temporary = folder / (stem + '_CONCISE.building')
        receipt = make_copy(source, temporary)
        temporary.rename(destination)
        receipt['output_file'] = str(destination)
        (folder / 'column_edit_receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        print('DONE: requested columns updated; all other tabs preserved.')
        print('NEW FILE: ' + str(destination))
    except Exception as error:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        print('STOPPED: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
