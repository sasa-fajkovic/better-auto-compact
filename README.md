# better-auto-compact

Keep the details your coding agent learned when its context gets compacted. Works with **Claude Code** and **Codex CLI**.

Built-in compaction keeps what you told the agent, but it drops details that only appeared in tool output: build ids, URLs, error codes, measured numbers, the value that replaced an older one. This adds one hook and one skill around the harness's normal compaction:

```
work ──► context reaches 75% of the compaction point
          │ hook: "run the compact-handoff skill now"
          ▼
        agent writes .agent-handoff/<session>/HANDOFF.md
        (exact values, including tool output and old -> new)
          │
          ▼
        harness compacts as usual (installer defaults: Claude ~30%, Codex 40% of the window)
          │ hook saves floor.md: your messages + git state
          ▼
        hook: "read HANDOFF.md and floor.md first" ──► work continues
```

Compaction itself is never blocked or replaced.

## Results

Same scripted sessions with and without the setup, three or more automatic compactions each. "Hidden facts" exist only in tool output and are asked for at the end.

| | Claude Code (4 runs/arm) | Codex CLI (10 runs/arm) |
|---|---|---|
| Built-in compaction, hidden facts kept (of 5) | 2.5 | 1.3 |
| **This setup**, hidden facts kept (of 5) | **5.0 in every run** | **5.0 in every run** |
| Cost vs built-in | about the same ($1.84 vs $1.83 per run) | +29% tokens |

Facts you state yourself and task behaviour were equal in both. Details, method and caveats: [docs/experiments.md](docs/experiments.md).

## Install

Requires Python 3.8+ on macOS or Linux.

```
git clone https://github.com/sasa-fajkovic/better-auto-compact.git
cd better-auto-compact
python3 install.py --dry-run
python3 install.py
```

It installs for every harness it finds. Use `--claude` or `--codex` to pick one, `--claude-compact-percent N` and `--codex-compact-percent N` to choose each client's compaction point, and `--no-threshold` to leave the compaction thresholds alone.

**Codex:** Codex only runs hooks you have approved. Start `codex` after installing and accept the hook review, or open `/hooks`.

## What it changes

Fresh installs default to 30% for Claude Code and 40% for Codex. Each client retains its own saved percentage on reinstall, including values from older installs; one client never inherits the other client's saved percentage. To adopt 40% for an existing Codex install, run `python3 install.py --codex --codex-compact-percent 40`; saved custom values are otherwise preserved, and user-managed thresholds still take precedence.

| | Claude Code (`~/.claude`) | Codex CLI (`~/.codex`) |
|---|---|---|
| Hook script | `better-auto-compact/handoff_hook.py` | `better-auto-compact/handoff_hook.py` |
| Skill | `skills/compact-handoff/SKILL.md` | `skills/compact-handoff/SKILL.md` |
| Hooks | added to `settings.json` | added to `hooks.json` |
| Threshold | `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = 30`, unless you already set it or a compaction window. The compaction window is left to you (`/autocompact` or `autoCompactWindow`); a reinstall removes the `CLAUDE_CODE_AUTO_COMPACT_WINDOW = 1000000` older installs added | `model_post_turn_compact_threshold_percent = 40` and `model_auto_compact_token_limit` at 40% of your `model`'s window in `config.toml`, unless you already set either |

Codex's 40% default is a provisional shared choice for long sessions: in a separate 90-trial confirmation, 40% was the only numeric threshold with exact results in all 9 of 9 trials on both GPT-6.1 Sol and Terra (built-in compaction: 4 of 9 each). Sol also passed 9 of 9 at 30% with lower observed elapsed time and token use; Terra passed 8 of 9 at each of 20%, 30% and 50%. This does not establish a global optimum or statistically proven superiority: all 16 persistent-workspace coding runs passed, and built-in compaction was faster in all 8 matched pairs. Token volume does not establish billing cost. These unpublished follow-up Codex measurements used CLI 0.167.1, high reasoning effort, hooks and root sessions, with a 500,000-token raw window at 95% usability (475,000 usable tokens). Their raw artifacts remain private and are not included in this repository, so they are not publicly replayable evidence.

