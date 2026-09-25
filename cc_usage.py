#!/usr/bin/env python3
"""
Claude Code Usage Calculator and Pre-commit Hook

Reads session JSONL files from ~/.claude/projects/<project-path>/ and
calculates total token usage, grouped by git branch.

Features:
- Basic usage reporting (text and JSON)
- Pre-commit hook integration (saves snapshots with commit info)
- HTML report generation with visual charts
"""

import re
import json
import os
import sys
import subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any


# =============================================================================
# CORE UTILITIES
# =============================================================================

def get_project_folder() -> Path:
    """Get the Claude Code project folder path for the current directory."""
    cwd = os.getcwd()
    # Claude Code uses a mangled path format:
    # - Slashes become hyphens
    # - Underscores become hyphens
    # - Dots become hyphens (e.g. api.effortlessapi.com → api-effortlessapi-com)
    # - Starts with hyphen (from leading /)
    mangled = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    claude_projects = Path.home() / ".claude" / "projects"
    return claude_projects / mangled


def get_candidate_folders() -> List[Tuple[Path, Optional[str]]]:
    """Return (folder, cwd_filter) pairs to read sessions from.

    The primary folder (exact cwd match) needs no filter.  Parent project
    folders are included only when they exist and their mangled name is a
    prefix of ours — those sessions must be filtered to cwd == our cwd so
    we don't count work done elsewhere in the parent project.
    """
    cwd = os.getcwd()
    mangled = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    claude_projects = Path.home() / ".claude" / "projects"
    exact = claude_projects / mangled

    results: List[Tuple[Path, Optional[str]]] = []

    # Always include the exact folder (no cwd filter needed — every session
    # in this folder was started from this directory).
    if exact.exists():
        results.append((exact, None))

    # Walk up the real filesystem path and add any ancestor project folders
    # that exist in ~/.claude/projects/.  Filter their sessions to our cwd.
    p = Path(cwd).parent
    while str(p) != p.root:
        parent_mangled = str(p).replace("/", "-").replace("_", "-").replace(".", "-")
        parent_folder = claude_projects / parent_mangled
        if parent_folder.exists() and parent_folder != exact:
            results.append((parent_folder, cwd))
        p = p.parent

    return results


def new_stats() -> Dict[str, Any]:
    """Create a fresh stats dict."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "message_count": 0,
        "tool_calls": 0,
        "sessions": 0,
        "first_timestamp": None,
        "last_timestamp": None,
    }


def merge_stats(target: Dict, source: Dict) -> None:
    """Merge source stats into target."""
    target["input_tokens"] += source["input_tokens"]
    target["output_tokens"] += source["output_tokens"]
    target["cache_read_input_tokens"] += source["cache_read_input_tokens"]
    target["cache_creation_input_tokens"] += source["cache_creation_input_tokens"]
    target["message_count"] += source["message_count"]
    target["tool_calls"] += source["tool_calls"]
    target["sessions"] += source["sessions"]

    if source["first_timestamp"]:
        if target["first_timestamp"] is None or source["first_timestamp"] < target["first_timestamp"]:
            target["first_timestamp"] = source["first_timestamp"]
        if target["last_timestamp"] is None or source["last_timestamp"] > target["last_timestamp"]:
            target["last_timestamp"] = source["last_timestamp"]


def total_tokens(stats: Dict) -> int:
    """Calculate total tokens from stats."""
    return (
        stats["input_tokens"] +
        stats["output_tokens"] +
        stats["cache_read_input_tokens"] +
        stats["cache_creation_input_tokens"]
    )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
# Rates are USD per 1M tokens, Anthropic first-party API list price.
# Verified against the Anthropic model catalog on 2026-09-25.
#
# Derived rates follow the published multipliers:
#   cache_read   = 0.1x input   (except Fable 5.1, which is 0.025x -> $0.25/1M)
#   cache_create = 1.25x input  (5-minute TTL, the Claude Code default;
#                                a 1-hour TTL would be 2x)
#
# Pricing is keyed on a MODEL TIER, not a bare family name. A substring match
# on "opus" is no longer sufficient: Opus 4.1 was $15/$75 while Opus 4.5 and
# later are $5/$25, and Sonnet 5 ($2/$10) is priced below Sonnet 4.6 ($3/$15).
# All 1M-context models above are billed at these standard rates -- there is
# no long-context premium tier.
PRICING = {
    # Fable / Mythos 5.x -- the most capable tier.
    # Fable 5.1 cache reads are 0.025x input, a quarter of Fable 5's.
    "fable-5-1":  {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_create": 12.50},
    "fable":      {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_create": 12.50},

    # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5 -- all $5/$25.
    "opus":       {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_create":  6.25},
    # Opus 4.1 / 4.0 and older were priced at the legacy $15/$75.
    "opus-4-1":   {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_create": 18.75},

    # Sonnet 5 is cheaper than the 4.x Sonnets it replaces.
    "sonnet":     {"input":  2.00, "output": 10.00, "cache_read": 0.20, "cache_create":  2.50},
    "sonnet-4":   {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_create":  3.75},

    "haiku":      {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_create":  1.25},
    # Haiku 3.5 / 3 -- retired or deprecated, but may appear in old transcripts.
    "haiku-3":    {"input":  0.80, "output":  4.00, "cache_read": 0.08, "cache_create":  1.00},
}

# Human-readable label per pricing tier, for reports.
TIER_LABELS = {
    "fable-5-1": "fable 5.1",
    "fable":     "fable 5",
    "opus":      "opus 5/4.x",
    "opus-4-1":  "opus 4.1 (legacy)",
    "sonnet":    "sonnet 5",
    "sonnet-4":  "sonnet 4.x",
    "haiku":     "haiku 4.5",
    "haiku-3":   "haiku 3.x",
}

DEFAULT_MODEL_FAMILY = "opus"


def model_family(model: Optional[str]) -> str:
    """Map a model id like 'claude-opus-5[1m]' to a pricing tier.

    Claude Code writes ids such as 'claude-opus-5', 'claude-fable-5-1',
    'claude-haiku-4-5-20251001' and long-context variants suffixed '[1m]'.
    The suffix does not change the rate, so it is stripped before matching.
    """
    if not model:
        return DEFAULT_MODEL_FAMILY
    m = model.lower()
    # Drop a long-context marker ('claude-opus-5[1m]') and any date stamp.
    m = re.sub(r"\[[^\]]*\]$", "", m)

    if "fable" in m or "mythos" in m:
        # Fable/Mythos 5.1 get the cheaper 0.025x cache-read rate.
        return "fable-5-1" if re.search(r"5[-.]1", m) else "fable"
    if "opus" in m:
        # Opus 4.1 and 4.0 are the only Opus models still on the legacy rate.
        return "opus-4-1" if re.search(r"opus-4([-.][01])?(-\d{8})?$", m) else "opus"
    if "sonnet" in m:
        # Sonnet 4.x and older keep the legacy rate; Sonnet 5+ (and a bare
        # "sonnet", which always means the current model) use the new one.
        return "sonnet-4" if re.search(r"sonnet-[1-4]\b|-[1-4][-.]\d*-?sonnet", m) else "sonnet"
    if "haiku" in m:
        # Haiku 3.x wrote the version BEFORE the name ('claude-3-5-haiku-...'),
        # so match that shape as well as the modern 'haiku-4-5' ordering.
        return "haiku-3" if re.search(r"haiku-[1-3]\b|-[1-3][-.]\d*-?haiku", m) else "haiku"
    return DEFAULT_MODEL_FAMILY


def cost_breakdown(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
    family: str = DEFAULT_MODEL_FAMILY,
) -> Dict[str, float]:
    """Cost breakdown for a given token mix at a given model family's rates."""
    p = PRICING.get(family, PRICING[DEFAULT_MODEL_FAMILY])
    input_cost = input_tokens / 1_000_000 * p["input"]
    output_cost = output_tokens / 1_000_000 * p["output"]
    cache_read_cost = cache_read_tokens / 1_000_000 * p["cache_read"]
    cache_create_cost = cache_create_tokens / 1_000_000 * p["cache_create"]
    return {
        "input": input_cost,
        "output": output_cost,
        "cache_read": cache_read_cost,
        "cache_create": cache_create_cost,
        "total": input_cost + output_cost + cache_read_cost + cache_create_cost,
    }


