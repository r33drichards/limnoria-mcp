import sys
from limnoria_mcp.files import ScriptFiles
files = ScriptFiles('/var/lib/limnoria-managed-scripts')
name = sys.argv[2] if len(sys.argv) == 3 else 'AgentFixture'
if len(sys.argv) >= 2:
    files.write(name, 'reply {' + sys.argv[1] + '}\n')
print(files.read(name))
files.close()
