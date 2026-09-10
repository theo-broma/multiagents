# Open questions

Things this project believes, is waiting to find out, or decided against — with
the evidence, and with what would change the answer.

It exists because most of what is written here was learned by reading a real
run rather than by reasoning about the code, and because several of the beliefs
below were *wrong the first time*. A conclusion with no evidence attached gets
re-derived at the worst moment; one with a measurement attached can be checked
in a minute.

Each entry says how to check it. Dates are when the evidence was taken.

---

## 1 · Settled by measurement

Recorded so nobody investigates them twice. Each was, at some point, believed
otherwise.

### A bind-mounted credential FILE cannot work — 2026-09-09

Docker binds a file by inode. An atomic rewrite on the host is invisible in the
container; the same rewrite attempted inside fails with `mv: Resource busy`.
Directory mounts are correct in both directions.

```
host       inode=42379566  refreshed 15:51
container  inode=42378882  written the previous night, expired 05:43
```

Eleven hours of `401 OAuth access token has been revoked` while `auth status` on
the host read the correct file and reported everything fine.

**Check:** `multiagents doctor` reports drift; `DockerExecutor.credential_drift()`
compares inodes directly.

**Note:** an earlier diagnosis of the same symptom blamed claude's background
daemon caching the token in memory. The observations were right and the
conclusion was wrong — nobody compared the file *inside* the container against
the host's, because the two paths are identical.

### A symlink to a credential file does not survive a refresh — 2026-09-10

A per-agent HOME linked `.claude/.credentials.json`. A token refresh writes a
temp file and renames over the path, which replaces the symlink with a plain
file: the new token lands in the agent's throwaway home, the shared profile
keeps the old one, and that refresh has just rotated the old one away. Every
other agent is then holding a revoked credential.

This is the mechanism that survived fixing the mount, and probably the main
reason revocations *repeated* rather than happening once. `home_links` now names
the directory.

**Check:** `tests/test_core.py::test_the_credential_link_is_the_directory_not_the_file`
reproduces the rename in three lines.

### Two claude sessions coexist on one account — 2026-09-10

The container profile was signed in to the same account as the host. The host
was **not** evicted:

```
$ claude auth status          # host
{"loggedIn": true, ...}
$ multiagents doctor          # container profile
claude  authenticated  container profile is logged in
```

So a second subscription is a throughput decision, not a correctness one.

**What would change it:** a vendor introducing a session cap. The login path
checks the host afterwards and warns if it flipped, so a regression announces
itself.

### `CLAUDE_CONFIG_DIR` relocates everything — 2026-09-10

Credentials, config, transcripts and `daemon.lock` all resolve through it. An
empty profile dir reports `{"loggedIn": false}` and grows its own state. Same
for opencode via `XDG_DATA_HOME`. **agy is the exception** — its host credential
is in the GNOME keyring, which no environment variable moves.

### One tool call is reported twice — 2026-09-10

agy emits `state=ACTIVE` then `state=DONE` for one call, with the same name,
arguments and step. The doom-loop counter took both, so its threshold was half
what the config said.

```
doom_loop      -> merged 25   discarded/cancelled/orphaned 3     (89% harmless)
runaway_steps  -> merged  7   discarded 3
silence        -> merged  6   discarded 4
timeout        -> merged  4   —
52 agents marked stuck; 42 finished fine
```

Fixed by treating a repeat within the same step as one call. **The numbers above
are therefore stale** — see §3.

### Providers describe usage in words that do not overlap — 2026-09-10

opencode sends `total`, agy sends `total_tokens`, claude sends neither and only
the API's own parts. Anything reading `total` counted the most expensive
provider as free: **nine million tokens dropped** on one project, every claude
row in `multiagents usage` reading zero. `tree.token_count()` normalises on read.

---

## 2 · Shipped but never exercised in anger

Written, tested, and not yet proven by a real run. Each names what would prove it.

| | proven when |
|---|---|
| **A container agent runs on the container profile.** The profile is logged in and verified from inside the container, but no agent has yet done real work through it. | one claude agent merges from inside a container |
| **A refresh inside an agent reaches the shared profile.** The directory link makes it possible; nothing has observed it happening. | an agent runs across a token expiry and the next agent is not revoked |
| **Two accounts on one provider.** Instance selection, family failover, the orchestrator's reserved account: all tested in unit tests against synthetic budgets, never against two real subscriptions. | a second subscription exists and work spreads across both |
| **The family circuit breaker.** Requires two accounts of one vendor failing at once; there has only ever been one account per vendor. | it fires, and does not fire on a single account's trouble |
| **`needs_login` auto-recovery.** The long cooldown was observed; the `check` probe clearing it within two minutes of a login was not. | `multiagents auth login` frees a cooled provider without waiting out the timer |
| **429 handling and `Retry-After`.** Never observed in the wild; the code paths are unit-tested only. | a rate-limited response appears in a log |
| **The usage-limit detection end to end.** The markers match a real message and the stall was diagnosed, but no session has yet been ended and restarted by it. | a session hits a window limit and comes back by itself |

---

## 3 · Retune from data, not from judgement