def estimate_cost(stats: Dict) -> Dict[str, float]:
    """Estimate cost using Opus rates (legacy; per-model cost is in cost_breakdown)."""
    return cost_breakdown(
        stats["input_tokens"],
        stats["output_tokens"],
        stats["cache_read_input_tokens"],
        stats["cache_creation_input_tokens"],
        family=DEFAULT_MODEL_FAMILY,
    )


def format_number(n: int) -> str:
    """Format large numbers with commas."""
    return f"{n:,}"


def format_millions(n: int) -> str:
    """Format large numbers as millions."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# =============================================================================
# SESSION PARSING
# =============================================================================

def parse_session_file(filepath: Path, cwd_filter: Optional[str] = None) -> Tuple[str, Dict]:
    """Parse a JSONL session file and extract token usage and branch.

    If cwd_filter is given, only count turns where the message's cwd field
    starts with cwd_filter (used when reading from a parent project folder).
    Sessions with no cwd field at all are included unconditionally.
    """
    stats = new_stats()
    branch = None
    # Determine the session-level cwd from the first message that has one.
    session_cwd: Optional[str] = None

    try:
        with open(filepath, "r") as f:
            for line in f:
                try:
                    data = json.loads(line.strip())

                    if session_cwd is None and "cwd" in data:
                        session_cwd = data["cwd"]

                    # When filtering by cwd, skip this session entirely if the
                    # session-level cwd is known and doesn't match.
                    if cwd_filter and session_cwd and not session_cwd.startswith(cwd_filter):
                        return "(unknown)", new_stats()

                    # Extract branch (from user or assistant messages)
                    if branch is None and "gitBranch" in data:
                        branch = data["gitBranch"]

                    # Track timestamps
                    if "timestamp" in data:
                        ts = data["timestamp"]
                        if stats["first_timestamp"] is None:
                            stats["first_timestamp"] = ts
                        stats["last_timestamp"] = ts

                    # Count turns: only user messages with text content (not tool results)
                    if data.get("type") == "user":
                        msg = data.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, list) and len(content) > 0:
                            first_block = content[0] if isinstance(content[0], dict) else {}
                            if first_block.get("type") == "text":
                                stats["message_count"] += 1

                    # Count tokens from assistant messages
                    if data.get("type") == "assistant" and "message" in data:
                        msg = data["message"]
                        if "usage" in msg:
                            usage = msg["usage"]
                            stats["input_tokens"] += usage.get("input_tokens", 0)
                            stats["output_tokens"] += usage.get("output_tokens", 0)
                            stats["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
                            stats["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)

                        # Count tool calls
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    stats["tool_calls"] += 1

                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)

    if stats["message_count"] > 0:
        stats["sessions"] = 1

    return branch or "(unknown)", stats


def collect_all_stats() -> Tuple[Dict[str, Dict], Dict, List]:
    """Collect stats from all session files."""
    candidates = get_candidate_folders()

    if not candidates:
        return {}, new_stats(), []

    all_session_files: List[Tuple[Path, Optional[str]]] = []
    for folder, cwd_filter in candidates:
        for sf in folder.glob("*.jsonl"):
            all_session_files.append((sf, cwd_filter))

    if not all_session_files:
        return {}, new_stats(), []

    # Group by branch
    branch_stats = defaultdict(new_stats)
    session_details = []

    for sf, cwd_filter in sorted(all_session_files, key=lambda x: x[0].stat().st_mtime):
        branch, stats = parse_session_file(sf, cwd_filter)
        merge_stats(branch_stats[branch], stats)
        if stats["message_count"] > 0:
            session_details.append((sf.stem, branch, stats))

    # Calculate totals
    grand_total = new_stats()
    for branch, stats in branch_stats.items():
        merge_stats(grand_total, stats)

    return dict(branch_stats), grand_total, session_details


# =============================================================================
# DETAILED PARSING (per-message events for hourly / per-session analysis)
# =============================================================================

def parse_session_detailed(filepath: Path, cwd_filter: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse a session JSONL and return (branch, list_of_events).

    Emits one event per assistant message (so tokens are always fully accumulated),
    but sets is_new_turn=True only on the first assistant message after a user text
    prompt — not on tool-result continuations.

    If cwd_filter is given, sessions whose cwd doesn't start with it return no events.
    """
    branch: Optional[str] = None
    events: List[Dict[str, Any]] = []
    next_is_new_turn: bool = False  # set True after a user text message
    session_cwd: Optional[str] = None

    try:
        with open(filepath, "r") as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                if session_cwd is None and "cwd" in data:
                    session_cwd = data["cwd"]
                    if cwd_filter and not session_cwd.startswith(cwd_filter):
                        return "(unknown)", []

                if branch is None and "gitBranch" in data:
                    branch = data["gitBranch"]

                if data.get("type") == "user":
                    msg = data.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list) and len(content) > 0:
                        first_block = content[0] if isinstance(content[0], dict) else {}
                        next_is_new_turn = (first_block.get("type") == "text")
                    else:
                        next_is_new_turn = False
                    continue

                if data.get("type") == "assistant":
                    msg = data.get("message") or {}
                    usage = msg.get("usage") or {}
                    content = msg.get("content") or []
                    tool_calls = sum(
                        1 for item in content
                        if isinstance(item, dict) and item.get("type") == "tool_use"
                    )
                    events.append({
                        "timestamp": data.get("timestamp"),
                        "model": msg.get("model"),
                        "input_tokens": usage.get("input_tokens", 0) or 0,
                        "output_tokens": usage.get("output_tokens", 0) or 0,
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                        "tool_calls": tool_calls,
                        "is_new_turn": next_is_new_turn,
                    })
                    next_is_new_turn = False  # subsequent assistant msgs in same turn are not new turns

    except Exception as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)

    return branch or "(unknown)", events


def _empty_bucket() -> Dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "message_count": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
        "models": defaultdict(int),  # model family -> message count
    }


def _add_event(bucket: Dict[str, Any], ev: Dict[str, Any]) -> None:
    bucket["input_tokens"] += ev["input_tokens"]
    bucket["output_tokens"] += ev["output_tokens"]
    bucket["cache_read_input_tokens"] += ev["cache_read_input_tokens"]
    bucket["cache_creation_input_tokens"] += ev["cache_creation_input_tokens"]
    if ev.get("is_new_turn"):
        bucket["message_count"] += 1  # only real user text prompts count as turns
    bucket["tool_calls"] += ev["tool_calls"]
    fam = model_family(ev.get("model"))
    bucket["cost_usd"] += cost_breakdown(
        ev["input_tokens"],
        ev["output_tokens"],
        ev["cache_read_input_tokens"],
        ev["cache_creation_input_tokens"],
        family=fam,
    )["total"]
    bucket["models"][fam] += 1


def _iso_hour_key(ts: str) -> str:
    """Return a 'YYYY-MM-DD HH:00' bucket key in local time for an ISO timestamp."""
    if not ts:
        return "unknown"
    try:
        # Python 3.11+ handles 'Z'; fall back for older.
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:00")
    except Exception:
        return ts[:13].replace("T", " ") + ":00"


