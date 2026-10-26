"""Fresh-process Safe Tcl executor; run from the dedicated unprivileged worker UID.

This is not an in-process bot sandbox. Deployment must isolate the worker account,
network and filesystem from bot credentials. Each invocation has OS resource limits
and an external kill/reap deadline in addition to Tcl interpreter limits.

API source: https://www.tcl-lang.org/man/tcl9.0/TclCmd/interp.html
"""
import base64
import json
import uuid
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

MAX_SCRIPT_BYTES = 64 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_STATE_KEYS = 16
MAX_REQUEST_BYTES = 96 * 1024
MAX_REPLY_BYTES = 350
MAX_REPLIES = 3
TIMEOUT_SECONDS = 2
RUN_TIMEOUT_SECONDS = 24
MAX_HTTP_CALLS = 2
EFFECT_PREFIX = b"LIMNORIA_EFFECT "
OUTCOMES = frozenset({"completed", "failed_before_send", "unknown"})
# Nix substitutes this literal with its immutable tkinter package site-packages.
# Never derive this from request data or environment (the child uses -I).
TKINTER_SEARCH_PATH = None
STATE_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}\Z')
CONTEXT_KEYS = frozenset({'network', 'channel', 'nick', 'text'})
# Start with interp -safe, then reduce its visible command surface further.
SAFE_COMMANDS = frozenset('''append apply array break catch concat continue dict error eval
expr for foreach format global if incr info join lappend lindex linsert list llength
lmap lrange lrepeat lreplace lreverse lsearch lset lsort proc regexp regsub return scan
set split string subst switch throw try unset uplevel upvar variable while'''.split())


def _text(value, maximum, label):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or '\x00' in value:
        raise ValueError('Invalid ' + label)
    return value


def validate_request(request):
    if not isinstance(request, dict) or set(request) not in ({'script', 'state', 'context'}, {'script', 'state', 'context', 'phase'}):
        raise ValueError('Expected script, state and context')
    if request.get('phase', 'validate') not in {'validate', 'run'}:
        raise ValueError('Invalid trusted execution phase')
    _text(request['script'], MAX_SCRIPT_BYTES, 'script')
    state = request['state']
    if not isinstance(state, dict) or len(state) > MAX_STATE_KEYS:
        raise ValueError('Invalid state')
    for key, value in state.items():
        if not isinstance(key, str) or not STATE_KEY.fullmatch(key):
            raise ValueError('Invalid state key')
        _text(value, 1024, 'state value')
    if len(json.dumps(state, ensure_ascii=False).encode()) > MAX_STATE_BYTES:
        raise ValueError('State quota exceeded')
    context = request['context']
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        raise ValueError('Explicit network, channel, nick and text context required')
    for key, value in context.items():
        _text(value, 2048 if key == 'text' else 128, 'context ' + key)
        if any(c in value for c in '\r\n'):
            raise ValueError('Context must be a single line')
    return request


class TclExecutionError(RuntimeError):
    def __init__(self, message, effects=None):
        self.effects = validate_effects([] if effects is None else effects)
        super().__init__(message)


