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
    assert first == {'replies': ['Hello tester'], 'state': {'count': '1'}}
    second = run_limited(request('state_set count [expr {[state_get count] + 1}]; reply updated', first['state']))
    assert second == {'replies': ['updated'], 'state': {'count': '2'}}
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
    assert result == {'replies': [value], 'state': {'x': value}}


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
