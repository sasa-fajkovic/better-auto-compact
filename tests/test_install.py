import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def get(path):
    with open(path) as f:
        return f.read()
sys.path.insert(0, REPO)
import install


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claude = os.path.join(self.tmp, "claude")
        self.codex = os.path.join(self.tmp, "codex")
        os.makedirs(self.claude)
        os.makedirs(self.codex)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_install(self, *args):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")}
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.dict(os.environ, clean, clear=True):
            install.main(list(args) + ["--claude-dir", self.claude, "--codex-home", self.codex])
        return out.getvalue()

    def settings(self):
        return json.loads(get(os.path.join(self.claude, "settings.json")))

    def write_settings(self, data):
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump(data, f)


class ClientPercentInstall(InstallCase):
    def manifest_path(self, root):
        return os.path.join(root, install.NAME, "manifest.json")

    def codex_text(self):
        return get(os.path.join(self.codex, "config.toml"))

    def assert_percents(self, claude, codex):
        self.assertEqual(self.settings()["env"][install.CLAUDE_PCT], str(claude))
        self.assertEqual(install.toml_get_top(self.codex_text(), install.CODEX_KEY), str(codex))
        for root, percent in ((self.claude, claude), (self.codex, codex)):
            self.assertEqual(json.loads(get(self.manifest_path(root)))["percent"], percent)

    def test_fresh_defaults_are_independent(self):
        self.run_install()
        self.assert_percents(30, 40)

    def test_client_options_are_retained_independently(self):
        self.run_install("--claude-compact-percent", "35", "--codex-compact-percent", "60")
        self.run_install()
        self.assert_percents(35, 60)

    def test_shared_option_sets_both_selected_clients(self):
        self.run_install("--compact-percent", "50")
        self.run_install()
        self.assert_percents(50, 50)

    def test_client_options_override_shared_option_in_any_order(self):
        self.run_install("--claude-compact-percent", "35", "--compact-percent", "50",
                         "--codex-compact-percent", "60")
        self.assert_percents(35, 60)
        self.run_install("--hard-percent", "55", "--codex-compact-percent", "45")
        self.assert_percents(55, 45)
        self.run_install("--claude-compact-percent", "30", "--compact-percent", "65")
        self.assert_percents(30, 65)

    def test_codex_installed_first_does_not_set_claude_default(self):
        self.run_install("--codex", "--codex-compact-percent", "65")
        self.run_install("--claude")
        self.assert_percents(30, 65)

    def test_single_client_upgrade_leaves_other_files_identical(self):
        self.run_install("--compact-percent", "30")
        for target, other, files, option in (
                ("--codex", self.claude, ("settings.json",), "--codex-compact-percent"),
                ("--claude", self.codex, ("config.toml", "hooks.json"), "--claude-compact-percent")):
            with self.subTest(target=target):
                paths = [os.path.join(other, name) for name in files] + [self.manifest_path(other)]
                before = {path: get(path) for path in paths}
                self.run_install(target, option, "45")
                self.assertEqual({path: get(path) for path in paths}, before)
        self.assert_percents(45, 45)

    def test_legacy_local_manifest_values_are_preserved(self):
        self.run_install("--claude-compact-percent", "45", "--codex-compact-percent", "55")
        for root in (self.claude, self.codex):
            path = self.manifest_path(root)
            manifest = json.loads(get(path))
            manifest.pop("percent")
            put(path, json.dumps(manifest))
        self.run_install()
        self.assert_percents(45, 55)

    def test_legacy_default_is_retained_until_explicitly_changed(self):
        self.run_install("--compact-percent", "30")
        self.run_install()
        self.assert_percents(30, 30)
        self.run_install("--codex", "--codex-compact-percent", "40")
        self.assert_percents(30, 40)

    def test_client_option_with_no_threshold_is_remembered(self):
        self.run_install("--no-threshold", "--claude-compact-percent", "45", "--codex-compact-percent", "55")
        self.assertNotIn("env", self.settings())
        self.assertFalse(os.path.exists(os.path.join(self.codex, "config.toml")))
        self.run_install()
        self.assert_percents(45, 55)

    def test_invalid_percent_options_fail_before_writing(self):
        for option in ("--compact-percent", "--hard-percent", "--claude-compact-percent", "--codex-compact-percent"):
            for value in ("9", "96", "0", "-1", "40.5", "abc"):
                with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.run_install(option, value)
                    self.assertEqual(os.listdir(self.claude), [])
                    self.assertEqual(os.listdir(self.codex), [])

    def test_range_boundaries_are_accepted(self):
        self.run_install("--claude-compact-percent", "10", "--codex-compact-percent", "95")
        self.assert_percents(10, 95)