def validate_effects(effects):
    if not isinstance(effects, list) or len(effects) > MAX_HTTP_CALLS:
        raise ValueError('Invalid external effect count')
    seen = set()
    for effect in effects:
        if (not isinstance(effect, dict) or set(effect) != {'operation_id', 'method', 'outcome'}
                or not isinstance(effect['operation_id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}:[12]', effect['operation_id'])
                or effect['operation_id'] in seen
                or effect['method'] not in {'GET', 'POST', 'PUT', 'DELETE'}
                or effect['outcome'] not in OUTCOMES):
            raise ValueError('Invalid external effect metadata')
        seen.add(effect['operation_id'])
    return effects


def _read_effects(stderr, run_id):
    effects = {}
    for line in stderr.splitlines():
        if not line.startswith(EFFECT_PREFIX) or len(line) > 512:
            continue
        try:
            effect = json.loads(line[len(EFFECT_PREFIX):])
            validate_effects([effect])
            if effect['operation_id'].split(':')[0] != run_id:
                continue
            old = effects.get(effect['operation_id'])
            if old is None or old['method'] == effect['method']:
                effects[effect['operation_id']] = effect
        except (ValueError, TypeError, KeyError):
            continue
    return validate_effects(list(effects.values()))


def run_limited(request):
    """Capture effect records even when the child fails or must be killed."""
    validate_request(request)
    run_id = uuid.uuid4().hex  # Never accepted from script/request caller.
    payload = json.dumps({'request': request, 'run_id': run_id}, ensure_ascii=False).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError('Request exceeds limit')
    timeout = RUN_TIMEOUT_SECONDS if request.get('phase', 'validate') == 'run' else TIMEOUT_SECONDS
    with subprocess.Popen(
        [sys.executable, '-I', str(Path(__file__).resolve()), '--child'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, cwd='/', env={'LANG': 'C.UTF-8'},
    ) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate()  # Drain audit records and reap.
        effects = _read_effects(stderr, run_id)
        if timed_out:
            raise TclExecutionError('Tcl worker exceeded wall-clock deadline; external effects are not rolled back', effects)
        if process.returncode != 0 or len(stdout) > 24 * 1024:
            raise TclExecutionError('Tcl worker terminated or rejected script; external effects are not rolled back', effects)
    try:
        response = json.loads(stdout)
        response['effects'] = effects  # Supervisor audit stream is authoritative.
        return validate_response(response)
    except (ValueError, TypeError):
        raise TclExecutionError('Invalid worker response; external effects are not rolled back', effects) from None


def validate_response(response):
    """Pure protocol/capability validation shared by worker and immutable bot host."""
    if not isinstance(response, dict) or set(response) != {'replies', 'state', 'effects'}:
        raise ValueError('Invalid Tcl worker result')
    validate_effects(response['effects'])
    validate_request({'script': '', 'state': response['state'],
                      'context': {key: '' for key in CONTEXT_KEYS}})
    replies = response['replies']
    if not isinstance(replies, list) or len(replies) > MAX_REPLIES:
        raise ValueError('Invalid reply count')
    for reply in replies:
        _validate_reply(reply)
    return response


def _validate_reply(text):
    _text(text, MAX_REPLY_BYTES, 'reply')
    if not text or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Reply must be nonempty single-line plain text')
    return text


def _apply_limits():
    if os.geteuid() == 0:
        raise RuntimeError('Refusing to execute scripts as root')
    # The process has already been created by the trusted service before NPROC=0.
    for kind, limit in ((resource.RLIMIT_CPU, 1), (resource.RLIMIT_AS, 128 * 1024 * 1024),
                        (resource.RLIMIT_NPROC, 0), (resource.RLIMIT_FSIZE, 0),
                        (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 32)):
        resource.setrlimit(kind, (limit, limit))


def _execute(request, run_id=None, effect_sink=None):
    if TKINTER_SEARCH_PATH is not None:
        sys.path.insert(0, TKINTER_SEARCH_PATH)
    import tkinter
    validate_request(request)
    run_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Invalid supervisor run identifier')
    interpreter = tkinter.Tcl()
    version = tuple(map(int, str(interpreter.call('info', 'patchlevel')).split('.')[:2]))
    if version < (8, 6):
        raise RuntimeError('Tcl 8.6 or newer is required')
    child = 'script'
    interpreter.call('interp', 'create', '-safe', child)
    # Tcl ensembles also expose fully-qualified implementation commands. Hiding
    # only global `file`/`chan` leaves ::tcl::file::* / ::tcl::chan::* reachable.
    # Delete non-computational namespaces before hiding namespace/rename itself.
    keep_namespaces = {'::tcl::string', '::tcl::dict', '::tcl::array',
                       '::tcl::info', '::tcl::mathfunc', '::tcl::mathop'}
    roots = interpreter.splitlist(interpreter.call('interp', 'eval', child,
                                                  ('namespace', 'children', '::')))
    children = interpreter.splitlist(interpreter.call('interp', 'eval', child,
                                                     ('namespace', 'children', '::tcl')))
    remove = [str(ns) for ns in roots if str(ns) != '::tcl']
    remove.extend(str(ns) for ns in children if str(ns) not in keep_namespaces)
    interpreter.call('interp', 'eval', child, ('namespace', 'delete', *remove))
    internal = interpreter.splitlist(interpreter.call('interp', 'eval', child,
                                                     ('info', 'commands', '::tcl::*')))
    for command in internal:
        interpreter.call('interp', 'eval', child, ('rename', str(command), ''))
    visible = interpreter.splitlist(interpreter.call('interp', 'eval', child, 'info commands'))
    for command in visible:
        if command not in SAFE_COMMANDS:
            interpreter.call('interp', 'hide', child, command)
    state = dict(request['state'])
    replies = []
    calls = 0
    capability_failed = False
    effects = []
    network_calls = 0
    uncertain_mutation = False

    def audit(effect):
        validate_effects([effect])
        if effect_sink is not None:
            effect_sink(dict(effect))
        else:
            sys.stderr.buffer.write(EFFECT_PREFIX + json.dumps(effect).encode() + b'\n')
            sys.stderr.buffer.flush()  # BEFORE dispatch: survives failure/kill.

    def http_request(method, url, body='', content_type=''):
        nonlocal network_calls, uncertain_mutation
        if uncertain_mutation:
            raise ValueError('Further HTTP calls forbidden after uncertain mutation delivery')
        network_calls += 1
        if network_calls > MAX_HTTP_CALLS:
            raise ValueError('HTTP call quota exceeded')
        # Only the immutable sibling module is importable; no script/environment
        # supplied path participates in the isolated child's module resolution.
        package_root = str(Path(__file__).resolve().parent.parent)
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        from limnoria_mcp import public_http
        headers = {'content-type': content_type} if method in {'POST', 'PUT'} else {}
        data = body.encode('utf-8')
        public_http.validate_request(method, url, headers, data)
        if request.get('phase', 'validate') == 'validate':
            # Pure syntax/policy validation: no socket, DNS, GET or POST.
            return ('status', '0', 'body', '', 'operation_id', '', 'outcome', 'dry_run')
        operation_id = run_id + ':' + str(network_calls)
        effect = {'operation_id': operation_id, 'method': method, 'outcome': 'unknown'}
        effects.append(effect)
        audit(effect)
        try:
            response = public_http.call_broker({'method': method, 'url': url, 'headers': headers,
                'body_base64': base64.b64encode(data).decode('ascii'), 'operation_id': operation_id})
            if (response.get('operation_id') != operation_id or response.get('outcome') != 'completed'
                    or type(response.get('status')) is not int or not 100 <= response['status'] <= 599):
                raise ValueError('Invalid public HTTP result')
            decoded = base64.b64decode(response['body_base64'], validate=True)
            if len(decoded) > public_http.MAX_RESPONSE:
                raise ValueError('Public HTTP response exceeded limit')
            effect['outcome'] = 'completed'
            audit(effect)
            return ('status', str(response['status']), 'body', decoded.decode('utf-8', errors='replace'),
                    'operation_id', operation_id, 'outcome', 'completed')
        except Exception as exc:
            if (isinstance(exc, public_http.BrokerError) and exc.operation_id == operation_id
                    and exc.outcome in OUTCOMES):
                effect['outcome'] = exc.outcome
            if method in {'POST', 'PUT', 'DELETE'} and effect['outcome'] == 'unknown':
                uncertain_mutation = True
            audit(effect)
            raise

    def http_get(url):
        return http_request('GET', url)

    def http_post(url, body, content_type):
        return http_request('POST', url, body, content_type)

    def http_put(url, body, content_type):
        return http_request('PUT', url, body, content_type)

    def http_delete(url):
        return http_request('DELETE', url)

    def checked(function):
        def callback(*args):
            nonlocal calls, capability_failed
            calls += 1
            try:
                if calls > 128:
                    raise ValueError('Capability call quota exceeded')
                return function(*args)
            except BaseException:
                capability_failed = True
                raise
        return callback

    def reply(text):
        _validate_reply(text)
        if len(replies) >= MAX_REPLIES:
            raise ValueError('Reply quota exceeded')
        replies.append(text)
        return ''

    def state_get(key):
        if not STATE_KEY.fullmatch(key):
            raise ValueError('Invalid state key')
        return state.get(key, '')

    def state_set(key, value):
        if not STATE_KEY.fullmatch(key):
            raise ValueError('Invalid state key')
        candidate = dict(state)
        candidate[key] = value
        validate_request({'script': '', 'state': candidate, 'context': request['context']})
        state.clear()
        state.update(candidate)
        return ''

    def context_get(key):
        if key not in CONTEXT_KEYS:
            raise ValueError('Invalid context key')
        return request['context'][key]

    for name, callback in [('reply', reply), ('state_get', state_get),
                           ('state_set', state_set), ('context_get', context_get),
                           ('http_get', http_get), ('http_post', http_post),
                           ('http_put', http_put), ('http_delete', http_delete)]:
        target = '_host_' + name
        interpreter.createcommand(target, checked(callback))
        interpreter.call('interp', 'alias', child, name, '', target)
    interpreter.call('interp', 'recursionlimit', child, 64)
    interpreter.call('interp', 'limit', child, 'command', '-value', 10000, '-granularity', 1)
    deadline = time.time() + (23 if request.get('phase', 'validate') == 'run' else 0.5)
    interpreter.call('interp', 'limit', child, 'time', '-seconds', int(deadline),
                     '-milliseconds', int((deadline % 1) * 1000), '-granularity', 1)
    # tkinter's object API treats the entire untrusted script as a single argument.
    # It is evaluated only by the safe child, never interpolated into parent Tcl.
    interpreter.call('interp', 'eval', child, request['script'])
    if capability_failed:
        raise ValueError('A host capability was rejected')
    return {'replies': replies, 'state': state, 'effects': effects}


def main():
    if sys.argv[1:] != ['--child']:
        raise SystemExit('This immutable child is launched only by the worker service')
    _apply_limits()
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('Request exceeds limit')
        envelope = json.loads(raw)
        result = _execute(envelope['request'], envelope['run_id'])
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except BaseException:
        # Never leak host exception details or emit partial replies/state on error.
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
