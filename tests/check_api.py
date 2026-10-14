"""Integration checks against a fresh disposable database; never seed the configured DB.

Run: .venv/Scripts/python.exe tests/check_api.py [--scale]
Uses only the application dependencies and Python's standard library.
"""
import concurrent.futures
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import random
import re
from pathlib import Path
from unittest.mock import MagicMock, patch
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bson import json_util
import yaml
import app.main as main

contract = yaml.safe_load((ROOT / 'openapi.yaml').read_text())

def validate_schema(value, schema):
    """Check the response fields/types against the supplied OpenAPI contract."""
    if '$ref' in schema:
        target = contract
        for key in schema['$ref'].removeprefix('#/').split('/'):
            target = target[key]
        return validate_schema(value, target)
    if value is None and schema.get('nullable'):
        return
    for subschema in schema.get('allOf', []):
        validate_schema(value, subschema)
    kind = schema.get('type')
    if kind == 'integer':
        assert type(value) is int, (schema, value)
    elif kind == 'number':
        assert type(value) in (int, float), (schema, value)
    elif kind == 'boolean':
        assert type(value) is bool, (schema, value)
    elif kind == 'string':
        assert isinstance(value, str), (schema, value)
        if schema.get('format') == 'date':
            assert date.fromisoformat(value).isoformat() == value
        if 'pattern' in schema:
            assert re.fullmatch(schema['pattern'], value)
    elif kind == 'array':
        assert isinstance(value, list), (schema, value)
        for item in value:
            validate_schema(item, schema['items'])
    elif kind == 'object':
        assert isinstance(value, dict), (schema, value)
        assert set(schema.get('required', [])) <= value.keys(), (schema, value)
        for key, subschema in schema.get('properties', {}).items():
            if key in value:
                validate_schema(value[key], subschema)
        additional = schema.get('additionalProperties')
        if isinstance(additional, dict):
            for key in value.keys() - schema.get('properties', {}).keys():
                validate_schema(value[key], additional)
    if 'enum' in schema:
        assert value in schema['enum'], (schema, value)
    if type(value) in (int, float):
        if 'minimum' in schema:
            assert value >= schema['minimum'], (schema, value)
        if 'maximum' in schema:
            assert value <= schema['maximum'], (schema, value)


def validate_response(path, method, status, body):
    path = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
    for template, operations in contract['paths'].items():
        pattern = re.sub(r'\{[^}]+\}', '[^/]+', template)
        if re.fullmatch(pattern, path) and method.lower() in operations:
            response = operations[method.lower()]['responses'].get(str(status))
            assert response is not None, ('unexpected status', path, status)
            if '$ref' in response:
                response = contract['components']['responses'][response['$ref'].split('/')[-1]]
            validate_schema(body, response['content']['application/json']['schema'])
            return
    raise AssertionError(('unrecognized operation', method, path))

passed = 0
def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print('PASS', label, flush=True)

def ms(day, clock):
    return int(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=main.IST).timestamp()) * 1000

def rounded(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))