def collect_detailed_stats() -> Dict[str, Any]:
    """Walk every session file and return hourly + per-session + branch aggregates.

    Returns a dict:
        {
          "sessions":  [ {id, branch, first, last, duration_s, messages,
                          tool_calls, tokens{...}, cost_usd, models{...}}, ... ],
          "hours":     { "YYYY-MM-DD HH:00": bucket, ... },
          "branches":  { branch: bucket, ... },
          "models":    { family: bucket, ... },
          "total":     bucket,
          "first_ts":  iso, "last_ts": iso,
        }
    """
    candidates = get_candidate_folders()
    sessions_out: List[Dict[str, Any]] = []
    hours: Dict[str, Dict[str, Any]] = defaultdict(_empty_bucket)
    branches: Dict[str, Dict[str, Any]] = defaultdict(_empty_bucket)
    models: Dict[str, Dict[str, Any]] = defaultdict(_empty_bucket)
    total = _empty_bucket()
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None

    if not candidates:
        return {
            "sessions": [], "hours": {}, "branches": {}, "models": {},
            "total": total, "first_ts": None, "last_ts": None,
        }

    all_session_files: List[Tuple[Path, Optional[str]]] = []
    for folder, cwd_filter in candidates:
        for sf in folder.glob("*.jsonl"):
            all_session_files.append((sf, cwd_filter))

    for sf, cwd_filter in sorted(all_session_files, key=lambda x: x[0].stat().st_mtime):
        branch, events = parse_session_detailed(sf, cwd_filter)
        if not events:
            continue

        s_bucket = _empty_bucket()
        s_first: Optional[str] = None
        s_last: Optional[str] = None

        for ev in events:
            _add_event(s_bucket, ev)
            _add_event(branches[branch], ev)
            _add_event(total, ev)
            fam = model_family(ev.get("model"))
            _add_event(models[fam], ev)
            hkey = _iso_hour_key(ev.get("timestamp") or "")
            _add_event(hours[hkey], ev)

            ts = ev.get("timestamp")
            if ts:
                if s_first is None or ts < s_first:
                    s_first = ts
                if s_last is None or ts > s_last:
                    s_last = ts
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

        duration_s = 0
        if s_first and s_last:
            try:
                a = datetime.fromisoformat(s_first.replace("Z", "+00:00"))
                b = datetime.fromisoformat(s_last.replace("Z", "+00:00"))
                duration_s = int((b - a).total_seconds())
            except Exception:
                duration_s = 0

        sessions_out.append({
            "id": sf.stem,
            "branch": branch,
            "first_ts": s_first,
            "last_ts": s_last,
            "duration_s": duration_s,
            "messages": s_bucket["message_count"],
            "tool_calls": s_bucket["tool_calls"],
            "input_tokens": s_bucket["input_tokens"],
            "output_tokens": s_bucket["output_tokens"],
            "cache_read_input_tokens": s_bucket["cache_read_input_tokens"],
            "cache_creation_input_tokens": s_bucket["cache_creation_input_tokens"],
            "total_tokens": (
                s_bucket["input_tokens"] + s_bucket["output_tokens"]
                + s_bucket["cache_read_input_tokens"]
                + s_bucket["cache_creation_input_tokens"]
            ),
            "cost_usd": s_bucket["cost_usd"],
            "models": dict(s_bucket["models"]),
        })

    # Normalize defaultdicts to plain dicts before returning
    def _finalize(b: Dict[str, Any]) -> Dict[str, Any]:
        b = dict(b)
        b["models"] = dict(b["models"])
        b["total_tokens"] = (
            b["input_tokens"] + b["output_tokens"]
            + b["cache_read_input_tokens"] + b["cache_creation_input_tokens"]
        )
        return b

    return {
        "sessions": sessions_out,
        "hours": {k: _finalize(v) for k, v in hours.items()},
        "branches": {k: _finalize(v) for k, v in branches.items()},
        "models": {k: _finalize(v) for k, v in models.items()},
        "total": _finalize(total),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def _fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16].replace("T", " ")


# =============================================================================
# GIT UTILITIES
# =============================================================================

def get_git_info() -> Dict[str, Optional[str]]:
    """Get current git information."""
    info = {
        "branch": None,
        "commit_hash": None,
        "commit_hash_short": None,
        "is_git_repo": False,
    }

    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=os.getcwd()
        )
        if result.returncode != 0:
            return info

        info["is_git_repo"] = True

        # Get branch name
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=os.getcwd()
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()

        # Get commit hash (staged commit for pre-commit, or HEAD)
        # During pre-commit, we want the hash that WILL be created
        # But that doesn't exist yet, so we use a timestamp-based identifier
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=os.getcwd()
        )
        if result.returncode == 0:
            info["commit_hash"] = result.stdout.strip()
            info["commit_hash_short"] = info["commit_hash"][:7]

    except Exception:
        pass

    return info


def get_usage_log_dir() -> Path:
    """Get the directory for storing usage logs."""
    return Path(os.getcwd()) / ".effortless" / "claude-token-usage"


# =============================================================================
# PRE-COMMIT FUNCTIONALITY
# =============================================================================

def save_pre_commit_snapshot() -> Optional[Path]:
    """Save a JSON snapshot for pre-commit hook."""
    detailed = collect_detailed_stats()
    grand_total = detailed["total"]

    if grand_total["message_count"] == 0:
        print("No Claude Code usage data found for this project.", file=sys.stderr)
        return None

    git_info = get_git_info()
    log_dir = get_usage_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with timestamp and commit info
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    commit_part = git_info["commit_hash_short"] or "no-commit"
    filename = f"{timestamp}_{commit_part}.json"
    filepath = log_dir / filename

    # Build the snapshot data
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "project": os.getcwd(),
        "git": {
            "branch": git_info["branch"],
            "commit_hash": git_info["commit_hash"],
            "commit_hash_short": git_info["commit_hash_short"],
        },
        "branches": {},
        "total": {
            "sessions": grand_total.get("message_count", 0),
            "turns": grand_total["message_count"],
            "messages": grand_total["message_count"],
            "tool_calls": grand_total["tool_calls"],
            "tokens": {
                "input": grand_total["input_tokens"],
                "output": grand_total["output_tokens"],
                "cache_read": grand_total["cache_read_input_tokens"],
                "cache_creation": grand_total["cache_creation_input_tokens"],
                "total": grand_total["total_tokens"],
            },
            "api_equivalent_value_usd": round(grand_total["cost_usd"], 2),
            "cost_per_turn": round(grand_total["cost_usd"] / grand_total["message_count"], 4)
                            if grand_total["message_count"] > 0 else 0,
        },
    }

    for branch, stats in sorted(detailed["branches"].items()):
        cost_per_turn = round(stats["cost_usd"] / stats["message_count"], 4) \
                       if stats["message_count"] > 0 else 0
        snapshot["branches"][branch] = {
            "sessions": stats.get("message_count", 0),
            "turns": stats["message_count"],
            "messages": stats["message_count"],
            "tool_calls": stats["tool_calls"],
            "tokens": {
                "input": stats["input_tokens"],
                "output": stats["output_tokens"],
                "cache_read": stats["cache_read_input_tokens"],
                "cache_creation": stats["cache_creation_input_tokens"],
                "total": stats["total_tokens"],
            },
            "api_equivalent_value_usd": round(stats["cost_usd"], 2),
            "cost_per_turn": cost_per_turn,
        }

    # Write the snapshot
    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Saved usage snapshot: {filepath.relative_to(os.getcwd())}")
    return filepath


def load_all_snapshots() -> List[Dict]:
    """Load all snapshot files from the usage log directory."""
    log_dir = get_usage_log_dir()

    if not log_dir.exists():
        return []

    snapshots = []
    for filepath in sorted(log_dir.glob("*.json")):
        if filepath.name == "claude-token-usage.html":
            continue
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                data["_filename"] = filepath.name
                snapshots.append(data)
        except (json.JSONDecodeError, Exception) as e:
            print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)

    return snapshots


# =============================================================================
# HTML REPORT GENERATION
# =============================================================================

def generate_html_report() -> Optional[Path]:
    """Generate an HTML report from all snapshots."""
    snapshots = load_all_snapshots()

    if not snapshots:
        print("No usage snapshots found. Run cc-usage --pre-commit first.", file=sys.stderr)
        return None

    log_dir = get_usage_log_dir()
    report_path = log_dir / "claude-token-usage.html"

    # Calculate deltas between commits
    commits_data = []
    prev_total = 0

    for i, snap in enumerate(snapshots):
        current_total = snap.get("total", {}).get("tokens", {}).get("total", 0)
        delta = current_total - prev_total

        tokens = snap.get("total", {}).get("tokens", {})
        turns = snap.get("total", {}).get("turns", 0)
        commits_data.append({
            "timestamp": snap.get("timestamp", "")[:16].replace("T", " "),
            "commit": snap.get("git", {}).get("commit_hash_short", "?"),
            "branch": snap.get("git", {}).get("branch", "?"),
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
            "cache_read": tokens.get("cache_read", 0),
            "cache_creation": tokens.get("cache_creation", 0),
            "total": current_total,
            "delta": delta if i > 0 else current_total,
            "cost": snap.get("total", {}).get("api_equivalent_value_usd", 0),
            "turns": turns,
        })
        prev_total = current_total

    # Calculate per-branch totals from latest snapshot
    latest = snapshots[-1] if snapshots else {}
    branch_totals = latest.get("branches", {})

    # Generate HTML
    html = generate_report_html(commits_data, branch_totals, latest)

    with open(report_path, "w") as f:
        f.write(html)

    print(f"Generated report: {report_path.relative_to(os.getcwd())}")
    return report_path


