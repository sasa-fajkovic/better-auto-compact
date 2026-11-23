import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "hooks"))
import handoff_hook

NAME = "better-auto-compact"
SKILL = "compact-handoff"
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "PostCompact"]
MARK = NAME + "/handoff_hook.py"
CLAUDE_PCT = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
CLAUDE_WINDOW = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
CODEX_KEY = "model_post_turn_compact_threshold_percent"
LIMIT_KEY = "model_auto_compact_token_limit"
DEFAULT_CLAUDE_PERCENT = 30
DEFAULT_CODEX_PERCENT = 40


class Plan:
    def __init__(self, dry):
        self.dry = dry
        self.steps = []

    def say(self, text):
        self.steps.append(text)
        print(("[dry-run] " if self.dry else "") + text)


def read_text(path):
    with open(path) as f:
        return f.read()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    text = read_text(path)
    if not text.strip():
        return default
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit("%s is not a JSON object; fix it first" % path)
    return data


def write_text(path, text, plan):
    if plan.dry:
        return
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
    tmp = path + ".tmp-" + NAME
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path, data, plan):
    write_text(path, json.dumps(data, indent=2) + "\n", plan)


def save_text(path, text, plan):
    if os.path.exists(path) and read_text(path) == text:
        return
    write_text(path, text, plan)


def save_json(path, data, plan):
    if os.path.exists(path):
        try:
            if json.loads(read_text(path)) == data:
                return
        except ValueError:
            pass
    write_json(path, data, plan)


def copy_files(base, skill_dir, plan):
    plan.say("copy hook -> %s" % os.path.join(base, "handoff_hook.py"))
    plan.say("copy skill -> %s" % os.path.join(skill_dir, "SKILL.md"))
    if plan.dry:
        return
    os.makedirs(base, exist_ok=True)
    shutil.copy2(os.path.join(REPO, "hooks", "handoff_hook.py"), os.path.join(base, "handoff_hook.py"))
    os.makedirs(skill_dir, exist_ok=True)
    shutil.copy2(os.path.join(REPO, "skills", SKILL, "SKILL.md"), os.path.join(skill_dir, "SKILL.md"))


def ours_hook(h):
    return isinstance(h, dict) and MARK in str(h.get("command", ""))


def ours(group):
    return any(ours_hook(h) for h in (group or {}).get("hooks") or [])


def add_hooks(data, command):
    hooks = data.setdefault("hooks", {})
    added = []
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        if not any(ours(g) for g in groups):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 30}]})
            added.append(ev)
    return added


def remove_hooks(data):
    hooks = data.get("hooks") or {}
    removed = []
    for ev in list(hooks):
        keep = []
        for g in hooks[ev]:
            if not ours(g):
                keep.append(g)
                continue
            removed.append(ev)
            rest = [h for h in g.get("hooks") or [] if not ours_hook(h)]
            if rest:
                keep.append(dict(g, hooks=rest))
        if keep:
            hooks[ev] = keep
        else:
            del hooks[ev]
    if not hooks and "hooks" in data:
        del data["hooks"]
    return removed


def skill_conflict(skill_dir, base):
    path = os.path.join(skill_dir, "SKILL.md")
    manifest = load_json(os.path.join(base, "manifest.json"), {})
    return os.path.exists(path) and not manifest and read_text(path) != read_text(
        os.path.join(REPO, "skills", SKILL, "SKILL.md"))


def hook_command(base, harness):
    return "python3 %s %s" % (shlex.quote(os.path.join(base, "handoff_hook.py")), harness)


def claude_window_models(settings):
    return [m for m, v in (settings.get("modelSettings") or {}).items()
            if isinstance(v, dict) and handoff_hook.window_tokens(v.get("autoCompactWindow"))]


