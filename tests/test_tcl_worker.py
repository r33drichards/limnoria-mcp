"""Execute real Tcl in real limited child processes; no mocked sandbox engine."""
import subprocess
import time
import pytest
from limnoria_mcp.tcl_worker import run_limited, MAX_SCRIPT_BYTES


def request(script, state=None, text='hello'):
    return {'script': script, 'state': {} if state is None else state,
            'context': {'network': 'synthetic', 'channel': '#test', 'nick': 'tester', 'text': text}}


def test_reply_state_context_and_fresh_reload():
    first = run_limited(request('state_set count 1; reply "Hello [context_get nick]"'))
    assert first == {'replies': ['Hello tester'], 'state': {'count': '1'}, 'effects': []}
    second = run_limited(request('state_set count [expr {[state_get count] + 1}]; reply updated', first['state']))
    assert second == {'replies': ['updated'], 'state': {'count': '2'}, 'effects': []}
    assert first['state'] == {'count': '1'}


@pytest.mark.parametrize('script', [
    '::tcl::file::exists /etc/passwd', '::tcl::chan::pipe',
    '::tcl::clock::getenv HOME', '::tcl::encoding::names', '::tcl::chan::names',
    '::tcl::binary::encode hex hello', '::tcl::zlib::compress hello',
    '::tcl::zipfs::root', '::tcl::process::list', '::tcl::pkgconfig list',
    '::oo::class create Escape', '::tcl::namespace::eval :: {exec id}',
    'exec id', 'open /etc/passwd', 'socket localhost 80', 'source /etc/passwd',
    'load /tmp/library.so', 'file readlink /etc/passwd', 'glob /*', 'pwd',
    'puts stdout secret', 'chan names', 'encoding names', 'package require Tcl',
    'interp eval {} {exec id}', 'interp invokehidden {} exec id',
    'namespace eval :: {exec id}', 'rename reply exec', 'after 100000', 'vwait x',
    'trace add variable x write {exec id}', 'set env(HOME)',
    'eval {_host_reply escaped}', 'uplevel #0 {_host_reply escaped}',
    'unknown exec id', 'reply one two', 'state_set invalid-key value',
    'context_get password', 'state_get a b',
])
def test_forbidden_capabilities(script):
    with pytest.raises(RuntimeError):
        run_limited(request(script))


def test_capability_data_is_never_parent_tcl_code():
    value = '[exec id]; set env(HOME)'
    result = run_limited(request('reply [context_get text]; state_set x [context_get text]', text=value))
    assert result == {'replies': [value], 'state': {'x': value}, 'effects': []}


def test_untrusted_script_is_not_interpolated_into_master():
    for script in ['}; exec id; #', 'reply [_host_reply escaped]', 'eval {open /etc/passwd}']:
        with pytest.raises(RuntimeError):
            run_limited(request(script))


@pytest.mark.parametrize('script', [
    'while 1 {}',
    'proc loop {} {loop}; loop',
    'set x [string repeat x 1000000000]',
    'for {set i 0} {$i < 4} {incr i} {reply hello}',
    'reply [string repeat x 351]',
    'reply "one\\nPRIVMSG #other :injected"',
    'reply "\\x01ACTION forbidden\\x01"',
    'state_set x [string repeat x 1025]',
    'for {set i 0} {$i < 17} {incr i} {state_set key$i value}',
])
def test_resource_and_callback_quotas(script):
    before = time.monotonic()
    with pytest.raises(RuntimeError):
        run_limited(request(script))
    assert time.monotonic() - before < 4


def test_failure_never_commits_partial_state_or_reply():
    initial = {'count': '1'}
    with pytest.raises(RuntimeError):
        run_limited(request('state_set count 2; reply partial; error failure', initial))
    assert initial == {'count': '1'}


def test_source_and_input_limits():
    with pytest.raises(ValueError):
        run_limited(request('x' * (MAX_SCRIPT_BYTES + 1)))
    malformed = request('reply hello')
    malformed['context']['channel'] = '#other\nINJECT'
    with pytest.raises(ValueError):
        run_limited(malformed)


def test_parent_deadline_kills_and_reaps(monkeypatch):
    # Actual child and Tcl; force wall deadline shorter than process startup to
    # cover kill/reap independently of interpreter command and CPU checkpoints.
    from limnoria_mcp import tcl_worker
    monkeypatch.setattr(tcl_worker, 'TIMEOUT_SECONDS', 0.00001)
    with pytest.raises(RuntimeError, match='wall-clock deadline'):
        run_limited(request('while 1 {}'))


