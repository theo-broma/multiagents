# C2 — Authentication, shipped provider scripts, config folding

Findings from characterizing `auth.check` / `auth.check_all` /
`auth.login_command` / `auth.looks_like_auth_failure`, the real shipped
`claude.sh` / `agy.sh` / `opencode.sh`, and `load_providers` /
`resolve_inheritance` / `families` against `providers.yaml`. See
`tests/test_c2_auth_characterization.py`.

One finding in this area is already known and is NOT refiled here: on a host
where `docker` is available, `tests/test_core.py::test_the_claude_script_uses_the_container_profile_only_where_it_should`
is red — `claude.sh check` reports a container profile "logged in" from a
credentials file that carries no readable expiry at all
(`{"claudeAiOauth": {}}`). `test_claude_sh_check_treats_an_empty_oauth_block_as_logged_in`
in this suite pins that exact case as a baseline. The findings below are the
same root cause found in places nobody had looked yet, plus two unrelated gaps
in `looks_like_auth_failure` and one in config folding.

**F130** — `claude.sh check` reports "logged in" for a credentials file that is corrupt, or present but missing the one field it reads
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/defaults/providers/claude.sh` (the `check` case, the embedded python clock-extraction heredoc — `for block in d.values(): ... if not block.get('expiresAt'): continue`, and its `except Exception: sys.exit(0)`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_claude_sh_check_unparseable_json_is_also_reported_as_logged_in`, `tests/test_c2_auth_characterization.py::test_claude_sh_check_an_access_token_with_no_expiresat_is_also_logged_in`
*What happens:* The script's own comment block says the whole point of this code is that "A NON-EMPTY FILE IS NOT A LOGIN" and that it must read the token's actual expiry rather than trust presence. But the embedded python that reads the expiry has exactly one way to fail — finding no block with an `expiresAt` key, whether because the file is invalid JSON (caught by a bare `except Exception: sys.exit(0)`, printing nothing) or because the block that IS there simply never got an `expiresAt` (e.g. `{"accessToken": "abc"}` with no expiry at all) — and in every one of those failure shapes, the shell script's `clocks` variable ends up empty, `access` ends up empty, the `if [ -n "$access" ]` guard is skipped, and execution falls through to the unconditional `echo "container profile is logged in ($PROFILE)"; exit 0` at the bottom of the block. The one case the author explicitly fixed (empty oauth block, already known) and these two do not differ in kind: all three are "the file exists and this code could not read a clock out of it," and all three report authenticated. A container profile whose credentials file was left in ANY unreadable state — truncated by a crash mid-write, hand-edited wrong, or simply an intermediate format the CLI used before it started writing `expiresAt` — is reported as logged in with no further check, and the runner discovers the truth only when the agent's first turn 401s.
*Disposition:* fix
*Reasoning:* The script already distinguishes "present but the clock says it's dead" from "present and fine" — it just has no third bucket for "present but I couldn't tell," and silently sorts everything unreadable into "fine." A `sys.exit(1)`/distinct print on parse failure, checked by the shell before the fallback echo, would turn this into the same `exit 20`/"unknown" verdict the host path already uses for its own unreadable case (`could not run '$BIN auth status'` — see `run_action`'s general contract of "unknown, not false positive, when uncertain").

**F131** — `agy.sh check` (container branch) authenticates on ANY non-empty token file, with no format or expiry check at all
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/defaults/providers/agy.sh` (the `check` case, docker branch: `if [ -s "$backing/$TOKEN_REL" ]; then echo "container token present"; exit 0; fi`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_agy_sh_check_docker_treats_any_non_empty_token_file_as_present`
*What happens:* Where `claude.sh` at least attempts to read an expiry (however incompletely — see F130), `agy.sh`'s container check does not attempt to interpret the token file's contents in any way. `[ -s file ]` only asks "does this file have nonzero size" — a file containing the literal string `not-a-real-token-just-garbage` reports `"container token present"`, exit 0, exactly the same as a genuine token would. There is no expiry to check (agy's file-token format is undocumented in this script), so this may be an intrinsic limit of what the container-side token format even carries — but that makes it more important, not less, since there is no way for this check to ever catch a stale or corrupted token, only a missing file.
*Disposition:* accept
*Reasoning:* Downgraded from "fix" because, unlike F130, there may genuinely be no expiry field in agy's on-disk token to check — the script's own comments describe it as "a plain file" with no documented structure. If that is correct, presence is the most this check can ever do, and the honest fix is documentation (this check answers "was a login attempted," not "is the account currently usable") rather than added parsing this script has no way to do correctly. Whoever owns agy's token format should confirm whether it carries an expiry before this is escalated to `fix`.

**F132** — `opencode.sh check` defaults to "1 stored credential(s)" (authenticated) for ANY CLI output it cannot parse, including no output at all
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/defaults/providers/opencode.sh` (the `check` case: `n=$(printf '%s' "$out" | sed -n '...credential...p' | head -1); echo "${n:-1} stored credential(s)"; exit 0`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_opencode_sh_check_defaults_to_one_credential_on_any_unmatched_output`, `tests/test_c2_auth_characterization.py::test_opencode_sh_check_defaults_to_one_credential_even_on_silent_success`
*What happens:* Unlike `claude.sh` and `agy.sh`, `opencode.sh` reads no credential file of its own at all — it trusts `$BIN providers list`'s exit code and text entirely. That text is matched against exactly one negative case (`*0 credentials*` → not authenticated) and one extraction pattern for a positive count; anything else — including a binary that runs, exits 0, and prints literally nothing — falls to the shell parameter expansion `${n:-1}`, which is not "unknown," it is a hardcoded claim of exactly one stored credential. A future `opencode` release that changes its wording even slightly (a version bump, a locale, a deprecation notice printed alongside the real answer) silently flips every "not logged in" or "who knows" case in this script's parsing to "authenticated," because the default on parse failure is success, not doubt.
*Disposition:* fix
*Reasoning:* The `${n:-1}` default should be `${n:-unknown}` with a non-zero exit when extraction fails and the "0 credentials" case didn't match either — mirroring the `unclear:`/exit 20 pattern `agy.sh`'s own host branch already uses for exactly this situation (CLI output that doesn't cleanly parse). The fix is local to this one line.

**F135** — `opencode.sh`'s credential-count extraction silently fails when the digit count opens the CLI's line, reporting 1 instead of the real number
*Class:* bug
*Severity:* medium
*Where:* `src/multiagents/defaults/providers/opencode.sh:17` (`sed -n 's/.*[^0-9]\([0-9][0-9]*\) credential.*/\1/p'`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_opencode_sh_check_count_extraction_fails_when_the_digit_opens_the_line`, contrasted with `tests/test_c2_auth_characterization.py::test_opencode_sh_check_extracts_the_reported_credential_count`
*What happens:* The extraction pattern requires a non-digit character (`[^0-9]`) to precede the digit run it captures. `"3 credentials stored"` — the count opening the line, with nothing before it — has no character for `[^0-9]` to match ahead of the `3`, so the whole substitution fails to match and prints nothing; `"found 3 credentials stored"` (anything at all ahead of the digit) extracts `3` correctly. This is independent of F132 above: it is not a case the script fails to anticipate, it is a working extraction pattern with an off-by-one assumption about where in the line the number sits. Whether it ever fires against the real `opencode` CLI depends entirely on that CLI's own phrasing, which this script does not control and this characterization did not verify either way — it is recorded as a property of the regex, not a confirmed live bug against the real binary.
*Disposition:* fix
*Reasoning:* Trivial to make robust — anchor the match on `^` with `[0-9]` allowed to open the line (`s/^\([0-9][0-9]*\) credential.*/\1/p` as a second pattern, or `[^0-9]*` instead of requiring `[^0-9]`) — and it compounds with F132: this failure mode, too, is invisible from the outside, silently returning 1 rather than any signal that extraction failed.

**F133** — a provider whose `extends:` names a nonexistent parent is grouped into a "family" that does not exist, named after the missing parent
*Class:* correctness
*Severity:* low
*Where:* `src/multiagents/providers.py:200` (`Provider.from_dict`: `family=data.get("family") or data.get("extends") or name`), `src/multiagents/providers.py` (`families`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_extends_a_missing_parent_still_sets_family_to_the_ghost_name`
*What happens:* `resolve_inheritance`'s docstring is explicit that a missing `extends:` base is "left alone rather than raised on," and the child's own fields (`bin`, `env`, etc.) do come through untouched, matching that intent. But `Provider.from_dict`'s family fallback reads the raw, UNRESOLVED `extends:` string regardless of whether that base was ever found — so `{"child": {"extends": "ghost", "bin": "x"}}` produces `family == "ghost"`, and `families()` reports `{"ghost": ["child"]}`: a family whose name corresponds to no real provider, containing exactly the one orphan. Anything downstream that reasons about families (failover between "the same family," a UI that lists family names) sees a phantom group rather than the child falling back to a family of its own name, which is what a typo'd or since-removed `extends:` most likely means. This is provider-config-folding only; whether `budget.py`'s failover logic actually does anything visible with the phantom family is outside this suite's surface.
*Disposition:* fix
*Reasoning:* The `or data.get("extends")` fallback should be conditioned on the extends target actually having resolved (e.g. computed in `resolve_inheritance`, where the base's presence in `raw` is already known, rather than in `from_dict`, which cannot tell "extends a real parent" from "extends nothing that exists").

**F136** — `looks_like_auth_failure`'s `"401"` marker is an unanchored substring match, not an HTTP-status match
*Class:* bug
*Severity:* medium
*Where:* `src/multiagents/auth.py:140` (`_AUTH_MARKERS` tuple, `"401"` entry), `src/multiagents/auth.py:160` (`if not any(marker in blob for marker in _AUTH_MARKERS)`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_looks_like_auth_failure_the_401_marker_is_an_unanchored_substring`
*What happens:* Every marker in `_AUTH_MARKERS` is tested with plain substring containment (`marker in blob`) against the lowercased `status\nstderr` blob, with no word boundary and no requirement that `"401"` appear as a standalone number, let alone as an HTTP status specifically. `"wrote 40105 bytes to disk"` and `"processed 401 items successfully"` — neither remotely an authentication failure — both classify as one, purely because the digit sequence `401` occurs somewhere in the string. A run whose real failure is unrelated (a disk-space message, an item count, a port number, a process id) can be misreported as an authentication problem if `401` happens to appear anywhere in its stderr, which changes what the runner's failure classifier does with it (see the module's own docstring: this exists so a caller can decide "not authenticated," with real consequences like the cooldown period claude.sh's comments describe).
*Disposition:* fix
*Reasoning:* Anchor the numeric markers with a regex requiring non-digit boundaries (`\b401\b` or equivalent), or drop the bare `"401"` marker in favor of phrases that actually appear alongside a real 401 (`"401 unauthorized"`, `"status code: 401"`) the way `"invalid_api_key"` and `"credentials not found"` already do.

**F137** — `looks_like_auth_failure` does not recognize several plausible real-world authentication-failure phrasings
*Class:* bug
*Severity:* medium
*Where:* `src/multiagents/auth.py:140-149` (`_AUTH_MARKERS`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auth_characterization.py::test_looks_like_auth_failure_misses_common_real_world_phrasings`
*What happens:* `_AUTH_MARKERS` catches `"token expired"` but not `"session ... expired"` or `"session expired"`; it catches `"please log in"`/`"please login"` but not `"please sign in"`; it has nothing for a missing API key phrased as `"API key missing"` or `"missing api key"` (as opposed to `"invalid api key"`, which IS caught). None of `"your session has expired, please sign in again"`, `"Error: session expired"`, `"API key missing"`, or `"missing api key"` share a substring with any entry in the list, so all four — each a phrasing a real CLI plausibly uses — are classified as an ordinary failure rather than an authentication one. The consequence is the mirror of F136: a real auth failure, worded slightly differently than the list anticipates, is NOT given the auth-specific treatment (the fix suggestion, the cooldown-vs-retry distinction the module docstring describes) and is instead treated as a generic, retryable failure.
*Disposition:* fix
*Reasoning:* Add `"session expired"`, `"sign in"` (alongside the existing `"log in"`/`"login"`), and an API-key-missing phrase to `_AUTH_MARKERS`. This is a coverage gap in a fixed list, not a design flaw — the list is exactly as complete as whoever wrote it anticipated, and both this and F136 are evidence the list has not been kept in sync with the CLIs it classifies.
