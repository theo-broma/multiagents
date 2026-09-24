#!/bin/sh
# Claude Code. Has a first-class auth surface, so this is thin.
set -u
BIN="${MULTIAGENTS_BIN:-claude}"

# The container's own claude profile, when there is one: a DIRECTORY, because a
# bind-mounted credential FILE is frozen at its inode on the host side and
# cannot be rewritten at all on the container side ("mv: Resource busy").
#
# It is READ here, and CLAUDE_CONFIG_DIR is exported only where this script acts
# on that profile — `check` and `login`. NOT for `launch`: the orchestrator runs
# on the host even in a docker project, and pointing it at the container's
# profile would leave it trying to start as an account it was never logged into.
# Agents need no variable at all; their per-agent HOME already resolves
# ~/.claude to whatever is mounted there.
PROFILE=""
if [ "${MULTIAGENTS_EXECUTOR:-local}" = "docker" ] \
   && [ -n "${MULTIAGENTS_PRIVATE_BACKING:-}" ]; then
    PROFILE="$MULTIAGENTS_PRIVATE_BACKING"
fi

# ...unless the caller is asking about the HOST one specifically. There are two
# stored logins under docker and only one of them was reachable: `check` saw
# the container's, so `multiagents auth` cheerfully reported claude logged in
# while the profile the ORCHESTRATOR actually runs on — this one, per the note
# above — could be signed out, and the only symptom was every turn coming back
# 401 from a CLI the user had just been told was fine.
if [ "${MULTIAGENTS_PROFILE:-}" = "host" ]; then
    PROFILE=""
fi

# Where the REAL credential lives, when there is a container profile at all: a
# host-only directory the container has no mount for. The mounted profile gets
# a PROJECTION — the eight-hour access token and nothing else — so a rogue
# agent that copies the credential file out gets a window that closes on its
# own instead of twenty-eight days of account access.
VAULT="${MULTIAGENTS_PRIVATE_VAULT:-}"

# Where the CLI in THIS environment keeps its sessions: CLAUDE_CONFIG_DIR when
# it is set, as `transcripts.default_root()` reads it, and ~/.claude otherwise.
# Read, never exported: `compact` must find the transcript the running CLI
# wrote (P0-R8f.16), which is not the same as choosing a profile for it. A
# function, so an action that never looks does not need HOME set.
claude_sessions_root() {
    printf '%s/projects' "${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}"
}

# The project folder name Claude Code itself derives from a directory path:
# every character outside [A-Za-z0-9] becomes '-'. Used wherever this script
# has to find a transcript the CLI already wrote, rather than one it is about
# to write — get this wrong and a project path with a space, or any other
# punctuation, silently looks empty.
claude_slug() {
    printf '%s' "$1" | sed 's/[^a-zA-Z0-9]/-/g'
}