class ClaudeInstall(InstallCase):
    def test_install_merges_and_is_idempotent(self):
        other = {"hooks": [{"type": "command", "command": "echo other"}]}
        self.write_settings({"model": "x", "hooks": {"SessionStart": [other]}, "env": {"FOO": "1"}})
        self.run_install("install", "--claude")
        self.run_install("install", "--claude")
        s = self.settings()
        self.assertEqual(s["model"], "x")
        self.assertEqual(s["env"], {"FOO": "1", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"})
        self.assertEqual(s["hooks"]["SessionStart"][0], other)
        for ev in install.EVENTS:
            ours = [g for g in s["hooks"][ev] if install.ours(g)]
            self.assertEqual(len(ours), 1, ev)
            self.assertTrue(ours[0]["hooks"][0]["command"].endswith("handoff_hook.py claude"))
        self.assertTrue(os.path.exists(os.path.join(self.claude, "better-auto-compact", "handoff_hook.py")))
        self.assertTrue(os.path.exists(os.path.join(self.claude, "skills", "compact-handoff", "SKILL.md")))

    def test_existing_threshold_is_kept_and_restored_on_uninstall(self):
        self.write_settings({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}})
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70")
        self.run_install("uninstall", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70")

    def test_uninstall_removes_only_ours(self):
        other = {"hooks": [{"type": "command", "command": "echo other"}]}
        self.write_settings({"hooks": {"Stop": [other]}})
        self.run_install("install", "--claude")
        self.run_install("uninstall", "--claude")
        s = self.settings()
        self.assertEqual(s, {"hooks": {"Stop": [other]}})
        self.assertFalse(os.path.exists(os.path.join(self.claude, "better-auto-compact")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "skills", "compact-handoff")))

    def test_dry_run_writes_nothing(self):
        self.run_install("install", "--claude", "--dry-run")
        self.assertEqual(os.listdir(self.claude), [])

    def test_foreign_skill_is_not_overwritten(self):
        d = os.path.join(self.claude, "skills", "compact-handoff")
        os.makedirs(d)
        put(os.path.join(d, "SKILL.md"), "mine")
        with self.assertRaises(SystemExit):
            self.run_install("install", "--claude")
        self.assertEqual(get(os.path.join(d, "SKILL.md")), "mine")

    def test_user_change_after_install_is_kept(self):
        self.run_install("install", "--claude")
        s = self.settings()
        s["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "75"
        self.write_settings(s)
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "75")
        self.run_install("uninstall", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "75")

    def test_uninstall_keeps_other_commands_in_a_shared_group(self):
        self.run_install("install", "--claude")
        s = self.settings()
        s["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo mine"})
        self.write_settings(s)
        self.run_install("uninstall", "--claude")
        self.assertEqual(self.settings()["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]})

    def test_file_mode_is_kept(self):
        self.write_settings({"env": {"SECRET": "x"}})
        os.chmod(os.path.join(self.claude, "settings.json"), 0o600)
        self.run_install("install", "--claude")
        self.assertEqual(os.stat(os.path.join(self.claude, "settings.json")).st_mode & 0o777, 0o600)

    def test_relative_dir_gives_absolute_command(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                install.main(["install", "--claude", "--claude-dir", "claude"])
        finally:
            os.chdir(cwd)
        script = shlex.split(self.settings()["hooks"]["Stop"][0]["hooks"][0]["command"])[1]
        self.assertTrue(os.path.isabs(script))
        self.assertEqual(os.path.realpath(script), os.path.realpath(os.path.join(self.claude, "better-auto-compact",
                                                                                  "handoff_hook.py")))

    def test_shell_characters_in_config_dir(self):
        odd = os.path.join(self.tmp, "c$HOME `x` it's")
        os.makedirs(odd)
        with contextlib.redirect_stdout(io.StringIO()):
            install.main(["install", "--claude", "--claude-dir", odd])
        cmd = json.loads(get(os.path.join(odd, "settings.json")))["hooks"]["Stop"][0]["hooks"][0]["command"]
        r = subprocess.run(cmd, shell=True, input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(shlex.split(cmd)[1], os.path.join(odd, "better-auto-compact", "handoff_hook.py"))

    def test_existing_compaction_window_is_respected(self):
        self.write_settings({"autoCompactWindow": 300000})
        self.run_install("install", "--claude")
        self.assertNotIn("env", self.settings())

    def test_exported_compaction_window_is_respected(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"}), \
                contextlib.redirect_stdout(io.StringIO()):
            install.main(["install", "--claude", "--claude-dir", self.claude])
        self.assertNotIn("env", self.settings())

    def test_symlinked_settings_stay_linked(self):
        target = os.path.join(self.tmp, "dotfiles-settings.json")
        put(target, "{}")
        os.symlink(target, os.path.join(self.claude, "settings.json"))
        self.run_install("install", "--claude")
        self.assertTrue(os.path.islink(os.path.join(self.claude, "settings.json")))
        self.assertIn("hooks", json.loads(get(target)))

    def test_install_and_uninstall_make_no_backup(self):
        self.write_settings({"model": "x"})
        self.run_install("install", "--claude")
        self.run_install("uninstall", "--claude")
        self.assertEqual([f for f in os.listdir(self.claude) if ".bak" in f], [])

    def test_uninstall_without_our_entries_leaves_file_alone(self):
        put(os.path.join(self.claude, "settings.json"), '{"model":"x"}')
        self.run_install("uninstall", "--claude")
        self.assertEqual(get(os.path.join(self.claude, "settings.json")), '{"model":"x"}')

    def test_chosen_percent_is_kept_on_reinstall(self):
        self.run_install("install", "--claude", "--compact-percent", "50")
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "50")
        self.run_install("install", "--claude", "--compact-percent", "30")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "30")

    def test_percent_from_older_install_is_kept(self):
        self.run_install("install", "--claude", "--compact-percent", "45")
        path = os.path.join(self.claude, "better-auto-compact", "manifest.json")
        manifest = json.loads(get(path))
        manifest.pop("percent")
        put(path, json.dumps(manifest))
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "45")

    def test_harness_installed_later_gets_its_own_default(self):
        self.run_install("install", "--claude", "--compact-percent", "50")
        self.run_install("install", "--codex")
        self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                              "model_post_turn_compact_threshold_percent"), "40")

    def test_broken_manifest_of_other_harness_is_ignored(self):
        self.run_install("install", "--claude", "--compact-percent", "50")
        os.makedirs(os.path.join(self.codex, "better-auto-compact"))
        put(os.path.join(self.codex, "better-auto-compact", "manifest.json"), "{broken")
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "50")

    def test_broken_manifest_of_other_harness_falls_back_to_default(self):
        os.makedirs(os.path.join(self.codex, "better-auto-compact"))
        put(os.path.join(self.codex, "better-auto-compact", "manifest.json"), "{broken")
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str(install.DEFAULT_CLAUDE_PERCENT))

    def test_percent_passed_with_no_threshold_is_remembered(self):
        self.run_install("install", "--claude", "--no-threshold", "--compact-percent", "60")
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "60")

    def test_hard_percent_alias(self):
        self.run_install("install", "--claude", "--hard-percent", "40")
        self.assertEqual(self.settings()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "40")

    def test_percent_out_of_range(self):
        with self.assertRaises(SystemExit):
            self.run_install("install", "--claude", "--compact-percent", "99")

    def test_no_threshold(self):
        self.run_install("install", "--claude", "--no-threshold")
        self.assertNotIn("env", self.settings())

    def test_status_shows_compaction_point(self):
        self.run_install("install", "--claude")
        out = self.run_install("status", "--claude")
        self.assertIn("claude 5.x: compacts at ~294k of a 1000k window", out)
        self.assertNotIn("warning", out)

    def test_status_warns_about_window_lowered_after_install(self):
        self.run_install("install", "--claude")
        s = self.settings()
        s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "500000"
        self.write_settings(s)
        out = self.run_install("status", "--claude")
        self.assertIn("claude 5.x: compacts at ~144k of a 500k window", out)
        self.assertIn("warning: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30", out)

    def test_status_warns_about_model_window_without_our_env(self):
        self.write_settings({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"},
                             "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 500000}}})
        out = self.run_install("status", "--claude")
        self.assertIn("claude-opus-5-5: compacts at ~144k of a 500k window", out)
        self.assertIn("warning: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30", out)

    def test_existing_window_env_is_kept_on_install_and_uninstall(self):
        self.write_settings({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"}})
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"], {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"})
        self.run_install("uninstall", "--claude")
        self.assertEqual(self.settings()["env"], {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"})

    def test_user_window_change_after_install_is_kept(self):
        self.run_install("install", "--claude")
        s = self.settings()
        s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "800000"
        self.write_settings(s)
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "800000")
        self.run_install("uninstall", "--claude")
        self.assertEqual(self.settings()["env"], {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"})

    def older_install_with_window(self):
        self.run_install("install", "--claude")
        s = self.settings()
        s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "1000000"
        self.write_settings(s)
        manifest = os.path.join(self.claude, "better-auto-compact", "manifest.json")
        m = json.loads(get(manifest))
        m["added_window"] = "1000000"
        put(manifest, json.dumps(m))
        return manifest

    def test_window_from_older_install_is_removed_on_reinstall(self):
        manifest = self.older_install_with_window()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"}), \
                contextlib.redirect_stdout(io.StringIO()):
            install.main(["install", "--claude", "--claude-dir", self.claude, "--compact-percent", "50"])
        self.assertEqual(self.settings()["env"], {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        self.assertNotIn("added_window", json.loads(get(manifest)))

    def test_window_from_older_install_is_removed_on_uninstall(self):
        self.older_install_with_window()
        self.run_install("uninstall", "--claude")
        self.assertNotIn("env", self.settings())

    def test_install_adds_no_window(self):
        self.run_install("install", "--claude")
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", self.settings()["env"])

    def learn(self, tokens):
        state = os.path.join(self.claude, "better-auto-compact", "state")
        os.makedirs(state, exist_ok=True)
        put(os.path.join(state, "learned-claude.json"), json.dumps({"claude-opus-5-5": {"tokens": tokens, "expected": 294000}}))

    def test_status_shows_learned_compaction_point(self):
        self.learn(174000)
        self.run_install("install", "--claude")
        status = self.run_install("status", "--claude")
        self.assertIn("claude-opus-5-5: last automatic compaction at ~174k", status)
        self.assertIn("note: CLAUDE_CODE_AUTO_COMPACT_WINDOW overrides a window set with /autocompact", status)

    def test_auto_compaction_window_counts_as_unset(self):
        self.write_settings({"autoCompactWindow": None, "modelSettings": {"claude-opus-5-5": {"autoCompactWindow": "auto"}}})
        self.run_install("install", "--claude")
        self.assertEqual(self.settings()["env"], {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "30"})
        out = self.run_install("status", "--claude")
        self.assertIn("claude 5.x: compacts at ~294k of a 1000k window", out)

    def test_status_without_percent_does_not_warn(self):
        self.write_settings({"autoCompactWindow": 300000})
        self.run_install("install", "--claude")
        out = self.run_install("status", "--claude")
        self.assertIn("claude 5.x: compacts at ~267k of a 300k window", out)
        self.assertNotIn("warning", out)


class CodexInstall(InstallCase):
    def test_install_and_uninstall(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        put(os.path.join(self.codex, "hooks.json"), json.dumps(existing))
        toml = 'model = "m"\n\n[features]\nhooks = true\n'
        put(os.path.join(self.codex, "config.toml"), toml)
        out = self.run_install("install", "--codex")
        self.assertIn("approve", out)
        text = get(os.path.join(self.codex, "config.toml"))
        self.assertLess(text.index("model_post_turn_compact_threshold_percent = 40"), text.index("[features]"))
        hooks = json.loads(get(os.path.join(self.codex, "hooks.json")))
        self.assertEqual(len(hooks["hooks"]["SessionStart"]), 2)
        self.assertTrue(hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"].endswith("handoff_hook.py codex"))
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), toml)
        self.assertEqual(json.loads(get(os.path.join(self.codex, "hooks.json"))), existing)

    def test_user_token_limit_change_survives_explicit_reinstall_and_uninstall(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("--codex", "--codex-compact-percent", "55")
        path = os.path.join(self.codex, "config.toml")
        text = get(path).replace("model_auto_compact_token_limit = 261250",
                                 "model_auto_compact_token_limit = 220000")
        put(path, text)
        self.run_install("--codex", "--codex-compact-percent", "40")
        self.assertEqual(get(path), text)
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(path), 'model_auto_compact_token_limit = 220000\nmodel = "m"\n')

    def test_unowned_token_cap_and_window_block_requested_percent(self):
        self.cache(max_context_window=872000)
        path = os.path.join(self.codex, "config.toml")
        put(path, 'model = "m"\nmodel_context_window = 1000000\n')
        self.run_install("--codex", "--compact-percent", "30")
        text = get(path).replace("model_auto_compact_token_limit = 248520",
                                 "model_auto_compact_token_limit = 200000")
        put(path, text)
        out = self.run_install("--codex", "--codex-compact-percent", "40")
        self.assertEqual(get(path), text)
        self.assertIn("keeping your model_auto_compact_token_limit = 200000; no thresholds added", out)
        manifest = json.loads(get(os.path.join(self.codex, install.NAME, "manifest.json")))
        self.assertEqual(manifest["percent"], 40)
        self.assertEqual(manifest["added_key"], "30")
        self.assertEqual(manifest["added_limit"], "248520")
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(path), 'model_auto_compact_token_limit = 200000\n'
                         'model = "m"\nmodel_context_window = 1000000\n')

    def test_legacy_unowned_overrides_require_removal_before_adopting_new_default(self):
        self.run_install("--claude")
        claude_before = {}
        for directory, _, filenames in os.walk(self.claude):
            for filename in filenames:
                path = os.path.join(directory, filename)
                with open(path, "rb") as f:
                    claude_before[path] = f.read()
        self.cache()
        path = os.path.join(self.codex, "config.toml")
        unrelated = 'model = "m"\n\n[features]\nhooks = true\n'
        legacy = ('model_context_window = 1000000\nmodel_auto_compact_token_limit = 200000\n'
                  + unrelated)
        put(path, legacy)
        hooks_path = os.path.join(self.codex, "hooks.json")
        hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        put(hooks_path, json.dumps(hooks))
        manifest_path = os.path.join(self.codex, install.NAME, "manifest.json")
        os.makedirs(os.path.dirname(manifest_path))
        put(manifest_path, json.dumps({"percent": 30}))
        out = self.run_install("--codex")
        self.assertEqual(get(path), legacy)
        self.assertIn("keeping your model_auto_compact_token_limit = 200000; no thresholds added", out)
        manifest = json.loads(get(manifest_path))
        self.assertEqual(manifest["percent"], 30)
        self.assertNotIn("added_key", manifest)
        self.assertNotIn("added_limit", manifest)
        self.assertIsNone(install.toml_get_top(get(path), install.CODEX_KEY))
        put(path, unrelated)
        out = self.run_install("--codex", "--codex-compact-percent", "40")
        self.assertEqual(install.toml_get_top(get(path), install.CODEX_KEY), "40")
        self.assertEqual(install.toml_get_top(get(path), install.LIMIT_KEY), "190000")
        self.assertIsNone(install.toml_get_top(get(path), "model_context_window"))
        self.assertIn("475000-token window", out)
        manifest = json.loads(get(manifest_path))
        self.assertEqual(manifest["percent"], 40)
        self.assertEqual(manifest["added_key"], "40")
        self.assertEqual(manifest["added_limit"], "190000")
        for saved_path, data in claude_before.items():
            with open(saved_path, "rb") as f:
                self.assertEqual(f.read(), data)
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(path), unrelated)
        self.assertEqual(json.loads(get(hooks_path)), hooks)
        self.assertTrue(os.path.exists(os.path.join(self.codex, "models_cache.json")))
        self.assertFalse(os.path.exists(os.path.dirname(manifest_path)))
        self.assertFalse(os.path.exists(os.path.join(self.codex, "skills", install.SKILL)))
        claude_after = {}
        for directory, _, filenames in os.walk(self.claude):
            for filename in filenames:
                saved_path = os.path.join(directory, filename)
                with open(saved_path, "rb") as f:
                    claude_after[saved_path] = f.read()
        self.assertEqual(claude_after, claude_before)

    def test_existing_post_turn_key_is_kept(self):
        put(os.path.join(self.codex, "config.toml"), "model_post_turn_compact_threshold_percent = 75\n")
        self.run_install("install", "--codex")
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")),
                         "model_post_turn_compact_threshold_percent = 75\n")


    def test_quoted_existing_key_is_kept(self):
        text = '"model_post_turn_compact_threshold_percent" = 75\n'
        put(os.path.join(self.codex, "config.toml"), text)
        self.run_install("install", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), text)


    def test_existing_token_limit_is_respected(self):
        text = "model_auto_compact_token_limit = 320000\n"
        put(os.path.join(self.codex, "config.toml"), text)
        out = self.run_install("install", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), text)
        self.assertIn("320000", out)

    def test_threshold_as_only_entry_is_removed(self):
        put(os.path.join(self.codex, "config.toml"), "")
        self.run_install("install", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), "model_post_turn_compact_threshold_percent = 40\n")
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), "")

    def test_chosen_percent_is_kept_on_reinstall(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("install", "--codex", "--codex-compact-percent", "50")
        self.run_install("install", "--codex")
        text = get(os.path.join(self.codex, "config.toml"))
        self.assertEqual(install.toml_get_top(text, "model_post_turn_compact_threshold_percent"), "50")
        self.assertEqual(install.toml_get_top(text, "model_auto_compact_token_limit"), "237500")

    def test_install_and_uninstall_make_no_backup(self):
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        put(os.path.join(self.codex, "hooks.json"), '{}')
        self.run_install("install", "--codex")
        self.run_install("uninstall", "--codex")
        self.assertEqual([f for f in os.listdir(self.codex) if ".bak" in f], [])

    def test_missing_config_is_not_created_when_nothing_is_added(self):
        self.run_install("install", "--codex", "--no-threshold")
        self.assertFalse(os.path.exists(os.path.join(self.codex, "config.toml")))

    def test_reinstall_changes_own_value(self):
        self.run_install("install", "--codex")
        self.run_install("install", "--codex", "--compact-percent", "55")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), "model_post_turn_compact_threshold_percent = 55\n")

    def cache(self, slug="m", window=500000, pct=95, **extra):
        put(os.path.join(self.codex, "models_cache.json"), json.dumps(
            {"models": [dict({"slug": slug, "context_window": window, "effective_context_window_percent": pct}, **extra)]}))

    def test_token_limit_from_model_window(self):
        self.cache()
        toml = 'model = "m"\n\n[features]\nhooks = true\n'
        put(os.path.join(self.codex, "config.toml"), toml)
        out = self.run_install("install", "--codex")
        text = get(os.path.join(self.codex, "config.toml"))
        self.assertLess(text.index("model_auto_compact_token_limit = 190000"), text.index("[features]"))
        self.assertIn("475000", out)
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), toml)

    def test_token_limit_uses_window_override(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\nmodel_context_window = 300000\n')
        self.run_install("install", "--codex")
        self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                              "model_auto_compact_token_limit"), "114000")

    def test_window_override_is_capped_at_model_maximum(self):
        self.cache(max_context_window=872000)
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\nmodel_context_window = 1000000\n')
        out = self.run_install("install", "--codex")
        self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                              "model_auto_compact_token_limit"), "331360")
        self.assertIn("828400", out)

    def test_model_default_window_is_not_capped(self):
        self.cache(window=900000, max_context_window=872000)
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("install", "--codex")
        self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                              "model_auto_compact_token_limit"), "342000")

    def test_maximum_is_used_when_model_has_no_default_window(self):
        self.cache(window=None, max_context_window=872000)
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("install", "--codex")
        self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                              "model_auto_compact_token_limit"), "331360")

    def test_unknown_model_gets_no_token_limit(self):
        self.cache(slug="other")
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        out = self.run_install("install", "--codex")
        self.assertIsNone(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                               "model_auto_compact_token_limit"))
        self.assertIn("unknown", out)

    def test_reinstall_changes_own_token_limit(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("install", "--codex")
        self.run_install("install", "--codex", "--compact-percent", "50")
        text = get(os.path.join(self.codex, "config.toml"))
        self.assertEqual(install.toml_get_top(text, "model_auto_compact_token_limit"), "237500")
        self.assertEqual(install.toml_get_top(text, "model_post_turn_compact_threshold_percent"), "50")
        self.run_install("uninstall", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), 'model = "m"\n')

    def test_hex_window_override_without_tomllib(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\nmodel_context_window = 0x493e0\n')
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.run_install("install", "--codex")
            self.assertEqual(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                                  "model_auto_compact_token_limit"), "114000")

    def test_unreadable_window_override_gets_no_token_limit(self):
        self.cache()
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\nmodel_context_window = "big"\n')
        self.run_install("install", "--codex")
        self.assertIsNone(install.toml_get_top(get(os.path.join(self.codex, "config.toml")),
                                               "model_auto_compact_token_limit"))

    def test_escaped_quoted_key_is_not_duplicated_without_tomllib(self):
        self.cache()
        text = 'model = "m"\n"\\u006dodel_auto_compact_token_limit" = 320000\n'
        put(os.path.join(self.codex, "config.toml"), text)
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.run_install("install", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), text)

    def test_broken_model_cache_is_ignored(self):
        put(os.path.join(self.codex, "models_cache.json"), "{not json")
        put(os.path.join(self.codex, "config.toml"), 'model = "m"\n')
        self.run_install("install", "--codex")
        self.assertIn("codex: installed", self.run_install("status", "--codex"))

    def test_existing_post_turn_key_blocks_token_limit(self):
        self.cache()
        text = 'model = "m"\nmodel_post_turn_compact_threshold_percent = 75\n'
        put(os.path.join(self.codex, "config.toml"), text)
        self.run_install("install", "--codex")
        self.assertEqual(get(os.path.join(self.codex, "config.toml")), text)


