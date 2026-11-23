# Experiments

Measured on 2026-11-05 with Claude Code 2.1.318 (Claude Sonnet 5.5) and Codex CLI 0.166.0 (GPT-6.1 Terra), all sessions headless.

## Method

Each run is one scripted multi-turn coding session in a throwaway repository:

1. The user states some facts and rules.
2. Tools print five "hidden" facts the user never repeats: the shipped build id, a superseded first build id, a benchmark p95, a docs preview URL and an error code.
3. Long filler reads force three or more automatic compactions, at least one after the last fact.
4. The final turn asks for every fact without running tools. Answers are scored by exact match.

Windows were made small so compaction happens often: Claude Code compacted at about 67k tokens, Codex at 45k. Hooks blocked reads of the session transcript in every arm, so the agent could not look answers up there.

| Arm | What it is |
|---|---|
| A | Built-in compaction, nothing added |
| B | A manual ritual before each compaction: "write a handoff file, update the tracker", then `/compact` with focus text |
| C | This project: threshold nudge, compact-handoff skill, floor at compaction, pointer after compaction |
| F | Floor and pointer only, no nudge or skill |

## Results

Hidden facts kept, mean of 5:

| | A | B | C | F |
|---|---|---|---|---|
| Claude Code (4 runs/arm) | 2.5 | 4.2 | **5.0** | 3.5 |
| Codex CLI (10 runs/arm) | 1.3 | 3.7 | **5.0** | 1.4 |

C kept all five facts in every run of both harnesses. Facts the user stated and task behaviour were equal in all arms.

Cost per run:

| | A | B | C | F |
|---|---|---|---|---|
| Claude Code, USD | 1.83 | 2.40 | 1.84 | 1.72 |
| Codex, total tokens | 2.25M | 2.69M | 2.91M | 2.29M |

## Caveats

- Small windows and two models. This test was not repeated on full windows.
- The win comes from the combination. A skill that asks for exact tool-output values and `old -> new` pairs matters most. The floor and pointer alone (F) help little.
- B was partly disadvantaged in Claude Code. The transcript guard blocked four of its handoff writes, and B compacted more often (6.75 vs about 4.3 per run).

## Clearing instead of compacting

Arm K is C with one change: at the compaction point the session is not compacted. The driver gives the hook a compaction event, so it saves the floor, and then starts a new headless session (a new `claude -p` session or `codex exec` thread, the equivalent of `/clear` in Claude Code and `/new` in Codex). The hook points the new session at `HANDOFF.md` and `floor.md`, and that is all it starts with. Same scripted session and seeds as C, so each K run is paired with one C run.

| | Hidden facts kept, of 5 | Cost per run |
|---|---|---|
| Claude Code, C (4 runs) | 5.0 | $1.84 |
| Claude Code, K (4 runs) | 5.0 | $1.76 |
| Codex, C (10 runs) | 5.0 | 2.91M tokens |
| Codex, K (10 runs) | 5.0 | 3.24M tokens |

K tied C in all 14 pairs, also on user-stated facts and behaviour. Cost was about the same in Claude Code (4% less on average, but two of four pairs cost more) and 11% higher in Codex tokens (nine of ten pairs).

Compaction stays the default: it happens on its own, while hooks cannot start `/clear` or `/new`. Neither harness fires compaction events on `/clear` or `/new`, so the installed hook writes no floor and gives no pointer there yet.

## Orchestrator with 80 subagent tasks

One main agent delegates 80 tasks to subagents, five per turn. Each subagent runs one command whose ~1.3k-token log ends with a result id. Ten tasks fail once and must be retried exactly once. The main agent compacts (or clears) at 60k tokens: 7 to 10 times per run in Claude Code, 2 to 3 in Codex. At the end it writes all 80 result ids and the 10 retry codes from memory.

| | Result ids right, of 80 | Retry codes right, of 10 | Claude Code cost per run |
|---|---|---|---|
| Claude Code, built-in compaction (2 runs) | 79, 80 | 9, 10 | $2.89 |
| Claude Code, C (2 runs) | 80, 79 | 10, 9 | $3.75 |
| Claude Code, handoff then clear (2 runs) | 80, 80 | 10, 10 | $3.16 |
| Claude Code, clear without a fresh handoff (1 run) | 54 | 10 | $2.88 |
| Codex, all four setups (1 to 2 runs each) | 79 to 80 | 10 | 15.0M to 16.6M tokens |

