import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def get(path):
    with open(path) as f:
        return f.read()


def cc_assistant(tokens, ts="2026-01-01T00:00:10Z", model="claude-sonnet-5-5"):
    return {"type": "assistant", "timestamp": ts, "message": {"model": model, "usage": {
        "input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0, "output_tokens": 0}}}


def cc_user(text, ts="2026-01-01T00:00:05Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def cx_tokens(tokens, window=400000):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"total_tokens": tokens}, "model_context_window": window}}}


def cx_user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": text}]}}


class HookCase(unittest.TestCase):
    harness = "claude"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "home", "better-auto-compact")
        os.makedirs(self.base)
        shutil.copy(os.path.join(REPO, "hooks", "handoff_hook.py"), self.base)
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump({"soft_tokens": 50000, "min_band_tokens": 10000}, f)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        self.write([])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, records):
        with open(self.transcript, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def hook(self, event, env=None, cwd=None, **extra):
        payload = dict(hook_event_name=event, session_id="s1", cwd=cwd or self.repo, transcript_path=self.transcript,
                       **extra)
        e = dict(os.environ, HOME=self.tmp, CODEX_HOME=self.tmp, **(env or {}))
        e.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
        e.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
        e.pop("BETTER_AUTO_COMPACT", None)
        e.pop("CLAUDE_CONFIG_DIR", None)
        e.pop("CLAUDE_PROJECT_DIR", None)
        e.update(env or {})
        r = subprocess.run([sys.executable, os.path.join(self.base, "handoff_hook.py"), self.harness],
                           input=json.dumps(payload), capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def boundary(self):
        self.boundaries = getattr(self, "boundaries", 0) + 1
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                                "timestamp": "2026-01-02T00:00:%02dZ" % self.boundaries}) + "\n")

    def handoff_path(self):
        return os.path.join(self.repo, ".agent-handoff", "s1", "HANDOFF.md")


class ClaudeHook(HookCase):
    def test_no_nudge_below_soft(self):
        self.write([cc_user("hi"), cc_assistant(20000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")

    def test_nudge_once_then_stop_blocks_until_handoff(self):
        self.write([cc_user("hi"), cc_assistant(60000)])
        out = json.loads(self.hook("PostToolUse"))
        self.assertIn("compact-handoff", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn(".agent-handoff/s1/HANDOFF.md", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(json.loads(self.hook("Stop"))["decision"], "block")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), "")
        with open(self.handoff_path(), "w") as f:
            f.write("# Handoff\n" + "x" * 500)
        self.assertEqual(self.hook("Stop"), "")

    def test_nudge_on_prompt_when_armed_between_turns(self):
        self.write([cc_assistant(60000)])
        out = json.loads(self.hook("UserPromptSubmit", prompt="next"))
        self.assertIn("compact-handoff", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_stop_blocks_are_capped(self):
        self.write([cc_assistant(60000)])
        self.assertTrue(self.hook("PostToolUse"))
        self.assertTrue(self.hook("Stop"))
        self.assertTrue(self.hook("Stop"))
        self.assertEqual(self.hook("Stop"), "")

    def test_stop_does_not_block_before_a_nudge_was_delivered(self):
        self.write([cc_assistant(60000)])
        self.assertEqual(self.hook("Stop"), "")
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_handoff_dir_stays_put_when_cwd_changes(self):
        elsewhere = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(elsewhere)
        self.write([cc_user("hi")])
        self.hook("SessionStart", source="startup")
        self.write([cc_assistant(60000)])
        text = json.loads(self.hook("PostToolUse", cwd=elsewhere))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(os.path.realpath(self.handoff_path()), text)
        self.assertFalse(os.path.exists(os.path.join(elsewhere, ".agent-handoff")))

    def test_project_dir_wins_over_cwd(self):
        elsewhere = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(elsewhere)
        self.write([cc_assistant(60000)])
        out = self.hook("PostToolUse", cwd=elsewhere, env={"CLAUDE_PROJECT_DIR": self.repo})
        self.assertIn(os.path.realpath(self.handoff_path()), json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_learns_compaction_point_from_auto_compaction(self):
        put(os.path.join(self.base, "config.json"), "{}")
        auto = {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T00:00:01Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 174000}}
        self.write([cc_assistant(140000, ts="2026-01-01T00:00:01Z")])
        self.assertEqual(self.hook("PostToolUse"), "")
        with open(self.transcript, "a") as f:
            f.write(json.dumps(auto) + "\n")
            f.write(json.dumps(cc_assistant(140000, ts="2026-01-02T00:00:05Z")) + "\n")
        self.hook("PostToolUse")
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        learned = json.loads(get(os.path.join(self.base, "state", "learned-claude.json")))
        self.assertEqual(learned, {"claude-sonnet-5-5": {"tokens": 174000, "expected": 967000}})

    def test_learned_point_applies_to_a_new_session(self):
        put(os.path.join(self.base, "config.json"), "{}")
        os.makedirs(os.path.join(self.base, "state"))
        put(os.path.join(self.base, "state", "learned-claude.json"),
            json.dumps({"claude-sonnet-5-5": {"tokens": 174000, "expected": 967000}}))
        self.write([cc_assistant(140000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))

    def learned(self, data):
        put(os.path.join(self.base, "config.json"), "{}")
        os.makedirs(os.path.join(self.base, "state"), exist_ok=True)
        put(os.path.join(self.base, "state", "learned-claude.json"), json.dumps(data))

    def test_learned_point_is_ignored_after_settings_change(self):
        self.learned({"claude-sonnet-5-5": {"tokens": 174000, "expected": 967000}})
        self.write([cc_assistant(140000)])
        self.assertEqual(self.hook("PostToolUse", env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}), "")

    def test_learned_point_is_dropped_when_context_grows_past_it(self):
        self.learned({"claude-sonnet-5-5": {"tokens": 174000, "expected": 967000}})
        self.write([cc_assistant(190000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(json.loads(get(os.path.join(self.base, "state", "learned-claude.json"))), {})

    def test_settings_change_rearms_nudges_used_up_under_a_learned_point(self):
        self.learned({"claude-sonnet-5-5": {"tokens": 174000, "expected": 967000}})
        self.write([cc_assistant(172000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        with open(self.handoff_path(), "w") as f:
            f.write("x" * 500)
        self.assertEqual(self.hook("PostToolUse"), "")
        old = time.time() - 5
        os.utime(self.handoff_path(), (old, old))
        pct = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}
        self.assertEqual(self.hook("PostToolUse", env=pct), "")
        self.write([cc_assistant(295000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse", env=pct))

    def test_older_state_with_used_up_bands_rearms_after_settings_change(self):
        self.learned({})
        self.write([cc_assistant(60000)])
        self.hook("PostToolUse")
        path = os.path.join(self.base, "state", "claude-s1.json")
        state = json.loads(get(path))
        state.pop("band_hard")
        state.update(band=3, armed_at=None)
        put(path, json.dumps(state))
        self.write([cc_assistant(510000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))

    def test_integer_observation_from_older_version_is_discarded(self):
        self.learned({})
        self.write([cc_assistant(60000)])
        self.hook("PostToolUse")
        path = os.path.join(self.base, "state", "claude-s1.json")
        state = json.loads(get(path))
        state["observed"] = 174000
        put(path, json.dumps(state))
        self.write([cc_assistant(510000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        self.assertEqual(json.loads(get(path))["observed"], {})

    def test_learning_keeps_other_models(self):
        self.learned({"claude-opus-5-5": {"tokens": 300000, "expected": 967000}})
        self.write([cc_assistant(100000, ts="2026-01-01T00:00:01Z")])
        self.hook("PostToolUse")
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T00:00:01Z",
                                "compactMetadata": {"trigger": "auto", "preTokens": 174000}}) + "\n")
            f.write(json.dumps(cc_assistant(20000, ts="2026-01-02T00:00:05Z")) + "\n")
        self.hook("PostToolUse")
        learned = json.loads(get(os.path.join(self.base, "state", "learned-claude.json")))
        self.assertEqual(sorted(learned), ["claude-opus-5-5", "claude-sonnet-5-5"])

    def test_compaction_under_another_model_is_not_learned(self):
        self.learned({})
        self.write([cc_assistant(100000, ts="2026-01-01T00:00:01Z", model="claude-opus-5-5")])
        self.hook("PostToolUse")
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T00:00:01Z",
                                "compactMetadata": {"trigger": "auto", "preTokens": 174000}}) + "\n")
            f.write(json.dumps(cc_assistant(140000, ts="2026-01-02T00:00:05Z")) + "\n")
        self.hook("PostToolUse")
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(json.loads(get(os.path.join(self.base, "state", "learned-claude.json"))), {})

    def test_sidechain_compaction_is_not_learned_on_first_scan(self):
        self.learned({})
        self.write([{"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T00:00:01Z",
                     "isSidechain": True, "compactMetadata": {"trigger": "auto", "preTokens": 40000}},
                    cc_assistant(140000, ts="2026-01-02T00:00:05Z")])
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_root_comes_from_the_transcript_when_state_has_none(self):
        elsewhere = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(elsewhere)
        self.write([dict(cc_user("hi"), cwd=self.repo), cc_assistant(60000)])
        text = json.loads(self.hook("PostToolUse", cwd=elsewhere))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(os.path.realpath(self.handoff_path()), text)

    def test_manual_compaction_does_not_lower_the_point(self):
        put(os.path.join(self.base, "config.json"), "{}")
        self.write([{"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T00:00:01Z",
                     "compactMetadata": {"trigger": "manual", "preTokens": 174000}},
                    cc_assistant(140000, ts="2026-01-02T00:00:05Z")])
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_next_band_rearms_after_handoff(self):
        self.write([cc_assistant(60000)])
        self.hook("PostToolUse")
        with open(self.handoff_path(), "w") as f:
            f.write("x" * 500)
        self.assertEqual(self.hook("PostToolUse"), "")
        old = time.time() - 5
        os.utime(self.handoff_path(), (old, old))
        self.write([cc_assistant(71000)])
        self.assertTrue(self.hook("PostToolUse"))

    def test_append_does_not_count_as_a_refresh(self):
        self.write([cc_assistant(60000)])
        self.hook("PostToolUse")
        put(self.handoff_path(), "# Handoff\n" + "x" * 500)
        self.assertEqual(self.hook("PostToolUse"), "")
        self.write([cc_assistant(71000)])
        with open(self.handoff_path(), "a") as f:
            f.write("- progress\n")
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        with open(self.handoff_path(), "a") as f:
            f.write("- more progress\n")
        self.assertEqual(json.loads(self.hook("Stop"))["decision"], "block")
        put(self.handoff_path(), "# Handoff 2\n" + "y" * 500)
        self.assertEqual(self.hook("Stop"), "")

    def test_append_on_the_first_call_does_not_count_as_a_refresh(self):
        os.makedirs(os.path.dirname(self.handoff_path()))
        put(self.handoff_path(), "# Handoff\n" + "x" * 500)
        self.write([cc_assistant(60000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        with open(self.handoff_path(), "a") as f:
            f.write("- progress\n")
        self.assertEqual(json.loads(self.hook("Stop"))["decision"], "block")

    def test_older_state_without_a_snapshot_still_detects_appends(self):
        os.makedirs(os.path.dirname(self.handoff_path()))
        put(self.handoff_path(), "# Handoff\n" + "x" * 500)
        self.write([cc_assistant(20000)])
        self.hook("PostToolUse")
        state_path = os.path.join(self.base, "state", "claude-s1.json")
        state = json.loads(get(state_path))
        state.pop("seen_len")
        state.pop("seen_hash")
        put(state_path, json.dumps(state))
        self.write([cc_assistant(60000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        with open(self.handoff_path(), "a") as f:
            f.write("- progress\n")
        self.assertEqual(json.loads(self.hook("Stop"))["decision"], "block")

    def test_nudge_names_the_configured_size_limit(self):
        put(os.path.join(self.base, "config.json"), json.dumps({"soft_tokens": 50000, "max_handoff_bytes": 4096}))
        self.write([cc_assistant(60000)])
        self.assertIn("under 4 KB", self.hook("PostToolUse"))

    def test_edit_inside_the_handoff_counts_as_a_refresh(self):
        os.makedirs(os.path.dirname(self.handoff_path()))
        put(self.handoff_path(), "# Handoff (old)\n" + "x" * 500)
        self.write([cc_assistant(20000)])
        self.hook("PostToolUse")
        self.write([cc_assistant(60000)])
        self.assertIn("compact-handoff", self.hook("PostToolUse"))
        put(self.handoff_path(), "# Handoff (new)\n" + "x" * 500 + "\n- fact\n")
        self.assertEqual(self.hook("Stop"), "")

    def test_oversized_handoff_asks_for_a_rewrite_once(self):
        self.write([cc_assistant(20000)])
        os.makedirs(os.path.dirname(self.handoff_path()))
        put(self.handoff_path(), "x" * 9000)
        out = json.loads(self.hook("PostToolUse"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("is 9 KB, over the 8 KB limit", out)
        self.assertIn(self.handoff_path(), out)
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")
        put(self.handoff_path(), "x" * 1000)
        self.assertEqual(self.hook("PostToolUse"), "")
        put(self.handoff_path(), "x" * 9000)
        self.assertIn("over the 8 KB limit", self.hook("UserPromptSubmit", prompt="next"))

    def test_nudge_covers_an_oversized_handoff(self):
        os.makedirs(os.path.dirname(self.handoff_path()))
        put(self.handoff_path(), "x" * 9000)
        old = time.time() - 5
        os.utime(self.handoff_path(), (old, old))
        self.write([cc_assistant(60000)])
        self.assertIn("Context is at", self.hook("PostToolUse"))
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_usage_before_compact_boundary_is_ignored(self):
        self.write([cc_assistant(90000, ts="2026-01-01T00:00:01Z"),
                    {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-01T00:00:02Z"},
                    cc_assistant(90000, ts="2026-01-01T00:00:01Z")])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")

    def test_synthetic_record_does_not_hide_context_fill(self):
        self.write([cc_assistant(50000), {"type": "assistant", "timestamp": "2026-01-01T00:00:20Z", "message": {
            "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0,
                                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "content": [{"type": "text", "text": "Autocompact is thrashing"}]}}])
        spec = importlib.util.spec_from_file_location("handoff_hook", os.path.join(REPO, "hooks", "handoff_hook.py"))
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        fill, model, _, _ = hook.claude_usage(self.transcript)
        self.assertEqual((fill, model), (50000, "claude-sonnet-5-5"))

    def test_floor_summary_note_and_recovery(self):
        self.write([cc_user("Never use regex in durations.py."), cc_user("<system-reminder>x</system-reminder>"),
                    cc_assistant(30000)])
        out = self.hook("PreCompact", trigger="auto")
        self.assertIn("Summarizer note", out)
        floor = get(os.path.join(self.repo, ".agent-handoff", "s1", "floor.md"))
        self.assertIn("Never use regex in durations.py.", floor)
        self.assertNotIn("system-reminder", floor)
        self.boundary()
        self.hook("PostCompact")
        out = json.loads(self.hook("UserPromptSubmit", prompt="continue"))
        self.assertIn("Context was just compacted", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("UserPromptSubmit", prompt="again"), "")

    def test_recovery_reaches_mid_turn_tool_calls(self):
        self.write([cc_assistant(30000)])
        self.hook("PreCompact", trigger="auto")
        self.boundary()
        self.hook("PostCompact")
        out = json.loads(self.hook("PostToolUse"))
        self.assertIn("Context was just compacted", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("UserPromptSubmit", prompt="next"), "")

    def test_recovery_blocks_stop_once_when_no_tool_ran(self):
        self.write([cc_user("keep going")])
        self.hook("PreCompact", trigger="auto")
        self.boundary()
        self.hook("PostCompact")
        out = json.loads(self.hook("Stop"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("floor.md", out["reason"])
        self.assertNotIn("HANDOFF.md", out["reason"])
        self.assertIn("repeating your last answer", out["reason"])
        self.assertEqual(self.hook("Stop"), "")

    def test_percent_in_repo_path(self):
        repo = os.path.join(self.tmp, "pct%20dir")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self.write([cc_assistant(60000)])
        self.assertIn("pct%20dir", json.loads(self.hook("PostToolUse", cwd=repo))["hookSpecificOutput"]["additionalContext"])

    def test_no_recovery_when_nothing_was_saved(self):
        self.hook("SessionStart", source="startup")
        self.boundary()
        self.hook("PostCompact")
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")

    def test_big_jump_skips_crossed_bands(self):
        self.write([cc_assistant(75000)])
        self.assertTrue(self.hook("PostToolUse"))
        with open(self.handoff_path(), "w") as f:
            f.write("x" * 500)
        self.write([cc_assistant(76000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")

    def test_every_compaction_gets_one_recovery(self):
        for _ in range(2):
            self.hook("PreCompact", trigger="auto")
            self.boundary()
            self.assertIn("Context was just compacted", self.hook("Stop"))
            self.hook("PostCompact")
            self.assertEqual(self.hook("UserPromptSubmit", prompt="next"), "")
        self.hook("PreCompact", trigger="auto")
        self.boundary()
        self.hook("PostCompact")
        self.assertIn("Context was just compacted", self.hook("UserPromptSubmit", prompt="next"))

    def test_paths_are_absolute_from_a_subdirectory(self):
        sub = os.path.join(self.repo, "pkg", "app")
        os.makedirs(sub)
        self.write([cc_assistant(60000)])
        text = json.loads(self.hook("PostToolUse", cwd=sub))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(os.path.realpath(self.handoff_path()), text)

    def test_floor_keeps_user_markup_and_command_args(self):
        self.write([cc_user("<task>Never deploy without approval.</task>"),
                    cc_user("<pasted_content>log line 42</pasted_content>"),
                    cc_user("<command-message>review</command-message><command-name>/review</command-name>"
                            "<command-args>PR 7</command-args>"),
                    cc_user("<command-name>/model</command-name><command-args></command-args>"),
                    cc_user("<task-notification>done</task-notification>"),
                    cc_user("<local-command-stdout>x</local-command-stdout>")])
        self.hook("PreCompact", trigger="manual")
        floor = get(os.path.join(self.repo, ".agent-handoff", "s1", "floor.md"))
        self.assertIn("Never deploy without approval.", floor)
        self.assertIn("log line 42", floor)
        self.assertIn("/review PR 7", floor)
        self.assertNotIn("/model", floor)
        self.assertNotIn("task-notification", floor)
        self.assertNotIn("local-command-stdout", floor)

    def test_subagent_compaction_is_ignored(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.hook("PostToolUse")
        self.hook("PostToolUse", agent_id="a1")
        self.assertEqual(self.hook("PreCompact", trigger="auto"), "")
        self.assertEqual(self.hook("SessionStart", source="compact"), "")
        self.hook("PostCompact")
        self.assertEqual(self.hook("PostToolUse"), "")
        self.hook("PreCompact", trigger="manual")
        self.boundary()
        self.hook("PostCompact")
        self.assertIn("Context was just compacted", self.hook("UserPromptSubmit", prompt="next"))

    def test_overlapping_subagent_compactions_leave_main_alone(self):
        self.write([cc_user("main task"), cc_assistant(60000)])
        self.assertTrue(self.hook("PostToolUse"))
        with open(self.handoff_path(), "w") as f:
            f.write("x" * 500)
        self.hook("PostToolUse", agent_id="a1")
        self.hook("PostToolUse", agent_id="a2")
        for ev, extra in (("PreCompact", {"trigger": "auto"}), ("PreCompact", {"trigger": "auto"}),
                          ("SessionStart", {"source": "compact"}), ("PostCompact", {}),
                          ("SessionStart", {"source": "compact"}), ("PostCompact", {})):
            self.assertEqual(self.hook(ev, **extra), "", ev)
        self.assertEqual(self.hook("PostToolUse"), "")
        self.assertEqual(self.hook("Stop"), "")

    def test_subagent_session_start_does_not_take_main_pointer(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.hook("PostToolUse")
        self.hook("PreCompact", trigger="manual")
        self.boundary()
        self.assertEqual(self.hook("SessionStart", source="compact"), "")
        self.assertIn("Context was just compacted", self.hook("PostToolUse"))

    def test_boundary_text_in_tool_output_is_not_a_compaction(self):
        self.write([cc_user("grep compact_boundary"), cc_assistant(20000)])
        self.hook("PostToolUse")
        with open(self.transcript, "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-01-03T00:00:00Z",
                                "message": {"content": [{"type": "tool_result", "content": '"compact_boundary"'}]}}) + "\n")
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_boundary_followed_by_more_than_the_tail_window(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.hook("PostToolUse")
        self.hook("PreCompact", trigger="manual")
        self.boundary()
        with open(self.transcript, "a") as f:
            pad = json.dumps(cc_user("x" * 1000, ts="2026-01-04T00:00:00Z")) + "\n"
            f.write(pad * 9000)
        self.assertIn("Context was just compacted", self.hook("Stop"))

    def test_shrunk_transcript_does_not_repeat_recovery(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.hook("PostToolUse")
        self.hook("PreCompact", trigger="manual")
        self.boundary()
        self.assertIn("Context was just compacted", self.hook("PostToolUse"))
        with open(self.transcript) as f:
            lines = f.readlines()
        with open(self.transcript, "w") as f:
            f.writelines(lines[:1] + [x for x in lines if '"compact_boundary"' in x])
        self.assertEqual(self.hook("Stop"), "")

    def test_main_compaction_with_stale_fill_while_subagent_runs(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.hook("PostToolUse")
        self.hook("PostToolUse", agent_id="a1")
        self.hook("PreCompact", trigger="auto")
        self.assertTrue(os.path.exists(os.path.join(self.repo, ".agent-handoff", "s1", "floor.md")))
        self.boundary()
        self.assertIn("Context was just compacted", self.hook("PostToolUse"))

    def test_main_compaction_while_subagent_runs(self):
        self.write([cc_user("main task"), cc_assistant(700000)])
        self.hook("PostToolUse", agent_id="a1")
        self.assertIn("Summarizer note", self.hook("PreCompact", trigger="auto"))
        self.boundary()
        self.assertIn("Context was just compacted", self.hook("PostToolUse"))

    def test_low_fill_main_compaction_without_subagents(self):
        self.write([cc_user("main task"), cc_assistant(20000)])
        self.assertIn("Summarizer note", self.hook("PreCompact", trigger="auto"))
        self.boundary()
        self.assertIn("Context was just compacted", self.hook("UserPromptSubmit", prompt="next"))

    def test_session_start_note_once_and_recovery_on_compact(self):
        self.assertIn("handoff hook", self.hook("SessionStart", source="startup"))
        self.assertEqual(self.hook("SessionStart", source="resume"), "")
        self.hook("PreCompact", trigger="auto")
        self.boundary()
        self.assertIn("Context was just compacted", self.hook("SessionStart", source="resume"))

    def test_subagent_and_disabled_are_silent(self):
        self.write([cc_assistant(90000)])
        self.assertEqual(self.hook("PostToolUse", agent_id="a1"), "")
        self.assertEqual(self.hook("PostToolUse", env={"BETTER_AUTO_COMPACT": "off"}), "")
        os.makedirs(os.path.join(self.repo, ".agent-handoff"), exist_ok=True)
        open(os.path.join(self.repo, ".agent-handoff", "DISABLED"), "w").close()
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_handoff_dir_is_git_excluded(self):
        self.write([cc_assistant(60000)])
        self.hook("PostToolUse")
        exclude = get(os.path.join(self.repo, ".git", "info", "exclude"))
        self.assertIn(".agent-handoff/", exclude.splitlines())

    def test_bad_input_never_fails(self):
        r = subprocess.run([sys.executable, os.path.join(self.base, "handoff_hook.py"), "claude"], input="not json",
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class ClaudeThreshold(HookCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump({}, f)

    def test_default_soft_is_half_of_a_1m_window(self):
        self.write([cc_assistant(450000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.write([cc_assistant(510000)])
        self.assertTrue(self.hook("PostToolUse"))

    def test_lower_compaction_window_pulls_soft_down(self):
        self.write([cc_assistant(160000)])
        self.assertTrue(self.hook("PostToolUse", env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}))

    def test_compaction_window_from_settings_env(self):
        put(os.path.join(self.tmp, "home", "settings.json"), json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}}))
        self.write([cc_assistant(160000)])
        self.assertTrue(self.hook("PostToolUse"))

    def test_auto_compaction_window_from_autocompact_is_the_full_window(self):
        put(os.path.join(self.tmp, "home", "settings.json"), json.dumps(
            {"autoCompactWindow": None, "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}}))
        self.write([cc_assistant(450000, model="claude-opus-5-5")])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.write([cc_assistant(510000, model="claude-opus-5-5")])
        self.assertTrue(self.hook("PostToolUse"))

    def test_settings_from_config_dir(self):
        with open(os.path.join(self.tmp, "home", "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 100000, "env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}}, f)
        self.write([cc_assistant(34000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.write([cc_assistant(37000)])
        self.assertTrue(self.hook("PostToolUse"))

    def test_project_threshold_overrides_user(self):
        put(os.path.join(self.tmp, "home", "settings.json"), json.dumps({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}}))
        os.makedirs(os.path.join(self.repo, ".claude"))
        put(os.path.join(self.repo, ".claude", "settings.json"), json.dumps({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}}))
        self.write([cc_assistant(65000, model="claude-sonnet-4-5")])
        self.assertTrue(self.hook("PostToolUse"))

    def test_provider_prefixed_model_ids(self):
        self.write([cc_assistant(110000, model="anthropic.claude-sonnet-5-5-v1:0")])
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_older_200k_models(self):
        self.write([cc_assistant(110000, model="claude-sonnet-4-5")])
        self.assertTrue(self.hook("PostToolUse"))


class CodexHook(HookCase):
    harness = "codex"

    def test_nudge_and_floor_from_rollout(self):
        self.write([cx_user("<environment_context>x</environment_context>"), cx_user("ship build B-1"),
                    cx_tokens(60000)])
        out = json.loads(self.hook("PostToolUse"))
        self.assertIn("compact-handoff", out["hookSpecificOutput"]["additionalContext"])
        self.hook("PreCompact", trigger="auto")
        floor = get(os.path.join(self.repo, ".agent-handoff", "s1", "floor.md"))
        self.assertIn("ship build B-1", floor)
        self.assertNotIn("environment_context", floor)

    def test_floor_skips_hook_texts(self):
        self.write([cx_user('<hook_prompt hook_run_id="stop:5:/x/hooks.json">Context is at 35k tokens</hook_prompt>'),
                    cx_user("Context was just compacted. Before doing anything else"),
                    cx_user("This session has a handoff hook installed by the user."), cx_user("real ask")])
        self.hook("PreCompact", trigger="auto")
        floor = get(os.path.join(self.repo, ".agent-handoff", "s1", "floor.md"))
        self.assertIn("real ask", floor)
        self.assertNotIn("Context was just compacted", floor)
        self.assertNotIn("handoff hook installed", floor)
        self.assertNotIn("35k", floor)

    def test_compacted_record_resets_fill(self):
        self.write([cx_tokens(90000), {"type": "compacted", "payload": {}}])
        self.assertEqual(self.hook("PostToolUse"), "")

    def test_codex_limit_from_config(self):
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(self.tmp, "config.toml"), "w") as f:
            f.write("model_auto_compact_token_limit = 100_000 # mine\n\n[features]\nx = true\n")
        self.write([cx_tokens(70000, window=800000)])
        self.assertEqual(self.hook("PostToolUse"), "")
        self.write([cx_tokens(76000, window=800000)])
        self.assertTrue(self.hook("PostToolUse"))


if __name__ == "__main__":
    unittest.main()
