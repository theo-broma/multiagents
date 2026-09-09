"""The monitor: one view of a running project, rendered two ways.

`multiagents monitor` serves it as a local web page; `--tui` draws the same
thing in the terminal. Both are thin: everything they show comes from
:mod:`.snapshot` and everything they do goes through :mod:`.actions`, so the
two front ends cannot drift into disagreeing about what is true, or into one of
them quietly growing a capability the other lacks.

Nothing here is a new source of truth. The tree, the event log, the per-agent
run directories and the provider budget readers already hold all of it; this
package is presentation, and it reads what the running system writes.

Import the submodules rather than names re-exported here — `snapshot` is both a
module and the function in it, and shadowing one with the other is the kind of
small confusion that costs an afternoon.
"""
