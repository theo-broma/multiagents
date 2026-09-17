# C2 — the plugin seam (`scripts.py`)

Findings from characterizing `run_action`, `exec_action`, `build_env`,
`resolve`, `find_script`, and `script_argv`. See
`tests/test_c2_seam_characterization.py`.

**F110** — `run_action` raises `UnicodeDecodeError` on non-UTF-8 script output, contradicting its own "never raises" contract
*Class:* correctness
*Severity:* high
*Where:* `src/multiagents/scripts.py:172-195` (`run_action`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_run_action_raises_uncaught_on_non_utf8_stdout`
*What happens:* `run_action`'s docstring states: "Never raises: a missing script, a timeout or an OS error all come back as a non-zero code with the reason in stderr, because every caller of this is reporting status rather than doing work." The implementation calls `subprocess.run([...], capture_output=True, text=True, ...)`, which decodes the child's stdout/stderr using the process's default text encoding with strict error handling. A script that writes even a single byte that is not valid in that encoding (demonstrated with `printf '\377\376\200\201'` on stdout) makes `subprocess.run` itself raise `UnicodeDecodeError` before `run_action` gets a chance to catch anything — the `except` clauses only cover `subprocess.TimeoutExpired` and `OSError`. Every caller that was written trusting "never raises" (budget reading, auth checking, the monitor's provider rows) can be crashed by a third-party provider script emitting one bad byte, e.g. from a stray non-UTF-8 log line, a binary blob printed by mistake, or a CLI upstream that changes its output encoding.
*Disposition:* fix
*Reasoning:* Either decode with `errors="replace"`/`"surrogateescape"` (matches the spirit of "report status, don't crash") or catch `UnicodeDecodeError` alongside the existing `except` clauses and fold it into the same non-zero-code contract the docstring already promises.

**F111** — a script's background children outlive `run_action`'s timeout
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/scripts.py:172-195` (`run_action`), specifically the `except subprocess.TimeoutExpired` branch
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_run_action_timeout_kills_the_direct_child_but_not_a_backgrounded_grandchild`
*What happens:* On timeout, `subprocess.run` kills only the process it started directly (`sh script.sh`), which is the same process `run_action` awaited. A script that does `( sleep 2 ) & wait` blocks that direct child for the full timeout window, so the timeout still fires correctly at the configured deadline — but the backgrounded grandchild is a separate pid, reparented once its parent is killed, and is never touched. In the reproduction, `run_action` returns `(124, ...)` after 1 second while the backgrounded work (here, writing a marker file) continues running and completes 2 seconds later, well after the caller has already been told the action failed and moved on (e.g. to trying a different provider under failover). Any provider script that shells out to something long-running in the background — a login flow polling a browser callback, a cache warm, a cleanup step — can leave that work running unsupervised and untracked after multiagents has stopped waiting on it.
*Disposition:* fix
*Reasoning:* `subprocess.Popen(..., start_new_session=True)` plus killing the whole process group (`os.killpg`) on timeout would reap backgrounded descendants too. This is a real hazard specifically because "providers are plugins" — the project does not control what a provider script does internally, so the seam that runs it needs to be the thing that bounds its lifetime, not merely bound the lifetime of its direct child.

**F112** — `build_env` copies the full ambient process environment into every provider script invocation
*Class:* security
*Severity:* high
*Where:* `src/multiagents/scripts.py:90-129` (`build_env`, specifically `env = dict(os.environ)` at line 93)
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_build_env_copies_the_full_ambient_process_environment`
*What happens:* `build_env` starts from `dict(os.environ)` — the calling process's own environment, unfiltered — and layers the `MULTIAGENTS_*` protocol keys and the provider's own `env:` block on top. Nothing removes or allowlists what was already there. Run inside this very review (an agent inside its own multiagents container), the environment handed to a script this way includes `CLAUDE_CODE_MESSAGING_TOKEN` (this agent's own session token), `ANTHROPIC_BASE_URL` and the sandbox's internal proxy URLs, and `MULTIAGENTS_ROOT`. `tests/support/c2_harness.py`'s own module docstring names this exact hazard as the reason `run_shipped_script` strips a fixed list of keys (`_SCRIPT_ENV_KEYS`) before every call in this test suite — the harness had to build a workaround for production behaviour rather than the production code doing the scrubbing itself. Since a provider's script can be resolved from `project_config` (see `resolve`'s precedence — a project-local script wins over the global and shipped ones), this hands the calling process's ambient environment, potentially including live credentials, to a script that ships with the project being orchestrated rather than with the user's own trusted configuration.
*Disposition:* fix
*Reasoning:* Provider scripts legitimately need some of the ambient environment (`PATH` at minimum, per the harness's own comment that "PATH matters — python3 is invoked by name inside these scripts"). The fix is an allowlist of what a script actually needs (PATH, TERM, locale variables, the `MULTIAGENTS_*` keys this function already computes, and the provider's own `env:` block) rather than the full ambient environment, mirroring what `_SCRIPT_ENV_KEYS` already had to reconstruct defensively in the test harness.

**F113** — `MULTIAGENTS_PROVIDER` in a script's environment can name a different provider than the one whose script actually ran
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/scripts.py:90-96` (`build_env`), `src/multiagents/scripts.py:132-135` (`resolve`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_run_action_uses_provider_dot_script_name_not_the_provider_name_argument`, `tests/test_c2_seam_characterization.py::test_build_env_provider_identity_env_var_comes_from_the_argument_not_provider_dot_name`
*What happens:* `run_action`, `exec_action`, `resolve`, and `build_env` all take TWO separate names for "which provider this is": the `provider_name` string argument, and the `Provider` object's own `.name`/`.script_name`. `resolve` picks the script to run using `provider.script_name` (derived from `provider.name`), completely ignoring the `provider_name` argument except as a fallback for a `Provider`-like object that lacks a `script_name` attribute at all. `build_env`, however, stamps `MULTIAGENTS_PROVIDER` in the child's environment from the `provider_name` ARGUMENT. When a caller passes a `provider_name` that does not match `provider.name` — plausible wherever a caller threads a name through several layers, or under the family/`extends` multi-account scheme where several provider configs share a script — the script that runs is chosen correctly, but the environment tells that script it is a different provider (or, in the multi-account case, potentially a different account) than the one that is actually executing it. Any script logic that branches on `MULTIAGENTS_PROVIDER` (e.g. to pick which credential file to read) is trusting a value the seam does not actually keep in sync with the script it chose to run.
*Disposition:* fix
*Reasoning:* `resolve`/`build_env` should derive the identity they report from the same source: either always use `provider.name` (the one that actually determined which script ran), or always use the `provider_name` argument for resolution too, and treat any caller-side mismatch as a bug at the call site rather than silently accepting two different answers to "who is this."

**F114** — a timeout and a "script is not executable" `OSError` are indistinguishable by exit code
*Class:* maintainability
*Severity:* low
*Where:* `src/multiagents/scripts.py:172-195` (`run_action`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_run_action_returns_124_on_timeout_with_the_reason_in_stderr`, `tests/test_c2_seam_characterization.py::test_run_action_on_a_non_sh_script_without_exec_bit_is_an_oserror_reported_as_124`
*What happens:* `run_action` returns `124` both when the child actually times out (`subprocess.TimeoutExpired`) and when the child could never be started at all (`OSError` — not executable, missing shebang, etc.). `124` is the conventional shell "command timed out" code; a caller that switches on the numeric code alone (rather than also parsing `stderr`) cannot tell "this provider is slow or hung" from "this provider's script is misconfigured and never ran," even though those call for very different remediation (retry/failover vs. fix the install).
*Disposition:* accept
*Reasoning:* `stderr` does distinguish the two cases today (`"TimeoutExpired: ..."` vs. `"... is not executable ..."`), so nothing is silently lost — this only bites a caller that inspects the return code without the message. Worth a dedicated exit code (or an explicit third case) if a caller is ever found doing that, but not urgent on its own.

**F115** — a provider's `env:` block cannot reference the `MULTIAGENTS_*` values `build_env` just computed for that same call
*Class:* maintainability
*Severity:* low
*Where:* `src/multiagents/scripts.py:122-127` (`build_env`'s provider-env loop)
*Evidence:* reproduction
*Proof:* `tests/test_c2_seam_characterization.py::test_build_env_expands_provider_env_vars_and_user_against_the_real_process_environment`
*What happens:* `build_env` expands each value in `provider.env` with `os.path.expanduser(os.path.expandvars(str(value)))`. `os.path.expandvars` reads the real `os.environ` of the calling process at call time — not the `env` dict `build_env` is assembling in the same function call. A provider config author writing `env: {SOME_VAR: "$MULTIAGENTS_PRIVATE_HOME/thing"}`, expecting to reference the docker private-home path `build_env` computes a few lines earlier in the same function, gets the literal unexpanded string `"$MULTIAGENTS_PRIVATE_HOME/thing"` back instead — silently, with no error to signal the reference did not resolve. A reference to something that genuinely was already in the parent process's environment (unrelated to anything `build_env` itself set up) DOES expand, which is what makes the failure mode easy to miss while testing with the wrong example.
*Disposition:* accept
*Reasoning:* Not a defect in the sense of doing the wrong thing — `expandvars`'s documented behaviour is exactly this — but a footgun specific to this seam's two-phase construction (ambient environment, then computed `MULTIAGENTS_*` keys, then `provider.env` expansion against a *different* environment than the one just built). Worth a doc note in `providers.yaml`'s header or the `Provider.env` field comment; a `Disposition: fix` would mean expanding against the in-progress `env` dict instead of `os.environ`, which is a larger behavioural change than this pass should make unreviewed.