def generate_report_html(commits_data: List[Dict], branch_totals: Dict, latest: Dict) -> str:
    """Generate the HTML content for the report."""
    project_name = os.path.basename(os.getcwd())
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Calculate grand totals from latest snapshot
    grand = latest.get("total", {})
    grand_tokens = grand.get("tokens", {})

    # Prepare data for chart
    chart_labels = json.dumps([c["timestamp"] for c in commits_data])
    chart_input = json.dumps([c["input"] for c in commits_data])
    chart_output = json.dumps([c["output"] for c in commits_data])
    chart_cache_read = json.dumps([c["cache_read"] for c in commits_data])
    chart_cache_create = json.dumps([c["cache_creation"] for c in commits_data])
    chart_deltas = json.dumps([c["delta"] for c in commits_data])

    # Generate branch rows
    branch_rows = ""
    for branch, data in sorted(branch_totals.items()):
        tokens = data.get("tokens", {})
        turns = data.get("turns", 0)
        cost = data.get('api_equivalent_value_usd', 0)
        cost_per_turn = round(cost / turns, 4) if turns > 0 else 0
        branch_rows += f"""
            <tr>
                <td><code>{branch}</code></td>
                <td class="num">{data.get('sessions', 0)}</td>
                <td class="num">{tokens.get('total', 0):,}</td>
                <td class="num">{turns}</td>
                <td class="num">${cost_per_turn:.4f}</td>
                <td class="num">${cost:.2f}</td>
            </tr>"""

    # Generate commit rows
    commit_rows = ""
    for c in reversed(commits_data):  # Most recent first
        commit_rows += f"""
            <tr>
                <td><code>{c['commit']}</code></td>
                <td><code>{c['branch']}</code></td>
                <td>{c['timestamp']}</td>
                <td class="num">{c['total']:,}</td>
                <td class="num delta">{c['delta']:+,}</td>
                <td class="num">{c.get('turns', '?')}</td>
                <td class="num">${c['cost']:.2f}</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Code Token Usage - {project_name}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg: #1a1a2e;
            --card: #16213e;
            --text: #eaeaea;
            --muted: #888;
            --accent: #e94560;
            --input: #4ecca3;
            --output: #e94560;
            --cache-read: #7b68ee;
            --cache-create: #ffd93d;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 2rem;
            line-height: 1.6;
        }}
        h1 {{ margin-bottom: 0.5rem; }}
        h2 {{ margin: 2rem 0 1rem; color: var(--accent); }}
        .subtitle {{ color: var(--muted); margin-bottom: 2rem; }}
        .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 1.5rem 0; }}
        .card {{
            background: var(--card);
            padding: 1.5rem;
            border-radius: 8px;
        }}
        .card-value {{ font-size: 2rem; font-weight: bold; }}
        .card-label {{ color: var(--muted); font-size: 0.9rem; }}
        .card.input .card-value {{ color: var(--input); }}
        .card.output .card-value {{ color: var(--output); }}
        .card.cache-read .card-value {{ color: var(--cache-read); }}
        .card.cache-create .card-value {{ color: var(--cache-create); }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--card);
            border-radius: 8px;
            overflow: hidden;
            margin: 1rem 0;
        }}
        th, td {{ padding: 0.75rem 1rem; text-align: left; }}
        th {{ background: rgba(255,255,255,0.05); font-weight: 600; }}
        tr:not(:last-child) td {{ border-bottom: 1px solid rgba(255,255,255,0.05); }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .delta {{ color: var(--accent); }}
        code {{ background: rgba(255,255,255,0.1); padding: 0.2rem 0.4rem; border-radius: 3px; font-size: 0.9em; }}
        .chart-container {{
            background: var(--card);
            padding: 1.5rem;
            border-radius: 8px;
            margin: 1.5rem 0;
        }}
        .legend {{ display: flex; gap: 1.5rem; justify-content: center; margin-top: 1rem; flex-wrap: wrap; }}
        .legend-item {{ display: flex; align-items: center; gap: 0.5rem; }}
        .legend-color {{ width: 16px; height: 16px; border-radius: 3px; }}
        footer {{ margin-top: 3rem; color: var(--muted); font-size: 0.85rem; text-align: center; }}
    </style>
</head>
<body>
    <h1>Claude Code Token Usage</h1>
    <p class="subtitle">{project_name} &mdash; Generated {generated_at}</p>

    <div class="cards">
        <div class="card">
            <div class="card-value">{grand_tokens.get('total', 0):,}</div>
            <div class="card-label">Total Tokens</div>
        </div>
        <div class="card input">
            <div class="card-value">{grand_tokens.get('input', 0):,}</div>
            <div class="card-label">Input Tokens</div>
        </div>
        <div class="card output">
            <div class="card-value">{grand_tokens.get('output', 0):,}</div>
            <div class="card-label">Output Tokens</div>
        </div>
        <div class="card cache-read">
            <div class="card-value">{grand_tokens.get('cache_read', 0):,}</div>
            <div class="card-label">Cache Read</div>
        </div>
        <div class="card cache-create">
            <div class="card-value">{grand_tokens.get('cache_creation', 0):,}</div>
            <div class="card-label">Cache Creation</div>
        </div>
        <div class="card">
            <div class="card-value">${grand.get('api_equivalent_value_usd', 0):.2f}</div>
            <div class="card-label">API Equivalent</div>
        </div>
    </div>

    <h2>Token Usage by Commit</h2>
    <div class="chart-container">
        <canvas id="deltaChart" height="120"></canvas>
        <div class="legend">
            <div class="legend-item"><div class="legend-color" style="background: var(--input)"></div> Input</div>
            <div class="legend-item"><div class="legend-color" style="background: var(--output)"></div> Output</div>
            <div class="legend-item"><div class="legend-color" style="background: var(--cache-read)"></div> Cache Read</div>
            <div class="legend-item"><div class="legend-color" style="background: var(--cache-create)"></div> Cache Creation</div>
        </div>
    </div>

    <h2>Cumulative Tokens Over Time</h2>
    <div class="chart-container">
        <canvas id="cumulativeChart" height="100"></canvas>
    </div>

    <h2>Usage by Branch</h2>
    <table>
        <thead>
            <tr><th>Branch</th><th class="num">Sessions</th><th class="num">Tokens</th><th class="num">Turns</th><th class="num">$/Turn</th><th class="num">API Value</th></tr>
        </thead>
        <tbody>{branch_rows}</tbody>
    </table>

    <h2>Commit History</h2>
    <table>
        <thead>
            <tr><th>Commit</th><th>Branch</th><th>Timestamp</th><th class="num">Total</th><th class="num">Delta</th><th class="num">Turns</th><th class="num">Cost</th></tr>
        </thead>
        <tbody>{commit_rows}</tbody>
    </table>

    <footer>
        Generated by <strong>cc-usage</strong> &mdash;
        Claude Code token tracking for git repositories
    </footer>

    <script>
        const labels = {chart_labels};
        const inputData = {chart_input};
        const outputData = {chart_output};
        const cacheReadData = {chart_cache_read};
        const cacheCreateData = {chart_cache_create};
        const deltaData = {chart_deltas};

        // Calculate deltas per token type
        const deltaInput = inputData.map((v, i) => i === 0 ? v : v - inputData[i-1]);
        const deltaOutput = outputData.map((v, i) => i === 0 ? v : v - outputData[i-1]);
        const deltaCacheRead = cacheReadData.map((v, i) => i === 0 ? v : v - cacheReadData[i-1]);
        const deltaCacheCreate = cacheCreateData.map((v, i) => i === 0 ? v : v - cacheCreateData[i-1]);

        // Stacked bar chart for deltas
        new Chart(document.getElementById('deltaChart'), {{
            type: 'bar',
            data: {{
                labels: labels,
                datasets: [
                    {{ label: 'Input', data: deltaInput, backgroundColor: '#4ecca3' }},
                    {{ label: 'Output', data: deltaOutput, backgroundColor: '#e94560' }},
                    {{ label: 'Cache Read', data: deltaCacheRead, backgroundColor: '#7b68ee' }},
                    {{ label: 'Cache Creation', data: deltaCacheCreate, backgroundColor: '#ffd93d' }},
                ]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ stacked: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#888' }} }},
                    y: {{ stacked: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#888' }} }}
                }}
            }}
        }});

        // Line chart for cumulative
        new Chart(document.getElementById('cumulativeChart'), {{
            type: 'line',
            data: {{
                labels: labels,
                datasets: [
                    {{ label: 'Total', data: inputData.map((v, i) => v + outputData[i] + cacheReadData[i] + cacheCreateData[i]),
                       borderColor: '#e94560', backgroundColor: 'rgba(233,69,96,0.1)', fill: true, tension: 0.3 }}
                ]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#888' }} }},
                    y: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#888' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>"""

    return html


