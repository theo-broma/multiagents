# opencode-zai: the Z.AI GLM Coding Plan as a provider, the contract

**Status:** contract, written by the orchestrator on 2026-09-29, at the
user's request.
- **The goal:** GLM becomes usable by the dev team through opencode, with
  real quota tracking.
- **Ids:** `ZA-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## Facts established live on 2026-09-29

These were verified by the user and the orchestrator; see BRIEF.
- The user signed in with `opencode providers login`. opencode stores the
  credential in its normal `auth.json`, under the entry `zai-coding-plan`
  with the fields `key` and `type: api`. It sits in the same store as
  `opencode-go`.
- `opencode models` lists `zai-coding-plan/{glm-4.7, glm-5-turbo, glm-5.2,
  glm-5.2-highspeed, glm-5.3, glm-5.3-flash, glm-5.3-highspeed}`.
- `opencode run -m zai-coding-plan/glm-5.3-flash --format json` emits the
  same NDJSON as the other opencode models: `step_start`, `text`, and
  `step_finish` with tokens and `cost: 0`.
- **The quota endpoint.** It was recovered from ZCode 3.14.4 (see
  `~/zcode-analysis/USAGE.md`) and confirmed live.
  - The request is `GET https://api.z.ai/api/monitor/usage/quota/limit`,
    with the header `Authorization: <raw key>`. There is **no** `Bearer `
    prefix and no body. Use a timeout of 15 s or less.
  - The response is `{... "data": {"level": "lite", "limits": [ ... ]}}`.
    It may be wrapped in a `{code, success, data}` envelope. `success:
    false`, or a `code` other than absent, null, 0 or 200, is a failure.
  - **The 5 h window** is the first limit with `type` in
    {`TOKENS_LIMIT`, `CREDIT_LIMIT`}, `unit == 3` and `number == 5`.
  - **The weekly window** is the first limit with `type` in the same set
    and `unit == 6`.
  - Each limit carries:
    - `percentage`: the percent **used**, from 0 to 100;
    - `nextResetTime`: epoch **milliseconds**;
    - `usage`: the window's *capacity*, e.g. 2000;
    - `currentValue`: the amount used;
    - `remaining`.
  - A missing window means *unknown*, not zero.
  - Observed live: `CREDIT_LIMIT`; 5 h `usage` 2000; weekly `usage` 10000.
    One tiny call cost one credit.

## Behaviours

**ZA-R1: a shipped provider instance.**
- **The definition.** The shipped `providers.yaml` gains:

  ```yaml
  opencode-zai:
    extends: opencode
    family: opencode-zai
    enabled: false
    billing: plan
    models_include: ["zai-coding-plan/*"]
    env:
      MULTIAGENTS_OPENCODE_PLAN: zai-coding-plan
  ```

  It ships disabled, with a comment explaining how to enable it.
- **Its own family.** Its billing and models are unrelated to OpenCode
  Go, so failover between the two is never implicit.
- **`opencode` itself is unchanged.** Its `models_include` stays
  `opencode/*`, `opencode-go/*`, so `zai-coding-plan/*` is never
  attributed to the Go subscription.
- **This project** enables it in `.multiagents/config/providers.yaml`.
  That is the orchestrator's change, not the implementer's.
- Verified by:
  - `load_providers` on the shipped file yields `opencode-zai` with
    `family == "opencode-zai"`, `billing == "plan"`, the opencode spawn
    and stream rules inherited, and `enabled == False`;
  - `refresh-models`, with a fake `opencode models` listing both
    namespaces, records only `zai-coding-plan/*` under `opencode-zai`,
    and only `opencode-go/*` and `opencode/*` under `opencode`.

**ZA-R2: `check` and `login` for the plan.**

The opencode script branches on `MULTIAGENTS_OPENCODE_PLAN`. When that
variable is unset, the script behaves exactly as it does today.

