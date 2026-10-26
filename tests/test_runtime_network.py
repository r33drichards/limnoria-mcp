"""Trusted phase and observable irreversible effects; no real network traffic."""
import pytest
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.tcl_worker import TclExecutionError

CONTEXT = {'network':'test','channel':'#room','nick':'alice','text':''}
EFFECT = {'operation_id':'a' * 32 + ':1', 'method':'POST', 'outcome':'unknown'}


def test_validation_is_trusted_phase_and_never_commits_network_receipts(tmp_path):
    phases = []
    def execute(request):
        phases.append(request['phase'])
        return {'state':{},'replies':[], 'effects':[]}
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=execute)
    files.write('Webhook', 'http_post https://example.com/hook hello text/plain')
    runtime.validate('Webhook')
    runtime.activate('Webhook')
    runtime.activate_bundle({'Webhook':'reply ok'}, 'b' * 40)
    assert phases == ['validate','validate','validate']
    assert runtime.network_status() == []
    runtime.invoke('Webhook', CONTEXT)
    assert phases[-1] == 'run'
    files.close()


@pytest.mark.parametrize('method', ['POST','PUT','DELETE'])
def test_failed_mutation_records_unknown_id_without_retry_or_state_commit(tmp_path, method):
    effect = {**EFFECT, 'method':method}
    invocations = []
    def execute(request):
        if request['phase'] == 'validate':
            return {'state':{},'replies':[], 'effects':[]}
        invocations.append(request)
        raise TclExecutionError('unknown POST followed by Tcl error', effects=[effect])
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=execute)
    files.write('Webhook', 'http_post https://example.com/hook hello text/plain')
    runtime.activate('Webhook')
    with pytest.raises(TclExecutionError) as error:
        runtime.invoke('Webhook', CONTEXT)
    assert error.value.effects == [effect]
    assert len(invocations) == 1
    assert runtime.active['Webhook'].state == {}
    assert runtime.network_status() == [{'script':'Webhook','generation':1,
                                         'script_failed':True,'operations':[effect]}]
    files.close()


def test_completed_effects_remain_observable_with_success(tmp_path):
    completed = {**EFFECT,'outcome':'completed'}
    def execute(request):
        return {'state':{},'replies':['done'],
                'effects':[] if request['phase']=='validate' else [completed]}
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=execute)
    files.write('Webhook', 'reply done')
    runtime.activate('Webhook')
    result = runtime.invoke('Webhook', CONTEXT)
    assert result['effects'] == [completed]
    assert runtime.network_status()[0]['script_failed'] is False
    files.close()


def test_worker_rpc_rejects_busy_before_connection_or_dispatch(monkeypatch):
    import json
    import threading
    import time
    import limnoria_mcp.runtime as module
    first_connected, release_first, second_connected = [threading.Event() for _ in range(3)]
    created = []
    class Connection:
        def __init__(self, *args):
            self.number = len(created)
            created.append(self)
            self.sent_response = False
            self.ready = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, timeout):
            assert 0 < timeout <= 2 or timeout == 30
        def connect(self, path):
            (first_connected if self.number == 0 else second_connected).set()
        def sendall(self, payload):
            pass
        def shutdown(self, how):
            pass
        def recv(self, size):
            if not self.ready:
                self.ready = True
                return b'READY\n'
            if self.number == 0:
                assert release_first.wait(5)
            if self.sent_response:
                return b''
            self.sent_response = True
            return json.dumps({'replies':[], 'state':{}, 'effects':[]}).encode()
    monkeypatch.setattr(module.socket, 'socket', Connection)
    results = []
    request = {'script':'', 'state':{},'context':CONTEXT,'phase':'run'}
    one = threading.Thread(target=lambda: results.append(module.execute_worker(request)))
    rejected = []
    def second():
        try:
            module.execute_worker(request)
        except RuntimeError as exc:
            rejected.append(str(exc))
    two = threading.Thread(target=second)
    one.start()
    assert first_connected.wait(5)
    two.start()
    two.join(1)
    assert not two.is_alive()
    assert rejected == ['Worker busy; request was not dispatched']
    assert not second_connected.is_set()
    assert len(created) == 1
    release_first.set()
    one.join(5)
    two.join(5)
    assert not second_connected.is_set()
    assert len(results) == 1


def test_busy_activation_and_same_script_invocation_never_queue(tmp_path):
    from limnoria_mcp.runtime import Generation
    files = ScriptFiles(str(tmp_path))
    dispatched = []
    runtime = ScriptRuntime(files, execute=lambda request: dispatched.append(request))
    files.write('One', 'reply new')
    old = Generation('reply old', 'oldhash', 1)
    runtime.active['One'] = old
    runtime.activations.acquire()
    try:
        with pytest.raises(RuntimeError, match='Activation busy'):
            runtime.activate('One')
        with pytest.raises(RuntimeError, match='Activation busy'):
            runtime.activate_bundle({'One':'reply new'}, 'a' * 40)
    finally:
        runtime.activations.release()
    old.lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='Script busy'):
            runtime.invoke('One', CONTEXT)
    finally:
        old.lock.release()
    assert dispatched == []
    assert runtime.active['One'] is old
    files.close()


@pytest.mark.parametrize('admission', ['eof', 'malformed', 'late'])
def test_no_script_sent_without_ready_handshake(monkeypatch, admission):
    import limnoria_mcp.runtime as module
    sent = []
    class Connection:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, path): pass
        def recv(self, size):
            return {'eof':b'', 'malformed':b'WRONG!', 'late':b'READY\n'}[admission]
        def sendall(self, payload): sent.append(payload)
    monkeypatch.setattr(module.socket, 'socket', Connection)
    if admission == 'late':
        ticks = iter([10.0, 10.0, 13.0])
        monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(RuntimeError, match='not dispatched'):
        module.execute_worker({'script':'http_delete https://example.com/item',
                               'state':{},'context':CONTEXT,'phase':'run'})
    assert sent == []
