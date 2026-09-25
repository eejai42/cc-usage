# cc-usage

Track and visualize how many tokens you're spending with Claude Code — per branch, per session, per hour, and per commit.

Claude Code stores session transcripts as JSONL files under `~/.claude/projects/`. `cc-usage` reads those files and gives you a clear picture of your token consumption and its API-equivalent cost.

## Features

- **Per-branch usage** — see how many tokens each git branch has cost
- **Per-model cost** — Fable, Opus, Sonnet, and Haiku are priced differently, and rates differ *within* a family by generation; costs are calculated per model tier
- **Verbose breakdown** — hourly buckets and per-session detail via `-v`
- **HTML reports** — dark-themed dashboard with stacked bar charts and cumulative token graphs
- **Pre-commit hook** — snapshot token usage with every commit, building a timeline over the life of a project
- **Reset** — archive a project's transcripts so `cc-usage` reports $0 here, without losing tokens from any global tally
- **JSON output** — pipe into CI or other tools

## Installation

Requires Python 3.8+ and `git`.

```bash
git clone https://github.com/eejai42/cc-usage.git
cd cc-usage
./install.sh
```

This copies `cc_usage.py` to `~/.local/bin/` and creates a `cc-usage` wrapper there.

Make sure `~/.local/bin` is on your PATH (add to `~/.zshrc` or `~/.bashrc` if needed):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Usage

Run `cc-usage` from any directory that has Claude Code sessions:

```bash
cc-usage              # summary by branch
cc-usage -v           # + hourly, per-session, per-model breakdowns (also writes verbose HTML)
cc-usage --json       # machine-readable JSON
```

### Pre-commit Hook

Snapshot token usage automatically on every commit:

```bash
# One-time setup per repo
cc-usage --install-hook
```

Or manually add to `.git/hooks/pre-commit`:

```bash
#!/bin/bash
cc-usage --pre-commit
exit 0   # never block the commit
```

Each commit writes a JSON snapshot and regenerates an HTML report under `.effortless/claude-token-usage/`:

```
.effortless/
└── claude-token-usage/
    ├── 2026-05-01_10-30-00_abc1234.json
    ├── 2026-05-02_14-15-00_def5678.json
    └── claude-token-usage.html
```

### Verbose HTML Report

```bash
cc-usage --verbose-report
# writes .effortless/claude-token-usage/verbose-cc-usage-report.html
```

Includes hourly charts, per-session table (sortable, filterable), per-model breakdown, and branch summary.

### Reset a Project Counter

If a project has accumulated a large token count and you want to start fresh:

```bash
cc-usage --reset           # archive transcripts; this project now reports $0
cc-usage --reset --dry-run # preview without moving anything
```

This renames the JSONL transcripts into an archived sibling directory under `~/.claude/projects/`. The tokens remain on disk — any global scan still finds them — but `cc-usage` inside this working directory will show $0. Claude Code memory and todos are left in place.

## Token Types and Pricing

Rates are Anthropic first-party API list prices per 1M tokens, verified against the
Anthropic model catalog on **2026-09-25**.

| Model tier | Input | Output | Cache Read | Cache Create |
|---|---|---|---|---|
| **Fable 5.1** (`claude-fable-5-1`, `claude-mythos-5-1`) | $10 | $50 | $0.25 | $12.50 |
| **Fable 5** (`claude-fable-5`, `claude-mythos-5`) | $10 | $50 | $1.00 | $12.50 |
| **Opus 5 / 4.8 / 4.7 / 4.6 / 4.5** | $5 | $25 | $0.50 | $6.25 |
| **Opus 4.1 / 4.0** (legacy) | $15 | $75 | $1.50 | $18.75 |
| **Sonnet 5** | $2 | $10 | $0.20 | $2.50 |
| **Sonnet 4.6 / 4.5 / 4** | $3 | $15 | $0.30 | $3.75 |
| **Haiku 4.5** | $1 | $5 | $0.10 | $1.25 |
| **Haiku 3.5 / 3** (retired) | $0.80 | $4 | $0.08 | $1.00 |

| Type | What it is |
|------|-----------|
| Input | Prompt tokens sent to the model |
| Output | Tokens the model generates |
| Cache Read | Tokens served from the prompt cache — 0.1x input (0.025x on Fable 5.1) |
| Cache Creation | Tokens written into the prompt cache — 1.25x input (5-minute TTL) |

**Pricing is keyed on a model *tier*, not a bare family name.** A substring match on
`opus` is no longer sufficient: Opus 4.1 was $15/$75 while Opus 4.5 and later are
$5/$25, and Sonnet 5 ($2/$10) is priced *below* the Sonnet 4.x it replaces. The
`[1m]` long-context suffix (`claude-opus-5[1m]`) is stripped before matching —
1M-context models bill at the standard rates, with no long-context premium.

> **Note:** Claude Code subscription users (Pro $20/mo, Max $100/mo) pay flat monthly fees rather than per-token rates. The costs shown by `cc-usage` are the *API-equivalent value* — useful for understanding consumption, not for reconciling a bill.

## How It Works

Claude Code writes a JSONL file per conversation session to:

```
~/.claude/projects/<mangled-cwd-path>/<session-id>.jsonl
```

The path mangling turns `/Users/alice/my-project` into `-Users-alice-my-project` (slashes, underscores, and dots all become hyphens).

`cc-usage` finds the right folder for the current working directory, parses every assistant message for token usage data, groups events by git branch (from the `gitBranch` field embedded in each message), and aggregates across sessions.

## JSON Output

```bash
cc-usage --json
```

```json
{
  "project": "/path/to/project",
  "branches": {
    "main": {
      "turns": 42,
      "tokens": 980000,
      "cost_usd": 8.75,
      "cost_per_turn": 0.2083
    }
  },
  "total": {
    "turns": 42,
    "tokens": {
      "input": 300000,
      "output": 80000,
      "cache_read": 550000,
      "cache_creation": 50000,
      "total": 980000
    },
    "cost_by_model": { "opus": 7.20, "sonnet": 1.55, "fable-5-1": 0.00 },
    "total_cost_usd": 8.75,
    "cost_per_turn": 0.2083
  }
}
```

## License

MIT
