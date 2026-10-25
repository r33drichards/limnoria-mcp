"""Actual Safe Tcl creation, staging, hot-reload, state and generation semantics."""
import threading
import pytest
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.tcl_worker import run_limited

CONTEXT = {'network':'test', 'channel':'#room', 'nick':'alice', 'text':'hello'}


def test_actual_safe_tcl_lifecycle(tmp_path):
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=run_limited)
    files.write('Hello', 'state_set count 1; reply "hello [context_get nick]"')
    assert runtime.validate('Hello')['valid']
    assert runtime.status() == {}  # staging has no active state or reply effects
    assert runtime.activate('Hello')['generation'] == 1
    assert runtime.invoke('Hello', CONTEXT)['replies'] == ['hello alice']
    files.write('Hello', 'reply "new [context_get text]"')
    assert runtime.activate('Hello')['generation'] == 2
    assert runtime.invoke('Hello', CONTEXT)['replies'] == ['new hello']
    for bad in ['invalid Tcl {', 'exec id', 'while 1 {}', 'reply "bad\\nline"']:
        files.write('Hello', bad)
        with pytest.raises((RuntimeError, ValueError)):
            runtime.activate('Hello')
        assert runtime.status()['Hello']['generation'] == 2
        assert runtime.invoke('Hello', CONTEXT)['replies'] == ['new hello']
    files.close()


def test_failure_does_not_commit_partial_state_or_replies(tmp_path):
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=run_limited)
    files.write('State', 'if {[context_get text] eq "fail"} {state_set n bad; reply leaked; error stop}; reply "value:[state_get n]"; state_set n good')
    runtime.activate('State')
    assert runtime.invoke('State', CONTEXT)['replies'] == ['value:']
    with pytest.raises(RuntimeError):
        runtime.invoke('State', {**CONTEXT, 'text':'fail'})
    assert runtime.invoke('State', CONTEXT)['replies'] == ['value:good']
    files.close()


def test_inflight_generation_cannot_overwrite_new_state(tmp_path):
    files = ScriptFiles(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    def execute(request):
        if request['context']['text'] == 'hold':
            entered.set()
            assert release.wait(5)
        return run_limited(request)
    runtime = ScriptRuntime(files, execute=execute)
    files.write('Hello', 'state_set old yes; reply old')
    runtime.activate('Hello')
    results = []
    thread = threading.Thread(target=lambda: results.append(runtime.invoke('Hello', {**CONTEXT,'text':'hold'})))
    thread.start()
    assert entered.wait(5)
    files.write('Hello', 'reply "new[state_get old]"')
    runtime.activate('Hello')
    release.set()
    thread.join(5)
    assert results == [{'generation':1, 'replies':['old']}]
    assert runtime.invoke('Hello', CONTEXT) == {'generation':2, 'replies':['new']}
    files.close()