# Write the vault's access token into the mounted profile, dropping everything
# that could mint another one. Atomic, because an agent may be starting while
# this runs and half a credential file is worse than an old one.
project_token() {
    [ -n "$VAULT" ] && [ -n "$PROFILE" ] || return 0
    # With the auth proxy in front, the container holds a name-tag instead and
    # the real token never leaves the host at all. Writing an access token here
    # would undo that quietly, and the window would be invisible.
    [ "${MULTIAGENTS_AUTH_PROXY:-0}" = "1" ] && return 0
    [ -s "$VAULT/.credentials.json" ] || return 1
    mkdir -p "$PROFILE"
    python3 -c "
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
keep = ('accessToken', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier')
try:
    data = json.load(open(src))
except Exception:
    sys.exit(1)
out = {}
for name, block in data.items():
    if isinstance(block, dict):
        # An allowlist, not a blocklist. A field the vendor adds tomorrow that
        # happens to mint tokens must not travel because nobody updated a list
        # of names to strip.
        out[name] = {k: v for k, v in block.items() if k in keep}
tmp = dst + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(out, fh)
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
" "$VAULT/.credentials.json" "$PROFILE/.credentials.json" || return 1
    return 0
}

# One-time move for a profile that predates the vault: its mounted credential
# still carries the refresh token, and leaving it there is the whole exposure.
#
# Vault FIRST, project second. Interrupted after the copy, the vault holds a
# good credential and the mount holds an old complete one — no worse than
# before, and the next call finishes the job. Interrupted the other way round
# would destroy the only copy of the refresh token.
migrate_to_vault() {
    [ -n "$VAULT" ] && [ -n "$PROFILE" ] || return 0
    [ -s "$VAULT/.credentials.json" ] && return 0      # already done
    [ -s "$PROFILE/.credentials.json" ] || return 0    # nothing to move
    mkdir -p "$VAULT"
    chmod 700 "$VAULT" 2>/dev/null
    cp "$PROFILE/.credentials.json" "$VAULT/.credentials.json" || return 1
    chmod 600 "$VAULT/.credentials.json" 2>/dev/null
    project_token && echo "moved the refresh token out of the container's reach"
}

case "${1:-check}" in
check)
    if [ -n "$PROFILE" ]; then
        # Read the file rather than asking the CLI: `auth status` would start a
        # background daemon on the HOST rooted in the container's profile, and
        # the container would then find a lock naming a pid it cannot signal.
        # A NON-EMPTY FILE IS NOT A LOGIN. Read the expiry that is sitting in
        # it. On 2026-09-14 this test was `[ -s ... ]` alone: the token had
        # expired, every run came back `401 OAuth access token has expired`,
        # and `check` reported the profile logged in throughout. That answer is
        # load-bearing — the runner asks it to decide whether a failed run means
        # "not authenticated" (a six-hour cooldown that a login clears at once)
        # or "something broke" (thirty minutes, cleared only by waiting) — so
        # the whole afternoon retried an expired token half-hourly, two spawns
        # at a time, and never once said the word.
        #
        # THEN IT READ THE WRONG CLOCK. Two live in this file:
        #
        #   expiresAt              the ACCESS token. Eight hours. It lapses
        #                          every single night, and the CLI renews it
        #                          from the refresh token on first use without
        #                          anyone being asked for anything.
        #   refreshTokenExpiresAt  the REFRESH token. Three to four weeks. When
        #                          THIS one is past, nothing can be renewed and
        #                          a login is genuinely the only fix.
        #
        # Reading the first and demanding a login was the answer from 2026-09-14
        # to 2026-09-16, and it made every gap longer than a working day — a
        # night, a weekend, the seven-hour power cut on 09-15 — look like a lost
        # login. That is not merely noise: each needless login is a SECOND CLI
        # session on the same account, which is the one thing that can revoke
        # the host's (see the warning under `login`, and the `401 ... has been
        # revoked` in this project's own event log on 09-09). The check was
        # manufacturing the outage it was reporting.
        #
        # What neither clock can see is a token revoked before its expiry. A
        # file cannot know that; only a request can. The runner's failure
        # classifier is what covers it, and this says so rather than implying
        # a completeness it does not have.
        migrate_to_vault >/dev/null 2>&1
        # Read the VAULT, not the projection. The projection lives in a
        # directory the container writes to, so an agent can put any expiry it
        # likes in there — 1970 to make the host refresh in a loop until the
        # account is rate-limited, 2099 to stop it refreshing at all. The host
        # must never take state from a file the sandbox can edit. The vault is
        # also the only place the refresh token is, which is the clock that
        # decides whether a login is needed.
        SOURCE="$PROFILE"
        [ -n "$VAULT" ] && [ -s "$VAULT/.credentials.json" ] && SOURCE="$VAULT"
        if [ -s "$SOURCE/.credentials.json" ]; then
            clocks=$(python3 -c "
import json, sys
try:
    d = json.load(open('$SOURCE/.credentials.json'))
except Exception:
    sys.exit(0)                      # unreadable: fall through to 'present'
for block in d.values():
    if not isinstance(block, dict) or not block.get('expiresAt'):
        continue
    access = int(block['expiresAt']) // 1000
    refresh = block.get('refreshTokenExpiresAt')
    if refresh:
        refresh = int(refresh) // 1000
    elif block.get('refreshToken'):
        refresh = 'unknown'          # present but undated: cannot tell, so do
                                     # not invent a verdict from its absence
    else:
        refresh = access             # nothing to renew with: access is all
    print(access, refresh)
    break
" 2>/dev/null)
            now=$(date +%s)
            access=${clocks%% *}
            refresh=${clocks##* }
            if [ -n "$access" ]; then
                if [ "$refresh" != "unknown" ] && [ "$refresh" -le "$now" ]; then
                    ago=$(( now - refresh ))
                    if [ "$ago" -ge 172800 ]; then ago="$(( ago / 86400 ))d"
                    else ago="$(( ago / 3600 ))h"; fi
                    # The two ways to arrive here are not the same fact, and a
                    # message that names the wrong one sends the reader looking
                    # for a refresh token that was never in the file.
                    if [ "$refresh" = "$access" ]; then
                        why="there is no refresh token to renew it with"
                    else
                        why="the refresh token, not just the access token"
                    fi
                    echo "the container profile's LOGIN expired $ago ago ($why) — run \`multiagents auth login claude\`"
                    exit 10
                fi
                if [ "$access" -le "$now" ]; then
                    # A lapsed ACCESS token is renewed before the next spawn,
                    # by the HOST, through this script's `refresh` action —
                    # the container has no route to the refresh endpoint and
                    # is not being given one.
                    #
                    # This line has now been wrong in both directions, which is
                    # worth recording. It first reported a login (false: the
                    # container could not renew, so agents 401'd three seconds
                    # into every run while `auth` said all was well). Then it
                    # reported "needs a login" (true, and a daily chore). It
                    # reports a login again — but this time because the renewal
                    # was built, not assumed. The lesson is the order: the
                    # check may only promise what something actually does.
                    #
                    # If the refresh token itself were dead, the branch above
                    # would have caught it and this would never run.
                    echo "container profile is logged in ($PROFILE); its access token lapsed $(( (now - access) / 60 ))m ago and the host renews it before the next spawn"
                    exit 0
                fi
            fi
            echo "container profile is logged in ($PROFILE)"; exit 0
        fi
        echo "the container profile has no credentials yet — run \`multiagents auth login claude\`"
        exit 10
    fi
    out=$("$BIN" auth status --json 2>/dev/null) || {
        echo "could not run '$BIN auth status'"; exit 20; }
    case "$out" in
        *'"loggedIn": true'*|*'"loggedIn":true'*)
            email=$(printf '%s' "$out" | sed -n 's/.*"email"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
            echo "logged in${email:+ as $email}"; exit 0 ;;
        *)  echo "not logged in"; exit 10 ;;
    esac
    ;;
login)
    if [ -n "$PROFILE" ]; then
        echo "Claude Code sign-in for the CONTAINER's profile."
        echo
        echo "It runs here on the host, with your own browser — the container"
        echo "reads a directory, and this writes into it:"
        echo "  $PROFILE"
        echo
        echo "Your own ~/.claude is untouched and is not mounted into the"
        echo "container at all, so agents cannot read your conversations."
        echo
        echo "READ THIS FIRST: this is a SECOND CLI session. If you sign in"
        echo "with the same account as the host and this provider allows only"
        echo "one session at a time, signing in here will sign the host OUT,"
        echo "and the two will keep evicting each other. A second account has"
        echo "no such problem. It is checked below either way."
        echo
        printf 'continue? [y/N] '
        read -r answer
        case "$answer" in
            [yY]*) ;;
            *) echo "nothing was changed."; exit 1 ;;
        esac
        echo
        # A labelled login is a SECOND account, kept beside the first rather
        # than replacing it: that is the whole point of labelling it. Work
        # moves onto it when the first runs out of window, and the proxy is
        # what does the moving.
        TARGET="${VAULT:-$PROFILE}"
        if [ -n "${MULTIAGENTS_ACCOUNT:-}" ]; then
            if [ -z "$VAULT" ]; then
                echo "several accounts need the auth proxy (executor.docker.auth_proxy: true);"
                echo "without it there is nowhere to hold more than one credential."
                exit 64
            fi
            TARGET="$VAULT/accounts/$MULTIAGENTS_ACCOUNT"
            echo "signing in as account '$MULTIAGENTS_ACCOUNT'."
            echo
        fi
        mkdir -p "$TARGET"
        chmod 700 "$TARGET" 2>/dev/null
        CLAUDE_CONFIG_DIR="$TARGET" "$BIN" auth login || exit $?
        # The sign-in lands in the vault; agents get the access token only.
        if [ -n "${MULTIAGENTS_ACCOUNT:-}" ]; then
            echo "account '$MULTIAGENTS_ACCOUNT' added. Agents move onto it when"
            echo "the others are out of window; nothing else changes."
            exit 0
        fi
        if [ -n "$VAULT" ]; then
            project_token || { echo "signed in, but could not write the container's copy"; exit 1; }
            echo "the refresh token stays here on the host; the container gets"
            echo "an eight-hour access token, replaced before each agent starts."
        fi
        # Host-pid state, meaningless inside a container and confusing to the
        # CLI that finds it.
        rm -rf "$PROFILE/daemon" "$PROFILE/daemon.lock" "$PROFILE/daemon.status.json"
        # A backend that caps concurrent CLI sessions would have just evicted
        # the host's login. Cheap to check, and silent otherwise.
        if ! "$BIN" auth status --json 2>/dev/null \
                | grep -q '"loggedIn":[ ]*true'; then
            echo
            echo "WARNING: the HOST profile is no longer logged in. This account"
            echo "appears to allow only one CLI session at a time, so the two"
            echo "profiles will keep evicting each other. Log the host back in"
            echo "with \`claude auth login\` and use one account per profile."
        fi
        exit 0
    fi
    echo "Claude Code sign-in."
    echo "A browser window will open; complete the sign-in there."
    echo
    exec "$BIN" auth login
    ;;