Every threshold in `limits:` was set from a small sample, and at least one was
being counted wrong. Now that a tool call is counted once, **the watchdog
statistics above are invalid** and must be re-gathered before any number moves.

- **`doom_loop_repeats: 5`** — the 89%-harmless figure was measured with the
  double count. Re-measure over a few nights, then decide.
- **Wall-clock `timeout`** — 4 trips, 4 merges, a 100% false-positive rate. An
  advisor argued for removing it outright; the honest fix is to derive each
  agent's timeout from the p90 of its successful runs, which needs more than 13
  runs to be worth doing. *Note when re-measuring:* a node's `ended_at` is when
  the parent MERGED it, not when the agent stopped — use `last_event_at`.
- **`silence_timeout`** — fired seven times on opencode implementers in one
  night, all of which merged. Long tool calls stream sparsely.
- **The doom-loop's second condition is vacuous for `writes: false` agents.**
  It requires the working tree to have stood still, which for a specifier,
  adversary, critic or RED-test author is their normal state, so the guard
  degenerates to "called the same read five times". An advisor's suggestion —
  compare the tool's *returned payload* instead — is right and **not currently
  possible**: our normalised stream captures tool calls and steps, never
  results. That would need a parse rule per provider.

**How to gather it:**

```python
# per agent: work duration and outcome for runs that finished
import json, collections
nodes = json.load(open(".multiagents/tree.json"))["nodes"].values()
by_agent = collections.defaultdict(list)
for n in nodes:
    if n.get("started_at") and n.get("last_event_at"):
        by_agent[n["agent"]].append(
            (n["last_event_at"] - n["started_at"], n["status"]))
```

Then compare the p90 of `merged` runs against each agent's configured `timeout`.

---

## 3b · Known gap: the drivers are not in the tree

`run` and `init-agent` both `exec` into a CLI, so neither creates a node. Only
the agents they consult or spawn do, and those appear as roots with `parent:
null`. An advisor's framing, which I think is right: the tree is meant to carry
"what is being done and why", and leaving the two decision-makers out of it
means an agent appears with no record of who asked or in which session.

**What it would take.** Create a node at launch, keep it updated from the same
samples the supervisor already takes, close it when the process ends, and set
`MULTIAGENTS_AGENT_ID` in the driver's environment so the agents it starts
become its children rather than roots.

**Why it is not done yet.** `parent` is load-bearing in more places than it
looks: depth limits, `max_children`, merge-into-parent, the orphan reaper, and
the monitor's forest. Giving every agent a parent it has never had changes all
of them at once, and the failure mode is subtle rather than loud.

**What would settle it:** whether the history view is actually hard to read
without it. Two sessions' agents currently interleave as roots ordered by time,
which is confusing at 40 nodes and probably unusable at 400.

---

## 4 · Declined, and what would change the answer

Positions taken against an advisor's recommendation. Each is a judgement, not a
fact, and each names its own falsifier.

- **TTL eviction of parked conversations.** Proposed on the grounds that
  resuming a week-old 220k-token advisor injects obsolete assumptions and costs
  a premium. Declined: that is a policy decision about somebody's context, and
  the monitor now shows the token weight so it can be theirs. *Revisit if* the
  cost of resuming stale conversations shows up in the usage report.
- **A whitelist for config keys copied into a container profile.** Proposed
  because a denylist guarantees a future leak when the vendor adds a
  secret-bearing key. Declined: a whitelist quietly drops legitimate settings
  every time the schema grows, which is a worse everyday failure. Mitigated by
  passing everything through the redactor, which catches secret *shapes*.
  *Revisit if* a secret is ever found in a container profile.
- **Weighting instance selection by account capacity.** Proposed because
  absolute agent counts cannot balance accounts of different size. Declined: no
  vendor exposes a per-account concurrency limit, so the weight would be
  invented. *Revisit if* one does, or if two accounts on one vendor visibly
  differ.
- **Dropping provider-specific options on failover.** Declined the other way:
  options travel unchanged unless a `models:` entry says otherwise, because
  silently losing `effort: high` from an agent that exists to reason deeply is
  a slow failure that merges mediocre work, while a refused flag costs eight
  seconds and says so.
- **Enforcing filename == payload role for status files.** Proposed on the
  grounds that accepting a payload's claim hides a zombie supervisor. Taken
  halfway: the label follows the payload, so it is never wrong, *and* the
  disagreement is raised as its own alert naming the file. Refusing to read it
  would have made the upgrade window report nothing at all.
- **An agent's pin meaning the account rather than the family.** Declined
  because the owner's requirement is precisely that agents run on the second
  account while the orchestrator keeps the first. Keeping two accounts apart on
  purpose is `family: <its own name>`.

---

## 5 · Not ours to decide

- **Multiple subscriptions to raise throughput.** Some providers' terms restrict
  using more than one account this way. That is the account holder's to check,
  and this project takes no position beyond making the mechanism explicit.
- **`limits.ask_provider_for_usage`.** Reading your own account's usage with
  your own token, at most once every five minutes, is the same request the
  vendor's own client makes — but the endpoint is undocumented and the account
  at risk is the user's. Shipped on, with a switch and the trade written down.
