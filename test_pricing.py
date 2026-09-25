#!/usr/bin/env python3
"""Regression tests for model -> pricing-tier mapping.

Run: python3 test_pricing.py

These exist because the mapping is a substring/regex match over model ids that
Anthropic keeps adding to. A new id that falls through to the default tier is
silently mispriced -- no error, just a wrong number -- so every id shape we
have actually seen in a transcript is pinned here.
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "cc_usage", pathlib.Path(__file__).with_name("cc_usage.py")
)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

EXPECTED = {
    # Current models, as written by Claude Code today.
    "claude-fable-5-1":            "fable-5-1",
    "claude-mythos-5-1":           "fable-5-1",
    "claude-fable-5":              "fable",
    "claude-mythos-5":             "fable",
    "claude-opus-5":               "opus",
    "claude-opus-5-5":             "opus",
    "claude-opus-4-8":             "opus",
    "claude-opus-4-7":             "opus",
    "claude-opus-4-6":             "opus",
    "claude-opus-4-5":             "opus",
    "claude-opus-4-5-20251101":    "opus",
    "claude-sonnet-5":             "sonnet",
    "claude-haiku-4-5":            "haiku",
    "claude-haiku-4-5-20251001":   "haiku",

    # Long-context variants bill at the standard rate.
    "claude-opus-5[1m]":           "opus",
    "claude-opus-5-5[1m]":         "opus",

    # Legacy rates -- these differ from their own family's current rate.
    "claude-opus-4-1":             "opus-4-1",
    "claude-opus-4-1-20250805":    "opus-4-1",
    "claude-opus-4-0":             "opus-4-1",
    "claude-opus-4-20250514":      "opus-4-1",
    "claude-sonnet-4-6":           "sonnet-4",
    "claude-sonnet-4-5":           "sonnet-4",
    "claude-sonnet-4-5-20250929":  "sonnet-4",
    "claude-sonnet-4-0":           "sonnet-4",
    "claude-sonnet-4-20250514":    "sonnet-4",
    # Version BEFORE the name -- the old id shape.
    "claude-3-7-sonnet-20250219":  "sonnet-4",
    "claude-3-5-sonnet-20241022":  "sonnet-4",
    "claude-3-5-haiku-20241022":   "haiku-3",
    "claude-3-haiku-20240307":     "haiku-3",

    # Bare aliases mean the CURRENT model of that family.
    "opus":                        "opus",
    "sonnet":                      "sonnet",
    "haiku":                       "haiku",
    "fable":                       "fable",

    # Junk falls back to the default tier rather than crashing.
    "<synthetic>":                 cc.DEFAULT_MODEL_FAMILY,
    None:                          cc.DEFAULT_MODEL_FAMILY,
}


def main() -> int:
    failures = []
    for model, want in EXPECTED.items():
        got = cc.model_family(model)
        if got != want:
            failures.append(f"  {model!r}: expected {want!r}, got {got!r}")

    # Every tier referenced above must exist in PRICING, and every tier in
    # PRICING must have a label.
    for tier in set(EXPECTED.values()):
        if tier not in cc.PRICING:
            failures.append(f"  tier {tier!r} missing from PRICING")
    for tier in cc.PRICING:
        if tier not in cc.TIER_LABELS:
            failures.append(f"  tier {tier!r} missing from TIER_LABELS")
        for key in ("input", "output", "cache_read", "cache_create"):
            if key not in cc.PRICING[tier]:
                failures.append(f"  tier {tier!r} missing rate {key!r}")

    if failures:
        print(f"FAILED ({len(failures)}):")
        print("\n".join(failures))
        return 1
    print(f"ok - {len(EXPECTED)} model ids mapped, {len(cc.PRICING)} tiers priced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