refresh)
    # Renew the CONTAINER profile's access token, from the host.
    #
    # The container cannot do this itself and never could: the refresh endpoint
    # is platform.claude.com and the egress allowlist carries api.anthropic.com,
    # so agents can infer and can never renew. Widening the allowlist to fix
    # that would hand every agent — all of which run with approvals off — a
    # host that also serves /settings/keys and /settings/billing.
    #
    # It does not need widening. The host already has the egress AND the CLI
    # that knows how to do this properly, so the refresh happens out here and
    # the container simply reads the file afterwards. No tunnel, no new
    # reachable host, and the OAuth flow stays where it belongs: inside the
    # vendor's own client, not reimplemented against an undocumented endpoint.
    #
    # There is no `claude auth refresh`, so the trigger is the cheapest real
    # call there is. At most once per token lifetime, on the cheapest model.
    [ -z "$PROFILE" ] && exit 64          # no container profile; nothing to do
    migrate_to_vault
    RENEW="${VAULT:-$PROFILE}"
    [ -n "$VAULT" ] && [ -s "$VAULT/.credentials.json" ] || RENEW="$PROFILE"
    if [ ! -s "$RENEW/.credentials.json" ]; then
        echo "no container credentials to refresh"; exit 10
    fi
    # `.oauth_refresh.lock` is a bare mkdir mutex — an empty DIRECTORY naming
    # no owner, so nothing can ask whether the holder is alive. An agent that
    # starts a refresh it cannot finish (see above: it cannot reach the
    # endpoint) leaves one behind, and every later refresh anywhere, host
    # included, then fails with "another Claude Code process is refreshing it".
    # Observed on 2026-09-16, and it blocked the host too.
    #
    # Removed only when old enough that no refresh could still be running. A
    # refresh takes seconds; a minute is not a close call.
    lock="$RENEW/.oauth_refresh.lock"
    if [ -d "$lock" ] && [ -z "$(find "$lock" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then
        rmdir "$lock" 2>/dev/null && echo "cleared a stale refresh lock"
    fi
    expiry() {
        python3 -c "
import json, sys
try:
    d = json.load(open('$RENEW/.credentials.json'))
except Exception:
    print(0); sys.exit(0)
for b in d.values():
    if isinstance(b, dict) and b.get('expiresAt'):
        print(int(b['expiresAt']) // 1000); break
else:
    print(0)
" 2>/dev/null || echo 0
    }
    before=$(expiry)
    # Run from a directory of our own. `-p` records a conversation under the
    # CWD's name, so invoking this from wherever the caller happened to stand
    # littered the container profile with a project entry per directory. One
    # fixed entry, emptied afterwards, instead of a growing pile of them.
    probe="$RENEW/.refresh-probe"
    mkdir -p "$probe"
    out=$(cd "$probe" && CLAUDE_CONFIG_DIR="$RENEW" "$BIN" -p "ok" --model haiku 2>&1) || {
        echo "refresh failed: $(printf '%s' "$out" | tail -1 | head -c 200)"
        exit 10
    }
    rm -rf "$RENEW/projects/$(claude_slug "$probe")" 2>/dev/null
    # Host-pid state is meaningless inside a container, same as after `login`.
    rm -rf "$RENEW/daemon" "$RENEW/daemon.lock" "$RENEW/daemon.status.json"
    # SAY WHAT HAPPENED, not what was attempted. There is no `claude auth
    # refresh`, so this forces a renewal by making a real call — and a real
    # call succeeds whether or not the token needed renewing. Announcing a
    # refresh on the strength of the call exiting zero is a claim about
    # something never looked at, and this file has already shipped two of those
    # today. The expiry moving is the only evidence there is.
    after=$(expiry)
    # Whatever the vault now holds, the container gets the access half of it.
    # Done even when nothing was renewed: this is also what repairs a
    # projection an agent overwrote, and what completes a migration that was
    # interrupted between the copy and the strip.
    [ -n "$VAULT" ] && [ "$RENEW" = "$VAULT" ] && { project_token || echo "could not write the container's copy"; }
    if [ "$after" -gt "$before" ]; then
        echo "refreshed the container profile's token (valid $(( (after - $(date +%s)) / 3600 ))h)"
    else
        echo "token already current; nothing to renew"
    fi
    exit 0
    ;;
budget)
    # Deliberately unimplemented. Claude's quota lives in ~/.claude.json under
    # cachedUsageUtilization — an undocumented internal cache with several
    # bucket shapes, staleness to account for, and an overage-credits block.
    # Parsing that defensively in shell would be worse code in two places, so
    # multiagents falls back to its built-in reader when a script returns 64.
    # A new provider without a built-in simply implements this action.
    exit 64
    ;;
usage)
    # How this provider's usage is shown in `multiagents monitor`. The budget
    # is already parsed and arrives in MULTIAGENTS_BUDGET, so this formats and
    # never re-fetches — one request per five minutes per machine is the whole
    # budget for asking the account anything.
    #
    # Claude's shape is two rolling windows plus a credit pool, and the pool is
    # worth naming: when it is spent, a full window stops work dead and the CLI
    # announces that as "you've hit your monthly spend limit". Somebody reading
    # this panel at that moment should not have to know that story.
    python3 -c "
