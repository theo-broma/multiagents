#!/bin/sh
# Claude Code. Has a first-class auth surface, so this is thin.
set -u
BIN="${MULTIAGENTS_BIN:-}"
require_bin() {
    if [ -z "$BIN" ]; then
        printf '%s\n' "${MULTIAGENTS_BIN_ERROR:-MULTIAGENTS_BIN is not set}" >&2
        exit 20
    fi
}
case "${1:-check}" in
    login|launch|compact|refresh) require_bin ;;
esac

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

# A pin selects a login in the owner's vault, never a separate host profile.
PIN="${MULTIAGENTS_CONTAINER_ACCOUNT:-}"
# Identity is read-only, and failures must never echo profile or credential data.
if [ "${1:-check}" = "identity" ]; then
    python3 - <<'PY'
import json, os, pathlib, re, stat, sys
from urllib.parse import unquote

def read_profile(root, name):
    relative = pathlib.Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('invalid profile path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.dup(root)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('invalid profile file')
            with os.fdopen(fd) as handle:
                fd = None
                return json.load(handle)
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(directory)

def normalized(value):
    value = ''.join(value.split()).casefold()
    for _ in range(3):
        result = ''.join(unquote(value).split()).casefold()
        if result == value:
            break
        value = result
    return value

def credential_key(key):
    key = key.casefold()
    # Include the CLI's native camelCase spellings of credential fields.
    return (key in {'token', 'secret', 'password', 'key', 'accesstoken',
                    'refreshtoken', 'idtoken', 'apikey', 'primaryapikey',
                    'sessionkey', 'clientsecret'}
            or key.endswith(('_token', '_key', '_secret')))

def secrets(value, credential=False):
    if isinstance(value, str):
        if (credential and len(value) <= 8192
                and len(value.encode('utf-8', errors='surrogatepass')) <= 8192):
            result = normalized(value)
            if len(result) >= 16:
                yield result
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from secrets(item, credential or credential_key(key))
    elif isinstance(value, list):
        for item in value:
            yield from secrets(item, credential)

root_fd = None
try:
    if os.environ.get('MULTIAGENTS_EXECUTOR', 'local') == 'docker':
        vault = os.environ.get('MULTIAGENTS_PRIVATE_VAULT')
        if not vault:
            sys.exit(64)
        root = pathlib.Path(vault)
        prefix = pathlib.Path()
        account = os.environ.get('MULTIAGENTS_CONTAINER_ACCOUNT') or 'default'
        if not re.fullmatch('[a-z0-9_-]+', account):
            sys.exit(64)
        if account != 'default':
            prefix = pathlib.Path('accounts') / account
        credentials = [prefix / '.credentials.json']
    elif os.environ.get('MULTIAGENTS_EXECUTOR', 'local') == 'local':
        configured = os.environ.get('CLAUDE_CONFIG_DIR')
        root = pathlib.Path(configured or os.environ['HOME'])
        prefix = pathlib.Path()
        credentials = [pathlib.Path('.credentials.json')]
        if not configured:
            credentials.append(pathlib.Path('.claude/.credentials.json'))
    else:
        sys.exit(64)
    root_fd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    data = read_profile(root_fd, prefix / '.claude.json')
    account = data.get('oauthAccount') if isinstance(data, dict) else None
    email = account.get('emailAddress') if isinstance(account, dict) else None
    if (not isinstance(email, str) or len(email) > 254
            or not re.fullmatch(r'[^@\s]+@[^@\s]+', email)
            or any(ord(c) < 32 or ord(c) == 127 for c in email)
            or re.search(r'(?i)(?:sk-|rt-|bearer\s|eyJ[A-Za-z0-9_-]*\.)', email)):
        sys.exit(64)
    candidate = normalized(email)
    if any(secret in candidate or candidate in secret for secret in secrets(data)):
        sys.exit(64)
    for credential_path in credentials:
        try:
            credential = read_profile(root_fd, credential_path)
        except FileNotFoundError:
            continue
        if any(secret in candidate or candidate in secret for secret in secrets(credential)):
            sys.exit(64)
except Exception:
    sys.exit(64)
finally:
    if root_fd is not None:
        os.close(root_fd)
print(json.dumps({'identity': email, 'kind': 'email'}))
PY
    exit $?
fi
if [ -n "$PROFILE" ] && [ -n "$PIN" ]; then
    if [ -n "${MULTIAGENTS_ACCOUNT:-}" ] && [ "$MULTIAGENTS_ACCOUNT" != "$PIN" ]; then
        echo "account '$MULTIAGENTS_ACCOUNT' differs from configured pin '$PIN'" >&2
        exit 64
    fi
    MULTIAGENTS_ACCOUNT="$PIN"
    export MULTIAGENTS_ACCOUNT
fi

# Labels are data, not paths supplied by a caller. Default lives at the top.
vault_labels() {
    python3 - "$VAULT" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
labels = ['default'] if (root / '.credentials.json').is_file() else []
accounts = root / 'accounts'
if (accounts / 'default').exists():
    sys.exit('accounts/default is reserved; rename it to another account label')
if accounts.is_dir():
    for path in accounts.iterdir():
        if not path.is_dir():
            continue
        if not re.fullmatch('[a-z0-9_-]+', path.name):
            sys.exit('account labels require lowercase letters, digits, - or _')
        labels.append(path.name)
print('\n'.join(sorted(labels)))
PY
}

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
    [ -d "$VAULT/accounts" ] && return 0              # multi-account vault
    [ -s "$PROFILE/.credentials.json" ] || return 0    # nothing to move
    # A sidecar name-tag is not an OAuth login. Never migrate it back into
    # the host vault and let a far-future placeholder clock invent a login.
    python3 - "$PROFILE/.credentials.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    placeholder = any(isinstance(b, dict) and isinstance(b.get('accessToken'), str)
                      and b['accessToken'].startswith(('mxa_', 'mxa2_')) for b in data.values())