def claude_window_set(settings, own=None):
    env = settings.get("env") or {}
    exported = os.environ.get(CLAUDE_WINDOW)
    return (bool(handoff_hook.window_tokens(settings.get("autoCompactWindow"))) or bool(claude_window_models(settings))
            or (CLAUDE_WINDOW in env and str(env[CLAUDE_WINDOW]) != own)
            or bool(exported and exported != own))


def claude_compaction_set(settings, own=None):
    return claude_window_set(settings, own) or bool(os.environ.get(CLAUDE_PCT))


def claude_learned(root):
    return handoff_hook.read_json(os.path.join(root, NAME, "state", "learned-claude.json"))


def learned_tokens(value):
    tokens = value.get("tokens") if isinstance(value, dict) else None
    return tokens if isinstance(tokens, int) else None


def claude_status(settings, learned=None):
    lines, reduced = [], False
    for label, model in [("claude 5.x", "claude-model-5")] + [(m, m) for m in claude_window_models(settings)]:
        window = handoff_hook.claude_window(model, [settings], {})
        reduced = reduced or window < handoff_hook.claude_window(model, [], {})
        lines.append("  %s: compacts at ~%dk of a %dk window"
                     % (label, handoff_hook.claude_hard(window, [settings]) // 1000, window // 1000))
    pct = os.environ.get(CLAUDE_PCT) or (settings.get("env") or {}).get(CLAUDE_PCT)
    if pct and reduced:
        lines.append("  warning: %s=%s also applies to your compaction window, so it compacts at %s%% of that window, "
                     "not of the full one; remove one of the two" % (CLAUDE_PCT, pct, pct))
    for model, value in sorted((learned or {}).items()):
        if learned_tokens(value):
            lines.append("  %s: last automatic compaction at ~%dk (seen by the hook)"
                         % (model, learned_tokens(value) // 1000))
    lines.append("  note: %s overrides a window set with /autocompact" % CLAUDE_WINDOW)
    return lines


def saved_percent(manifest, harness):
    value = manifest.get("percent") or manifest.get("added_env" if harness == "claude" else "added_key")
    return int(value) if value else None


def compact_percent(args, manifest, harness):
    specific = getattr(args, harness + "_compact_percent")
    if specific is not None:
        return specific
    if args.compact_percent is not None:
        return args.compact_percent
    saved = saved_percent(manifest, harness)
    if saved is not None:
        return saved
    return DEFAULT_CLAUDE_PERCENT if harness == "claude" else DEFAULT_CODEX_PERCENT


def install_claude(root, args, plan):
    base, skill_dir = os.path.join(root, NAME), os.path.join(root, "skills", SKILL)
    if skill_conflict(skill_dir, base) and not args.force:
        raise SystemExit("%s already exists and is not ours; rerun with --force to replace it" % skill_dir)
    settings_path = os.path.join(root, "settings.json")
    settings = load_json(settings_path, {})
    manifest = load_json(os.path.join(base, "manifest.json"), {})
    copy_files(base, skill_dir, plan)
    added = add_hooks(settings, hook_command(base, "claude"))
    plan.say("settings.json: add hooks %s" % (", ".join(added) or "(already present)"))
    env = settings.setdefault("env", {})
    pct = manifest["percent"] = compact_percent(args, manifest, "claude")
    if args.no_threshold:
        plan.say("settings.json: threshold untouched (--no-threshold)")
    elif CLAUDE_PCT in env and str(env[CLAUDE_PCT]) != manifest.get("added_env"):
        plan.say("settings.json: keeping your %s=%s" % (CLAUDE_PCT, env[CLAUDE_PCT]))
    elif CLAUDE_PCT not in env and claude_compaction_set(settings, manifest.get("added_window")):
        plan.say("settings.json: keeping your compaction settings; no %s added" % CLAUDE_PCT)
    else:
        env[CLAUDE_PCT] = str(pct)
        manifest["added_env"] = str(pct)
        plan.say("settings.json: env %s=%s (auto-compaction at ~%s%% of the window)" % (CLAUDE_PCT, pct, pct))
    own_window = manifest.pop("added_window", None)
    if own_window is not None and str(env.get(CLAUDE_WINDOW)) == own_window:
        env.pop(CLAUDE_WINDOW)
        plan.say("settings.json: remove env %s added by an older install" % CLAUDE_WINDOW)
    if not env:
        del settings["env"]
    save_json(settings_path, settings, plan)
    manifest.update(harness="claude", settings=settings_path, skill=skill_dir, installed_at=int(time.time()))
    write_json(os.path.join(base, "manifest.json"), manifest, plan)


def uninstall_claude(root, plan):
    base, skill_dir = os.path.join(root, NAME), os.path.join(root, "skills", SKILL)
    manifest = load_json(os.path.join(base, "manifest.json"), {})
    settings_path = os.path.join(root, "settings.json")
    settings = load_json(settings_path, {})
    removed = remove_hooks(settings)
    plan.say("settings.json: remove hooks %s" % (", ".join(removed) or "(none found)"))
    env = settings.get("env") or {}
    if manifest.get("added_env") is not None and str(env.get(CLAUDE_PCT)) == manifest["added_env"]:
        env.pop(CLAUDE_PCT, None)
        plan.say("settings.json: remove env %s" % CLAUDE_PCT)
    if manifest.get("added_window") is not None and str(env.get(CLAUDE_WINDOW)) == manifest["added_window"]:
        env.pop(CLAUDE_WINDOW, None)
        plan.say("settings.json: remove env %s" % CLAUDE_WINDOW)
    if "env" in settings and not env:
        settings.pop("env", None)
    if os.path.exists(settings_path):
        save_json(settings_path, settings, plan)
    remove_tree(skill_dir, plan, manifest or os.path.exists(base))
    remove_tree(base, plan, True)


def toml_checked(old, new, key, value):
    try:
        import tomllib
    except ImportError:
        if re.search(r'^\s*"[^"\n]*\\', old, re.M):
            return None
        before, after = handoff_hook.toml_scan(old)[0], handoff_hook.toml_scan(new)[0]
        before.pop(key, None)
        got = after.pop(key, None)
        same = {k: v[1] for k, v in before.items()} == {k: v[1] for k, v in after.items()}
        return new if same and (got[1] if got else None) == value else None
    try:
        before, after = tomllib.loads(old), tomllib.loads(new)
    except ValueError:
        return None
    before.pop(key, None)
    got = after.pop(key, None)
    return new if after == before and got == (None if value is None else int(value)) else None


def toml_set_top(text, key, value):
    return toml_checked(text, "%s = %s\n" % (key, value) + text, key, value)


def toml_get_top(text, key):
    value = handoff_hook.toml_top(text).get(key)
    return None if value is None else str(value)


def toml_del_top(text, key, value):
    entry = handoff_hook.toml_scan(text)[0].get(key)
    if not entry or str(handoff_hook.toml_int(entry[1])) != str(value):
        return text
    lines = text.splitlines(True)
    del lines[entry[0]]
    return toml_checked(text, "".join(lines), key, None)


def codex_window(root, text):
    top = handoff_hook.toml_top(text)
    m = re.match(r"""\s*(["']?)([^"'#\s]+)\1""", str(top.get("model") or ""))
    model = m.group(2) if m else None
    try:
        cache = load_json(os.path.join(root, "models_cache.json"), {})
    except (ValueError, OSError, SystemExit):
        cache = {}
    models = cache.get("models")
    entry = next((e for e in models or [] if isinstance(e, dict) and model and e.get("slug") == model), None)
    if not entry:
        return model, None
    cap = handoff_hook.toml_int(entry.get("max_context_window"))
    cap = cap if cap and cap > 0 else None
    if "model_context_window" in top:
        window = handoff_hook.toml_int(top["model_context_window"])
        if window and cap:
            window = min(window, cap)
    else:
        window = handoff_hook.toml_int(entry.get("context_window")) or cap
    pct = handoff_hook.toml_int(entry.get("effective_context_window_percent"))
    return model, (window * pct // 100 if window and pct else None)


def codex_set_own(text, key, wanted, manifest, own, plan, why):
    current = toml_get_top(text, key)
    if current == wanted:
        plan.say("config.toml: %s = %s already set" % (key, wanted))
        return text
    base = text if current is None else toml_del_top(text, key, current)
    new = None if base is None or (current is not None and base == text) else toml_set_top(base, key, wanted)
    if new is None:
        plan.say("config.toml: could not edit it safely; add `%s = %s` at the top yourself" % (key, wanted))
        return text
    manifest[own] = wanted
    plan.say("config.toml: %s = %s (%s)" % (key, wanted, why))
    return new


def install_codex(root, args, plan):
    base, skill_dir = os.path.join(root, NAME), os.path.join(root, "skills", SKILL)
    if skill_conflict(skill_dir, base) and not args.force:
        raise SystemExit("%s already exists and is not ours; rerun with --force to replace it" % skill_dir)
    hooks_path = os.path.join(root, "hooks.json")
    hooks = load_json(hooks_path, {})
    manifest = load_json(os.path.join(base, "manifest.json"), {})
    copy_files(base, skill_dir, plan)
    added = add_hooks(hooks, hook_command(base, "codex"))
    plan.say("hooks.json: add hooks %s" % (", ".join(added) or "(already present)"))
    save_json(hooks_path, hooks, plan)
    config_path = os.path.join(root, "config.toml")
    text = read_text(config_path) if os.path.exists(config_path) else ""
    found = {key: toml_get_top(text, key) for key in (CODEX_KEY, LIMIT_KEY)}
    theirs = ["%s = %s" % (key, found[key]) for key, own in ((CODEX_KEY, "added_key"), (LIMIT_KEY, "added_limit"))
              if found[key] is not None and found[key] != manifest.get(own)]
    pct = manifest["percent"] = compact_percent(args, manifest, "codex")
    if args.no_threshold:
        plan.say("config.toml: threshold untouched (--no-threshold)")
    elif theirs:
        plan.say("config.toml: keeping your %s; no thresholds added; the hook nudges at most at 75%% of the "
                 "compaction point" % ", ".join(theirs))
    else:
        new = codex_set_own(text, CODEX_KEY, str(pct), manifest, "added_key", plan,
                            "compaction after a turn ends past %s%% of the window" % pct)
        model, window = codex_window(root, text)
        if found[LIMIT_KEY] is not None and not window:
            plan.say("config.toml: keeping %s = %s from the last install; window of model %s unknown"
                     % (LIMIT_KEY, found[LIMIT_KEY], model or "(not set)"))
        elif window:
            new = codex_set_own(new, LIMIT_KEY, str(window * pct // 100), manifest, "added_limit", plan,
                                "compaction mid-turn too, %s%% of %s's %s-token window; rerun install after changing "
                                "model" % (pct, model, window))
        else:
            plan.say("note: window of model %s unknown, so no %s added; for one long turn (an orchestrator, for "
                     "example) set it to about %s%% of the window, or set `model` and rerun after starting codex once"
                     % (model or "(not set)", LIMIT_KEY, pct))
        if new != text:
            save_text(config_path, new, plan)
    manifest.update(harness="codex", hooks=hooks_path, skill=skill_dir, installed_at=int(time.time()))
    write_json(os.path.join(base, "manifest.json"), manifest, plan)
    plan.say("Codex runs new hooks only after you approve them: start codex and accept the hook review "
             "(or open /hooks).")


def uninstall_codex(root, plan):
    base, skill_dir = os.path.join(root, NAME), os.path.join(root, "skills", SKILL)
    manifest = load_json(os.path.join(base, "manifest.json"), {})
    hooks_path = os.path.join(root, "hooks.json")
    if os.path.exists(hooks_path):
        hooks = load_json(hooks_path, {})
        removed = remove_hooks(hooks)
        plan.say("hooks.json: remove hooks %s" % (", ".join(removed) or "(none found)"))
        save_json(hooks_path, hooks, plan)
    config_path = os.path.join(root, "config.toml")
    owned = [(key, manifest[own]) for key, own in ((CODEX_KEY, "added_key"), (LIMIT_KEY, "added_limit"))
             if manifest.get(own)]
    if owned and os.path.exists(config_path):
        text = new = read_text(config_path)
        for key, value in owned:
            after = toml_del_top(new, key, value)
            if after is None:
                plan.say("config.toml: could not remove %s safely; remove it yourself" % key)
            elif after != new:
                plan.say("config.toml: remove %s" % key)
                new = after
        if new != text:
            save_text(config_path, new, plan)
    remove_tree(skill_dir, plan, manifest or os.path.exists(base))
    remove_tree(base, plan, True)


def remove_tree(path, plan, allowed):
    if allowed and os.path.isdir(path):
        plan.say("remove %s" % path)
        if not plan.dry:
            shutil.rmtree(path)


def status(claude_root, codex_root):
    for label, root, cfg in (("claude", claude_root, "settings.json"), ("codex", codex_root, "hooks.json")):
        base = os.path.join(root, NAME)
        data = load_json(os.path.join(root, cfg), {})
        present = [ev for ev, groups in (data.get("hooks") or {}).items() if any(ours(g) for g in groups)]
        print("%s: %s, hooks: %s" % (label, "installed" if os.path.exists(os.path.join(base, "manifest.json"))
                                      else "not installed", ", ".join(present) or "none"))
        if label == "claude":
            print("\n".join(claude_status(data, claude_learned(root))))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install.py", description="Handoff-before-compaction for Claude Code and Codex CLI")
    ap.add_argument("action", nargs="?", default="install", choices=["install", "uninstall", "status"])
    ap.add_argument("--claude", action="store_true", help="only Claude Code")
    ap.add_argument("--codex", action="store_true", help="only Codex CLI")
    ap.add_argument("--claude-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    ap.add_argument("--compact-percent", "--hard-percent", dest="compact_percent", type=int,
                    help="shared compaction percentage for selected clients; client-specific options take precedence")
    ap.add_argument("--claude-compact-percent", type=int,
                    help="Claude Code compaction percentage (last Claude install, otherwise %d)" % DEFAULT_CLAUDE_PERCENT)
    ap.add_argument("--codex-compact-percent", type=int,
                    help="Codex compaction percentage (last Codex install, otherwise %d)" % DEFAULT_CODEX_PERCENT)
    ap.add_argument("--no-threshold", action="store_true", help="leave the harness compaction thresholds alone")
    ap.add_argument("--force", action="store_true", help="replace an existing skill with the same name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    args.claude_dir, args.codex_home = os.path.abspath(args.claude_dir), os.path.abspath(args.codex_home)
    for option in ("compact_percent", "claude_compact_percent", "codex_compact_percent"):
        value = getattr(args, option)
        if value is not None and not 10 <= value <= 95:
            raise SystemExit("--%s must be between 10 and 95" % option.replace("_", "-"))
    targets = [t for t, flag in (("claude", args.claude), ("codex", args.codex)) if flag] or ["claude", "codex"]
    if args.action == "status":
        status(args.claude_dir, args.codex_home)
        return 0
    plan = Plan(args.dry_run)
    for t in targets:
        root = args.claude_dir if t == "claude" else args.codex_home
        if args.action == "install" and not os.path.isdir(root) and not (args.claude or args.codex):
            print("skip %s: %s not found" % (t, root))
            continue
        print("== %s (%s)" % (t, root))
        if args.action == "install":
            (install_claude if t == "claude" else install_codex)(root, args, plan)
        else:
            (uninstall_claude if t == "claude" else uninstall_codex)(root, plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
