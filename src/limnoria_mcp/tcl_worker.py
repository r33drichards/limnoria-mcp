"""Fresh-process Safe Tcl executor; run from the dedicated unprivileged worker UID.

This is not an in-process bot sandbox. Deployment must isolate the worker account,
network and filesystem from bot credentials. Each invocation has OS resource limits
and an external kill/reap deadline in addition to Tcl interpreter limits.

API source: https://www.tcl-lang.org/man/tcl9.0/TclCmd/interp.html
"""
import json
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
    if not isinstance(request, dict) or set(request) != {'script', 'state', 'context'}:
        raise ValueError('Expected script, state and context')
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


def run_limited(request):
    """Fixed one-request subprocess, hard wall-clock deadline and fail-closed result."""
    validate_request(request)
    payload = json.dumps(request, ensure_ascii=False).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError('Request exceeds limit')
    try:
        result = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).resolve()), '--child'],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT_SECONDS, check=False, close_fds=True,
            cwd='/', env={'LANG': 'C.UTF-8'},
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the child before raising.
        raise RuntimeError('Tcl worker exceeded wall-clock deadline') from None
    if result.returncode != 0 or len(result.stdout) > 24 * 1024:
        raise RuntimeError('Tcl worker terminated or exceeded resource limits')
    return validate_response(json.loads(result.stdout))


def validate_response(response):
    """Pure protocol/capability validation shared by worker and immutable bot host."""
    if not isinstance(response, dict) or set(response) != {'replies', 'state'}:
        raise ValueError('Invalid Tcl worker result')
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


def _execute(request):
    if TKINTER_SEARCH_PATH is not None:
        sys.path.insert(0, TKINTER_SEARCH_PATH)
    import tkinter
    validate_request(request)
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
                           ('state_set', state_set), ('context_get', context_get)]:
        target = '_host_' + name
        interpreter.createcommand(target, checked(callback))
        interpreter.call('interp', 'alias', child, name, '', target)
    interpreter.call('interp', 'recursionlimit', child, 64)
    interpreter.call('interp', 'limit', child, 'command', '-value', 10000, '-granularity', 1)
    deadline = time.time() + 0.5
    interpreter.call('interp', 'limit', child, 'time', '-seconds', int(deadline),
                     '-milliseconds', int((deadline % 1) * 1000), '-granularity', 1)
    # tkinter's object API treats the entire untrusted script as a single argument.
    # It is evaluated only by the safe child, never interpolated into parent Tcl.
    interpreter.call('interp', 'eval', child, request['script'])
    if capability_failed:
        raise ValueError('A host capability was rejected')
    return {'replies': replies, 'state': state}


def main():
    if sys.argv[1:] != ['--child']:
        raise SystemExit('This immutable child is launched only by the worker service')
    _apply_limits()
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('Request exceeds limit')
        result = _execute(json.loads(raw))
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except BaseException:
        # Never leak host exception details or emit partial replies/state on error.
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
