# instantiate_template accepts its own parameters (TI)

Source: scheduler first-real-use trial, 2026-10-05
(`context/plans/2026-10-05-scheduler-first-real-use.md`, Apply now §4: scheduler
defects found during the trial are fixed before part 2).

Observed: every call of the MCP tool `instantiate_template` is refused with
`{"error": "invalid", "problems": ["urgent: field is not client-writable"]}`,
even with `urgent` left at its default. The MCP tool always forwards `urgent`
and `window`; the scheduler RPC accepts only `name`, `params`, `parent`,
`plan_revision`. The template path has therefore never worked from the
orchestrator.

Also missing: the plan deposits each template instance **after** a researcher
node and **under a lock** shared with other groups. `create_node` takes
`depends_on`, `inputs` and `locks`; `instantiate_template` takes none of them,
so an instance cannot be ordered or locked at deposit time.

## Behaviours

**TI-R1.** `instantiate_template(name, params, plan_revision)` through the MCP
tool, with every optional argument left at its default, deposits the instance.
Verified by: a test calling the MCP tool function (not only the RPC).

**TI-R2.** `urgent` and `window`, when given, apply to the instance's root
node with the same meaning and the same validation as on `create_node` for a
composite node. Their defaults (`false`, none) are the same as omitting them.
Verified by: tests on the deposited root, and that an invalid window is refused
the way `create_node` refuses it.

**TI-R3.** `instantiate_template` accepts `depends_on`, `inputs` and `locks`,
with the same meaning and validation as on `create_node` for a composite node,
applied to the instance's root.
Verified by: tests that an instance with `depends_on` on an unfinished node does
not launch any child until the dependency is satisfied, and an instance with a
lock does not launch while another holder of that lock runs.

**TI-R4.** These take effect at deposit, atomically: no child of the instance can
launch under a state where they are not yet applied.
Verified by: the TI-R3 tests run with the scheduler ticking.

**TI-R5.** Any argument that is neither a tool parameter nor in TI-R2/R3 is still
refused with `field is not client-writable`. A stale `plan_revision` is still
refused with `conflict`.
Verified by: tests.

**TI-R6.** The tool's description states the arguments it takes (it says
"available in M4" today).
Verified by: review.

## Out of scope
- Adding parameters to the shipped templates themselves.