The historical basis for 30% was an orchestrator delegating 150 tasks: it got the same results (148 to 150 of 150) at 30%, 50%, 70% and the default, and 30% cost 37% less in Claude Code and 61% fewer tokens in Codex ([details](docs/experiments.md#where-to-compact)). That was measured on a 1M window (Claude Code) and a 475k window (Codex), and does not establish a universal Codex recommendation. A rerun with the skill working kept the same accuracy and saved 32% and 62%. With the window set to 1M (the tested Codex catalog capped it at 828k usable), compacting at about 400k instead showed no accuracy gain; it cost 2% less in Claude Code and used 56% more tokens in Codex ([details](docs/experiments.md#a-larger-window-and-a-later-compaction-point)). On a 200k window, 30% compacts at about 54k tokens; pick a higher `--claude-compact-percent` if that is too early. Codex gets both settings because the percentage applies only when a turn ends; the token limit also compacts inside one long turn. The window comes from Codex's model cache (capped at the model's maximum, as Codex does when you set `model_context_window`), so start codex once before installing, and rerun the installer after changing `model`.

To change managed thresholds independently, rerun `python3 install.py install --claude-compact-percent 30 --codex-compact-percent 40`. Each client resolves its percentage from its explicit option, then an explicit shared `--compact-percent N` (also available as `--hard-percent N`), then its own saved value, then its default. For example, `--compact-percent 50 --codex-compact-percent 40` sets Claude to 50% and Codex to 40%. Later installs retain each choice. Selecting only `--codex` or `--claude` leaves the other client's files unchanged. Existing thresholds that the installer does not own remain respected, even with explicit percentage options. If either Codex threshold is user-managed, both thresholds stay unchanged; the requested percentage is saved for future installs but does not establish the active compaction point. Defaults live in `DEFAULT_CLAUDE_PERCENT` and `DEFAULT_CODEX_PERCENT` in `install.py`.

`python3 install.py status` shows where Claude Code compacts for each model. It warns when the percentage is set together with a compaction window smaller than the model's full window: the percentage then applies to that window, so a 500k window compacts at about 144k.

It edits files in place without making backups; a rerun that changes nothing leaves them untouched. Your other settings and hooks are left as they are. Existing compaction limits are respected: the soft threshold is never above 75% of the point where the harness compacts.

In each project, the hook writes `.agent-handoff/<session>/` in the directory the session started in (its git root, from `CLAUDE_PROJECT_DIR` or the first directory in the transcript), even when the agent later `cd`s elsewhere, and adds `.agent-handoff/` to `.git/info/exclude`, so nothing gets committed.

## Configure

Optional `config.json` next to the installed hook script:

| Key | Default | Meaning |
|---|---|---|
| `soft_percent` | `50` | Nudge when the context reaches this percent of the window |
| `hard_ratio` | `0.75` | Never nudge later than this fraction of the harness's compaction point |
| `soft_tokens` | none | Fixed nudge point in tokens, overrides both of the above |
| `band_percent` | `2` | After the first nudge, ask for a refresh every this many percent of the window (up to 2 times); appending to the handoff does not count as a refresh |
| `max_handoff_bytes` | `8192` | Ask for a rewrite when `HANDOFF.md` grows past this size, once each time it does |
| `max_stop_blocks` | `2` | How often the agent may be held at the end of a turn to write the handoff after a nudge it did not act on |
| `claude_window_tokens` | detected | Claude Code window size, if detection is wrong for your model |

Turn it off for one session with `BETTER_AUTO_COMPACT=off`, or for a project by creating `.agent-handoff/DISABLED`.

## Uninstall

```
python3 install.py uninstall
```

This removes the hooks, the skill and the threshold and window values it added.

## Limitations

- Subagents are not asked to write handoffs, and their own compactions are left alone. Claude Code reports a subagent's compaction like the main session's, so the hook tells them apart by recent subagent activity and the main session's fill.
- In Claude Code's plan mode the edit tools can only change the plan file, so writing `HANDOFF.md` that way is refused; in tests the agent either used a shell command or went on without it, and plans were not worse ([details](docs/experiments.md#planning-past-the-compaction-point)).
- A handoff cannot hold every detail whose relevance shows up only later. In the planning test such values were missing from the handoff, and the task forbade searching, so two Codex runs lost most of them. In the 24 reruns that allowed searching, every run found all 32 again by searching the files ([details](docs/experiments.md#values-that-matter-only-after-a-compaction)).
- The window size and compaction point are worked out from the model name and your settings. Claude Code's `/autocompact` saves the window as `modelSettings.<model>.autoCompactWindow` in `settings.json`, where `auto` means the model's full window, and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` overrides it. When an automatic compaction comes earlier than expected, the hook uses that point for that model (kept in `state/learned-claude.json`) until your settings change or the context grows past it without a compaction, so only the first compaction after such a change goes without a nudge. To avoid that first miss, set `soft_tokens` or `claude_window_tokens`.
- The earlier experiments behind the historical 30% token-volume results used Claude Sonnet 5.5 and GPT-6.1 Terra. In those experiments, the hidden-fact test used small windows; the fill and threshold tests used the full 1M and 475k windows. The separate follow-up Codex study described above measured GPT-6.1 Sol and Terra at 475k usable tokens. These results do not validate other models or smaller full windows; an optimal Claude threshold has not been benchmarked.

## License

MIT
