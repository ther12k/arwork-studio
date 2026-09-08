"use client";

/**
 * Shared difficulty-profile helpers (contract §5).
 *
 * `manifest.difficulty` is a union: legacy bundles carry the string "unrated",
 * upgraded ones the full profile `{ rating, score, metrics }`. Both the compact
 * canvas mini-panel and the full right-panel profile render from these
 * primitives so the tier math cannot drift between them.
 */

import type { DifficultyProfile } from "@/lib/studio-api";

export const DIFFICULTY_TIERS = [
  { key: "easy", label: "Easy", max: 25, color: "#7fc8b0" },
  { key: "medium", label: "Medium", max: 50, color: "#3fa08d" },
  { key: "hard", label: "Hard", max: 75, color: "#087f74" },
  { key: "master", label: "Master", max: 100, color: "#055e56" },
] as const;

export type DifficultyTier = (typeof DIFFICULTY_TIERS)[number];

const clampScore = (v: unknown): number => {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : 0;
};

export interface NormalizedDifficulty {
  tier: DifficultyTier;
  score: number;
  profile: DifficultyProfile;
}

/**
 * Normalize `manifest.difficulty`:
 *  - null / undefined / legacy string ("unrated", anything else) → null
 *    (callers render the muted "unrated" note);
 *  - profile → tier (the compiler's `rating` wins; score thresholds are the
 *    fallback: easy < 25 ≤ medium < 50 ≤ hard < 75 ≤ master) + clamped score.
 */
export function normalizeDifficulty(raw: string | DifficultyProfile | undefined | null): NormalizedDifficulty | null {
  if (raw == null || typeof raw === "string") return null;
  const score = clampScore(raw.score);
  const byRating = DIFFICULTY_TIERS.find((t) => t.key === raw.rating);
  const byScore =
    DIFFICULTY_TIERS.find((t) => score < t.max) ?? DIFFICULTY_TIERS[DIFFICULTY_TIERS.length - 1];
  return { tier: byRating ?? byScore, score, profile: raw };
}
