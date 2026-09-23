# A burst is not a burn rate (ticket bug-c050b0)

**Status:** contract, 2026-09-23.

**Ids.** Prefix `BR-`. Never renumber; retire with `BR-Rn — withdrawn: <why>`.

## The defect

`Tree.burn()` (`tree.py` ~930) projects `seconds_to_wall` from the first and
last headroom samples inside the last hour. It only requires two samples and a
positive span. When the older samples have expired, two readings 39 seconds
apart (0.60 → 0.49, while four agents loaded their first context) became
16.9 points a minute and a wall in 174 s. `Runner._wind_down()` (~370) then
cooled the provider down and paused the tree for 8 minutes. The wrap-up
watcher (~989) told four agents, in their first two minutes, to write
handoffs. They did, and produced nothing else. Headroom was 49 % and the
window's reset was 2 h 45 min away.

The lead times (`wind_down_seconds`, `wrap_up_seconds`) are right and are not
changed here. What is wrong is projecting from too little observation.

## Behaviours

**BR-R1 — no projection from a short observation.** `burn()` returns
`seconds_to_wall` only when the samples it uses span at least
`budget.burn_min_span_seconds` (default 300) **and** number at least
`budget.burn_min_samples` (default 3). Otherwise it still returns `samples`
and, if it has one, `headroom`. It omits `seconds_to_wall`. It may keep
`points_per_minute` as information. Nothing acts on that value alone.
*Verified by:* the ticket's series (0.60 at t, 0.49 at t+39 s, 0.49 at t+76 s,
0.49 at t+107 s, with older samples expired) yields no `seconds_to_wall`.
A steady drain, for example 5 points a minute sampled every 60 s over
6 minutes, does yield one, and it is within 10 % of the linear value.

**BR-R2 — nothing winds down or wraps up without a projection.**
`_wind_down()` and the wrap-up watcher do nothing for a provider whose
`burn()` has no `seconds_to_wall`. That is already how they read it. The
test pins it for the BR-R1 case.
*Verified by:* with the ticket's series, a pass of each watcher sets no
cooldown, no pause, and no `wrap_up` event or steer.

**BR-R3 — a genuine drain still triggers with the full lead.** When the
observation is long enough and the projection is under
`wind_down_seconds` or `wrap_up_seconds`, behaviour is unchanged from today:
same events, same cooldown, same single wrap-up message.
*Verified by:* a steady-drain series projecting a wall within
`wrap_up_seconds` triggers both, exactly as today.

**BR-R4 — the keys are read safely.** `burn_min_span_seconds` and
`burn_min_samples` are parsed as the P0-R8f.13 keys are, with
`config.limit_number` or its equivalent for the `budget` section. A malformed
value falls back to the shipped default and never raises. An explicit 0
means "no minimum", which gives back today's behaviour. They are documented
in `defaults/project.yaml` next to `wind_down_seconds`, with one line on why
they exist.
*Verified by:* malformed values ("x", -1, null) behave as the defaults; 0
restores projection from two samples.

## Out of scope, recorded

- Gating the wind-down on absolute headroom (for example, "never below 20 %
  left"). The ticket proposes it. I declined it: an arbitrary floor either
  fires too late on a fast drain or never fires on a slow one. The lead time
  already covers that, once the projection rests on enough data.
- Using the window's known reset time to cancel a projected wall that falls
  after the reset. It is worth doing, but it depends on quota-window data
  that not every provider gives. Record it in BRIEF if it comes up again.