# =============================================================================
# VERBOSE HTML REPORT
# =============================================================================

def generate_verbose_html_report() -> Optional[Path]:
    """Generate verbose-cc-usage-report.html with hourly + per-session detail."""
    detailed = collect_detailed_stats()
    if detailed["total"]["message_count"] == 0:
        print("No Claude Code usage data found for this project.", file=sys.stderr)
        return None

    log_dir = get_usage_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    report_path = log_dir / "verbose-cc-usage-report.html"

    html = _render_verbose_html(detailed)
    with open(report_path, "w") as f:
        f.write(html)

    try:
        rel = report_path.relative_to(os.getcwd())
    except ValueError:
        rel = report_path
    print(f"Generated verbose report: {rel}")
    return report_path


def _render_verbose_html(detailed: Dict[str, Any]) -> str:
    project_name = os.path.basename(os.getcwd())
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = detailed["total"]

    hour_keys = sorted(detailed["hours"].keys())
    hour_labels = json.dumps(hour_keys)
    hour_input = json.dumps([detailed["hours"][k]["input_tokens"] for k in hour_keys])
    hour_output = json.dumps([detailed["hours"][k]["output_tokens"] for k in hour_keys])
    hour_cache_read = json.dumps([detailed["hours"][k]["cache_read_input_tokens"] for k in hour_keys])
    hour_cache_create = json.dumps([detailed["hours"][k]["cache_creation_input_tokens"] for k in hour_keys])
    hour_cost = json.dumps([round(detailed["hours"][k]["cost_usd"], 4) for k in hour_keys])
    hour_msgs = json.dumps([detailed["hours"][k]["message_count"] for k in hour_keys])

    # Session rows (most expensive first)
    sessions_sorted = sorted(detailed["sessions"], key=lambda s: s["cost_usd"], reverse=True)
    session_rows = []
    for s in sessions_sorted:
        models_str = ", ".join(f"{fam}×{n}" for fam, n in sorted(s["models"].items()))
        cost_per_turn = s['cost_usd'] / s['messages'] if s['messages'] > 0 else 0
        session_rows.append(f"""
            <tr>
                <td><code title="{s['id']}">{s['id'][:12]}</code></td>
                <td><code>{s['branch']}</code></td>
                <td>{_fmt_ts(s['first_ts'])}</td>
                <td>{_fmt_ts(s['last_ts'])}</td>
                <td class="num">{_fmt_duration(s['duration_s'])}</td>
                <td class="num">{s['messages']:,}</td>
                <td class="num">{s['tool_calls']:,}</td>
                <td class="num">{s['input_tokens']:,}</td>
                <td class="num">{s['output_tokens']:,}</td>
                <td class="num">{s['cache_read_input_tokens']:,}</td>
                <td class="num">{s['cache_creation_input_tokens']:,}</td>
                <td class="num">{s['total_tokens']:,}</td>
                <td class="num">${cost_per_turn:.4f}</td>
                <td class="num cost">${s['cost_usd']:,.2f}</td>
                <td>{models_str}</td>
            </tr>""")
    session_rows_html = "".join(session_rows)

    # Hour rows
    hour_rows = []
    for k in hour_keys:
        b = detailed["hours"][k]
        hour_rows.append(f"""
            <tr>
                <td>{k}</td>
                <td class="num">{b['message_count']:,}</td>
                <td class="num">{b['tool_calls']:,}</td>
                <td class="num">{b['input_tokens']:,}</td>
                <td class="num">{b['output_tokens']:,}</td>
                <td class="num">{b['cache_read_input_tokens']:,}</td>
                <td class="num">{b['cache_creation_input_tokens']:,}</td>
                <td class="num">{b['total_tokens']:,}</td>
                <td class="num cost">${b['cost_usd']:,.2f}</td>
            </tr>""")
    hour_rows_html = "".join(hour_rows)

    # Model rows
    model_rows = []
    for fam in sorted(detailed["models"].keys()):
        b = detailed["models"][fam]
        rates = PRICING.get(fam, PRICING[DEFAULT_MODEL_FAMILY])
        cost_per_turn = b['cost_usd'] / b['message_count'] if b['message_count'] > 0 else 0
        model_rows.append(f"""
            <tr>
                <td><code>{TIER_LABELS.get(fam, fam)}</code></td>
                <td class="num">{b['message_count']:,}</td>
                <td class="num">{b['input_tokens']:,}</td>
                <td class="num">{b['output_tokens']:,}</td>
                <td class="num">{b['cache_read_input_tokens']:,}</td>
                <td class="num">{b['cache_creation_input_tokens']:,}</td>
                <td class="num">{b['total_tokens']:,}</td>
                <td class="num cost">${b['cost_usd']:,.2f}</td>
                <td class="num">${cost_per_turn:.4f}</td>
                <td class="num">${rates['input']:.2f} / ${rates['output']:.2f}</td>
            </tr>""")
    model_rows_html = "".join(model_rows)

    # Branch rows
    branch_rows = []
    for br in sorted(detailed["branches"].keys()):
        b = detailed["branches"][br]
        cost_per_turn = b['cost_usd'] / b['message_count'] if b['message_count'] > 0 else 0
        branch_rows.append(f"""
            <tr>
                <td><code>{br}</code></td>
                <td class="num">{b['message_count']:,}</td>
                <td class="num">{b['tool_calls']:,}</td>
                <td class="num">{b['total_tokens']:,}</td>
                <td class="num">{b['message_count']:,}</td>
                <td class="num">${cost_per_turn:.4f}</td>
                <td class="num cost">${b['cost_usd']:,.2f}</td>
            </tr>""")
    branch_rows_html = "".join(branch_rows)

    first_ts = _fmt_ts(detailed["first_ts"])
    last_ts = _fmt_ts(detailed["last_ts"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Verbose Claude Code Usage — {project_name}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg:#0f1724; --card:#16213e; --text:#eaeaea; --muted:#8892b0;
            --accent:#e94560; --input:#4ecca3; --output:#e94560;
            --cache-read:#7b68ee; --cache-create:#ffd93d; --cost:#ff7ab6;
        }}
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:var(--bg);color:var(--text);padding:2rem;line-height:1.5}}
        h1{{margin-bottom:.25rem}} h2{{margin:2rem 0 1rem;color:var(--accent)}}
        .subtitle{{color:var(--muted);margin-bottom:1.5rem}}
        .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin:1.5rem 0}}
        .card{{background:var(--card);padding:1.25rem;border-radius:8px}}
        .card-value{{font-size:1.75rem;font-weight:bold}}
        .card-label{{color:var(--muted);font-size:.85rem}}
        .card.input .card-value{{color:var(--input)}}
        .card.output .card-value{{color:var(--output)}}
        .card.cache-read .card-value{{color:var(--cache-read)}}
        .card.cache-create .card-value{{color:var(--cache-create)}}
        .card.cost .card-value{{color:var(--cost)}}
        table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;
              overflow:hidden;margin:1rem 0;font-size:.88rem}}
        th,td{{padding:.55rem .75rem;text-align:left;white-space:nowrap}}
        th{{background:rgba(255,255,255,.05);font-weight:600;position:sticky;top:0;cursor:pointer;user-select:none}}
        th:hover{{background:rgba(255,255,255,.1)}}
        tr:not(:last-child) td{{border-bottom:1px solid rgba(255,255,255,.05)}}
        tr:hover td{{background:rgba(255,255,255,.03)}}
        .num{{text-align:right;font-variant-numeric:tabular-nums}}
        .cost{{color:var(--cost);font-weight:600}}
        code{{background:rgba(255,255,255,.08);padding:.15rem .4rem;border-radius:3px;font-size:.88em}}
        .chart-container{{background:var(--card);padding:1.25rem;border-radius:8px;margin:1rem 0}}
        .table-wrap{{max-height:600px;overflow:auto;border-radius:8px}}
        .legend{{display:flex;gap:1.25rem;justify-content:center;margin-top:.75rem;flex-wrap:wrap;font-size:.85rem}}
        .legend-item{{display:flex;align-items:center;gap:.4rem}}
        .legend-color{{width:14px;height:14px;border-radius:3px}}
        footer{{margin-top:3rem;color:var(--muted);font-size:.8rem;text-align:center}}
        .search{{width:100%;padding:.5rem .75rem;background:var(--card);color:var(--text);
                border:1px solid rgba(255,255,255,.1);border-radius:6px;margin:.5rem 0;font-size:.9rem}}
    </style>
