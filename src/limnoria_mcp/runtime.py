"""Immutable generation snapshots and a fixed, separately isolated worker client."""
import hashlib
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from collections import deque
from contextlib import contextmanager
from .files import script_name

WORKER_SOCKET = '/run/limnoria-tcl-worker/worker.sock'
MAX_RESPONSE = 64 * 1024
WORKER_RPC_LOCK = threading.Lock()


@contextmanager
def _admit(lock, message):
    if not lock.acquire(blocking=False):
        raise RuntimeError(message)
    try:
        yield
    finally:
        lock.release()


def execute_worker(request):
    # The dedicated worker broker is serial. Serialize before connecting so an
    # invocation's socket deadline cannot expire while queued behind another run.
    # Fail fast before dispatch: waiting could outlive an HTTP caller's deadline
    # and start a mutation or activation after the caller has already timed out.
    if not WORKER_RPC_LOCK.acquire(blocking=False):
        raise RuntimeError('Worker busy; request was not dispatched')
    try:
        return _execute_worker_unlocked(request)
    finally:
        WORKER_RPC_LOCK.release()


def _execute_worker_unlocked(request):
    payload = json.dumps(request, ensure_ascii=True).encode() + b'\n'
    if len(payload) > 256 * 1024:
        raise ValueError('Worker request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        # No executable payload is sent while queued behind an earlier broker
        # request (including one belonging to a previous bot process).
        conn.settimeout(2)
        conn.connect(WORKER_SOCKET)
        ready = bytearray()
        admission_deadline = time.monotonic() + 2
        while len(ready) < 6:
            remaining = admission_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Worker admission timed out; request was not dispatched')
            conn.settimeout(remaining)
            chunk = conn.recv(6 - len(ready))
            if not chunk:
                break
            ready.extend(chunk)
        if ready != b'READY\n' or time.monotonic() >= admission_deadline:
            raise RuntimeError('Worker admission failed; request was not dispatched')
        conn.settimeout(30 if request.get("phase") == "run" else 5)
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
        from .tcl_worker import TclExecutionError, validate_effects
        effects = validate_effects(result.get('effects', []))
        raise TclExecutionError('Safe Tcl execution failed', effects=effects)
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
        self.effects = deque(maxlen=64)

    def _candidate(self, name):
        script_name(name)
        source = self.files.read(name)
        digest = hashlib.sha256(source.encode()).hexdigest()
        # Staging never sends IRC and cannot modify live state. Context-dependent
        # runtime errors remain possible; validation is not a proof of all paths.
        self.execute({'script': source, 'state': {}, 'phase':'validate', 'context': {
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
        with _admit(self.activations, 'Activation busy; candidate was not dispatched'):
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
        with _admit(self.activations, 'Activation busy; candidate was not dispatched'):
            prepared = {}
            for name, source in candidates.items():
                script_name(name)
                if not isinstance(source, str) or len(source.encode()) > 65536:
                    raise ValueError('Invalid script source')
                self.execute({'script': source, 'state': {}, 'phase':'validate', 'context': {
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

    def network_status(self):
        with self.lock:
            return list(self.effects)

    def _record_effects(self, name, generation, effects, failed):
        if effects:
            with self.lock:
                self.effects.append({'script':name, 'generation':generation.number,
                                     'script_failed':failed, 'operations':effects})

    def invoke(self, name, context):
        script_name(name)
        with self.lock:
            generation = self.active.get(name)
        if generation is None:
            raise ValueError('Script is not active')
        with _admit(generation.lock, 'Script busy; invocation was not dispatched'):
            try:
                result = self.execute({'script': generation.source, 'phase':'run',
                                       'state': dict(generation.state), 'context': context})
            except Exception as exc:
                self._record_effects(name, generation, getattr(exc, 'effects', []), True)
                raise
            generation.state = dict(result['state'])
            self._record_effects(name, generation, result.get('effects', []), False)
        response = {'generation': generation.number, 'replies': result['replies']}
        if result.get('effects'):
            response['effects'] = result['effects']
        return response
