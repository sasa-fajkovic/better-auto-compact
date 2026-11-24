import hashlib
import json
import os
import re
import subprocess
import sys
import time

HARNESS = sys.argv[1] if len(sys.argv) > 1 else "claude"
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_ROOT = os.path.dirname(BASE)
NOISE = re.compile(r"<(hook_prompt|system-reminder|task-notification|local-command-[\w-]+|bash-std\w+|environment_context|"
                   r"user_instructions|user_shell_command|turn_aborted|subagent_notification)\b")
REPEAT = " Then end by briefly repeating your last answer to the user, since this note interrupted it."
HOOK_TEXTS = ("# AGENTS.md instructions", "Context is at", "Context was just compacted", "The handoff at ",
              "This session has a handoff hook installed")
TOML_KEY = re.compile(r"""\s*(?:"([^"\\]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*=\s*(.*?)\s*$""")
SKILL = os.path.normpath(os.path.join(BASE, "..", "skills", "compact-handoff", "SKILL.md"))
STATE_TTL = 14 * 86400
SUBAGENT_RECENT = 120
try:
    import fcntl
except ImportError:
    fcntl = None
DEFAULTS = {
    "soft_percent": 50,
    "hard_ratio": 0.75,
    "band_percent": 2,
    "min_band_tokens": 6000,
    "max_bands": 3,
    "max_stop_blocks": 2,
    "max_handoff_bytes": 8192,
    "handoff_dir": ".agent-handoff",
    "floor_message_chars": 700,
    "floor_total_chars": 20000,
    "soft_tokens": None,
    "claude_window_tokens": None,
}


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(read_json(os.path.join(BASE, "config.json")))
    return cfg