</head>
<body>
    <h1>Verbose Claude Code Usage</h1>
    <p class="subtitle">{project_name} &mdash; {first_ts} → {last_ts} &mdash; Generated {generated_at}</p>

    <div class="cards">
        <div class="card"><div class="card-value">{total['total_tokens']:,}</div><div class="card-label">Total Tokens</div></div>
        <div class="card input"><div class="card-value">{total['input_tokens']:,}</div><div class="card-label">Input</div></div>
        <div class="card output"><div class="card-value">{total['output_tokens']:,}</div><div class="card-label">Output</div></div>
        <div class="card cache-read"><div class="card-value">{total['cache_read_input_tokens']:,}</div><div class="card-label">Cache Read</div></div>
        <div class="card cache-create"><div class="card-value">{total['cache_creation_input_tokens']:,}</div><div class="card-label">Cache Create</div></div>
        <div class="card cost"><div class="card-value">${total['cost_usd']:,.2f}</div><div class="card-label">Total API Cost</div></div>
        <div class="card"><div class="card-value">{total['message_count']:,}</div><div class="card-label">Assistant Msgs</div></div>
        <div class="card"><div class="card-value">{total['tool_calls']:,}</div><div class="card-label">Tool Calls</div></div>
        <div class="card"><div class="card-value">{len(detailed['sessions'])}</div><div class="card-label">Sessions</div></div>
    </div>

    <h2>Hourly Token Mix</h2>
    <div class="chart-container">
        <canvas id="hourlyChart" height="110"></canvas>
        <div class="legend">
            <div class="legend-item"><div class="legend-color" style="background:var(--input)"></div>Input</div>
            <div class="legend-item"><div class="legend-color" style="background:var(--output)"></div>Output</div>
            <div class="legend-item"><div class="legend-color" style="background:var(--cache-read)"></div>Cache Read</div>
            <div class="legend-item"><div class="legend-color" style="background:var(--cache-create)"></div>Cache Create</div>
        </div>
    </div>

    <h2>Hourly API Cost (USD)</h2>
    <div class="chart-container">
        <canvas id="hourlyCostChart" height="90"></canvas>
    </div>

    <h2>Usage by Model Family</h2>
    <table>
        <thead><tr>
            <th>Model</th><th class="num">Msgs</th><th class="num">Input</th><th class="num">Output</th>
            <th class="num">Cache Read</th><th class="num">Cache Create</th>
            <th class="num">Total Tokens</th><th class="num">Cost</th><th class="num">$/Turn</th>
            <th class="num">In/Out $/1M</th>
        </tr></thead>
        <tbody>{model_rows_html}</tbody>
    </table>

    <h2>Usage by Branch</h2>
    <table>
        <thead><tr>
            <th>Branch</th><th class="num">Msgs</th><th class="num">Tools</th>
            <th class="num">Tokens</th><th class="num">Turns</th><th class="num">$/Turn</th><th class="num">Cost</th>
        </tr></thead>
        <tbody>{branch_rows_html}</tbody>
    </table>

    <h2>Usage by Hour ({len(hour_keys)} hours)</h2>
    <div class="table-wrap">
    <table id="hourTable">
        <thead><tr>
            <th>Hour</th><th class="num">Msgs</th><th class="num">Tools</th>
            <th class="num">Input</th><th class="num">Output</th>
            <th class="num">Cache Read</th><th class="num">Cache Create</th>
            <th class="num">Tokens</th><th class="num">Cost</th>
        </tr></thead>
        <tbody>{hour_rows_html}</tbody>
    </table>
    </div>

    <h2>Per-Session Breakdown ({len(sessions_sorted)} sessions)</h2>
    <input class="search" id="sessionSearch" placeholder="Filter sessions by id or branch…" />
    <div class="table-wrap">
    <table id="sessionTable">
        <thead><tr>
            <th>Session</th><th>Branch</th><th>Start</th><th>End</th>
            <th class="num">Duration</th><th class="num">Msgs</th><th class="num">Tools</th>
            <th class="num">Input</th><th class="num">Output</th>
            <th class="num">Cache Read</th><th class="num">Cache Create</th>
            <th class="num">Tokens</th><th class="num">$/Turn</th><th class="num">Cost</th>
            <th>Models</th>
        </tr></thead>
        <tbody>{session_rows_html}</tbody>
    </table>
    </div>

    <footer>
        Generated by <strong>cc-usage --verbose-report</strong>.
        Costs are Anthropic API equivalents, per 1M input/output tokens:
        Fable 5.x $10/$50 &middot; Opus 5 &amp; 4.5&ndash;4.8 $5/$25 &middot; Sonnet 5 $2/$10 &middot; Haiku 4.5 $1/$5.
        Subscription users pay flat monthly fees instead of per-token.
    </footer>

    <script>
        const hourLabels = {hour_labels};
        const hourInput = {hour_input};
        const hourOutput = {hour_output};
        const hourCacheRead = {hour_cache_read};
        const hourCacheCreate = {hour_cache_create};
        const hourCost = {hour_cost};
        const hourMsgs = {hour_msgs};

        new Chart(document.getElementById('hourlyChart'), {{
            type: 'bar',
            data: {{
                labels: hourLabels,
                datasets: [
                    {{ label: 'Input', data: hourInput, backgroundColor: '#4ecca3' }},
                    {{ label: 'Output', data: hourOutput, backgroundColor: '#e94560' }},
                    {{ label: 'Cache Read', data: hourCacheRead, backgroundColor: '#7b68ee' }},
                    {{ label: 'Cache Create', data: hourCacheCreate, backgroundColor: '#ffd93d' }},
                ]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }},
                    tooltip: {{ callbacks: {{
                        afterTitle: (ctx) => {{
                            const i = ctx[0].dataIndex;
                            return 'Msgs: ' + hourMsgs[i] + ' — Cost: $' + hourCost[i].toFixed(2);
                        }}
                    }} }}
                }},
                scales: {{
                    x: {{ stacked: true, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#8892b0' }} }},
                    y: {{ stacked: true, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#8892b0' }} }}
                }}
            }}
        }});

        new Chart(document.getElementById('hourlyCostChart'), {{
            type: 'line',
            data: {{
                labels: hourLabels,
                datasets: [{{
                    label: 'Cost (USD)', data: hourCost,
                    borderColor: '#ff7ab6', backgroundColor: 'rgba(255,122,182,.15)',
                    fill: true, tension: 0.3, pointRadius: 2
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#8892b0' }} }},
                    y: {{ grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#8892b0',
                          callback: v => '$' + v.toFixed(2) }} }}
                }}
            }}
        }});

        // Simple session search
        const search = document.getElementById('sessionSearch');
        const rows = document.querySelectorAll('#sessionTable tbody tr');
        search.addEventListener('input', () => {{
            const q = search.value.toLowerCase();
            rows.forEach(r => {{
                r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none';
            }});
        }});

        // Sort tables by clicking headers (numeric-aware)
        function makeSortable(tableId) {{
            const table = document.getElementById(tableId);
            if (!table) return;
            const headers = table.querySelectorAll('th');
            headers.forEach((th, colIdx) => {{
                let asc = false;
                th.addEventListener('click', () => {{
                    const tbody = table.tBodies[0];
                    const sorted = Array.from(tbody.rows).sort((a, b) => {{
                        const av = a.cells[colIdx].textContent.trim();
                        const bv = b.cells[colIdx].textContent.trim();
                        const an = parseFloat(av.replace(/[$,]/g, ''));
                        const bn = parseFloat(bv.replace(/[$,]/g, ''));
                        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
                        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
                    }});
                    asc = !asc;
                    sorted.forEach(r => tbody.appendChild(r));
                }});
            }});
        }}
        makeSortable('hourTable');
        makeSortable('sessionTable');
    </script>