except (OSError, ValueError, AttributeError):
    sys.exit(1)
sys.exit(1 if placeholder else 0)
PY
    [ "$?" = "0" ] || return 0
    mkdir -p "$VAULT"
    chmod 700 "$VAULT" 2>/dev/null
    cp "$PROFILE/.credentials.json" "$VAULT/.credentials.json" || return 1
    chmod 600 "$VAULT/.credentials.json" 2>/dev/null
    project_token && echo "moved the refresh token out of the container's reach"
}

case "${1:-check}" in
check)
    if [ -n "$PROFILE" ]; then
        migrate_to_vault >/dev/null 2>&1
        if [ -n "$VAULT" ] && { [ -s "$VAULT/.credentials.json" ] || [ -d "$VAULT/accounts" ] || [ -n "$PIN" ] || [ "${MULTIAGENTS_AUTH_PROXY:-0}" = "1" ]; }; then
            labels=$(vault_labels) || exit 20
            python3 - "$VAULT" "$PIN" "${MULTIAGENTS_RESERVED_ACCOUNTS:-[]}" "$labels" <<'PY'
import json, pathlib, sys, time
root, pin = pathlib.Path(sys.argv[1]), sys.argv[2]
reserved = json.loads(sys.argv[3])
labels = sys.argv[4].splitlines()
if not labels and not pin:
    labels = ['default']
labels = [pin] if pin else [label for label in labels if label not in reserved]
statuses, usable = {}, []
for label in labels:
    path = root if label == 'default' else root / 'accounts' / label
    status = 'missing'
    try:
        data = json.loads((path / '.credentials.json').read_text())
        for block in data.values():
            if not isinstance(block, dict) or not block.get('accessToken'):
                continue
            access = float(block.get('expiresAt', 0)) / 1000
            refresh = float(block.get('refreshTokenExpiresAt', 0)) / 1000
            renewable = bool(block.get('refreshToken')) and (not refresh or refresh > time.time())
            status = 'ok' if access > time.time() or renewable else 'expired'
            if refresh and refresh <= time.time():
                status = 'expired'
            break
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    statuses[label] = status
    if status == 'ok':
        usable.append(label)