class Toml(unittest.TestCase):
    def test_set_top_prepends(self):
        self.assertEqual(install.toml_set_top("a = 1\n", "k", "2"), "k = 2\na = 1\n")

    def test_get_top_ignores_tables(self):
        self.assertIsNone(install.toml_get_top("[x]\nk = 1\n", "k"))

    def test_fallback_scanner_without_tomllib(self):
        text = ('notify = ["sh", "-c", "echo ["]\nq = \'it\'\'s\'\ns = "a \\" [ b"\n'
                'model_post_turn_compact_threshold_percent = 75 # mine\n[t]\nx = 1\n')
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(install.toml_get_top(text, "model_post_turn_compact_threshold_percent"), "75 # mine")
            self.assertIsNone(install.toml_get_top(text, "x"))
            self.assertIsNone(install.toml_set_top(text, "notify", "1"))

    def test_multiline_string_ending_in_quotes(self):
        text = 'notify = ["sh", "-c", """echo "hi""""]\nk = 5\n[t]\nk = 1\n'
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(install.toml_get_top(text, "k"), "5")

    def test_brackets_inside_strings_and_arrays(self):
        text = 'a = """\n[not a table]\nk = 9\n"""\nb = [\n  [1, 2],\n]\nk = 5\n[t]\nk = 1\n'
        self.assertEqual(install.toml_get_top(text, "k"), "5")
        self.assertEqual(install.toml_del_top(text, "k", "5"), text.replace("k = 5\n", ""))


if __name__ == "__main__":
    unittest.main()