When it is set to `zai-coding-plan`:
- **`check`:**
  - It reads the opencode auth store at
    `${XDG_DATA_HOME:-$HOME/.local/share}/opencode/auth.json`.
  - It exits 0 when an entry `zai-coding-plan` with a non-empty `key`
    exists. Otherwise it exits 10, with a message naming the missing
    entry and the fix: `multiagents auth login opencode-zai`, then choose
    Z.AI Coding Plan.
  - A missing store, or unparsable JSON, gives exit 10 with its own
    message.
  - It never prints the key.
- **`login`:** it prints one line saying to choose the Z.AI Coding Plan
  provider, then runs `$MULTIAGENTS_BIN providers login`.

Verified by, with a temporary `XDG_DATA_HOME`:
- a store with the entry gives exit 0;
- a store without it gives exit 10;
- a missing store gives exit 10;
- a store holding only `opencode-go` gives exit 10 for the plan and
  exit 0 without it;
- in every case, the key string never appears in the output.

**ZA-R3: `budget` from the z.ai quota endpoint.**

When the plan is `zai-coding-plan`, the `budget` action:
- **Calls the endpoint.** It reads the key from the auth store and
  passes it to the request without exposing it on the process command
  line, in the same way as the existing `curl --config -`.
  - It sends the key verbatim.
  - It refuses redirects, and uses a timeout of 15 s or less.
  - The endpoint's origin can be overridden by the environment variable
    `MULTIAGENTS_ZAI_ORIGIN`, which defaults to `https://api.z.ai`. The
    override is **for tests only**, and is documented as such.
- **Prints the budget object** used elsewhere:

  ```json
  {"known": true,
   "headroom": 1 - max(used%)/100,
   "resets_at": "<ISO of the fuller window>",
   "source": "api.z.ai/api/monitor/usage/quota/limit",
   "note": "<window> window is the constraint at N% used",
   "windows": {"five_hour": {"percent", "resets_at"},
               "weekly": {"percent", "resets_at"}}}
  ```

  - Headroom is rounded to 4 decimals and never negative.
  - `resets_at` is ISO-8601 UTC, derived from milliseconds.
  - Only the windows present are listed.
- **Reports honestly when it cannot tell.** In each of these cases it
  prints `{"known": false, "note": ...}` and exits 0. The note never
  contains the key.

  | Case | Note says |
  |---|---|
  | no key | there is no key |
  | endpoint unreachable | the endpoint is unreachable |
  | non-JSON body | the body is not JSON |
  | failure envelope | the request failed |
  | neither window found | no window was reported |

Verified by, with a local fake HTTP server through
`MULTIAGENTS_ZAI_ORIGIN`:
- the live sample (1% and 1%) gives `known: true` with headroom 0.99;
- windows at 5% and 80% give headroom 0.2 and the weekly reset;
- the header is exactly the raw key, with no `Bearer`;
- each failure case gives `known: false`.

**ZA-R4: `usage` shows both windows.**
- When the plan is set, the `usage` action shows the 5 h and weekly
  windows. Each shows the percent used and the reset time, and, when
  present, `currentValue` of `usage` credits.
- It follows the output format of the existing opencode `usage` action.

Verified by: a fake-server test.

**ZA-R5: no regression, and no new egress.**
- Without `MULTIAGENTS_OPENCODE_PLAN`, every opencode action behaves
  byte-for-byte as it does today, including the existing tests.
- The docker `egress_allowlist` is **not** changed. `api.z.ai` under
  docker needs the user's approval, and that is out of scope here.
- The existing suite stays green, apart from the known reds.

## Out of scope

- **The BigModel (`bigmodel.cn`) family and team plans.** The contract
  leaves room: add a plan value later.
- **The off-peak ticket protocol.** It is a future idea; see BRIEF.
- **Roster changes, meaning which agents use GLM.** These are the
  orchestrator's, after merge, with the advisor.