def test_caught_capability_rejection_still_fails_transaction():
    with pytest.raises(RuntimeError):
        run_limited(request('catch {reply [string repeat x 351]}; reply otherwise_valid'))


def test_alias_introspection_shadowing_and_namespace_cannot_reach_master():
    result = run_limited(request('if {[llength [info commands _host_*]] != 0} {error leaked}; reply safe'))
    assert result['replies'] == ['safe']
    for script in ['proc reply {x} {_host_reply $x}; reply escape',
                   'apply {{x} {_host_reply $x} ::} escape']:
        with pytest.raises(RuntimeError):
            run_limited(request(script))


def test_http_validation_is_pure_and_cannot_be_overridden(monkeypatch):
    from limnoria_mcp import tcl_worker, public_http
    def forbidden(*args, **kwargs):
        raise AssertionError('validation must not use broker or DNS/socket')
    monkeypatch.setattr(public_http, 'call_broker', forbidden)
    monkeypatch.setattr(public_http.socket, 'socket', forbidden)
    monkeypatch.setattr(public_http.socket, 'getaddrinfo', forbidden)
    result = tcl_worker._execute(request('set r [http_post https://example.test/webhook payload text/plain]; reply [dict get $r outcome]'))
    assert result == {'replies': ['dry_run'], 'state': {}, 'effects': []}
    # Setting a Tcl variable cannot change the trusted Python phase.
    result = tcl_worker._execute(request('set phase run; http_get https://example.test/rss; reply safe'))
    assert result['effects'] == []


def test_http_callbacks_typed_data_and_effects(monkeypatch):
    import base64
    from limnoria_mcp import tcl_worker, public_http
    received, records = [], []
    def broker(payload):
        received.append(payload)
        return {'status': 200, 'headers': [], 'body_base64': base64.b64encode(b'[exec id]; literal').decode(),
                'operation_id':payload['operation_id'], 'outcome':'completed'}
    monkeypatch.setattr(public_http, 'call_broker', broker)
    value = request('set r [http_get https://example.test/feed]; reply [dict get $r body]; http_post https://example.test/hook {[exec id]} application/json')
    value['phase'] = 'run'
    result = tcl_worker._execute(value, run_id='a'*32, effect_sink=records.append)
    assert result['replies'] == ['[exec id]; literal']
    assert [r['method'] for r in received] == ['GET', 'POST']
    assert base64.b64decode(received[1]['body_base64']) == b'[exec id]'
    assert received[1]['headers'] == {'content-type':'application/json'}
    assert result['effects'] == [{'operation_id':'a'*32+':1','method':'GET','outcome':'completed'},
                                 {'operation_id':'a'*32+':2','method':'POST','outcome':'completed'}]
    assert [x['outcome'] for x in records] == ['unknown','completed','unknown','completed']


def test_http_calls_bounded_and_failure_does_not_erase_completed_post(monkeypatch):
    from limnoria_mcp import tcl_worker, public_http
    records, calls = [], []
    def broker(payload):
        calls.append(payload)
        return {'status':204,'headers':[],'body_base64':'','operation_id':payload['operation_id'],'outcome':'completed'}
    monkeypatch.setattr(public_http, 'call_broker', broker)
    value = request('http_post https://example.test/hook data text/plain; error later_failure')
    value['phase'] = 'run'
    with pytest.raises(Exception):
        tcl_worker._execute(value, run_id='b'*32, effect_sink=records.append)
    assert len(calls) == 1
    assert records[-1] == {'operation_id':'b'*32+':1','method':'POST','outcome':'completed'}
    value['script'] = 'http_get https://example.test/; http_get https://example.test/; http_get https://example.test/'
    calls.clear()
    with pytest.raises(Exception):
        tcl_worker._execute(value, effect_sink=lambda record: None)
    assert len(calls) == 2


def test_http_uncertain_failure_has_no_retry(monkeypatch):
    from limnoria_mcp import tcl_worker, public_http
    records, calls = [], []
    def broker(payload):
        calls.append(payload)
        raise public_http.BrokerError('synthetic disconnect', operation_id=payload['operation_id'], outcome='unknown')
    monkeypatch.setattr(public_http, 'call_broker', broker)
    value = request('catch {http_post https://example.test/hook data text/plain}; catch {http_post https://example.test/hook retry text/plain}; reply swallowed')
    value['phase'] = 'run'
    with pytest.raises(Exception):
        tcl_worker._execute(value, effect_sink=records.append)
    assert len(calls) == 1
    assert records[-1]['outcome'] == 'unknown'


