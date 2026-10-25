"""Immutable generation snapshots and a fixed, separately isolated worker client."""
import hashlib
import json
import socket
import threading
from dataclasses import dataclass, field
from .files import script_name

WORKER_SOCKET = '/run/limnoria-tcl-worker/worker.sock'
MAX_RESPONSE = 64 * 1024


def execute_worker(request):
    payload = json.dumps(request, ensure_ascii=True).encode() + b'\n'
    if len(payload) > 256 * 1024:
        raise ValueError('Worker request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        conn.connect(WORKER_SOCKET)
        conn.sendall(payload)
        conn.shutdown(socket.SHUT_WR)
        chunks = bytearray()
        while len(chunks) <= MAX_RESPONSE:
            chunk = conn.recv(8192)
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_RESPONSE:
            raise ValueError('Worker response too large')
    result = json.loads(chunks)
    if not isinstance(result, dict):
        raise ValueError('Invalid worker response')
    if 'error' in result:
        raise ValueError('Safe Tcl rejected script: ' + str(result['error'])[:512])
    # Repeat the worker's exact typed quotas before anything reaches IRC.
    from .tcl_worker import validate_response
    return validate_response(result)



@dataclass
class Generation:
    source: str
    digest: str
    number: int
    state: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ScriptRuntime:
    def __init__(self, files, execute=execute_worker, gitops=False):
        self.files, self.execute = files, execute
        self.gitops = gitops
        self.revision = None
        self.bundle_error = None
        self.bundle_warning = None
        self.active = {}
        self.lock = threading.Lock()
        self.activations = threading.Lock()

    def _candidate(self, name):
        script_name(name)
        source = self.files.read(name)
        digest = hashlib.sha256(source.encode()).hexdigest()
        # Staging never sends IRC and cannot modify live state. Context-dependent
        # runtime errors remain possible; validation is not a proof of all paths.
        self.execute({'script': source, 'state': {}, 'context': {
            'network': 'validation', 'channel': '#validation', 'nick': 'validator', 'text': ''}})
        return source, digest

    def validate(self, name):
        _, digest = self._candidate(name)
        return {'script': name, 'sha256': digest, 'valid': True}

    def activate(self, name):
        if self.gitops:
            raise ValueError('Direct activation disabled: publish changes through configured main')
        # Serialize candidate validation/activation, but never block old execution
        # on a global lock. Each invocation captures one immutable generation.
        with self.activations:
            source, digest = self._candidate(name)
            with self.lock:
                old = self.active.get(name)
                if old is None and len(self.active) >= 64:
                    raise ValueError('Active script limit reached')
                generation = Generation(source, digest, old.number + 1 if old else 1)
                # State deliberately resets on activation. Old in-flight requests
                # finish against their old state and cannot overwrite the new one.
                self.active[name] = generation
        return {'script': name, 'generation': generation.number, 'sha256': digest}

    def activate_bundle(self, candidates, revision, persist=None):
        if (not isinstance(candidates, dict) or not candidates or len(candidates) > 64 or
            sum(len(v.encode()) for v in candidates.values() if isinstance(v, str)) > 1024 * 1024):
            raise ValueError('Invalid bundle size')
        with self.activations:
            prepared = {}
            for name, source in candidates.items():
                script_name(name)
                if not isinstance(source, str) or len(source.encode()) > 65536:
                    raise ValueError('Invalid script source')
                self.execute({'script': source, 'state': {}, 'context': {
                    'network':'validation', 'channel':'#validation', 'nick':'validator', 'text':''}})
                prepared[name] = (source, hashlib.sha256(source.encode()).hexdigest())
            # Durable full snapshot is committed first. Disk failure leaves both
            # live generation and previous durable last-good unchanged.
            warning = persist() if persist else None
            with self.lock:
                self.active = {name: Generation(source, digest,
                    self.active[name].number + 1 if name in self.active else 1)
                    for name, (source, digest) in prepared.items()}
                self.revision = revision
                self.bundle_error = None
                self.bundle_warning = warning
        return self.bundle_status()

    def bundle_status(self):
        with self.lock:
            return {'mode':'gitops' if self.gitops else 'direct',
                    'revision':self.revision, 'error':self.bundle_error, 'warning':self.bundle_warning}

    def status(self):
        with self.lock:
            return {name: {'generation': value.number, 'sha256': value.digest}
                    for name, value in self.active.items()}

    def invoke(self, name, context):
        script_name(name)
        with self.lock:
            generation = self.active.get(name)
        if generation is None:
            raise ValueError('Script is not active')
        with generation.lock:
            result = self.execute({'script': generation.source,
                                   'state': dict(generation.state), 'context': context})
            generation.state = dict(result['state'])
        return {'generation': generation.number, 'replies': result['replies']}