</body>
</html>"""


# =============================================================================
# HOOK INSTALLATION
# =============================================================================

def reset_usage(dry_run: bool = False) -> bool:
    """Detach this project's JSONL transcripts so cc-usage in this folder reports
    $0, while the tokens remain on disk under ~/.claude/projects/ so global scans
    still find them (attributed to an archived path that doesn't decode to any
    real cwd).

    Only *.jsonl files are moved. memory/, todos/, and other sibling state stay
    in place so this project keeps its auto-memory and resumable todos.
    """
    project_folder = get_project_folder()
    if not project_folder.exists():
        print(f"No Claude Code project folder for {os.getcwd()}", file=sys.stderr)
        print(f"  (expected at {project_folder})", file=sys.stderr)
        return False

    jsonls = list(project_folder.glob("*.jsonl"))
    if not jsonls:
        print("No *.jsonl transcripts to reset — already at $0 here.", file=sys.stderr)
        return False

    # Pre-compute current cost for the message we'll print.
    _, grand_total, _ = collect_all_stats()
    current_cost = estimate_cost(grand_total)["total"]

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_folder = project_folder.parent / f"{project_folder.name}-archived-{ts}"

    if dry_run:
        print(f"[dry-run] Would move {len(jsonls)} transcript(s) "
              f"(${current_cost:,.2f}) to:")
        print(f"  {archive_folder}")
        print("Global cc-usage totals would be unchanged; this folder would report $0.")
        return True

    archive_folder.mkdir(parents=True, exist_ok=False)
    for jf in jsonls:
        jf.rename(archive_folder / jf.name)

    print(f"Moved {len(jsonls)} transcript(s) (${current_cost:,.2f}) to:")
    print(f"  {archive_folder}")
    print("This project now reports $0. Global token totals are preserved "
          "(archived path does not decode to a real cwd).")
    return True


def install_pre_commit_hook() -> bool:
    """Install the pre-commit hook in the current repository."""
    git_info = get_git_info()
    if not git_info["is_git_repo"]:
        print("Error: Not a git repository.", file=sys.stderr)
        return False

    hooks_dir = Path(os.getcwd()) / ".git" / "hooks"
    hook_path = hooks_dir / "pre-commit"

    hook_content = """#!/bin/bash
# Claude Code usage tracking pre-commit hook
# Installed by: cc-usage --install-hook

cc-usage --pre-commit

