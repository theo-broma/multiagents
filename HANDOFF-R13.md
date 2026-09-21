# Handoff — R13 (F50–F54, F56), interrupted mid-task

Branch `agents/implementer-deep/d37c3a`, one commit: `40813d7`.

## Done

`tests/support/c1_harness.py` — added section "2b. The generated tinyproxy
configuration" (after `allowlist_admits`, before the auth-proxy section) and
two names to `__all__`:

- `parse_tinyproxy_conf(text) -> dict[str, list[str]]` — parses the generated
  config the way tinyproxy's grammar reads it. Keyword lower-cased, `#`
  comment stripped, quoted value unquoted, values kept in a LIST per keyword.
  Verified by hand: a commented-out `FilterDefaultDeny Yes` does NOT appear in
  the result, and `XFilterURLs Off` does not land under `filterurls`.
- `proxy_config(tmp_path, allowlist=None, **config) -> SimpleNamespace` —
  calls the real `write_proxy_config`, returns
  `(executor, dir, conf_path, filter_path, conf_text, filter_text,
  directives, directive)`. `directive("FilterType")` asserts the keyword
  appears exactly once and returns its value.

55 C1 tests pass. Full suite not re-run since the change (it only adds names).

## Left to do — the whole guard suite

Write `tests/test_phase2_proxy_directives.py`. Nothing exists yet. Planned
tests, all through `h.proxy_config(...)`, asserting `conf.directive(name) ==
value` (never a substring of `conf_text`):

| finding | test name | assertion |
|---|---|---|
| F50 | `test_filter_default_deny_is_yes_so_the_filter_is_an_allow_list` | `directive("FilterDefaultDeny") == "Yes"` |
| F51 | `test_filter_type_is_ere_so_the_generated_patterns_mean_what_they_say` | `directive("FilterType") == "ere"` |
| F52 | `test_filter_case_sensitive_is_off_so_a_capitalised_host_still_matches` | `directive("FilterCaseSensitive") == "Off"` |
| F53 | `test_filter_urls_is_off_so_the_patterns_match_the_host_not_the_url` | `directive("FilterURLs") == "Off"` |
| F54 | `test_the_filter_directive_names_the_file_the_patterns_were_written_to` | `directive("Filter")` is absolute, basename == `conf.filter_path.name` |
| F54 | `test_the_bind_mount_puts_the_generated_filter_file_where_the_directive_looks` | via `ensure_proxy` (below) |
| — | `test_the_security_directives_do_not_depend_on_what_is_in_the_allowlist` | parametrize allowlists `[]`, `None`, one host, many, metacharacters — same five values every time; blocks a future conditional |
| F56 | `test_the_filter_file_is_one_line_per_pattern_and_ends_with_a_newline` | `filter_text == "\n".join(patterns) + "\n"` |

## Things worked out that the diff does not show

- **The directive is `Filter`, not `FilterFile`.** The finding text and the
  contract both say "the FilterFile directive"; the production code emits
  `Filter "/etc/tinyproxy/filter"`, which is tinyproxy's actual keyword. Assert
  `Filter`. Worth flagging to the orchestrator so the mutation check looks for
  the right line.
- **F54's "the two agree" cannot be settled inside `write_proxy_config`.** The
  directive names a CONTAINER path (`/etc/tinyproxy/filter`); the patterns are
  written to a HOST path (`<config_dir>/proxy/<slug>/filter`). The only thing
  that makes them the same file is the bind mount in `ensure_proxy`
  (`docker.py:775`, `-v {config_dir/'filter'}:/etc/tinyproxy/filter:ro`). So
  the second F54 test must reach `ensure_proxy`:
  - `monkeypatch.setattr(docker_mod, "_run", fake_run)` where `fake_run`
    records argv and returns `returncode 0`, `stdout "absent"` (so
    `image_exists` is True and `container_state` is `"absent"`, skipping the
    `docker rm -f` branch). This is the idiom `test_core.py:5454` and `:6636`
    already use — it is argv inspection, not docker-in-docker, which the
    harness docstring forbids.
  - Find `next(c for c in calls if c[:2] == ["docker", "run"])`, pull the `-v`
    values, `host, container, mode = value.rsplit(":", 2)`, and assert
    `mounts[str(config_dir / "filter")] == directive_value`.
  - `config_dir` is `tmp_path / "proxy" / ex.slug` (`make_docker_executor`
    passes `config_dir=tmp_path`).
- **Do NOT touch `write_proxy_config`.** It already emits all five directives
  correctly; the defect is purely that nothing asserts them. No production
  change is needed or planned.
- **Five weak guards already exist** in `tests/test_adversary_allowlist_mutation.py`
  (commit `38f3786`, the adversary run that filed F50–F56):
  `test_tinyproxy_conf_contains_filter_default_deny_yes` and four siblings, all
  `assert "FilterDefaultDeny Yes" in conf`. They are substring checks — they
  would catch a plain deletion but not a commented-out line, a misspelled
  keyword, or an appended `FilterDefaultDeny No`. That file also already
  carries `test_filter_file_ends_with_newline` (F56, so F56 is *partly* covered
  already) and the F2/F55 non-string-entry tests, which are out of scope.
  **Decision taken: leave that file alone** — it is the record of the finding —
  and add the precise guards in a new file. Say so in the result, because the
  contract's claim that the directives are validated "not at all" is not quite
  true as of `38f3786`; they are validated loosely.
- **Boolean spellings.** tinyproxy accepts `Yes`/`On`/`1` and `No`/`Off`/`0`
  interchangeably, so an exact-literal assertion would go red on a
  semantically-identical rewrite. The plan asserts the exact literal anyway
  (the contract asks for it) and the docstrings should say that a legitimate
  change of spelling must update the test deliberately.

## Tooling note for the orchestrator

`consult("dev-advisor", ...)` and `read_finding("F50")` are named in the brief
and the task, but neither is exposed as a tool in this run. The findings were
recovered from `context/review/REPORT.md` instead. No consult was possible.

## Verify with

    uv run --frozen python -m pytest -q tests/test_c1_allowlist_characterization.py tests/test_char_c1_allowlist.py tests/test_c1_sandbox_harness.py
    uv run --frozen python -m pytest
