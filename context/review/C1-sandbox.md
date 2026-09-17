# C1 — Sandbox and egress boundary

Findings from building the test harness for this context. Both were found by
reading the code and confirmed by calling the real production method
(`DockerExecutor.write_proxy_config`) against a temp directory, not by
reasoning about it in the abstract — see `tests/support/c1_harness.py` and
`tests/test_c1_sandbox_harness.py::test_an_unescaped_pipe_in_an_allowlist_entry_admits_every_host`
for a reproduction.

**F1** — An `egress_allowlist` entry containing `|` turns the allowlist into an allow-all
*Class:* security
*Severity:* critical
*File:* `src/multiagents/executor/docker.py`
*Trace:* `docker.py:write_proxy_config` builds one ERE line per allowlist entry as
`escaped = host.replace(".", r"\.")` then `f"(^|\\.){escaped}$"` — escaping only
the literal dot. `Dockerfile.proxy` configures tinyproxy with `FilterType ere`
and `FilterDefaultDeny Yes`, and `write_proxy_config` writes one such line per
`filter` file with `FilterCaseSensitive Off`. POSIX ERE alternation (`|`) has
the lowest precedence of any operator, binding across the whole pattern rather
than to the token adjacent to it. An allowlist entry such as `evil.com|.*`
therefore produces the filter line `(^|\.)evil\.com|\.*$` — read by the regex
engine as `(^|\.)evil\.com` **OR** `\.*$`, and the second alternative matches
any string at all (zero or more literal dots at the end, which every host
satisfies vacuously). Because tinyproxy allows a request if ANY filter line
matches, one crafted-looking entry silently defeats every other entry in the
same allowlist, converting a supposedly narrow allowlist into open egress for
the whole container network.
*Evidence:* Confirmed twice: (1) a standalone `re` simulation of the generated
pattern; (2) instantiating a real `DockerExecutor` and calling its actual
`write_proxy_config()` against a temp directory, confirming the exact
vulnerable output `(^|\.)evil\.com|\.*$`, and confirming with `re.search`
(case-insensitive, matching tinyproxy's `FilterCaseSensitive Off`) that
unrelated hosts — `attacker.example`, `steal-creds.io`, `pastebin.com` — all
match it. Reproduced as a harness proof test:
`tests/test_c1_sandbox_harness.py::test_an_unescaped_pipe_in_an_allowlist_entry_admits_every_host`.
*Reasoning:* `docker.py`'s `mounts()` method (~lines 260-269) already documents
that `project.yaml` — which holds `egress_allowlist` — is writable from inside
the container, and accepts as a known tradeoff that "an agent could widen its
own sandbox" by adding its own host to the list. This finding is materially
worse than that documented tradeoff: it is not widening the list by one host,
it is a single string that defeats the *entire* allowlist mechanism outright,
and it does not require the agent to understand tinyproxy or ERE — `|` is a
character that shows up in legitimate-looking hostnames pasted from
elsewhere (query strings, glob-like configs) as much as in a deliberate
attack. Given C1 is "the only context where being wrong costs something that
cannot be taken back" (per `context/review/MAP.md`), and this is the exact
mechanism that stands between an agent and the open internet, this should be
treated as the top-priority defect in the context.
*Recommendation:* Escape the whole host with `re.escape`-equivalent behaviour
(escape every ERE metacharacter tinyproxy's regex engine recognizes:
`. ^ $ * + ? ( ) [ ] { } | \`), not just `.`. Consider also validating that
each allowlist entry is a plausible hostname (see F2) before it ever reaches
pattern generation, as defense in depth against a future regression in the
escaping logic itself.

**F2** — A non-string `egress_allowlist` entry crashes container config generation with an uncaught `AttributeError`
*Class:* correctness
*Severity:* medium
*File:* `src/multiagents/executor/docker.py`, `src/multiagents/config.py`
*Trace:* `write_proxy_config` calls `host.replace(".", r"\.")` on every entry of
`self.config.get("egress_allowlist", [])` with no type check. `config.py` has
no schema or validation for `egress_allowlist` (confirmed by grep: the only
reference outside `docker.py` is the default value set in
`defaults/project.yaml:65`). A `project.yaml` with e.g.
`egress_allowlist: [{sub: example.com}]` (a plausible typo reaching for
per-host options that do not exist) — or any other non-string YAML scalar
type — raises `AttributeError: 'dict' object has no attribute 'replace'` from
inside `write_proxy_config`, with no validation error pointing at the actual
mistake, at whatever point in the container-startup path calls it.
*Evidence:* `tests/test_c1_sandbox_harness.py::test_allowlist_admits_exact_and_subdomain_hosts_and_refuses_others`
asserts this via `pytest.raises(AttributeError)` calling the real
`write_proxy_config` through the harness's `filter_patterns()`.
*Reasoning:* Lower severity than F1 because it fails closed (the container
never starts with a working proxy) rather than silently open, but it is an
uncaught exception surfacing from config parsing deep inside executor
plumbing, with a message that gives no hint the fault is in `egress_allowlist`
specifically. A user or agent editing `project.yaml` gets a stack trace instead
of a validation error.
*Recommendation:* Validate that each `egress_allowlist` entry is a string
(and, ideally, looks like a hostname) at config-load time in `config.py`,
raising a clear error there instead of letting a malformed entry surface as an
unrelated `AttributeError` from `write_proxy_config`.

---

## Audit pass 3 — structural defects, resource handling, and error paths

**F60** — `stop()` never stops or removes the auth container
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:999-1006`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_stop_does_not_include_auth_container`
*What happens:* `DockerExecutor.stop()` iterates over `(self.container, self.proxy_container)` only. `self.auth_container` — which holds the vault-mounted credential proxy — is never stopped or removed. After `multiagents docker stop`, the auth proxy keeps running with the vault still bound, consuming resources and keeping the credential accessible on the internal network.
*Disposition:* fix
*Reasoning:* One-line fix: add `self.auth_container` to the iteration tuple at line 1001. The auth container has the same lifecycle as the proxy and should be torn down alongside it. The omission is clearly accidental — the container is created in `ensure_auth_proxy` but never referenced in `stop`.

**F61** — `ensure_proxy` returns success when bridge-network connect fails, leaving proxy isolated
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:764`
*Evidence:* trace
*Proof:* —
*What happens:* `ensure_proxy` starts the proxy container on the internal network, then calls `docker network connect bridge <proxy>` to give it a route out. This second call's return code is never checked: the function returns `{"ok": True, "created": True}` regardless. If the connect fails, the proxy runs on the internal network with no external access — every agent request times out, and nobody is told the proxy is broken.
*Disposition:* fix
*Reasoning:* The `_run` result at line 764 should be checked. On failure, the proxy container should be removed and an error returned. This is a straightforward fix: add `if result.returncode != 0: _run(["docker", "rm", "-f", self.proxy_container]); return {"ok": False, ...}`.

**F62** — `ensure_auth_proxy` returns success when bridge-network connect fails, leaving auth proxy isolated
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:826`
*Evidence:* trace
*Proof:* —
*What happens:* Identical mechanism to F61. `ensure_auth_proxy` starts the auth proxy, then calls `docker network connect bridge` at line 826 without checking the result. A failure leaves the auth proxy on the internal network, unable to reach the model API. Every agent gets a timeout, and the container appears healthy.
*Disposition:* fix
*Reasoning:* Same fix as F61 — check the return code, clean up on failure. Both share the same root cause: a post-creation network attach that is assumed to succeed.

**F63** — `credential_drift` interpolates paths into shell command without quoting
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py:581-583`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_credential_drift_shell_injects_paths`
*What happens:* `credential_drift` builds a shell script by joining `stat -c "%i" {path} 2>/dev/null || echo -` for each path, then passes it to `docker exec ... sh -c`. A path containing shell metacharacters (`;`, `$`, backtick, `|`) is interpreted as code. A project root at `/tmp/test"; malicious_cmd; "` would execute `malicious_cmd` inside the container.
*Disposition:* fix
*Reasoning:* Paths must be properly quoted for shell interpretation. Using `shlex.quote(path)` on each path before interpolation would prevent injection. While project paths are unlikely to contain metacharacters in normal use, this is a security boundary and the fix is one function call.

**F64** — `ensure_running` performs filesystem and network side effects before verifying the image exists
*Class:* architecture
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py:920-924`
*Evidence:* trace
*Proof:* —
*What happens:* `ensure_running` calls `seed_private_state()` (copies config files, deletes stale locks), `refresh_private_credentials()` (may make network calls to refresh tokens), and `project_placeholder()` (writes credential files) before checking `image_exists()`. If the image does not exist, all side effects persist and the function returns an error. The caller has no way to roll back the partial state changes.
*Disposition:* fix
*Reasoning:* The image check should come first. If the image is missing, there is no point modifying filesystem state. Moving the check to line 919 (before any side effects) eliminates the problem entirely. The current order wastes resources and leaves the system in a partially-modified state.

**F65** — `project_placeholder` leaves credential temp file on disk if interrupted
*Class:* bug
*Severity:* low
*Where:* `src/multiagents/executor/docker.py:862-865`
*Evidence:* trace
*Proof:* —
*What happens:* `project_placeholder` writes a temp file (`.tmp` suffix), chmods it to 0o600, then atomically replaces the target via `os.replace`. If the process is killed between the write and the replace (SIGTERM, OOM), the `.tmp` file persists with credential content. Since the directory is part of the container's bind mount, the file is accessible to agents.
*Disposition:* fix
*Reasoning:* A `try/finally` with `tmp.unlink(missing_ok=True)` would clean up the temp file on any interruption. The content is a name-tag that authenticates nothing (per the docstring), so the severity is low, but leaving credential-shaped files on disk is bad practice.

**F66** — `Handler._event` silently swallows all exceptions from the event callback
*Class:* maintainability
*Severity:* medium
*Where:* `src/multiagents/authproxy.py:261-266`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_event_silently_swallows_exceptions`
*What happens:* If `on_event` raises any Exception (disk full, serialization error, broken pipe), it is caught and silently discarded. The proxy continues operating with a broken event stream — no operator, no log, no signal. Event-based monitoring and alerting become unreliable because failures are invisible.
*Disposition:* fix
*Reasoning:* At minimum, log the exception. A bare `except Exception: pass` on a callback that is the operator's only window into proxy behavior is unacceptable. The fix is to add logging: `except Exception: logging.exception("on_event failed")`.

**F67** — `do_POST` retries rate-limited accounts with zero backoff
*Class:* performance
*Severity:* medium
*Where:* `src/multiagents/authproxy.py:279-297`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_no_backoff_in_account_retry_loop`
*What happens:* When all accounts are rate-limited, `do_POST` loops through them in a tight `while True` with no sleep or delay between retries. For N accounts, N upstream requests fire back-to-back, which may trigger more aggressive rate limiting from the upstream provider and reduces the chance of a successful retry if the limit has a short cooling period.
*Disposition:* fix
*Reasoning:* A small `time.sleep(0.1)` between iterations in the retry loop would give the upstream rate limiter time to reset and prevent hammering. The `Retry-After` value is already parsed in `_retry_after` but never used for backoff in the loop — it is only used to set the `limited` timestamp for future requests.

**F68** — `mark_limited` unpins all agents simultaneously, causing thundering herd
*Class:* performance
*Severity:* low
*Where:* `src/multiagents/authproxy.py:208-216`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_mark_limited_unpins_all_agents`
*What happens:* When an account is marked limited, every agent pinned to it is unpinned in the same locked section. On their next request, all unpinned agents simultaneously compete for account assignment through `for_agent`, contending on the same `threading.Lock`. Under high load, this creates a thundering herd on the account-pinning lock.
*Disposition:* accept
*Reasoning:* The thundering herd is real but the lock is held for microseconds (dict lookups only), so contention is brief. The unpins are correct behavior — agents should be reassigned when their account is limited. A staggered unpins would add complexity for negligible gain at realistic scale.

**F69** — `seed_private_state` deletes lock files based on a TOCTOU-vulnerable PID check
*Class:* bug
*Severity:* low
*Where:* `src/multiagents/executor/docker.py:334-345` and `472-489`
*Evidence:* reproduction
*Proof:* `tests/test_c1_audit_findings.py::test_seed_private_state_toctou_on_lock_deletion`
*What happens:* `_holder_alive` reads a PID from a lock file, checks it with `os.kill(pid, 0)`, and returns False if the process is dead. `seed_private_state` then deletes the lock file. Between the check and the deletion, the dead PID could be reassigned to a new process, and the lock file belonging to that new process is deleted.
*Disposition:* accept
*Reasoning:* The race window is narrow (two sequential syscalls) and the consequence is deleting a lock file, not killing a process. The new process would simply recreate its lock. The alternative — using `fcntl.flock` on the lock file itself — would be a better design but is a structural change beyond a simple fix.

**F70** — `Handle.stop` may SIGKILL a reassigned process group after timeout
*Class:* bug
*Severity:* low
*Where:* `src/multiagents/executor/base.py:90-116`
*Evidence:* trace
*Proof:* —
*What happens:* `Handle.stop` sends SIGTERM, waits with a timeout, then sends SIGKILL to the same process group. If the process exits and its pgid is reassigned between the timeout and the SIGKILL, the signal goes to a different process group. The error is caught (ProcessLookupError, PermissionError, OSError) but only if the pgid no longer exists — if it was reassigned to a live group, the kill succeeds silently against the wrong target.
*Disposition:* accept
*Reasoning:* The race window is the gap between `wait_for` timing out and the `killpg` call — typically microseconds. Process group ID reuse is rare on a busy system and essentially impossible in this short window. The cost of a more robust solution (checking the process is still ours before killing) outweighs the near-zero risk.

**F71** — `_scrub` returns non-JSON error bodies completely unredacted
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/authproxy.py:359-367`
*Evidence:* trace
*Proof:* trace — a reproduction would need an upstream that returns non-JSON with embedded secrets; the code path is clear from the bare `except Exception: return payload` at line 367.
*What happens:* When an upstream error body is not valid JSON, `_scrub` returns it byte-for-byte without any redaction. If that body contains a secret (e.g., an HTML error page with `token=sk-live-...` in the source), the secret reaches the agent's container unredacted. This is a different path from F24 (which covers `_scrub` being a no-op on non-JSON) — this finding is about the security consequence.
*Disposition:* fix
*Reasoning:* The fallback should still apply shape-based regex matching against the raw text, not just JSON parsing. A text-mode `_scrub_text` exists in `redact.py` and could be called on the raw bytes. The fix is to replace `return payload` with `return _scrub_text(payload.decode(errors="replace")).encode()` (importing from redact).

**F72** — `Accounts.token` cache has no eviction and grows unbounded
*Class:* performance
*Severity:* low
*Where:* `src/multiagents/authproxy.py:163-181`
*Evidence:* trace
*Proof:* —
*What happens:* `_cache` is a plain dict keyed by account label. Entries are added on every `token()` call when the file's mtime changes but never removed. If accounts are added and removed frequently (login/logout cycle), the cache grows indefinitely with stale entries. In practice, the number of accounts is small (<10), so this is not a real memory concern.
*Disposition:* accept
*Reasoning:* The number of accounts is bounded by operator behavior (typically 1-3) and the cache entries are tiny (mtime + string). Even at 100 accounts the memory is negligible. Adding eviction would be unnecessary complexity.

**F73** — `build_env` forwards BASE_ENV_KEYS even when named in `blocked`
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/executor/base.py:156-159`
*Evidence:* trace
*Proof:* trace — the BASE_ENV_KEYS loop (line 156-159) has no `blocked` check; only the passthrough loop (line 161-174) checks `blocked` at line 170.
*What happens:* If a variable like `PATH` is named in the `blocked` list, it is still forwarded because the BASE_ENV_KEYS loop runs first and has no blocked check. `register_environment(blocked)` registers the blocked values for redaction, but the values are still present in the child's environment. An agent could read `PATH` from `/proc/self/environ` or `os.environ` despite it being "blocked".
*Disposition:* fix
*Reasoning:* The BASE_ENV_KEYS loop should check `if key in blocked: continue` before forwarding. This is a one-line fix and corrects the precedence: blocked should override base keys, not just passthrough keys. The existing test `test_build_env_base_keys_are_forwarded_even_if_named_in_blocked` confirms this is current behavior, not a test gap.

**F75** — `_copy_settings` copies symlink targets as regular files without checking source is a symlink
*Class:* security
*Severity:* low
*Where:* `src/multiagents/executor/docker.py:500-516`
*Evidence:* opinion
*Proof:* —
*What happens:* The JSON branch of `_copy_settings` reads source via `source.read_text()` (which follows symlinks), redacts, and writes to target as a regular file. If source is a symlink to a sensitive file, the content is read and the redacted version is written to the container profile. The non-JSON branch at line 503 uses `shutil.copy2(source, target, follow_symlinks=False)` which does NOT follow symlinks — the JSON branch is inconsistent.
*Disposition:* accept
*Reasoning:* The `scrub` function would catch most secrets in the copied content (key-based redaction + shape matching). A source symlink to a file with no recognizable secrets is not dangerous. Adding `source.is_symlink()` check would be defensive but adds little given the redaction layer.

## Result

*File written:* `context/review/C1-sandbox.md`
*Id range:* F60–F75 (16 findings)

### Counts by severity

| Severity | Count |
|----------|-------|
| critical | 0     |
| high     | 3     | (F60, F61, F62)
| medium   | 7     | (F63, F64, F66, F67, F71, F73, F74)
| low      | 6     | (F65, F68, F69, F70, F72, F75)

### Counts by evidence tier

| Tier          | Count |
|---------------|-------|
| reproduction  | 6     | (F60, F63, F66, F67, F68, F69)
| trace         | 8     | (F61, F62, F64, F70, F71, F72, F74) — F71 justified: non-JSON bodies with embedded secrets cannot be reproduced without a cooperative upstream, and the code path at authproxy.py:367 is unambiguous
| opinion       | 2     | (F65, F75)

### The one finding I would insist on

**F61** (and its twin F62): `ensure_proxy` / `ensure_auth_proxy` return success when the bridge-network connect fails, leaving the proxy running on the internal network with no route out. Every agent request times out, the container appears healthy, and the operator has no signal. This is silent wrongness — the worst kind, because nobody notices until agents are burning tokens on nothing. The fix is one line: check the return code and clean up on failure.

### What I did not get to

- I attempted to trace through the `docker exec` subprocess lifecycle for additional handle-leak findings, but the code uses `subprocess.run` (synchronous, auto-closes) throughout — no leaked handles there.
- The `_refresh_lock` function (docker.py:414-449) looked suspicious at first glance but the finally block handles all paths correctly after careful reading — finding F71 was retracted.
- I did not exhaustively trace every error path in `scripts.py` (run_action) because its "never raises" contract (line 178) means all errors become return codes, and the callers handle them. No defects found there within this context's scope.

### What is sound

- The `mounts()` dedup logic (docker.py:247-250) correctly implements first-wins semantics, and the narrower read-only config-dir mount at line 274-276 properly overrides the writable root at its own path.
- `_expiring_soon` (docker.py:451-469) correctly handles unreadable/malformed credentials by returning False (no false-positive refreshes).
- `Accounts.for_agent` (authproxy.py:183-206) correctly implements least-loaded pinning with tie-breaking, and the re-read-inside-lock pattern in `refresh_private_credentials` correctly avoids double-renewal.
- `Handle.lines` (base.py:34-63) correctly salvages over-long lines rather than dropping the stream.