# Don't block the commit if cc-usage fails
exit 0
"""

    # Check if hook already exists
    if hook_path.exists():
        with open(hook_path, "r") as f:
            existing = f.read()
        if "cc-usage" in existing:
            print("Pre-commit hook already includes cc-usage.")
            return True
        # Append to existing hook
        with open(hook_path, "a") as f:
            f.write("\n# Claude Code usage tracking\ncc-usage --pre-commit\n")
        print(f"Added cc-usage to existing pre-commit hook: {hook_path}")
    else:
        with open(hook_path, "w") as f:
            f.write(hook_content)
        hook_path.chmod(0o755)
        print(f"Created pre-commit hook: {hook_path}")

    return True


# =============================================================================
# TEXT OUTPUT
# =============================================================================

def print_text_report(branch_stats: Dict, grand_total: Dict, session_details: List, verbose: bool):
    """Print the text-format usage report."""
    print(f"Claude Code Usage: {os.path.basename(os.getcwd())}")
    print("=" * 100)

    # Per-branch breakdown
    print(f"\n{'BRANCH':<40} {'Sessions':>8} {'Messages':>10} {'Tokens':>12} {'Turns':>8} {'$/Turn':>10} {'API Value':>12}")
    print("-" * 115)

    for branch in sorted(branch_stats.keys()):
        stats = branch_stats[branch]
        if stats["sessions"] > 0:
            cost = estimate_cost(stats)
            tokens = total_tokens(stats)
            cost_per_turn = cost['total'] / stats['message_count'] if stats['message_count'] > 0 else 0
            print(f"{branch:<40} {stats['sessions']:>8} {stats['message_count']:>10} {format_millions(tokens):>12} {stats['message_count']:>8} ${cost_per_turn:>9,.2f} ${cost['total']:>10,.2f}")

    print("-" * 115)

    # Grand total row
    grand_cost = estimate_cost(grand_total)
    grand_tokens = total_tokens(grand_total)
    grand_cost_per_turn = grand_cost['total'] / grand_total['message_count'] if grand_total['message_count'] > 0 else 0
    print(f"{'TOTAL':<40} {grand_total['sessions']:>8} {grand_total['message_count']:>10} {format_millions(grand_tokens):>12} {grand_total['message_count']:>8} ${grand_cost_per_turn:>9,.2f} ${grand_cost['total']:>10,.2f}")

    # Date range
    if grand_total["first_timestamp"] and grand_total["last_timestamp"]:
        first = grand_total["first_timestamp"][:10]
        last = grand_total["last_timestamp"][:10]
        print(f"\nDate range: {first} to {last}")

    # Detailed token breakdown (by model family)
    detailed = collect_detailed_stats()
    print("\n" + "=" * 125)
    print("TOKEN BREAKDOWN BY MODEL FAMILY")
    print("-" * 125)
    print(f"{'Model':<18} {'Input':>12} {'Output':>12} {'Cache Read':>14} {'Cache Create':>14} {'Turns':>8} {'$/Turn':>10} {'Cost':>12}")
    print("-" * 125)
    for fam in sorted(detailed["models"].keys()):
        b = detailed["models"][fam]
        cost_per_turn = b['cost_usd'] / b['message_count'] if b['message_count'] > 0 else 0
        print(
            f"{TIER_LABELS.get(fam, fam):<18} "
            f"{format_number(b['input_tokens']):>12} "
            f"{format_number(b['output_tokens']):>12} "
            f"{format_number(b['cache_read_input_tokens']):>14} "
            f"{format_number(b['cache_creation_input_tokens']):>14} "
            f"{b['message_count']:>8} "
            f"${cost_per_turn:>9,.2f} "
            f"${b['cost_usd']:>10,.2f}"
        )
    print("-" * 125)
    total_cost_per_turn = detailed['total']['cost_usd'] / detailed['total']['message_count'] if detailed['total']['message_count'] > 0 else 0
    print(f"{'TOTAL':<18} "
          f"{format_number(grand_total['input_tokens']):>12} "
          f"{format_number(grand_total['output_tokens']):>12} "
          f"{format_number(grand_total['cache_read_input_tokens']):>14} "
          f"{format_number(grand_total['cache_creation_input_tokens']):>14} "
          f"{detailed['total']['message_count']:>8} "
          f"${total_cost_per_turn:>9,.2f} "
          f"${detailed['total']['cost_usd']:>10,.2f}")

    print("\n" + "=" * 90)
    print("Note: Cost calculated per-model at actual Anthropic API rates,")
    print("      per 1M input/output tokens:")
    print("      Fable 5.x: $10/$50 | Opus 5 & 4.5-4.8: $5/$25 | Sonnet 5: $2/$10 | Haiku 4.5: $1/$5")
    print("      Subscription users (Pro $20/mo, Max $100/mo) pay flat fees instead.")

    # Verbose: hourly + per-session + per-model breakdown with costs
    if verbose:
        detailed = collect_detailed_stats()
        print_verbose_sections(detailed)


def print_verbose_sections(detailed: Dict[str, Any]) -> None:
    """Print hour-by-hour, per-model, and per-session breakdowns with costs."""
    width = 128

    # ---- Per-model summary (useful context for cost interpretation) ----
    print("\n" + "=" * width)
    print("USAGE BY MODEL FAMILY")
    print("-" * width)
    print(
        f"{'Model':<18} {'Turns':>7} {'Input':>12} {'Output':>12} "
        f"{'CacheRead':>14} {'CacheCreate':>14} {'Tokens':>12} {'Cost':>12} {'$/Turn':>10}"
    )
    print("-" * width)
    for fam in sorted(detailed["models"].keys()):
        b = detailed["models"][fam]
        cost_per_turn = b['cost_usd'] / b['message_count'] if b['message_count'] > 0 else 0
        print(
            f"{TIER_LABELS.get(fam, fam):<18} {b['message_count']:>7} "
            f"{format_number(b['input_tokens']):>12} "
            f"{format_number(b['output_tokens']):>12} "
            f"{format_number(b['cache_read_input_tokens']):>14} "
            f"{format_number(b['cache_creation_input_tokens']):>14} "
            f"{format_millions(b['total_tokens']):>12} "
            f"${b['cost_usd']:>10,.2f} "
            f"${cost_per_turn:>8,.2f}"
        )

    # ---- Hourly breakdown ----
    print("\n" + "=" * width)
    print("USAGE BY HOUR (local time)")
    print("-" * width)
    print(
        f"{'Hour':<18} {'Turns':>6} {'Tools':>6} {'Input':>12} {'Output':>12} "
        f"{'CacheRead':>14} {'CacheCreate':>14} {'Tokens':>12} {'Cost':>12} {'$/Turn':>10}"
    )
    print("-" * width)
    hours = detailed["hours"]
    for hkey in sorted(hours.keys()):
        b = hours[hkey]
        cost_per_turn = b['cost_usd'] / b['message_count'] if b['message_count'] > 0 else 0
        print(
            f"{hkey:<18} {b['message_count']:>6} {b['tool_calls']:>6} "
            f"{format_number(b['input_tokens']):>12} "
            f"{format_number(b['output_tokens']):>12} "
            f"{format_number(b['cache_read_input_tokens']):>14} "
            f"{format_number(b['cache_creation_input_tokens']):>14} "
            f"{format_millions(b['total_tokens']):>12} "
            f"${b['cost_usd']:>10,.2f} "
            f"${cost_per_turn:>8,.2f}"
        )

    # ---- Per-session breakdown (most expensive first) ----
    print("\n" + "=" * width)
    print("PER-SESSION BREAKDOWN (most expensive first)")
    print("-" * width)
    print(
        f"{'Session':<14} {'Branch':<22} {'Start':<17} {'Dur':>7} "
        f"{'Turns':>6} {'Tools':>5} {'Tokens':>10} {'Cost':>10} {'$/Turn':>10}"
    )
    print("-" * width)
    sessions = sorted(detailed["sessions"], key=lambda s: s["cost_usd"], reverse=True)
    for s in sessions:
        sid = s["id"][:12]
        branch = s["branch"]
        branch_short = branch[:20] + ".." if len(branch) > 22 else branch
        cost_per_turn = s['cost_usd'] / s['messages'] if s['messages'] > 0 else 0
        print(
            f"{sid:<14} {branch_short:<22} "
            f"{_fmt_ts(s['first_ts']):<17} "
            f"{_fmt_duration(s['duration_s']):>7} "
            f"{s['messages']:>6} {s['tool_calls']:>5} "
            f"{format_millions(s['total_tokens']):>10} "
            f"${s['cost_usd']:>8,.2f} "
            f"${cost_per_turn:>8,.2f}"
        )
    print("-" * width)
    total = detailed["total"]
    cost_per_turn = total['cost_usd'] / total['message_count'] if total['message_count'] > 0 else 0
    print(
        f"{'TOTAL':<14} {'':<22} {'':<17} {'':>7} "
        f"{total['message_count']:>6} {total['tool_calls']:>5} "
        f"{format_millions(total['total_tokens']):>10} "
        f"${total['cost_usd']:>8,.2f} "
        f"${cost_per_turn:>8,.2f}"
    )


def print_json_report(branch_stats: Dict, grand_total: Dict):
    """Print the JSON-format usage report with per-model cost and turns."""
    detailed = collect_detailed_stats()
    output = {
        "project": os.getcwd(),
        "branches": {},
        "total": {
            "sessions": grand_total["sessions"],
            "turns": grand_total["message_count"],
            "messages": grand_total["message_count"],
            "tool_calls": grand_total["tool_calls"],
            "tokens": {
                "input": grand_total["input_tokens"],
                "output": grand_total["output_tokens"],
                "cache_read": grand_total["cache_read_input_tokens"],
                "cache_creation": grand_total["cache_creation_input_tokens"],
                "total": total_tokens(grand_total),
            },
            "cost_by_model": {fam: round(detailed["models"][fam]["cost_usd"], 2)
                              for fam in detailed["models"]},
            "total_cost_usd": round(detailed["total"]["cost_usd"], 2),
            "cost_per_turn": round(detailed["total"]["cost_usd"] / detailed["total"]["message_count"], 4)
                              if detailed["total"]["message_count"] > 0 else 0,
        },
    }
    for branch, stats in sorted(detailed["branches"].items()):
        cost_per_turn = round(stats["cost_usd"] / stats["message_count"], 4) \
                       if stats["message_count"] > 0 else 0
        output["branches"][branch] = {
            "sessions": grand_total["sessions"],
            "turns": stats["message_count"],
            "messages": stats["message_count"],
            "tokens": stats["total_tokens"],
            "cost_usd": round(stats["cost_usd"], 2),
            "cost_per_turn": cost_per_turn,
        }
    print(json.dumps(output, indent=2))


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Calculate Claude Code token usage for this project")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show hourly + per-session + per-model breakdown (also writes verbose HTML report)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--pre-commit", action="store_true", help="Run in pre-commit mode (save snapshot + generate report)")
    parser.add_argument("--report", action="store_true", help="Generate HTML report from snapshots")
    parser.add_argument("--verbose-report", action="store_true",
                        help="Generate verbose-cc-usage-report.html (hourly + per-session detail)")
    parser.add_argument("--no-html", action="store_true",
                        help="With -v, skip writing the verbose HTML report")
    parser.add_argument("--install-hook", action="store_true", help="Install pre-commit hook in current repo")
    parser.add_argument("--reset", action="store_true",
                        help="Archive this project's *.jsonl transcripts so cc-usage here reports $0. "
                             "Tokens stay on disk under ~/.claude/projects/ (renamed dir) so global "
                             "totals are unchanged. memory/ and todos/ are left in place.")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --reset, show what would happen without moving anything.")
    args = parser.parse_args()

    # Handle special modes
    if args.reset:
        success = reset_usage(dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    if args.install_hook:
        success = install_pre_commit_hook()
        sys.exit(0 if success else 1)

    if args.pre_commit:
        # Save snapshot and generate report
        snapshot_path = save_pre_commit_snapshot()
        if snapshot_path:
            generate_html_report()
        sys.exit(0)

    if args.report:
        report_path = generate_html_report()
        sys.exit(0 if report_path else 1)

    if args.verbose_report:
        report_path = generate_verbose_html_report()
        sys.exit(0 if report_path else 1)

    # Normal usage report
    branch_stats, grand_total, session_details = collect_all_stats()

    if grand_total["sessions"] == 0:
        print("No session data found for this project.")
        sys.exit(1)

    if args.json:
        print_json_report(branch_stats, grand_total)
    else:
        print_text_report(branch_stats, grand_total, session_details, args.verbose)
        # When verbose, also drop the verbose HTML report for offline review.
        if args.verbose and not args.no_html:
            generate_verbose_html_report()


if __name__ == "__main__":
    main()
