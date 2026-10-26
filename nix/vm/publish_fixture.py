import sys
from limnoria_mcp.bundle_update import publish_candidate
mode = sys.argv[1]
revision = {'a':'a', 'b':'b', 'bad':'c'}[mode] * 40
source = 'exec id' if mode == 'bad' else 'reply revision-' + mode
publish_candidate('/var/lib/limnoria-script-bundles',
    'https://github.com/example/fixture', 'x86_64-linux',
    build=lambda repository, system, home: (revision, {'Greeting': source}))
