import json
import pytest
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.bundle_runtime import BundleWatcher
from limnoria_mcp.tcl_worker import run_limited

CONTEXT = {'network':'test','channel':'#room','nick':'alice','text':''}


def test_atomic_bundle_lastgood_restart_and_no_draft_bypass(tmp_path):
    drafts, bundles, private = [tmp_path / n for n in ('drafts','bundles','private')]
    for directory in (drafts, bundles, private):
        directory.mkdir()
    files = ScriptFiles(str(drafts))
    runtime = ScriptRuntime(files, execute=run_limited, gitops=True)
    watcher = BundleWatcher(runtime, str(bundles), str(private / 'last-good.json'))
    def candidate(revision, scripts):
        (bundles / 'candidate.json').write_text(json.dumps({'revision':revision,'scripts':scripts}))
    candidate('a' * 40, {'One':'reply one', 'Two':'reply two'})
    watcher.check()
    previous = (private / 'last-good.json').read_bytes()
    assert runtime.bundle_status()['revision'] == 'a' * 40
    assert runtime.invoke('One', CONTEXT)['replies'] == ['one']
    files.write('One', 'reply draft')
    with pytest.raises(ValueError, match='Direct activation disabled'):
        runtime.activate('One')
    candidate('b' * 40, {'One':'reply changed', 'Two':'exec id'})
    with pytest.raises(RuntimeError):
        watcher.check()
    assert runtime.invoke('One', CONTEXT)['replies'] == ['one']
    assert (private / 'last-good.json').read_bytes() == previous
    restarted = ScriptRuntime(files, execute=run_limited, gitops=True)
    BundleWatcher(restarted, str(bundles), str(private / 'last-good.json')).restore()
    assert restarted.invoke('One', CONTEXT)['replies'] == ['one']
    assert restarted.bundle_status()['revision'] == 'a' * 40
    candidate('c' * 40, {'Three':'reply newbundle'})
    watcher.check()
    assert set(runtime.status()) == {'Three'}
    assert runtime.invoke('Three', CONTEXT)['replies'] == ['newbundle']
    files.close()


def test_durability_failure_does_not_swap_live_bundle(tmp_path):
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=run_limited, gitops=True)
    runtime.activate_bundle({'One':'reply old'}, 'a' * 40)
    def fail():
        raise OSError('synthetic disk full')
    with pytest.raises(OSError):
        runtime.activate_bundle({'One':'reply new'}, 'b' * 40, persist=fail)
    assert runtime.invoke('One', CONTEXT)['replies'] == ['old']
    assert runtime.bundle_status()['revision'] == 'a' * 40
    files.close()


def test_postrename_fsync_failure_commits_matching_live_state_with_warning(tmp_path, monkeypatch):
    import os
    import stat
    from limnoria_mcp.bundle_runtime import persist_last_good, read_last_good
    files = ScriptFiles(str(tmp_path))
    runtime = ScriptRuntime(files, execute=run_limited, gitops=True)
    runtime.activate_bundle({'One':'reply old'}, 'a' * 40)
    real_fsync = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic directory fsync failure after rename')
        return real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    path = str(tmp_path / 'last-good.json')
    runtime.activate_bundle({'One':'reply new'}, 'b' * 40,
        persist=lambda: persist_last_good(path, 'b' * 40, {'One':'reply new'}))
    assert runtime.invoke('One', CONTEXT)['replies'] == ['new']
    assert read_last_good(path) == ('b' * 40, {'One':'reply new'})
    assert 'uncertain' in runtime.bundle_status()['warning']
    files.close()


def test_bundle_swap_preserves_inflight_snapshot_and_removes_old_names(tmp_path):
    import threading
    files = ScriptFiles(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    def execute(request):
        if request['context']['text'] == 'hold':
            entered.set()
            assert release.wait(5)
        return run_limited(request)
    runtime = ScriptRuntime(files, execute=execute, gitops=True)
    runtime.activate_bundle({'Old':'state_set value old; reply old', 'Keep':'reply keep'}, 'a' * 40)
    results = []
    thread = threading.Thread(target=lambda: results.append(runtime.invoke('Old', {**CONTEXT,'text':'hold'})))
    thread.start()
    assert entered.wait(5)
    runtime.activate_bundle({'New':'reply "new[state_get value]"'}, 'b' * 40)
    release.set()
    thread.join(5)
    assert results[0]['replies'] == ['old']
    with pytest.raises(ValueError, match='not active'):
        runtime.invoke('Old', CONTEXT)
    assert runtime.invoke('New', CONTEXT)['replies'] == ['new']
    assert runtime.bundle_status()['revision'] == 'b' * 40
    files.close()