def read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run(cmd, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def tail_lines(path, nbytes=8_000_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def parse(line):
    try:
        data = json.loads(line)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def auto_pre_tokens(o):
    meta = o.get("compactMetadata") or {}
    pre = meta.get("preTokens")
    return int(pre) if meta.get("trigger") == "auto" and isinstance(pre, int) and pre > 0 else None


def claude_usage(path):
    fill, model, boundary, observed = 0, "", "", None
    for line in tail_lines(path):
        if '"compact_boundary"' in line:
            o = parse(line)
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary" and not o.get("isSidechain"):
                boundary = o.get("timestamp") or boundary
                observed = {"tokens": auto_pre_tokens(o), "model": model} if auto_pre_tokens(o) else observed
                fill = 0
                continue
        if '"assistant"' not in line or '"usage"' not in line:
            continue
        o = parse(line)
        if o.get("type") != "assistant" or o.get("isSidechain") or (o.get("timestamp") or "") < boundary:
            continue
        msg = o.get("message") or {}
        if msg.get("model") == "<synthetic>":
            continue
        u = msg.get("usage") or {}
        fill = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens", "output_tokens"))
        model = msg.get("model") or model
    return fill, model, boundary, observed


def new_boundary(path, offset):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(offset if offset <= size else 0)
            data = f.read()
    except OSError:
        return None, None, offset
    start = offset if offset <= size else 0
    end = data.rfind(b"\n") + 1
    latest, observed, model = None, None, ""
    for line in data[:end].decode("utf-8", "replace").splitlines():
        if '"compact_boundary"' in line:
            o = parse(line)
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary" and not o.get("isSidechain"):
                latest = o.get("timestamp") or "seen"
                observed = {"tokens": auto_pre_tokens(o), "model": model} if auto_pre_tokens(o) else observed
        elif '"assistant"' in line and '"model"' in line:
            o = parse(line)
            if o.get("type") == "assistant" and not o.get("isSidechain") and \
                    (o.get("message") or {}).get("model") != "<synthetic>":
                model = (o.get("message") or {}).get("model") or model
    return latest, observed, start + end


def session_start_dir(path):
    try:
        with open(path, errors="replace") as f:
            for _, line in zip(range(200), f):
                o = parse(line)
                cwd = o.get("cwd") or ((o.get("payload") or {}).get("cwd") if o.get("type") == "session_meta" else None)
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def update_learned(state_dir, change):
    path = os.path.join(state_dir, "learned-claude.json")
    with open(path + ".lock", "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        learned = read_json(path)
        if change(learned):
            try:
                tmp = "%s.%d.tmp" % (path, os.getpid())
                with open(tmp, "w") as f:
                    json.dump(learned, f)
                os.replace(tmp, path)
            except OSError:
                pass
        return learned


def learned_hard(state_dir, model, state, expected):
    obs = state.get("observed")
    if not isinstance(obs, dict):
        state["observed"] = obs = {}
    if obs.get("publish"):
        obs.pop("publish")
        if obs.get("model") != model:
            state["observed"] = obs = {}
        else:
            obs["expected"] = expected

            def publish(learned):
                learned[model] = {"tokens": obs["tokens"], "expected": expected}
                return True
            update_learned(state_dir, publish)
    entry = obs if obs.get("model") == model else update_learned(state_dir, lambda learned: False).get(model)
    if isinstance(entry, dict) and entry.get("expected") == expected and isinstance(entry.get("tokens"), int):
        return entry["tokens"]
    return None


def forget_learned(state_dir, model, tokens):
    def forget(learned):
        if (learned.get(model) or {}).get("tokens") == tokens:
            del learned[model]
            return True
        return False
    update_learned(state_dir, forget)


def codex_usage(path):
    for line in reversed(tail_lines(path)):
        if '"compacted"' in line and parse(line).get("type") == "compacted":
            return 0, 0
        if '"token_count"' not in line:
            continue
        info = (parse(line).get("payload") or {}).get("info") or {}
        last = info.get("last_token_usage") or {}
        if last.get("total_tokens"):
            return int(last["total_tokens"]), int(info.get("model_context_window") or 0)
    return 0, 0


def claude_settings(root):
    home = os.environ.get("CLAUDE_CONFIG_DIR") or CONFIG_ROOT
    return [read_json(path) for path in (os.path.join(home, "settings.json"), os.path.join(root, ".claude", "settings.json"),
                                         os.path.join(root, ".claude", "settings.local.json"))]


def window_tokens(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    return int(value) if isinstance(value, str) and value.isdigit() and int(value) > 0 else None


def claude_window(model, settings, cfg):
    if cfg.get("claude_window_tokens"):
        return int(cfg["claude_window_tokens"])
    native = 1_000_000 if re.search(r"claude-[a-z]+-5", model) else 200_000
    env = ""
    for s in settings:
        env = str((s.get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or "") or env
    env = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or env
    if env.isdigit():
        return min(int(env), native)
    window = None
    for s in settings:
        window = window_tokens(s.get("autoCompactWindow")) or window
        per_model = ((s.get("modelSettings") or {}).get(model) or {}).get("autoCompactWindow")
        window = window_tokens(per_model) or window
    return min(window, native) if window else native


def claude_hard(window, settings):
    effective = window - 20000
    pct = ""
    for s in settings:
        pct = str((s.get("env") or {}).get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or "") or pct
    pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or pct
    if re.fullmatch(r"\d+(\.\d+)?", pct) and 0 < float(pct) <= 100:
        return min(int(effective * float(pct) / 100), effective - 13000)
    return effective - 13000


def toml_skip(line, mode, depth):
    i = 0
    while i < len(line):
        c = line[i]
        if mode in ('"""', "'''"):
            if mode == '"""' and c == "\\":
                i += 2
                continue
            if line.startswith(mode, i):
                n = 3
                while n < 5 and i + n < len(line) and line[i + n] == mode[0]:
                    n += 1
                mode, i = None, i + n
                continue
        elif mode == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                mode = None
        elif mode == "'":
            if c == "'":
                mode = None
        elif line.startswith(('"""', "'''"), i):
            mode, i = line[i:i + 3], i + 3
            continue
        elif c in "\"'":
            mode = c
        elif c == "#":
            break
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth = max(0, depth - 1)
        i += 1
    return (None if mode in ('"', "'") else mode), depth


def toml_scan(text):
    entries, table_at, mode, depth = {}, None, None, 0
    for i, line in enumerate(text.splitlines()):
        if mode or depth:
            mode, depth = toml_skip(line, mode, depth)
            continue
        if line.strip().startswith("["):
            table_at = i
            break
        m = TOML_KEY.match(line)
        if m:
            entries[m.group(1) or m.group(2) or m.group(3)] = (i, m.group(4))
            mode, depth = toml_skip(m.group(4), None, 0)
        else:
            mode, depth = toml_skip(line, None, 0)
    return entries, table_at


def toml_top(text):
    try:
        import tomllib
    except ImportError:
        return {k: v[1] for k, v in toml_scan(text)[0].items()}
    try:
        return {k: v for k, v in tomllib.loads(text).items() if not isinstance(v, dict)}
    except ValueError:
        return {}


def toml_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\+?(0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|\d[\d_]*)\s*(#.*)?", str(value).strip())
    if not m:
        return None
    digits = m.group(1).replace("_", "")
    return int(digits, 0 if digits[:2] in ("0x", "0o", "0b") else 10)


def codex_hard(window):
    home = os.environ.get("CODEX_HOME") or CONFIG_ROOT
    limits = [int(window * 0.9)]
    try:
        with open(os.path.join(home, "config.toml")) as f:
            top = toml_top(f.read())
    except OSError:
        top = {}
    limit = toml_int(top.get("model_auto_compact_token_limit"))
    pct = toml_int(top.get("model_post_turn_compact_threshold_percent"))
    if limit:
        limits.append(limit)
    if pct and 0 < pct <= 100:
        limits.append(int(window * pct / 100))
    return min(limits)


def clean_message(text):
    if text.startswith("<command-"):
        name = re.search(r"<command-name>\s*(.*?)\s*</command-name>", text, re.S)
        args = re.search(r"<command-args>\s*(.*?)\s*</command-args>", text, re.S)
        return "%s %s" % (name.group(1), args.group(1)) if name and args and args.group(1) else ""
    if NOISE.match(text) or text.startswith(HOOK_TEXTS):
        return ""
    return text


def user_messages(path, per, total):
    out = []
    try:
        lines = open(path, errors="replace")
    except OSError:
        return out
    with lines:
        for line in lines:
            if '"user"' not in line:
                continue
            o = parse(line)
            text = ""
            if HARNESS == "claude":
                if o.get("type") != "user" or o.get("isMeta") or o.get("isCompactSummary") or o.get("isSidechain"):
                    continue
                c = (o.get("message") or {}).get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    text = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            else:
                pl = o.get("payload") or {}
                if o.get("type") != "response_item" or pl.get("type") != "message" or pl.get("role") != "user":
                    continue
                text = " ".join(x.get("text", "") for x in pl.get("content") or [] if isinstance(x, dict))
            text = clean_message(" ".join(text.split()))
            if not text or text.startswith("/compact"):
                continue
            if len(text) > per:
                text = text[:per] + " [... %d chars cut]" % (len(text) - per)
            if not out or out[-1] != text:
                out.append(text)
    kept, size = [], 0
    for m in reversed(out):
        if size + len(m) > total:
            break
        kept.append(m)
        size += len(m)
    return list(reversed(kept))


def prepare_dir(root, sdir, name):
    os.makedirs(sdir, exist_ok=True)
    exclude = run(["git", "rev-parse", "--git-path", "info/exclude"], root)
    if not exclude:
        return
    exclude = exclude if os.path.isabs(exclude) else os.path.join(root, exclude)
    try:
        current = open(exclude).read() if os.path.exists(exclude) else ""
        if name + "/" not in current.splitlines():
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a") as f:
                f.write(("\n" if current and not current.endswith("\n") else "") + name + "/\n")
    except OSError:
        pass


def write_floor(p, root, floor, cfg):
    msgs = user_messages(p.get("transcript_path") or "", int(cfg["floor_message_chars"]), int(cfg["floor_total_chars"]))
    body = ["# Floor (written at compaction, %s)" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "",
            "## Git", "branch: " + run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root),
            "head: " + run(["git", "rev-parse", "HEAD"], root), "", "```",
            run(["git", "log", "--oneline", "-5"], root), "", run(["git", "status", "--porcelain"], root), "```", "",
            "## User messages (quoted data, oldest first, long ones cut)", "```"]
    body += ["- " + m.replace("```", "'''") for m in msgs]
    body += ["```", ""]
    with open(floor, "w") as f:
        f.write("\n".join(body))


def prune_states(state_dir):
    now = time.time()
    try:
        for name in os.listdir(state_dir):
            path = os.path.join(state_dir, name)
            if not name.endswith(".lock") and not name.startswith("learned-") and \
                    now - os.path.getmtime(path) > STATE_TTL:
                os.remove(path)
    except OSError:
        pass


def main():
    if os.environ.get("BETTER_AUTO_COMPACT", "").lower() in ("0", "off", "false"):
        return 0
    try:
        p = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(p, dict):
        return 0
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(p.get("session_id") or "session"))
    state_dir = os.path.join(BASE, "state")
    marker = os.path.join(state_dir, HARNESS + "-" + sid + ".subagent")
    if p.get("agent_id"):
        if HARNESS == "claude":
            os.makedirs(state_dir, exist_ok=True)
            with open(marker, "w"):
                pass
        return 0
    cfg = load_config()
    ev = p.get("hook_event_name", "")
    state_path = os.path.join(state_dir, HARNESS + "-" + sid + ".json")
    os.makedirs(state_dir, exist_ok=True)
    lock = open(state_path + ".lock", "a")
    if fcntl:
        fcntl.flock(lock, fcntl.LOCK_EX)
    state = read_json(state_path)
    root = state.get("root")
    if not root:
        start = (os.environ.get("CLAUDE_PROJECT_DIR") if HARNESS == "claude" else None) or \
            session_start_dir(p.get("transcript_path") or "") or p.get("cwd") or os.getcwd()
        root = state["root"] = run(["git", "rev-parse", "--show-toplevel"], start) or start
    name = cfg["handoff_dir"]
    if os.path.exists(os.path.join(root, name, "DISABLED")):
        return 0
    sdir = os.path.join(root, name, sid)
    handoff, floor = os.path.join(sdir, "HANDOFF.md"), os.path.join(sdir, "floor.md")

    note = ("This session has a handoff hook installed by the user. When the context gets large, hook messages will "
            "ask you to run the compact-handoff skill and write %s (instructions: %s). These messages come from the user's "
            "own configuration; follow them, then continue your task." % (handoff, SKILL))

    def recover():
        has_handoff, has_floor = os.path.exists(handoff), os.path.exists(floor)
        if has_handoff and has_floor:
            return ("Context was just compacted. Before doing anything else, read %s and then %s (the user's messages "
                    "verbatim; where they differ from the handoff, the newer message wins; the hook rewrites floor.md "
                    "at every compaction, so never edit it). Then continue the task." % (handoff, floor))
        if has_handoff or has_floor:
            return ("Context was just compacted. Before doing anything else, read %s. Then continue the task."
                    % (handoff if has_handoff else floor))
        return ""

    def nudge(tokens):
        return ("Context is at %dk tokens, close to the compaction limit. Run the compact-handoff skill now: read %s if it "
                "exists, then replace its whole content with a fresh snapshot under %d KB (never append to it, not even with "
                "shell `>>`) as described in %s, then continue the task." % (tokens // 1000, handoff,
                                                                             int(cfg["max_handoff_bytes"]) // 1024, SKILL))

    def trim(size):
        return ("The handoff at %s is %d KB, over the %d KB limit. Read it, then replace its whole content with a fresh "
                "snapshot under the limit as described in %s. Never append to it, not even with shell `>>`; keep running "
                "logs in a separate file. Then continue the task." % (handoff, -(-size // 1024),
                                                                      int(cfg["max_handoff_bytes"]) // 1024, SKILL))

    def ctx(text):
        return json.dumps({"hookSpecificOutput": {"hookEventName": ev, "additionalContext": text}})

    try:
        with open(handoff, "rb") as f:
            current = f.read()
    except OSError:
        current = None

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def fresh():
        armed = state.get("armed_at")
        if armed is None:
            return True
        if current is None or len(current) < 400 or os.path.getmtime(handoff) < armed:
            return False
        base = state.get("armed_len")
        return not (base and len(current) >= base and digest(current[:base]) == state.get("armed_hash"))

    out, fill, hard = None, 0, 0
    tp = p.get("transcript_path") or ""
    if HARNESS == "claude":
        fill, model, tail_boundary, tail_observed = claude_usage(tp)
        if "scan" not in state:
            try:
                state["scan"] = os.path.getsize(tp)
            except OSError:
                state["scan"] = 0
            state["last_boundary"] = tail_boundary
            if tail_observed:
                state["observed"] = dict(tail_observed, model=tail_observed["model"] or state.get("model"), publish=True)
        else:
            latest, observed, state["scan"] = new_boundary(tp, state["scan"])
            if latest and latest > (state.get("last_boundary") or ""):
                state.update(last_boundary=latest, pending_recovery=True, armed_at=None, band=0, nudged=False,
                             stop_blocks=0)
                if observed:
                    state["observed"] = dict(observed, model=observed["model"] or state.get("model"), publish=True)
    if ev in ("PostToolUse", "Stop", "UserPromptSubmit", "PreCompact"):
        if HARNESS == "codex":
            fill, window = codex_usage(tp)
            window = window or state.get("window") or 400_000
            hard = codex_hard(window)
        else:
            model = model or state.get("model") or ""
            state["model"] = model
            settings = claude_settings(root)
            window = claude_window(model, settings, cfg)
            hard = claude_hard(window, settings)
            seen = learned_hard(state_dir, model, state, hard)
            if seen and seen < hard * 0.9 and fill > seen * 1.05:
                forget_learned(state_dir, model, seen)
                state["observed"], seen = None, None
            if seen and seen < hard * 0.9:
                hard = seen
        state["window"] = window
        if state.get("band_hard") != hard:
            state.update(band=0, band_hard=hard)
        soft = int(cfg["soft_tokens"] or min(window * cfg["soft_percent"] / 100, hard * cfg["hard_ratio"]))
        band = max(int(cfg["min_band_tokens"]), int(window * cfg["band_percent"] / 100))
        level = state.get("band", 0)
        if fill >= soft + band * level and level < int(cfg["max_bands"]):
            level = min(int(cfg["max_bands"]), max(level + 1, (fill - soft) // band + 1))
            if "seen_len" in state:
                base = state.get("seen_len"), state.get("seen_hash")
            else:
                base = (None, None) if current is None else (len(current), digest(current))
            state.update(band=level, armed_at=time.time() - 1, nudged=False, stop_blocks=0,
                         armed_len=base[0], armed_hash=base[1])
            prepare_dir(root, sdir, name)

    subagent_busy = HARNESS == "claude" and os.path.exists(marker) and \
        time.time() - os.path.getmtime(marker) < SUBAGENT_RECENT
    if ev == "SessionStart":
        prune_states(state_dir)
        if (HARNESS == "codex" and p.get("source") == "compact") or \
                (state.get("pending_recovery") and (HARNESS == "codex" or p.get("source") != "compact")):
            out = ctx("\n\n".join(x for x in (note, recover()) if x))
            state.update(pending_recovery=False, recovered=state.get("compactions", 0), noted=True)
        elif not state.get("noted") and p.get("source") != "compact":
            out = ctx(note)
            state["noted"] = True
    elif ev in ("UserPromptSubmit", "PostToolUse") and state.get("pending_recovery"):
        text = recover()
        out = ctx(text) if text else None
        state.update(pending_recovery=False, recovered=state.get("compactions", 0))
    elif ev == "Stop" and state.get("pending_recovery") and not p.get("stop_hook_active"):
        text = recover()
        out = json.dumps({"decision": "block", "reason": text + REPEAT}) if text else None
        state.update(pending_recovery=False, recovered=state.get("compactions", 0))
    elif ev in ("UserPromptSubmit", "PostToolUse"):
        big = current is not None and len(current) > int(cfg["max_handoff_bytes"])
        if not fresh() and not state.get("nudged"):
            out = ctx(nudge(fill))
            state["nudged"] = True
        elif big and not state.get("oversize"):
            out = ctx(trim(len(current)))
        state["oversize"] = big
    elif ev == "Stop":
        if not fresh() and state.get("nudged") and not p.get("stop_hook_active") and \
                state.get("stop_blocks", 0) < int(cfg["max_stop_blocks"]):
            out = json.dumps({"decision": "block", "reason": nudge(fill) + REPEAT})
            state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    elif ev == "PreCompact":
        prepare_dir(root, sdir, name)
        write_floor(p, root, floor, cfg)
        if HARNESS == "codex":
            state["compactions"] = state.get("compactions", 0) + 1
        elif not (p.get("trigger") == "auto" and subagent_busy and fill < 0.6 * hard):
            out = ("Summarizer note: this session keeps its handoff at %s and a floor of the user's messages at %s. "
                   "Name both paths in the summary." % (handoff, floor))
    elif ev == "PostCompact" and HARNESS == "codex":
        state.update(pending_recovery=state.get("recovered", -1) != state.get("compactions", 0), armed_at=None, band=0,
                     nudged=False, stop_blocks=0)

    state.update(seen_len=None if current is None else len(current),
                 seen_hash=None if current is None else digest(current))
    try:
        tmp = "%s.%d.tmp" % (state_path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)
    except OSError:
        pass
    if out:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