def stages(value):
    found = []
    if isinstance(value, dict):
        if 'stage' in value:
            found.append(value['stage'])
        for child in value.values():
            found.extend(stages(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(stages(child))
    return found

name = 'check_' + uuid.uuid4().hex
test_db = main.client[name]
server = None
log = tempfile.TemporaryFile(mode='w+b')
started = time.monotonic()
try:
    main.client.admin.command('ping')
    unavailable = MagicMock()
    unavailable.admin.command.side_effect = RuntimeError('test outage')
    with patch.object(main, 'client', unavailable):
        try:
            main.health()
        except main.HTTPException as error:
            check('health reports database outage', error.status_code == 503)
        else:
            check('health reports database outage', False)
    print('Configured database counts:', {c: main.db[c].count_documents({}) for c in ('employees', 'attendance_logs')}, flush=True)
    for collection in ('employees', 'attendance_logs'):
        docs = json_util.loads((ROOT / 'sample_data' / (collection + '.json')).read_text())
        test_db[collection].insert_many(docs)
    extra = []
    for n, joined in [(1, '2026-01-01'), (2, '2026-01-01'), (3, '2026-07-15'), (4, '2026-08-01')]:
        extra.append({'emp_code': 'EMP900' + str(n), 'name': 'Check ' + str(n), 'email': 'check@example.com',
                      'department': 'Checks', 'joined_on': joined, 'shift_start': '09:30', 'shift_end': '18:30',
                      'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc)})
    extra.append({**extra[0], 'emp_code': 'EMP9005', 'department': 'Empty'})
    test_db.employees.insert_many(extra)
    test_db.attendance_logs.insert_many([
        {'emp_code': 'EMP9001', 'date': '2026-07-06', 'status': 'PRESENT', 'work_hours': 8.02,
         'late_minutes': 20, 'overtime_minutes': 30, 'half_day': False},
        {'emp_code': 'EMP9002', 'date': '2026-07-06', 'status': 'WFH', 'work_hours': 8.03,
         'late_minutes': 20, 'half_day': True},
        {'emp_code': 'EMP9001', 'date': '2026-07-11', 'status': 'ON_DUTY', 'work_hours': 6.0,
         'late_minutes': 15, 'half_day': False},
        {'emp_code': 'EMP9002', 'date': '2026-07-11', 'status': 'LEAVE', 'late_minutes': 15},
        {'emp_code': 'EMP999999', 'date': '2026-07-06', 'status': 'PRESENT', 'late_minutes': 10000},
    ])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'MONGO_DB': name}
    server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                              cwd=ROOT, env=env, stdout=log, stderr=log)
    def request(path, body=None, method=None):
        verb = method or ('POST' if body is not None else 'GET')
        req = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status, result = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, result = error.code, json.load(error)
        validate_response(path, verb, status, result)
        return status, result
    ready_start = time.monotonic()
    while time.monotonic() - ready_start < 20:
        try:
            if request('/health')[0] == 200:
                break
        except OSError:
            pass
        if server.poll() is not None:
            raise RuntimeError('API failed to start')
        time.sleep(.1)
    check('API ready within 20 seconds', request('/health')[0] == 200 and time.monotonic() - ready_start < 20)
    status, employees = request('/employees?department=Engineering')
    check('MongoDB fetch and filtered employee total', status == 200 and employees['total'] == 2 and len(employees['items']) == 2)
    check('employee instants serialized without internal ID', all(isinstance(e['created_at'], int) and '_id' not in e for e in employees['items']))
    status, attendance = request('/attendance?emp_code=EMP0001&page_size=1')
    check('attendance pagination and nested history milliseconds', status == 200 and attendance['total'] == 2 and len(attendance['items']) == 1
          and isinstance(attendance['items'][0]['history'][0]['changes']['punch_in']['from'], int))
    status, legacy = request('/attendance?emp_code=EMP0002')
    check('legacy defaults', legacy['items'][0]['history'] == [] and legacy['items'][0]['half_day'] is False)
    reason = {'reason': 'verification only', 'regularized_by': 'check'}
    check('legacy no-op correction rejected', request('/attendance/EMP0002/2026-07-14', reason, 'PATCH')[0] == 422)
    check('unknown correction record', request('/attendance/EMP0002/2026-07-01', reason, 'PATCH')[0] == 404)
    check('bad employee shift rejected', request('/employees', {'emp_code': 'EMP9020', 'name': 'Invalid', 'email': 'test@example.com',
          'department': 'Test', 'joined_on': '2026-01-01', 'shift_start': '09:30', 'shift_end': '09:30'})[0] == 422)
    for path in ['/employees?page=0', '/employees?page_size=101', '/attendance?status=BOGUS',
                 '/attendance?date_from=2026-07-01T00:00:00', '/attendance?date_to=1782864000',
                 '/admin/explain/attendance_list?date_from=2026-07-01T00:00:00',
                 '/analytics/departments/Checks/trend?from=1782864000&to=2026-07-01',
                 '/attendance?date_from=2026-07-10&date_to=2026-07-01', '/analytics/departments/summary?month=0000-01',
                 '/analytics/departments/summary?month=2026-13', '/analytics/leaderboard/late?month=2026-07&limit=0',
                 '/analytics/departments/Checks/trend?from=2026-07-01&to=2026-10-01',
                 '/analytics/departments/Checks/trend?from=2026-07-02&to=2026-07-01',
                 '/admin/explain/employee_monthly', '/admin/explain/department_trend', '/admin/explain/BOGUS']:
        status, body = request(path)
        check('validation ' + path, status == 422 and isinstance(body['detail'], list))
    for value in [1751767800, 1783312500000.0, '1783312500000', True, None, 4102444800001]:
        check('strict epoch validation ' + repr(value), request('/attendance/punch-in', {'emp_code': 'EMP0001', 'punched_at': value})[0] == 422)
    check('derived inputs ignored', request('/attendance/punch-in', {'emp_code': 'EMP999998', 'work_hours': 5})[0] == 404)
    check('unknown employee', request('/attendance/punch-out', {'emp_code': 'EMP999998'})[0] == 404)
    body = {'emp_code': 'EMP9010', 'name': 'Race', 'email': 'race@example.com', 'department': 'Race', 'joined_on': '2026-01-01'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: request('/employees', body)[0], range(8)))
    check('concurrent employee creation', codes.count(201) == 1 and codes.count(409) == 7)
    body = {'emp_code': 'EMP9010', 'punched_at': ms('2026-07-20', '09:40:01') + 999,
            'late_minutes': 999, 'work_hours': 999, 'overtime_minutes': 999, 'half_day': True}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request('/attendance/punch-in', body), range(8)))
    check('concurrent punch-in', sum(r[0] == 201 for r in responses) == 1 and sum(r[0] == 409 for r in responses) == 7)
    created = next(b for s, b in responses if s == 201)
    check('grace boundary and second truncation', created['late_minutes'] == 10 and created['punch_in'] == body['punched_at'] // 1000 * 1000)
    check('supplied derived values never stored', created['work_hours'] is None and created['overtime_minutes'] == 0 and not created['half_day'])
    for clock, expected in [('09:40:01', 422), ('09:40:02', 200)]:
        if expected == 422:
            status, invalid = request('/attendance/punch-out', {'emp_code': 'EMP9010', 'punched_at': ms('2026-07-20', clock)})
            check('punch-out equal instant validation body', status == 422 and isinstance(invalid['detail'], list))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request('/attendance/punch-out', {'emp_code': 'EMP9010', 'punched_at': ms('2026-07-20', '19:10:00')}), range(8)))
    check('concurrent punch-out', sum(r[0] == 200 for r in responses) == 1 and sum(r[0] == 409 for r in responses) == 7)
    closed = next(b for s, b in responses if s == 200)
    check('punch-out derived values', closed['overtime_minutes'] == 40 and closed['history'] == [] and not closed['half_day'])
    correction = {'punch_in': ms('2026-07-20', '09:28:00') + 900, 'reason': 'biometric correction', 'regularized_by': 'check'}
    status, corrected = request('/attendance/EMP9010/2026-07-20', correction, 'PATCH')
    check('timestamp correction and BSON storage', status == 200 and corrected['punch_in'] == correction['punch_in'] // 1000 * 1000
          and isinstance(test_db.attendance_logs.find_one({'emp_code': 'EMP9010'})['punch_in'], datetime))
    check('correction history conversion', corrected['late_minutes'] == 0 and len(corrected['history']) == 1
          and corrected['history'][0]['changes']['late_minutes'] == {'from': 10, 'to': 0})
    check('no-op correction rejected', request('/attendance/EMP9010/2026-07-20', correction, 'PATCH')[0] == 422)
    check('null status rejected', request('/attendance/EMP9010/2026-07-20', {'status': None, 'reason': 'check reason', 'regularized_by': 'check'}, 'PATCH')[0] == 422)
    check('strict correction calendar date', request('/attendance/EMP9010/1784505600', correction, 'PATCH')[0] == 422)
    check('absence plus times rejected', request('/attendance/EMP9010/2026-07-20', {**correction, 'status': 'ABSENT'}, 'PATCH')[0] == 422)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        edits = list(pool.map(lambda n: request('/attendance/EMP9010/2026-07-20', {**correction, 'punch_in': ms('2026-07-20', '09:20:00') + n * 1000}, 'PATCH'), range(8)))
    check('concurrent corrections return success or conflict', all(s in (200, 409) for s, _ in edits))
    stored = test_db.attendance_logs.find_one({'emp_code': 'EMP9010'})
    check('concurrent correction history retained', len(stored['history']) == 1 + sum(s == 200 for s, _ in edits))
    status, absent = request('/attendance/EMP9010/2026-07-20', {'status': 'ABSENT', 'reason': 'approved correction', 'regularized_by': 'check'}, 'PATCH')
    check('absence clears times and derived values', status == 200 and absent['punch_in'] is None and absent['punch_out'] is None
          and absent['work_hours'] is None and absent['late_minutes'] == 0 and absent['overtime_minutes'] == 0 and not absent['half_day'])
    status, restored = request('/attendance/EMP9010/2026-07-20', {**reason, 'status': 'WFH',
        'punch_in': ms('2026-07-20', '09:30:00'), 'punch_out': ms('2026-07-20', '17:31:30')}, 'PATCH')
    check('restore absence with both timestamps and half-up duration', status == 200 and restored['work_hours'] == 8.03)
    status, short = request('/attendance/EMP9010/2026-07-20', {**reason, 'punch_out': ms('2026-07-20', '13:59:40')}, 'PATCH')
    check('rounded duration governs half-day boundary', status == 200 and short['work_hours'] == 4.49 and short['half_day'])
    status, full = request('/attendance/EMP9010/2026-07-20', {**reason, 'punch_out': ms('2026-07-20', '13:59:50')}, 'PATCH')
    check('rounded duration 4.50 is full day', status == 200 and full['work_hours'] == 4.50 and not full['half_day'])
    check('correction over 24 hours rejected', request('/attendance/EMP9010/2026-07-20', {**reason, 'punch_out': ms('2026-07-21', '09:30:01')}, 'PATCH')[0] == 422)
    status, full = request('/attendance/EMP9010/2026-07-20', {**reason, 'punch_out': ms('2026-07-21', '09:30:00')}, 'PATCH')
    check('correction exactly 24 hours accepted', status == 200 and full['work_hours'] == 24)
    status, overnight = request('/attendance/punch-in', {'emp_code': 'EMP0005', 'punched_at': ms('2026-07-21', '01:00:00')})
    check('overnight attendance date', status == 201 and overnight['date'] == '2026-07-20')
    status, overnight = request('/attendance/punch-out', {'emp_code': 'EMP0005', 'punched_at': ms('2026-07-21', '06:40:00')})
    check('overnight overtime', status == 200 and overnight['overtime_minutes'] == 40 and overnight['work_hours'] == 5.67)
    status, monthly = request('/analytics/employees/EMP9001/monthly?month=2026-07')
    check('monthly weekday presence and weekend totals', status == 200 and monthly['working_days'] == 23 and monthly['present_days'] == 1
          and monthly['total_late_minutes'] == 35 and monthly['late_count'] == 2 and monthly['attendance_pct'] == 4.35)
    status, monthly = request('/analytics/employees/EMP9003/monthly?month=2026-07')
    check('mid-month joiner with no logs', monthly['working_days'] == 13 and monthly['present_days'] == 0 and monthly['attendance_pct'] == 0)
    status, monthly = request('/analytics/employees/EMP9004/monthly?month=2026-07')
    check('future joiner zero denominator', monthly['working_days'] == 0 and monthly['attendance_pct'] is None)
    status, summary = request('/analytics/departments/summary?month=2026-07&department=Checks')
    row = summary['items'][0]
    check('summary headcount includes zero logs and excludes future joiners', status == 200 and row['headcount'] == 3 and row['present_days'] == 1.5)
    check('summary average weighted over records and stored values', row['avg_work_hours'] == 7.35 and row['late_count'] == 4 and row['total_late_minutes'] == 70
          and row['on_duty_count'] == 1 and row['leave_count'] == 1)
    status, summary = request('/analytics/departments/summary?month=2026-07&department=Empty')
    check('department with no logs retained', summary['items'][0]['headcount'] == 1 and summary['items'][0]['avg_work_hours'] is None)
    status, summary = request('/analytics/departments/summary?month=2025-01')
    check('zero-headcount departments omitted', summary['items'] == [])
    status, leaders = request('/analytics/leaderboard/late?month=2026-07&department=Checks&limit=1')
    check('ties included past requested row count', status == 200 and [r['emp_code'] for r in leaders['items']] == ['EMP9001', 'EMP9002']
          and [r['rank'] for r in leaders['items']] == [1, 1])
    status, leaders = request('/analytics/leaderboard/late?month=2026-07')
    check('orphan logs excluded', all(r['emp_code'] != 'EMP999999' for r in leaders['items']))
    test_db.attendance_logs.insert_one({'emp_code': 'EMP9003', 'date': '2026-07-15', 'status': 'PRESENT', 'late_minutes': 10})
    status, leaders = request('/analytics/leaderboard/late?month=2026-07&department=Checks')
    check('competition ranks skip after ties', [r['rank'] for r in leaders['items']] == [1, 1, 3])
    status, trend = request('/analytics/departments/Checks/trend?from=2026-07-06&to=2026-07-15')
    rows = trend['items']
    check('trend fills every calendar day and counts joining dates', status == 200 and len(rows) == 10 and rows[0]['headcount'] == 2 and rows[-1]['headcount'] == 3)
    check('trend weekend presence with null rate', rows[5]['present_count'] == 1 and rows[5]['attendance_rate'] is None and not rows[5]['is_working_day'])
    check('trend missing weekday has zero rate', rows[1]['attendance_rate'] == 0)
    check('moving average ignores nulls within requested window', rows[0]['moving_avg_7d'] == .75 and rows[1]['moving_avg_7d'] == .375
          and rows[6]['moving_avg_7d'] == .15 and rows[7]['moving_avg_7d'] == 0)
    status, trend = request('/analytics/departments/Empty/trend?from=2026-07-11&to=2026-07-12')
    check('empty department weekend trend', len(trend['items']) == 2 and all(r['moving_avg_7d'] is None for r in trend['items']))
    check('unknown department', request('/analytics/departments/Missing/trend?from=2026-07-01&to=2026-07-01')[0] == 404)
    status, trend = request('/analytics/departments/Checks/trend?from=2026-07-15&to=2026-07-15')
    check('single-day trend', status == 200 and len(trend['items']) == 1)
    # A midpoint mean must round up, not half-even.
    test_db.attendance_logs.update_one({'emp_code': 'EMP9001', 'date': '2026-07-11'}, {'$set': {'work_hours': None}})
    status, summary = request('/analytics/departments/summary?month=2026-07&department=Checks')
    check('aggregation half-up midpoint 8.025', summary['items'][0]['avg_work_hours'] == 8.03)
    # Independent Python oracle over reproducible, awkward seeded records.
    rng = random.Random(8172)
    oracle_employees, oracle_logs = [], []
    for n in range(12):
        joined = ['2026-01-01', '2026-07-15', '2026-08-05'][n % 3]
        employee = {**{k: v for k, v in extra[0].items() if k != '_id'},
                    'emp_code': 'EMP' + str(8000 + n), 'department': 'Oracle', 'joined_on': joined}
        oracle_employees.append(employee)
        for offset in range(37):
            day = date(2026, 6, 28) + timedelta(days=offset)
            if n == 9 or day.isoformat() < joined or rng.random() < .3:
                continue
            status = rng.choice(['PRESENT', 'WFH', 'ON_DUTY', 'ABSENT', 'LEAVE'])
            record = {'emp_code': employee['emp_code'], 'date': day.isoformat(), 'status': status}
            if status in main.PRESENCE:
                record['work_hours'] = rng.choice([None, 3.5, 4.5, 8.02, 8.03, 9.12])
                if rng.random() > .2:
                    record['half_day'] = rng.choice([True, False])
                record['late_minutes'] = rng.choice([0, 10, 35, 60])
                record['overtime_minutes'] = rng.choice([0, 30, 90])
            oracle_logs.append(record)
    test_db.employees.insert_many(oracle_employees)
    test_db.attendance_logs.insert_many(oracle_logs)
    july = [r for r in oracle_logs if '2026-07-01' <= r['date'] <= '2026-07-31']
    def presence(record):
        return (.5 if record.get('half_day', False) else 1) if record['status'] in main.PRESENCE else 0
    def reference_stats(logs):
        return {'present_days': sum(presence(r) for r in logs if date.fromisoformat(r['date']).weekday() < 5),
                'leave_days': sum(r['status'] == 'LEAVE' for r in logs),
                'late_count': sum(r.get('late_minutes', 0) > 0 for r in logs),
                'total_late_minutes': sum(r.get('late_minutes', 0) for r in logs),
                'total_overtime_minutes': sum(r.get('overtime_minutes', 0) for r in logs)}
    for employee in oracle_employees:
        logs = [r for r in july if r['emp_code'] == employee['emp_code']]
        days = sum((date(2026, 7, 1) + timedelta(days=i)).weekday() < 5
                   and (date(2026, 7, 1) + timedelta(days=i)).isoformat() >= employee['joined_on'] for i in range(31))
        expected = {'emp_code': employee['emp_code'], 'month': '2026-07', 'working_days': days, **reference_stats(logs)}
        expected['attendance_pct'] = rounded(Decimal(str(expected['present_days'])) * 100 / days) if days else None
        status, actual = request('/analytics/employees/' + employee['emp_code'] + '/monthly?month=2026-07')
        check('oracle monthly ' + employee['emp_code'], actual == expected)
    eligible = [e for e in oracle_employees if e['joined_on'] <= '2026-07-31']
    stats = reference_stats(july)
    hours = [Decimal(str(r['work_hours'])) for r in july if r['status'] in main.PRESENCE and r.get('work_hours') is not None]
    expected = {'department': 'Oracle', 'headcount': len(eligible), 'present_days': stats['present_days'],
                'avg_work_hours': rounded(sum(hours) / len(hours)) if hours else None,
                'late_count': stats['late_count'], 'total_late_minutes': stats['total_late_minutes'],
                'leave_count': stats['leave_days'], 'on_duty_count': sum(r['status'] == 'ON_DUTY' for r in july)}
    status, actual = request('/analytics/departments/summary?month=2026-07&department=Oracle')
    check('oracle department summary', actual['items'] == [expected])
    totals = []
    for employee in oracle_employees:
        stats = reference_stats([r for r in july if r['emp_code'] == employee['emp_code']])
        if stats['total_late_minutes'] > 0:
            totals.append({'emp_code': employee['emp_code'], 'name': employee['name'], 'department': 'Oracle',
                           'total_late_minutes': stats['total_late_minutes'], 'late_count': stats['late_count']})
    totals.sort(key=lambda r: (-r['total_late_minutes'], r['emp_code']))
    for row in totals:
        row['rank'] = 1 + sum(other['total_late_minutes'] > row['total_late_minutes'] for other in totals)
    for limit in [1, 3, 10]:
        status, actual = request('/analytics/leaderboard/late?month=2026-07&department=Oracle&limit=' + str(limit))
        check('oracle leaderboard cutoff ' + str(limit), actual['items'] == [r for r in totals if r['rank'] <= limit])
    reference_trend = []
    for offset in range(31):
        day = date(2026, 7, 1) + timedelta(days=offset)
        logs = [r for r in july if r['date'] == day.isoformat()]
        count = sum(e['joined_on'] <= day.isoformat() for e in oracle_employees)
        present = sum(presence(r) for r in logs)
        rate = rounded(Decimal(str(present)) / count, 4) if count and day.weekday() < 5 else None
        window = [Decimal(str(r['attendance_rate'])) for r in reference_trend[-6:] if r['attendance_rate'] is not None]
        if rate is not None:
            window.append(Decimal(str(rate)))
        reference_trend.append({'date': day.isoformat(), 'is_working_day': day.weekday() < 5,
            'headcount': count, 'present_count': present, 'late_count': sum(r.get('late_minutes', 0) > 0 for r in logs),
            'attendance_rate': rate, 'moving_avg_7d': rounded(sum(window) / len(window), 4) if window else None})
    status, actual = request('/analytics/departments/Oracle/trend?from=2026-07-01&to=2026-07-31')
    check('oracle full 31-day trend', actual['items'] == reference_trend)
    explain_paths = [
        'attendance_list', 'attendance_list?status=PRESENT&page=2&page_size=2', 'attendance_list?emp_code=EMP0001',
        'employee_monthly?emp_code=EMP0001&month=2026-07', 'department_summary?month=2026-07',
        'department_summary?month=2026-07&department=Checks', 'late_leaderboard?month=2026-07&limit=1',
        'department_trend?department=Checks&from=2026-07-01&to=2026-07-31',
    ]
    for path in explain_paths:
        status, result = request('/admin/explain/' + path)
        plans = stages(result.get('explain', {}))
        check('indexed explain ' + path, status == 200 and 'IXSCAN' in plans and 'COLLSCAN' not in plans)
    if '--scale' in sys.argv:
        print('Adding 100,000 attendance records to disposable database...', flush=True)
        records = []
        people = []
        for n in range(1000):
            code = 'EMP' + str(100000 + n)
            people.append({**{k: v for k, v in extra[0].items() if k != '_id'}, 'emp_code': code, 'department': 'Scale'})
            for offset in range(100):
                records.append({'emp_code': code, 'date': (date(2026, 6, 1) + timedelta(days=offset)).isoformat(),
                                'status': 'PRESENT', 'work_hours': 8.0, 'late_minutes': n % 60, 'half_day': False})
        test_db.employees.insert_many(people)
        for offset in range(0, len(records), 5000):
            test_db.attendance_logs.insert_many(records[offset:offset + 5000])
        for path in explain_paths + ['department_trend?department=Scale&from=2026-07-01&to=2026-07-31']:
            status, result = request('/admin/explain/' + path)
            plans = stages(result.get('explain', {}))
            check('100k indexed explain ' + path, status == 200 and 'IXSCAN' in plans and 'COLLSCAN' not in plans)
        for path in ['/attendance?page_size=20', '/analytics/departments/summary?month=2026-07',
                     '/analytics/leaderboard/late?month=2026-07', '/analytics/departments/Scale/trend?from=2026-07-01&to=2026-07-31']:
            begin = time.monotonic()
            status, body = request(path)
            elapsed = time.monotonic() - begin
            check('100k endpoint ' + path, status == 200)
            if path.startswith('/analytics/departments/summary'):
                scale = next(row for row in body['items'] if row['department'] == 'Scale')
                check('100k summary values', scale['headcount'] == 1000 and scale['present_days'] == 23000
                      and scale['avg_work_hours'] == 8 and scale['total_late_minutes'] == sum(n % 60 for n in range(1000)) * 31)
            if path.startswith('/analytics/departments/Scale/trend'):
                check('100k trend values', len(body['items']) == 31 and all(r['headcount'] == 1000 and r['present_count'] == 1000
                      and r['attendance_rate'] == (1 if r['is_working_day'] else None) for r in body['items']))
            print('TIMING', path, round(elapsed, 3), 'seconds', flush=True)
    print('RESULT:', passed, 'checks passed in', round(time.monotonic() - started, 2), 'seconds', flush=True)
except Exception:
    log.seek(0)
    print(log.read().decode(errors='replace')[-10000:], file=sys.stderr)
    raise
finally:
    if server is not None:
        server.terminate()
        server.wait(timeout=10)
    # Delete only the database with the unique name generated by this run.
    if name.startswith('check_') and len(name) == len('check_') + 32:
        main.client.drop_database(name)
    log.close()
