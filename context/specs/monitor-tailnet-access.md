# Reaching the monitor from another device through a reverse proxy (MT)

Source: user, 2026-10-06: open the monitor on a phone through Tailscale
(`tailscale serve` → `https://<host>.<tailnet>.ts.net`). Decision: "oui, lance
--allow-host avec option de jeton fixe".

Today (`src/multiagents/monitor/server.py`) there are three protections:
- the monitor binds to 127.0.0.1;
- every route needs a per-run token;
- the Host header must be a loopback name.

The third one refuses a request that comes through a proxy keeping the public
Host, which `tailscale serve` does. The token changes on every start, so a
bookmark on the phone stops working.

## Behaviours

**MT-R1.** `multiagents monitor --allow-host NAME` makes the monitor accept
NAME as a Host:
- the option may be repeated;
- with or without a port in the Host header, compared the way loopback names
  already are;
- the match ignores case and is exact: `x.example.ts.net` does not admit
  `evil.example.ts.net`.

A request whose Host is not loopback or an allowed name is still refused,
before the token is checked.
Verified by: handler tests with Host = allowed name, allowed name with a port,
another name, a subdomain of the allowed name, and empty.

**MT-R2.** The Origin check accepts an Origin whose host is an allowed name,
over http or https and with any port, when the request's Host is also allowed.
A cross-site Origin is still refused.
Verified by: tests of POST actions with Origin `https://NAME`, with a foreign
Origin, and with no Origin.

**MT-R3.** The bind address never changes: it is 127.0.0.1 whatever options are
given. `--allow-host` refuses at start (exit 2, message, nothing listening):
- an empty value;
- a value containing `*`, `/`, `:` or whitespace;
- an IP literal.
Verified by: a test that the listening socket is loopback with
`--allow-host`; and one test per refused value.

**MT-R4.** `--persistent-token` reuses one token across starts:
- The first start creates it and stores it outside the project tree, in the
  user's state directory (`$XDG_STATE_HOME/multiagents/`, defaulting to
  `~/.local/state/multiagents/`), one file per project.
- The file is mode 0600 in a directory of mode 0700, so it is never somewhere
  an agent's worktree or the project mount exposes.
- Later starts with the flag reuse it.
- A start without the flag still mints a fresh per-run token and leaves the
  stored one in place.
- A stored token that is unreadable, empty, shorter than the minted length, or
  in a file readable by group or others is not used. The monitor refuses to
  start, says why and suggests `--rotate-token`. It never silently replaces
  the token.

Verified by: tests with a temp state dir. Two starts give the same token. A
start without the flag gives a different one. The file and directory modes
are as stated. Each refused case is covered.

**MT-R5.** `--rotate-token` (which implies `--persistent-token`) replaces the
stored token with a new one before serving. An old bookmark stops working.
Verified by: a test that the token differs before and after, and that the old
one is refused.

**MT-R6.** At start, besides the loopback URL, the monitor prints
`https://NAME/?token=…` for each allowed name, so the phone URL can be copied
directly. The token never appears in any log or in the process arguments.
Verified by: a test on the start output; and a test that no option takes the
token as a value.

**MT-R7.** Nothing changes without these flags. The behaviour and the existing
monitor tests stay as they are.
Verified by: the existing monitor tests, unchanged and green.

## Out of scope
- Running `tailscale serve` or Funnel from multiagents. The user runs
  `tailscale serve --bg 8787` themselves, and Funnel (public internet) is never
  used.
- A config-file key for these options. Use the CLI flags only.
- The TUI front end.

## Clarifications (2026-10-06, answering tester ag-01b831)
- MT-R2 covers POST `/api/action` too, which today never checks Origin. A request carrying an Origin that fails the rule is refused on every POST route. A request with no Origin keeps today's behaviour.
- MT-R2 matching: the Origin's host must be the same name as the request's Host, compared case-insensitively. That name must be loopback or an allowed name. For an allowed name, the scheme may be http or https and the ports are not compared, because a proxy terminates TLS on another port. Loopback keeps today's exact rule.
- MT-R4: a refused stored token exits with a non-zero code. A plain start (without `--persistent-token`) ignores the stored file, even a corrupt one, and leaves it untouched. "Minted length" is the length of a token the monitor mints today.