print('accounts: ' + json.dumps(statuses, sort_keys=True) +
      ('; authenticated' if usable else '; no usable account; run `multiagents auth login ' +
       __import__('os').environ.get('MULTIAGENTS_PROVIDER', 'claude') + '`'))
sys.exit(0 if usable else 10)
PY
            exit $?
        fi
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
    require_bin
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
            case "$MULTIAGENTS_ACCOUNT" in
                *[!a-z0-9_-]*) echo "invalid account label" >&2; exit 64 ;;
            esac
            if [ "$MULTIAGENTS_ACCOUNT" != "default" ]; then
                TARGET="$VAULT/accounts/$MULTIAGENTS_ACCOUNT"
            fi
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
    if [ -n "$VAULT" ] && [ -z "${MULTIAGENTS_REFRESH_ACCOUNT:-}" ]; then
        labels=$(vault_labels) || exit 20
        failed=0
        for label in $labels; do
            account="$VAULT"
            [ "$label" = "default" ] || account="$VAULT/accounts/$label"
            if python3 - "$account/.credentials.json" <<'PY'
import json, sys, time
try:
    data = json.load(open(sys.argv[1]))
    due = any(isinstance(b, dict) and b.get('expiresAt') and
              float(b['expiresAt']) / 1000 <= time.time() + 1800 for b in data.values())
except (OSError, ValueError, TypeError, AttributeError):
    due = False
sys.exit(0 if due else 1)
PY
            then
                echo "renewing account '$label'"
                # The child's vault is this account. It must never project a
                # labelled credential into the common container profile.
                MULTIAGENTS_PRIVATE_VAULT="$account" MULTIAGENTS_REFRESH_ACCOUNT="$label" \
                    MULTIAGENTS_AUTH_PROXY=1 sh "$0" refresh || failed=1
            fi
        done
        # Repair the access-only projection even when no renewal was due.
        # With a sidecar this is a no-op: only name-tags may enter that mount.
        if [ -s "$VAULT/.credentials.json" ]; then
            project_token || failed=1
        fi
        [ "$failed" = "0" ] && echo "vault accounts checked"
        exit "$failed"
    fi
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
    # Quota windows are rendered by the monitor; this action supplies extras.
    python3 - <<'PYEOF'
import json, os, sys
b = json.loads(os.environ.get('MULTIAGENTS_BUDGET') or '{}')
lines = []
spent = b.get('spent') or {}
u, limit = spent.get('extra_credits_used'), spent.get('extra_credits_limit')
if u is not None and limit:
    lines.append(f"credits {u / 100:.2f} of {limit / 100:.2f} — {'spent' if u >= limit else 'available'}")
    if u >= limit:
        lines.append('nothing carries a session past a full window')
if b.get('account'):
    lines.append('vault account ' + str(b['account']))
note = b.get('note') or ''
if note and 'credits' not in note:
    lines.append(note[:120])
if not lines:
    raise SystemExit(64)
print('\n'.join(lines))
PYEOF
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
    # CW-R8: what to keep, as the command's own argument — one argument,
    # never re-parsed by a shell, cut to its bound rather than refused. The
    # trailing x survives the command substitution's newline stripping.
    prompt="/compact"
    if [ -n "${MULTIAGENTS_COMPACT_FOCUS:-}" ]; then
        focus=$(python3 -c 'import os, sys; sys.stdout.write(os.environ["MULTIAGENTS_COMPACT_FOCUS"][:1000])'; printf x)
        focus=${focus%x}
        [ -n "$focus" ] && prompt="/compact $focus"
    fi
    out=$("$BIN" -p "$prompt" --resume "$sid" --output-format json 2>&1)
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