- Built-in compaction kept almost everything here, unlike the single-agent test above. The orchestrator repeats each result in its own replies, and summaries keep what the agent said.
- Clearing loses everything since the last handoff write: all 26 missing ids in the stale-clear run came from the batch before each clear. Writing the handoff right before clearing fixed that.
- C cost 30% more than built-in compaction in Claude Code, for no gain on this task.
- The skill's text reached the agent in only one run here. The transcript guard matched the skill's install path, so reads of `SKILL.md` by full path were blocked, and all 14 Claude Code Skill tool calls failed with "Unknown skill". Only Codex C run 1 got it, with a `cat` by relative path that the guard missed. The other C, clear and handoff-then-clear runs measure the nudge, floor and pointer without the skill.

## Subagent that overflows

One subagent reads 16 files (~96k tokens) and reports one value from each. Two runs per row.

| | Values right, of 16 | Subagent compactions | Claude Code cost per run |
|---|---|---|---|
| Claude Code, window large enough | 16, 16 | 0 | $0.19 |
| Claude Code, compaction at 60k | 16, 16 | 3 | $0.10 |
| Claude Code, compaction at 60k, hook before the fix below | 16, 16 | 3 | $0.14 |
| Claude Code, compaction at 60k, hook with the fix | 16, 16 | 3 | $0.11 |
| Codex, all three setups | 16, 16 | 0 | |

- Built-in compaction was enough for the subagent, and cheaper than a full window.
- Codex extracted the values with a script instead of reading the files into its context, so it never needed to compact.
- Claude Code reports a subagent's compaction as if it were the main session's. The hook used to point the subagent at the main handoff, which cost about $0.04 per run. It now gives the pointer only when the main transcript gains a compaction record, which a subagent's compaction never adds. The end-to-end check below passed with this change.

## Quality vs context fill

No compaction. A session reads logs until it reaches each fill point (Claude Code reads files; Codex gets them pasted into the prompt, because it shortened or truncated file reads), then a copy of the session answers: a value from the first message, a value from the first log, a value from the latest log, their sum, every log's value in order, and how many are above 3000. It also writes a parser checked by 16 hidden tests, and must keep two rules from the first message. One run per harness; an earlier Claude Code run without the last two questions was also all right.

| | Fill points | All answers right | Hidden tests |
|---|---|---|---|
| Claude Code, 1M window | 119k, 217k, 311k, 503k, 690k, 878k | at every point | 16/16 at every point |
| Codex, 475k window | 52k, 96k, 141k, 239k, 337k, 408k | at every point | 16/16 at every point |