import json, os, sys
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
if not b.get('known'):
    print(b.get('note') or 'no usage reading'); raise SystemExit
used = b.get('used_percent') or 0
bar = '#' * int(round(used / 10)) + '.' * (10 - int(round(used / 10)))
print(f\"{bar}  {used:.0f}% of the tightest window\")
# resets_label is the local clock plus a countdown, computed by the caller so
# that every provider script shows the same time the user's own clock does.
# Slicing the ISO string here printed UTC on a local face: two hours out.
# The fallback keeps the offset rather than trimming to a tidy 16 characters.
# A script here can be newer than the Python that feeds it — provider scripts
# sync into the global config dir on their own, a running monitor does not
# reload — and a fallback that prints \"2026-09-15T00:00\" states a local time
# it has not computed. Raw and unambiguous is the right way to be out of date.
if b.get('resets_label') or b.get('resets_at'):
    print(f\"resets {b.get('resets_label') or b['resets_at']}\")
spent = b.get('spent') or {}
u, limit = spent.get('extra_credits_used'), spent.get('extra_credits_limit')
if u is not None and limit:
    print(f\"credits {u / 100:.2f} of {limit / 100:.2f} — {'spent' if u >= limit else 'available'}\")
    if u >= limit:
        print('nothing carries a session past a full window')
note = b.get('note') or ''
if note and 'credits' not in note:
    print(note[:120])
" 2>/dev/null || exit 64
    ;;
prepare)
    # Nothing to register: claude takes its MCP config per invocation, so
    # nothing persists and no other session or subagent inherits it.
    exit 0
    ;;
launch)
    set -- --model "${MULTIAGENTS_MODEL:-sonnet}"
    [ -n "${MULTIAGENTS_MCP_CONFIG:-}" ] && \
        set -- "$@" --mcp-config "$MULTIAGENTS_MCP_CONFIG" --strict-mcp-config
    [ -n "${MULTIAGENTS_PROMPT_FILE:-}" ] && \
        set -- "$@" --append-system-prompt-file "$MULTIAGENTS_PROMPT_FILE"
    # The same per-agent key a spawned agent gets through `spawn.optional`
    # (providers.yaml); a launched role receives it as environment instead,
    # because `launch` builds argv here rather than through build_command.
    [ -n "${MULTIAGENTS_AUTOCOMPACT:-}" ] && \
        set -- "$@" --autocompact "$MULTIAGENTS_AUTOCOMPACT"
    # Each launched role owns a session id, because `--continue` resumes the
    # most recent conversation IN THE DIRECTORY and both roles share the project
    # root — so `init-agent` after `run` would reopen the orchestrator's
    # conversation. Naming the session removes the ambiguity: resume it if it
    # exists, create it under that id if it does not.
    if [ -n "${MULTIAGENTS_SESSION_ID:-}" ]; then
        sessions="$(claude_sessions_root)/$(claude_slug "$(pwd)")"
        if [ "${MULTIAGENTS_RESUME:-0}" = "1" ] \
           && [ -f "$sessions/$MULTIAGENTS_SESSION_ID.jsonl" ]; then
            set -- "$@" --resume "$MULTIAGENTS_SESSION_ID"
        else
            set -- "$@" --session-id "$MULTIAGENTS_SESSION_ID"
        fi
    elif [ "${MULTIAGENTS_RESUME:-0}" = "1" ]; then
        # No id: an install predating this. Fall back to the old behaviour,
        # which is still better than passing --continue into nothing.
        sessions="$(claude_sessions_root)/$(claude_slug "$(pwd)")"
        if ls "$sessions"/*.jsonl >/dev/null 2>&1; then
            set -- "$@" --continue
        else
            echo "no previous conversation in this directory; starting a fresh one" >&2
        fi
    fi

    # A restarted interactive session opens with a message instead of waiting
    # for one to be typed. `claude [options] [prompt]` is interactive WITH a
    # first user turn; `-p` is the non-interactive form and belongs only to the
    # unattended path below.
    if [ "${MULTIAGENTS_UNATTENDED:-0}" != "1" ] && [ -n "${MULTIAGENTS_RESUME_PROMPT:-}" ]; then
        set -- "$@" "$MULTIAGENTS_RESUME_PROMPT"
    fi

    # Unattended: one non-interactive turn, so the supervisor above can decide
    # whether to run another. `-p` IS headless — it prints and exits — which is
    # exactly right here and exactly wrong for the interactive path, where it
    # would turn `multiagents run` into a one-shot.
    if [ "${MULTIAGENTS_UNATTENDED:-0}" = "1" ]; then
        set -- "$@" --permission-mode bypassPermissions -p "${MULTIAGENTS_NUDGE:-continue}"
    fi
    exec "$BIN" "$@"
    ;;
compact)
    # Triggered from OUTSIDE the running session, between turns — see BRIEF
    # § R8. `claude -p "/compact" --resume <sid>` is consumed by the CLI
    # itself, costs no turn, and appends a record to the session transcript;
    # the caller learns success only by reading that record back, not by
    # trusting the CLI's own exit code, because an exit of 0 here means
    # "the call completed", not "something was compacted".
    sid="${MULTIAGENTS_SESSION_ID:-}"
    if [ -z "$sid" ]; then
        echo "MULTIAGENTS_SESSION_ID is required to compact a session" >&2
        exit 2
    fi
    slug=$(claude_slug "$(pwd)")
    transcript="$(claude_sessions_root)/$slug/$sid.jsonl"
    if [ ! -f "$transcript" ]; then
        echo "no transcript for session $sid at $transcript" >&2
        exit 1
    fi
    # Readable, not merely present (P0-R8f.17): the figures below are read
    # back from it, so a compaction on a transcript we cannot read must fail.
    if [ ! -r "$transcript" ]; then
        echo "transcript for session $sid is not readable: $transcript" >&2
        exit 1
    fi
    # Check mode (P0-R8f.1): the driver asks this before it stops a live
    # session, so it must answer from what is on disk and start nothing. A
    # session id and a readable transcript are all a real call needs up front.
    if [ "${MULTIAGENTS_COMPACT_CHECK:-}" = "1" ]; then
        exit 0
    fi
    # `wc -l` counts newlines, not lines: an unterminated last line (a writer
    # killed mid-flush) is one line short. Left uncorrected, the python below
    # slices from one line too early and hands an old, pre-existing record to
    # a caller that appended nothing.
    before=$(wc -l < "$transcript" 2>/dev/null || echo 0)
    if [ -s "$transcript" ] && [ -n "$(tail -c1 "$transcript")" ]; then
        before=$((before + 1))
    fi
    out=$("$BIN" -p "/compact" --resume "$sid" --output-format json 2>&1)
    code=$?
    if [ "$code" -ne 0 ]; then
        printf 'compact failed: %s\n' "$(printf '%s' "$out" | tail -1 | head -c 200)" >&2
        exit 1
    fi
    # Only lines APPENDED by this call count — a compaction record already in
    # the transcript, manual or automatic, was somebody else's and proves
    # nothing about this invocation.
    figures=$(python3 -c "
import json, sys
path, before = sys.argv[1], int(sys.argv[2])
# errors='replace': a byte elsewhere in the session that is not valid UTF-8
# (written long before this call) must not turn a real compaction into a
# reported failure.
with open(path, errors='replace') as fh:
    lines = fh.readlines()
for line in lines[before:]:
    line = line.strip()
    if not line:
        continue
    try:
        record = json.loads(line)
    except ValueError:
        continue
    if record.get('type') != 'system' or record.get('subtype') != 'compact_boundary':
        continue
    meta = record.get('compactMetadata')
    if not isinstance(meta, dict) or meta.get('trigger') != 'manual':
        continue
    pre, post = meta.get('preTokens'), meta.get('postTokens')
    if pre is not None and post is not None:
        print('%s -> %s tokens' % (pre, post))
        # P0-R8f.15: a compaction that left the context no smaller did not
        # do its job, and reporting it as one would have the caller stop the
        # session for it again at the next boundary. Figures that are not
        # numbers cannot say so; the record itself is genuine, as before.
        try:
            shrank = float(post) < float(pre)
        except (TypeError, ValueError):
            shrank = True
        if not shrank:
            print('the context did not shrink (%s -> %s tokens)' % (pre, post),
                  file=sys.stderr)
            sys.exit(3)
        break
" "$transcript" "$before")
    [ $? -eq 3 ] && exit 1
    if [ -z "$figures" ]; then
        echo "the CLI exited 0 but no compaction was recorded" >&2
        exit 1
    fi
    echo "$figures"
    exit 0
    ;;
*)  echo "usage: $0 check|login|refresh|budget|usage|prepare|launch|compact" >&2; exit 64 ;;
esac
