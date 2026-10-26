import pathlib, sys
from limnoria_mcp.files import ScriptFiles
from limnoria_mcp.runtime import ScriptRuntime
from limnoria_mcp.bundle_runtime import BundleWatcher
files = ScriptFiles('/var/lib/limnoria-managed-scripts')
runtime = ScriptRuntime(files, gitops=True)
last_good = '/var/lib/supybot/tcl-last-good.json'
watcher = BundleWatcher(runtime, '/var/lib/limnoria-script-bundles', last_good)
watcher.restore()
old = pathlib.Path(last_good).read_bytes() if pathlib.Path(last_good).exists() else None
old_status = runtime.status()
try:
    watcher.check()
except ValueError:
    assert sys.argv[1] == 'reject'
    assert pathlib.Path(last_good).read_bytes() == old
    assert runtime.status() == old_status
else:
    assert sys.argv[1] != 'reject'
expected = sys.argv[2]
assert runtime.bundle_status()['revision'] == expected * 40
reply = runtime.invoke('Greeting', {'network':'offline', 'channel':'#fixture',
                                  'nick':'tester', 'text':''})
assert reply['replies'] == ['revision-' + expected]
try:
    runtime.activate('Greeting')
except ValueError:
    pass
else:
    raise AssertionError('draft activation bypassed main')
files.close()
