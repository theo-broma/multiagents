# C1 — auth proxy as deployed (AP adversary pass 2, 2026-10-14)

**F205** — the deployed auth proxy registers no literals, so literal masking is inert in production
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py:2778`
*Evidence:* trace
*Proof:* —
*What happens:* the proxy sidecar is started without registering any account literal with `redact`, so every rule keyed on a registered literal (AP1 keys and values, AP7, AP11, AP12) never fires in a real container. Only the pattern rules are active there: e-mail addresses, the organisation and e-mail keys, `sk-` and bearer tokens. An account or organisation name returned upstream in plain text reaches the agent.
*Disposition:* fix
*Reasoning:* the masking was specified and tested against a registered literal, so its tests pass while production gets none of it. Fixing it means deciding which literals are registered, where they are taken from without the container reading credential files, and how they reach the sidecar: a design question for phase 7 part 2 (containers), recorded in part 2's Decision 18.