No drop up to 88% of the window on these tasks. Harder work, such as long multi-file edits or conflicting instructions, was not tested. A full context mainly costs more. In Claude Code the same short check read 1.76M cached tokens near 880k against 0.24M near 120k, and cost $0.21 against $0.06 (the check's own cost, after subtracting the session it was copied from).

## Where to compact

The orchestrator test at the real windows (1M in Claude Code, 475k in Codex): 150 tasks in batches of 5, 1 in 8 fails once, one subagent per task (Claude Haiku 5.5 in Claude Code), the hook installed. The harness compacts at 30%, 50% or 70% of the window, or at its default. With the default, context reached 932k in Claude Code and 451k in Codex. Two runs per row.

| | Result ids right, of 150 | Retry codes right, of 18 | Compactions | Cost per run |
|---|---|---|---|---|
| Claude Code, 30% (~294k) | 150, 149 | 18, 17 | 3, 3 | $7.80 |
| Claude Code, 50% (~490k) | 150, 150 | 18, 18 | 2, 1 | $9.27 |
| Claude Code, 70% (~686k) | 148, 150 | 16, 18 | 1, 1 | $9.95 |
| Claude Code, default | 149, 149 | 17, 18 | 0, 0 | $12.46 |
| Codex, 30% (~143k) | 150, 150 | 18, 18 | 4, 4 | 65M tokens |
| Codex, 50% (~238k) | 150, 150 | 18, 18 | 2, 2 | 98M tokens |
| Codex, 70% (~333k) | 150, 150 | 18, 18 | 1, 1 | 124M tokens |
| Codex, default | 149, 150 | 18, 18 | 1, 1 | 170M tokens |

Accuracy did not change with the compaction point. Cost fell with every step down: at 30%, 37% less in Claude Code and 61% fewer tokens in Codex than the default, because every call re-reads less context. Codex was given the limit as `model_auto_compact_token_limit`, so it also compacted mid-turn. The installer's default is 30% from this result, and for Codex it sets that same limit (142,500 for this model) next to the end-of-turn percentage.

The skill's text reached the agent in only one of these 16 runs. The transcript guard blocked reads of `SKILL.md` by full path, and all 5 Claude Code Skill tool calls failed with "Unknown skill", so the agent wrote its handoff from the hook's nudge alone. The exception is Codex 50% run 1, which read the skill with a `cat` by relative path that the guard missed; it matched its twin (98.7M vs 97.6M tokens, every result right). So the comparison between compaction points holds. The rerun below has the skill working.

### With the skill

The 30% and default runs again, with the same tasks and seeds, but the agent can load the skill: the guard lets `SKILL.md` through, and Claude Code finds it in the project. The skill's text reached the agent in all 8 runs; none of the 8 matching runs above had it. Claude Code loaded it with the Skill tool 8 and 5 times at 30%, once per run at the default.

| | Result ids right, of 150 | Retry codes right, of 18 | Compactions | Cost per run | Without the skill |
|---|---|---|---|---|---|
| Claude Code, 30% | 150, 148 | 18, 18 | 3, 3 | $8.21 | $7.80 |
| Claude Code, default | 150, 150 | 18, 18 | 0, 0 | $12.12 | $12.46 |
| Codex, 30% | 150, 150 | 18, 18 | 4, 4 | 64M tokens | 65M tokens |
| Codex, default | 150, 150 | 18, 18 | 1, 1 | 166M tokens | 170M tokens |

With the skill, 30% still cost 32% less in Claude Code and used 62% fewer tokens in Codex than the default, with the same accuracy. The skill added 5% at 30% in Claude Code; the other differences are within the spread between runs. On this task the results were already near complete without it, so the skill showed no accuracy gain here.

## Planning past the compaction point

One agent plans a fictional migration from a pile of project documents (service notes, decision records, postmortems, runbooks, meeting notes). It is told to read every document in full, without subagents, and then write the plan with a fixed `plan.json`. The facts the plan needs are scattered through the documents in prose, next to look-alike values for other projects. The plan is scored by exact match:

- Easier: 40 values: workstreams, settings (6 of them changed by a later document), ordering rules, incidents to mitigate, sign-offs and freeze dates. Every one names the project.
- Harder: the same 40, plus the limits, timeouts, required headers and owners of 24 services. Only the last 3 documents say which 8 of them the project calls. That is 32 more values whose relevance only shows up at the end, and 3 checks that the right services are listed: 75 in all.

Claude Code read 80 documents; reading all of them fills 955k tokens, just under its 1M window. Codex read 48 (about 350k). The 30% runs compacted during the reading, so most values were read before a compaction: 64 to 65 of 75 in Claude Code, 51 to 72 in Codex. One run per row.

| Claude Code | Easier, of 40 | Harder, of 75 | Compactions | Cost, easier / harder |
|---|---|---|---|---|
| No early compaction | 40 | 75 | 0, 0 | $4.87 / $5.55 |
| 30%, built-in only | 40 | 75 | 3, 3 | $4.92 / $5.18 |
| 30%, with the hook | 40 | 75 | 3, 3 | $5.52 / $6.25 |
| 30%, plan mode, with the hook | 40 | 75 | 3, 3 | $5.12 / $5.70 |

| Codex | Easier, of 40 | Harder, of 75 | Compactions | Tokens, easier / harder |
|---|---|---|---|---|
| No early compaction | 40 | 75 | 0, 0 | 10.0M / 10.2M |
| 30%, built-in only | 40 | 74 | 2, 5 | 3.9M / 8.4M |
| 30%, with the hook | 40 | 73 | 2, 2 | 5.2M / 5.0M |
| 30%, read-only, with the hook | 40 | 75 | 2, 2 | 4.0M / 4.2M |

- Compacting during a long planning read lost almost nothing. Claude Code kept all 75 values through 3 compactions, with or without the hook. Codex missed 3 values in 2 runs: the required headers of 3 services that were only named as dependencies at the end, left blank in the plan.
- Cost: in Claude Code one long turn costs about the same with or without early compaction. Every document is written to the cache once either way (about 1M tokens), and the extra cache reads without compaction (14.7M against 4.6M in the harder runs) are cheap. The hook added 12 to 21%. In Codex 30% used about half the tokens. The Codex run that compacted 5 times re-read every document and used 8.4M.
- Without early compaction, Claude Code reached 955k tokens. A slightly longer read would have hit its own compaction point (about 967k) or the window.
- Codex does not finish a long reading turn on its own. It read 3 to 6 documents in its first turn and then declined to plan from a partial read. The runs above add "continue" turns until a plan exists (2 to 6 turns in all).
- Claude Code's plan mode refuses edits outside the plan file, so writing `HANDOFF.md` with the edit tools failed (3 times in 2 runs). In one run the agent wrote it with a shell command at the first compaction and, after a later refusal, put the handoff into the plan file; in the other it carried on without it. In Codex's read-only sandbox no handoff was written. Plan quality was the same in both.

## A larger window and a later compaction point

With `model_context_window = 1000000`, Codex caps the window at the model's maximum (872k for GPT-6.1 Terra) and uses 95% of it: 828,400 tokens. On that window and on Claude Code's 1M window, the 30% default was compared with compacting at about 400k: 41% in Claude Code, and `model_auto_compact_token_limit = 400000` in Codex. The Codex planning runs also set the end-of-turn percentage (30% or 48%); the orchestrator runs set only the token limit.

The orchestrator test from "Where to compact", with the same tasks and seeds and the skill working. The Claude Code 30% row is the one from "With the skill". Two runs per row.

| | Result ids right, of 150 | Retry codes right, of 18 | Compactions | Cost per run |
|---|---|---|---|---|
| Claude Code, 30% (~294k) | 150, 148 | 18, 18 | 3, 3 | $8.21 |
| Claude Code, ~400k | 149, 148 | 18, 16 | 2, 2 | $8.08 |
| Codex, 30% (~249k) | 150, 150 | 18, 18 | 2, 2 | 96M tokens |
| Codex, 400k | 150, 149 | 18, 18 | 1, 1 | 150M tokens |

The harder planning task from the section above, with 80 documents in both harnesses (about 600k tokens to read in Codex). Two seeds, one run each.

| | Values right, of 75 | Compactions | Cost per run |
|---|---|---|---|
| Claude Code, 30% | 75, 75 | 3, 3 | $6.25, $5.98 |
| Claude Code, ~400k | 75, 75 | 2, 2 | $5.92, $5.53 |
| Codex, 30% | 43, 74 | 2, 4 | 14.5M, 27.1M tokens |
| Codex, 400k | 75, 46 | 2, 1 | 54.2M, 20.1M tokens |

- Compacting later showed no consistent accuracy gain. In Claude Code it cost 2% less on the orchestrator and 6% less on planning. In Codex it used 56% more tokens on the orchestrator.
- The Codex planning results went opposite ways on the two seeds and did not follow the compaction point. The 43 and 46 runs listed the right 8 services but left 32 and 29 of their 32 values blank; the 74 run had one stale value. The next section explains why.

## Values that matter only after a compaction

In those planning runs the service values were read before a compaction, while nothing said they would matter, so the handoff did not keep them. Only the last 3 documents name the services the project calls. The task forbade grep. Re-reading whole documents was still allowed, and the 75/75 Codex run re-read all 80. The agent did read `HANDOFF.md` and `floor.md` after every compaction.

The rerun changes the task to read every document in full and in order first, and then search and re-read as needed. It also compares three variants meant to help recovery: retrieval guidance in the pointer, that guidance plus broader Facts, and that guidance plus a source index. Codex, 80 documents, 828k window, compaction at 30%. Four new seeds, plus one seed run twice more: six runs per row. The order was randomized within each seed.

| | Runs with at least 30 of 32 service values | Other values, of 43 | Tokens per run |
|---|---|---|---|
| Current skill and pointer | 6 (32 in every run) | 43 in every run | 14.2M |
| Pointer says how to look up missing details | 6 (32 in every run) | 43 in every run | 13.9M |
| That pointer, and Facts keep values read from files | 6 (32 in every run) | 43 in every run | 14.0M |
| That pointer, and the handoff keeps an index of where each detail is stated | 6 (32 in every run) | 43 in every run | 14.1M |

- Every run compacted twice, at 242k to 249k. After the last 3 documents named the services, each run found their values with 3 to 6 searches of the documents, and re-read no document other than the overview. Every service value reached the model again after the last compaction.
- None of the three variants made a difference, so none was adopted. At the last compaction the index named the right document for 96 of the 192 service values the model had read, and covered at least 6 of the 8 services fully in none of the 6 runs. Handoffs stayed under 8,000 bytes in every run (3.9 to 6.8 KB).
- A handoff cannot keep every detail whose relevance shows up only later. The agent recovers those details by searching the files again. A task that forbids searching can lose them.

## End-to-end check

The installer was run into fresh config dirs, then a live session ran in each harness until it compacted. In both harnesses, the nudge fired and the agent wrote `HANDOFF.md`. The floor was saved and the pointer was injected after compaction. The final answer recalled the branch, the build id and the docs URL exactly.