@pytest.mark.parametrize('finish', ['failure', 'timeout', 'completed_then_failure'])
def test_supervisor_retains_effect_ids_on_child_failure_or_timeout(monkeypatch, finish):
    import sys
    from limnoria_mcp import tcl_worker
    real_popen = tcl_worker.subprocess.Popen
    # A real synthetic child emits the same trusted audit protocol; no HTTP sent.
    code = '''import json,sys,time
request=json.load(sys.stdin)
effect={'operation_id':request['run_id']+':1','method':'POST','outcome':'unknown'}
sys.stderr.write('LIMNORIA_EFFECT '+json.dumps(effect)+'\\n');sys.stderr.flush()
'''
    if finish == 'completed_then_failure':
        code += "effect['outcome']='completed';sys.stderr.write('LIMNORIA_EFFECT '+json.dumps(effect)+'\\n');sys.stderr.flush()\n"
    code += 'time.sleep(5)' if finish == 'timeout' else 'raise SystemExit(1)'
    def child(argv, **kwargs):
        return real_popen([sys.executable, '-c', code], **kwargs)
    monkeypatch.setattr(tcl_worker.subprocess, 'Popen', child)
    monkeypatch.setattr(tcl_worker, 'RUN_TIMEOUT_SECONDS', .15)
    value = request('irrelevant synthetic fixture')
    value['phase'] = 'run'
    with pytest.raises(tcl_worker.TclExecutionError) as caught:
        run_limited(value)
    effects = caught.value.effects
    assert len(effects) == 1
    assert effects[0]['method'] == 'POST'
    assert effects[0]['outcome'] == ('completed' if finish == 'completed_then_failure' else 'unknown')
    assert len(effects[0]['operation_id']) == 34


def test_real_child_validation_can_use_http_without_broker():
    # The isolated child imports the real broker's pure validator, but phase
    # validation must finish without any socket service being present.
    result = run_limited(request('set r [http_get https://example.test/rss]; reply [dict get $r outcome]'))
    assert result == {'replies':['dry_run'], 'state':{}, 'effects':[]}


@pytest.mark.parametrize('script', [
    'http_get file:///etc/passwd',
    'http_get https://example.test/ extra',
    'http_post https://example.test/ payload "text/plain\\r\\nInjected: yes"',
    'http_post https://example.test/ [string repeat x 65537] text/plain',
])
def test_http_alias_validation_and_arity_fail_closed(script):
    with pytest.raises(RuntimeError):
        run_limited(request(script))


@pytest.mark.parametrize('method,script', [
    ('GET','http_get https://example.test/resource'),
    ('POST','http_post https://example.test/resource payload text/plain'),
    ('PUT','http_put https://example.test/resource replacement application/json'),
    ('DELETE','http_delete https://example.test/resource'),
])
def test_all_http_methods_stage_without_network_and_run_typed(monkeypatch, method, script):
    from limnoria_mcp import tcl_worker, public_http
    calls = []
    def broker(payload):
        calls.append(payload)
        return {'status':204,'headers':[],'body_base64':'','operation_id':payload['operation_id'],'outcome':'completed'}
    monkeypatch.setattr(public_http, 'call_broker', broker)
    value = request(script)
    assert tcl_worker._execute(value)['effects'] == []
    assert calls == []
    value['phase'] = 'run'
    result = tcl_worker._execute(value, effect_sink=lambda record: None)
    assert calls[0]['method'] == method
    assert result['effects'][0]['method'] == method
    if method == 'DELETE':
        assert calls[0]['body_base64'] == '' and calls[0]['headers'] == {}


@pytest.mark.parametrize('script', [
    'http_post https://example.test/resource data text/plain',
    'http_put https://example.test/resource data text/plain',
    'http_delete https://example.test/resource',
])
def test_unknown_mutation_blocks_all_further_calls_even_if_caught(monkeypatch, script):
    from limnoria_mcp import tcl_worker, public_http
    calls, records = [], []
    def broker(payload):
        calls.append(payload)
        raise public_http.BrokerError('synthetic', operation_id=payload['operation_id'], outcome='unknown')
    monkeypatch.setattr(public_http, 'call_broker', broker)
    value = request('catch {' + script + '}; catch {' + script + '}; catch {http_get https://example.test/}')
    value['phase'] = 'run'
    with pytest.raises(Exception):
        tcl_worker._execute(value, effect_sink=records.append)
    assert len(calls) == 1
    assert len({r['operation_id'] for r in records}) == 1
    assert records[-1]['outcome'] == 'unknown'
