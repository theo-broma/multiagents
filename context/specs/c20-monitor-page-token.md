# C20 — the monitor page itself requires the token: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Found by:** the user, who removed `?token=` from the URL and still got the page.
- **Ids:** `MT-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer. It starts after C19 merges, because both touch `monitor/server.py`.

## The defect

`monitor/server.py` `do_GET`, around lines 88-90, serves `/` and `/index.html` without checking the token. It then substitutes the real token into the page (`PAGE.read_text().replace("__TOKEN__", self.token)`).

So any process that can reach `127.0.0.1:<port>` reads the token with one `GET /`, then drives `/api/*`, including `/api/action`, which can stop and steer agents and rewrite config. That process could be another local user on a shared machine, or any program.

The module docstring claims the token protects against "other programs on this machine". It does not. What it does protect against is cross-site requests from a browser tab, because the same-origin policy stops that tab from reading the page. In practice, then, the token works only as a CSRF token.

A related gap: `_host_is_ours()` (~68-77) also accepts an empty or missing `Host` header.

## Behaviours

**MT-R1: no page without the token.**
- `GET /` and `/index.html` require the token, given as the `?token=` query on the first load, as the URL printed at start-up does today.
- Without it, or with a wrong one, the server answers 403 with a short plain page saying "open the URL printed when the monitor started". It never embeds the token in that answer.
- **Verified by:** `GET /` without a token, and with a wrong token, gives 403, and the response body does not contain the real token. With the right token it gives 200 and the page works as today.

**MT-R2: the token does not linger in the address bar.**
- After a successful tokened load, the page removes the token from the visible URL and from history, using `history.replaceState`. It keeps the token in memory for its `X-Monitor-Token` requests.
- A reload of the bare URL then gives MT-R1's 403. This is accepted: the user reopens the printed URL.
- Responses carry `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- **Verified by:** the served page contains the `replaceState` call. Both headers are present on `/`.

**MT-R3: the Host header is required.**
- A request with no `Host` header, or an empty one, is refused like a foreign Host.
- **Verified by:** a raw HTTP/1.0 request without `Host` gives 403 on `/` and on `/api/state`.

**MT-R4: the docstring tells the truth.**
- The module docstring states the real threat model:
  - the bind keeps the network out;
  - the token, now required on every route including `/`, keeps out other local processes that do not have the printed URL, and cross-site browser requests;
  - the Host check defeats DNS rebinding.

**MT-R5: no regression.**
- `/api/*` keeps its header or query token check.
- C19's `POST /quota` handoff and its routes keep working.
- The TUI monitor (`--tui`), which does not use HTTP, is unaffected.
- The existing monitor-server tests stay green, or are updated deliberately by the tester where they assumed a tokenless `/`.
