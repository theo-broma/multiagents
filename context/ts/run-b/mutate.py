import subprocess, sys, re, json, os
W = "/home/theobroma/.multiagents/worktrees/multiagents-3e439ff1/ag-6e59b2"
R = "src/multiagents/runner.py"; S = "src/multiagents/supervisor.py"; V = "src/multiagents/viewer.py"
N = "src/multiagents/notices.py"; O = "src/multiagents/occupancy.py"; D = "src/multiagents/driver.py"
T = "tests/"
M = [
 ("A consult lock-wait result loses its keys", R,
  """        node = self._find_conversation(agent_name)
        return self._consult_result(agent_name, node.id if node else None, None,
                                    error=error)""",
  """        node = self._find_conversation(agent_name)
        return {"agent": agent_name, "error": error}""",
  ["tests/test_consult_fresh_worktree.py::test_decided_lock_wait_timeout_result_carries_every_key"]),
 ("B inf min-runtime taken as given (no default fallback)", D,
  """    survived = _limit_number(config, "restart_min_runtime_seconds")""",
  """    survived = float(config.limits.get("restart_min_runtime_seconds", 60))""",
  ["tests/test_phase0_r8f_leftovers.py::test_p0_r8f_20_an_inf_min_runtime_still_retries_a_crash_past_60s"]),
 ("C1 view never leaves after a terminal run", V,
  """            if time.time() - terminal_time > LINGER_SECONDS:""",
  """            if False:""",
  ["tests/test_d2_view.py::test_tm_r1_terminal_run_prints_everything_and_final_status_then_exits[flags1]",
   "tests/test_d2_view.py::test_tm_r1_follow_prints_new_events_then_final_status_when_the_run_ends",
   "tests/test_d2_view.py::test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal"]),
 ("C2 linger default 30 s instead of 60 s", V,
  """LINGER_SECONDS = 60""", """LINGER_SECONDS = 30""",
  ["tests/test_d2_view.py::test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal"]),
 ("C3 truncation fingerprint not checked", V,
  """                or (offset and not _same_file(stream, offset, head, tail))):""",
  """                ):""",
  ["tests/test_d2_adversary.py::test_follower_drops_lines_when_file_grows_after_truncation"]),
 ("D1 wall-clock timeout never trips", S,
  """        if not self._timeout_reported and now - self.started > self.wall_timeout:""",
  """        if False and now - self.started > self.wall_timeout:""",
  ["tests/test_d1_limit_notices.py::test_ln_c1_ln_c2_ln_c6_agent_watchdog_source[timeout-timeout]",
   "tests/test_d1_limit_notices.py::test_ln_c1_ln_c6_project_watchdog_rows[limits.default_timeout-default_timeout]",
   "tests/test_d1_limit_notices.py::test_ln_c2_call_timeout_overrides_agent_and_project_source",
   "tests/test_d1_review_findings.py::test_d1_f8_provenance_is_captured_at_launch_not_read_from_current_yaml[timeout]",
   "tests/test_d1_adversary.py::test_adv_adopted_run_provenance_is_not_taken_from_container_writable_tree_json",
   "tests/test_d1_adversary_followup.py::test_d1_finding8_steer_keeps_original_call_value_despite_forged_tree",
   "tests/test_d1_adversary_followup.py::test_d1_finding8_deleted_host_record_falls_back_to_current_config",
   "tests/test_h4_h14_routing_limits.py::test_lm_r1_project_timeout_is_enforced_on_start",
   "tests/test_h4_h14_routing_limits.py::test_lm_r1b_consult_uses_project_timeout",
   "tests/test_h4_h14_routing_limits.py::test_lm_r1b_steer_keeps_original_call_timeout",
   "tests/test_phase0_watchdog.py::test_p0_r2_9_a_failing_poll_is_recorded_and_the_next_poll_runs",
   "tests/test_phase0_watchdog.py::test_p0_r2_1_and_r2_5_timeout_after_loop_is_a_second_stuck_event",
   "tests/test_phase0_watchdog.py::test_p0_r2_9_wall_timeout_reaches_the_tree"]),
 ("D2 silence never trips", S,
  """        if not self._silence_pending and now - self.last_event > self.silence_timeout:""",
  """        if False and now - self.last_event > self.silence_timeout:""",
  ["tests/test_d1_limit_notices.py::test_ln_c1_ln_c2_ln_c6_agent_watchdog_source[silence_timeout-silence_timeout]",
   "tests/test_d1_limit_notices.py::test_ln_c1_ln_c6_project_watchdog_rows[limits.silence_timeout-silence_timeout]",
   "tests/test_d1_review_findings.py::test_d1_f8_provenance_is_captured_at_launch_not_read_from_current_yaml[silence_timeout]",
   "tests/test_h4_h14_routing_limits.py::test_lm_r1_project_silence_timeout_is_enforced_on_start"]),
 ("D3 trip provenance read from the config as it is now", R,
  """        limits = run.limits
        if not limits:
            limits = self.launch_limits.lookup(node_id)
        if not limits:""",
  """        limits = None
        if not limits:""",
  ["tests/test_d1_review_findings.py::test_d1_f8_provenance_is_captured_at_launch_not_read_from_current_yaml[timeout]",
   "tests/test_d1_review_findings.py::test_d1_f8_provenance_is_captured_at_launch_not_read_from_current_yaml[silence_timeout]"]),
 ("D4 adopted run's limits read from tree.json", R,
  """        limits = self.launch_limits.lookup(node.id)
        run = Run(""",
  """        limits = (self.tree.read()["nodes"].get(node.id) or {}).get("limits") or {}
        run = Run(""",
  ["tests/test_d1_adversary.py::test_adv_adopted_run_provenance_is_not_taken_from_container_writable_tree_json"]),
 ("D5 steer's recorded call timeout read from tree.json", R,
  """            recorded = self.launch_limits.lookup(node_id).get("timeout")""",
  """            recorded = ((self.tree.read()["nodes"].get(node_id) or {}).get("limits") or {}).get("timeout")""",
  ["tests/test_d1_adversary_followup.py::test_d1_finding8_steer_keeps_original_call_value_despite_forged_tree",
   "tests/test_d1_adversary_followup.py::test_d1_finding8_deleted_host_record_falls_back_to_current_config"]),
 ("D6 steer forgets the call timeout", R,
  """        if not timeout:
            recorded = self.launch_limits.lookup(node_id).get("timeout")""",
  """        if False:
            recorded = self.launch_limits.lookup(node_id).get("timeout")""",
  ["tests/test_h4_h14_routing_limits.py::test_lm_r1b_steer_keeps_original_call_timeout"]),
 ("D7 notice mirror written after the state lock is released", N,
  """        state.commit(data)
        _mirror(tree, data)
    return result""",
  """        state.commit(data)
    _mirror(tree, data)
    return result""",
  ["tests/test_d1_adversary.py::test_adv_a_stale_mirror_cannot_resurrect_a_cleared_notice"]),
 ("D8 occupancy ignores a sibling that already ended", O,
  """                if since is None:
                    continue
                ended = run.get("ended")""",
  """                continue
                ended = run.get("ended")""",
  ["tests/test_d1_adversary.py::test_adv_oom_sibling_that_ended_before_the_kill_is_still_a_sibling"]),
 ("D9 occupancy ignores a live sibling", O,
  """                if run.get("ended") is None:
                    return True      # live now: overlapped by definition""",
  """                if run.get("ended") is None:
                    continue      # live now: overlapped by definition""",
  ["tests/test_d1_review_findings.py::test_d1_f1_oom_increase_with_a_sibling_in_another_runner_is_kill_uncertain"]),
 ("E1 a failing poll ends the watch loop", R,
  """            except Exception as exc:
                self.tree.emit(run.node_id, "watchdog_poll_error",""",
  """            except Exception as exc:
                return
                self.tree.emit(run.node_id, "watchdog_poll_error",""",
  ["tests/test_phase0_watchdog.py::test_p0_r2_9_a_failing_poll_is_recorded_and_the_next_poll_runs"]),
 ("E2 timeout re-reported on every poll", S,
  """            self._timeout_reported = True""",
  """            self._timeout_reported = False""",
  ["tests/test_phase0_watchdog.py::test_p0_r2_8_idle_polls_after_a_terminal_trip_write_nothing"]),
 ("E3 a steered turn inherits the tripped wall clock", R,
  """            supervisor = self._supervisor(spec, provider, wall,
                                          limits["silence_timeout"]["value"])""",
  """            supervisor = self._supervisor(spec, provider, wall,
                                          limits["silence_timeout"]["value"])
            _prev = self.runs.get(node_id)
            if session_id and _prev is not None and _prev.supervisor is not None:
                supervisor._timeout_reported = _prev.supervisor._timeout_reported""",
  ["tests/test_phase0_watchdog.py::test_p0_r2_7_steer_starts_a_fresh_watchdog_timeout"]),
 ("F1 a stream event does not clear a silence trip", R,
  """            run.trip_kind == "silence"
            or supervisor.last_digest""",
  """            False
            or supervisor.last_digest""",
  ["tests/test_stuck_lifecycle.py::test_sl_r3_any_stream_event_clears_a_silence_trip"]),
 ("F2 a declared (opaque) tool exempts silence", S,
  """            previous = self.progress_when_last_quiet
            self.progress_when_last_quiet = self.current_progress""",
  """            if self.opaque_calls:
                return None
            previous = self.progress_when_last_quiet
            self.progress_when_last_quiet = self.current_progress""",
  ["tests/test_stuck_lifecycle.py::test_sl_r6_silence_still_bounds_a_declared_tool"]),
 ("G1 unadoptable node left detached", R,
  """            self.tree.set_status(node.id, "failed",
                                 f"could not be adopted after its server exited: \"""",
  """            (lambda *a: None)(node.id, "failed",
                                 f"could not be adopted after its server exited: \"""",
  ["tests/test_agent_survival_adversary.py::test_adversary_adoption_does_not_loop_forever_on_corrupt_node"]),
 ("G2 silence counted from launch across the server gap", R,
  """        supervisor.started = time.monotonic() - max(0.0, now() - launched)""",
  """        supervisor.started = time.monotonic() - max(0.0, now() - launched)
        supervisor.last_event = supervisor.started""",
  ["tests/test_agent_survival.py::test_sv_r8_the_servers_downtime_is_not_silence"]),
 ("G3 silence counted from launch, replay keeps the old clock", R,
  """                if replayed:
                    run.supervisor.observe(event)
                    continue""",
  """                if replayed:
                    _keep = run.supervisor.last_event
                    run.supervisor.observe(event)
                    run.supervisor.last_event = _keep
                    continue""",
  ["tests/test_agent_survival.py::test_sv_r8_the_servers_downtime_is_not_silence"]),
 ("G4 (G3 against the ORIGINAL sv_r8)", R,
  """                if replayed:
                    run.supervisor.observe(event)
                    continue""",
  """                if replayed:
                    _keep = run.supervisor.last_event
                    run.supervisor.observe(event)
                    run.supervisor.last_event = _keep
                    continue""",
  ["tests/test_zz_tmp_sv_orig.py::test_sv_r8_the_servers_downtime_is_not_silence"]),
 ("H1 wrap-up flag kept on the Run (bug-c050b0)", R,
  """            if node.wrap_up_asked:
                recovered""",
  """            if getattr(run, "_asked", False):
                recovered""",
  ["tests/test_burn_rate_baseline.py::test_br_r5_one_wrap_up_per_agent_through_steers_and_many_passes",
   "tests/test_burn_rate_baseline.py::test_br_r5_each_agent_is_asked_once_not_once_per_provider"]),
 ("I1 consult turns not locked", R,
  """                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)""",
  """                    pass""",
  ["tests/test_consult_fresh_worktree.py::test_cf_r7_two_concurrent_consults_never_overlap_and_never_refresh_mid_turn"]),
 ("I2 a steer never hears the first event", R,
  """                if run.done.is_set() or run.events:""",
  """                if run.done.is_set():""",
  ["tests/test_core.py::test_a_steer_says_whether_anything_answered"]),
 ("J1 reload rebuilds the tree", R,
  """        providers = load_providers(config.providers)
        if config.providers != self.config.providers:""",
  """        providers = load_providers(config.providers)
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        if config.providers != self.config.providers:""",
  ["tests/test_phase0_config_reload.py::test_p0_r5_6_tree_and_in_flight_runs_are_the_same_objects_after_a_reload"]),
 ("J2 reload rewrites running supervisors' max_steps", R,
  """        providers = load_providers(config.providers)
        if config.providers != self.config.providers:""",
  """        providers = load_providers(config.providers)
        for _r in self.runs.values():
            if _r.supervisor is not None:
                _r.supervisor.max_steps = int(config.limits.get("max_steps", 250))
        if config.providers != self.config.providers:""",
  ["tests/test_phase0_config_reload.py::test_p0_r5_3_a_running_agent_keeps_the_config_it_started_with"]),
]
only = sys.argv[1:]
H1_SET = """            self.tree.update(node_id, wrap_up_asked=True, wrap_up_headroom=headroom)"""
for name, f, old, new, tests in M:
    if only and not any(name.startswith(o) for o in only):
        continue
    path = os.path.join(W, f); src = open(path).read()
    assert src.count(old) == 1, (name, src.count(old))
    mut = src.replace(old, new)
    if name.startswith(("G3", "G4")):
        a = "        supervisor.started = time.monotonic() - max(0.0, now() - launched)\n"
        assert mut.count(a) == 1
        mut = mut.replace(a, a + "        supervisor.last_event = supervisor.started\n")
    if name.startswith("H1"):
        assert mut.count(H1_SET) == 1
        mut = mut.replace(H1_SET, "            run._asked = True\n" + H1_SET)
    open(path, "w").write(mut)
    try:
        p = subprocess.run([W + "/.venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "--basetemp=/var/tmp/ag-6e59b2-mut", "-n", str(min(len(tests), 8)),
                            "-rf", "--tb=line", *tests],
                           cwd=W, env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True, timeout=900)
    finally:
        subprocess.run(["git", "checkout", "--", f], cwd=W, check=True)
    out = p.stdout
    failed = set(re.findall(r"^FAILED (\S+)", out, re.M))
    lines = [l for l in out.splitlines() if re.match(r"^/.*:\d+: ", l) or l.startswith("E ")]
    print(f"### {name}")
    for t in tests:
        print(("  KILLED  " if t in failed else "  SURVIVED") + "  " + t.split("tests/")[1])
    for l in lines[:len(tests)+2]:
        print("     " + l[-230:])
    print(out.strip().splitlines()[-1] if out.strip() else p.stderr[-500:])
    sys.stdout.flush()
