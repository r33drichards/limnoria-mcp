# Public HTTP capability security contract

The optional dedicated broker provides bounded HTTP/HTTPS GET (RSS), POST
(webhooks), PUT and DELETE without a hostname allowlist. It is not general networking access.
The Tcl worker remains a different UID with `PrivateNetwork` and AF_UNIX only;
HTTP is requested through a fixed Unix socket, checked against one explicit
caller UID using Linux `SO_PEERCRED`. The broker has no script/state/credential
mounts and must not run as the bot or worker user.

## Tcl API

- `http_get URL`
- `http_post URL BODY CONTENT_TYPE`
- `http_put URL BODY CONTENT_TYPE`
- `http_delete URL`

POST/PUT bodies are UTF-8 text and CONTENT_TYPE is required; DELETE has no body
in the Tcl API. Results are dictionaries with `status`, `body`, `operation_id`
and `outcome`; response bodies are decoded as UTF-8 with replacement. Validation
returns status `0`, empty body/operation ID and outcome `dry_run`; scripts should
handle that sentinel before parsing response-dependent content. At most two
HTTP calls are permitted per invocation. An uncertain mutating request blocks
all subsequent HTTP calls in that invocation, including explicit caught retries.

## Destination policy

All answers from a single DNS lookup must pass public-address validation. One
validated numeric IP is passed directly to `connect`; HTTP Host and HTTPS
SNI/certificate validation retain the original URL hostname. No second lookup,
address fallback, redirect following, or request retry occurs. A redirect is a
normal 3xx result; any new request must go through the entire policy again.
IPv4/IPv6 loopback, private, link-local, multicast, reserved, documentation,
CGNAT, known metadata and Azure WireServer addresses are rejected. IPv4-mapped
IPv6, standard NAT64 prefixes, 6to4 and Teredo/transition ranges are rejected.
Every current local interface address is rejected even if globally numbered.
Linux `getifaddrs` needs AF_NETLINK for that local-only enumeration; if interface
enumeration fails the request fails closed. Deployment must not introduce custom
network translation routes that map otherwise-public addresses to internal
services: application address checks cannot detect arbitrary administrator-
configured NAT/proxy/routing changes.

Only ASCII HTTP(S) URLs without URL credentials/fragments/control characters are
accepted. Ports 1–65535 are supported. Request headers are limited to Accept,
Content-Type, User-Agent and Authorization; only explicitly supplied values are
used. No proxy environment variables, netrc, inherited HTTP credentials,
cookie jar or inherited CA-path overrides are used. TLS requires the system
OpenSSL CA trust paths (or the immutable package-selected CA bundle) and certificate/hostname verification; inability to load
trust fails closed. HTTPS is recommended when content is sensitive.

## Quotas and effects

Requests have an eight-second total alarm deadline including DNS, connect,
TLS, status/headers and slow response bodies. Bodies are limited to 64 KiB sent
and 256 KiB received; response status/headers are limited to 16 KiB. Encoded
responses are refused rather than decompressed. The Unix protocol has a
512 KiB frame cap and bounded reads. The caller first waits at most two seconds
for a fixed READY admission message before transmitting any operation bytes,
then starts a separate ten-second request/response budget. A client that gives
up while the sequential broker is busy leaves no queued operation to run later.
After request bytes are transmitted, a disconnect remains an uncertain outcome;
admission is not cancellation or exactly-once delivery. GET cannot contain a body. Only GET/POST/PUT/DELETE
are supported. The supervisor further limits calls per Tcl invocation.

Validation/staging performs pure syntax validation and returns synthetic data.
It does **not** contact the broker, resolve DNS, fetch RSS, or deliver webhooks.
Consequently validation does not establish destination reachability or validate
response-dependent script paths. A malformed or non-public live destination is
rejected when invoked; staging is not an authorization to send a request.

Request operation IDs originate in the trusted supervisor, not Tcl. Results
carry `completed`, `failed_before_send`, or `unknown`. Completed means an HTTP
response was received, not that the remote application committed an operation.
A timeout or lost IPC/HTTP response can mean a POST, PUT or DELETE was delivered; it is never
automatically retried. These are correlation IDs, **not** remote idempotency
keys and do not guarantee exactly-once delivery. Later Tcl errors cannot roll
back external HTTP effects. Inspect effects before manually retrying anything.

## Disclosure boundary

Public egress can transmit anything explicitly supplied by a script, including
IRC context or persisted state. This is **not** a privacy/content filter and does
not establish consent to disclose private information to an arbitrary endpoint.
No actual endpoint, secret, credential profile, private-data grant or deployment
is configured by this implementation. Do not mount bot credentials, MCP tokens,
private keys, or unrelated host files into the broker. Error responses omit
URLs, bodies and request headers to avoid leaking embedded secrets into logs.

Tests inject synthetic DNS/interface/transport adapters and fake sockets. No
external fetch/webhook is made, and production never enables a local-destination
bypass for tests.
