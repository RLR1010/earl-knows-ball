"""
Parlay / same-game-parlay engine for Earl Knows Ball.

Pure math over stored model predictions — NEVER runs inference (all inference
is done on the current-live model only, per project rule). This module reads
legs that the caller has already fetched from `game_predictions` and combines
them into a parlay ticket with honest EV/fair-value math and same-game
correlation flags.

The value proposition: sportsbooks hide the compound vig on parlays. Two
+EV single legs can make a NEGATIVE-EV parlay because book vig stacks
multiplicatively across legs. This engine surfaces that so the UI can show
the user the "receipt" before they place it.

Per-project stat rules (TOOLS.md / MEMORY.md):
  - Never average per-row rates; derive from counts. (N/A here — pure odds math.)
  - Rolling/cumulative must be INCLUSIVE. (N/A here.)
  - Season aggregates must gate on REGULAR season. (N/A here — parlay legs are
    single upcoming games, not season aggregates.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------

def american_to_decimal(odds: float) -> float:
    """Convert American odds (+120, -130, +100) to decimal odds.

    Decimal = 1 + (stake_return / stake).
      +120 -> 2.20 ; -130 -> 1.769 ; +100 -> 2.00
    """
    if odds is None:
        return 1.0  # no odds -> treat as fair/even; caller decides
    o = float(odds)
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / abs(o)


def decimal_to_american(dec: float) -> int:
    """Decimal odds -> American odds (rounded to nearest integer)."""
    if dec is None or dec <= 1.0:
        return 100
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100.0))
    return int(round(-100.0 / (dec - 1.0)))


# ---------------------------------------------------------------------------
# Correlation: logical exclusion rules for same-game legs
# ---------------------------------------------------------------------------
# These are HARD constraints / heavy warnings for same-game parlays built from
# a single game's model outputs. The naive independent p1*p2*...*pn assumption
# is WRONG when legs are directionally correlated (e.g. a favorite's ML and
# that same favorite's ATS are near-duplicates). A v1.1 enhancement can replace
# these with an empirical pairwise joint-hit table computed from stored
# `actual_*` / `*_result` columns (same game_type the user is betting).

# Result the leg produces (matches the *pick / *result semantics).
# Leg kinds per sport, normalized to these tags:
LEG_ML = "ml"          # moneyline: pick home|away
LEG_SPREAD = "spread"  # ATS / run line: picks a side + line
LEG_TOTAL = "total"    # over/under


def normalize_leg_kind(sport: str, raw_kind: str) -> str:
    """Map sport-specific kind strings to canonical LEG_* tags."""
    k = (raw_kind or "").strip().lower()
    if k in ("ml", "moneyline"):
        return LEG_ML
    if k in ("spread", "ats", "runline", "run_line", "rl"):
        return LEG_SPREAD
    if k in ("total", "ou", "overunder", "over_under"):
        return LEG_TOTAL
    raise ValueError(f"Unknown leg kind for {sport}: {raw_kind!r}")


def _same_team(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True if two legs come from the same game (not just same team)."""
    return bool(a.get("game_id") and a.get("game_id") == b.get("game_id"))


def _ml_aligns(a: str, b: str) -> bool:
    """True if two ML picks point at the same side (home/home or away/away)."""
    return bool(a and b and a == b)


def _opposing_sides(side_pick_a: Optional[str], side_pick_b: Optional[str]) -> bool:
    """True if two side (ML/spread) legs pick opposite teams, ignoring line."""
# ---------------------------------------------------------------------------
# Correlation: structural block + empirical same-game correlation
# ---------------------------------------------------------------------------
# V2 (2026-08-28): the V1 heuristic warning "favorite-ML + Over / underdog-ML +
# Under correlate" was DISPROVEN by real settled data (computed joint-hit rates;
# all ~0.00-0.02 corr). We now rely on:
#   (1) a STRUCTURAL block for same-game ML + spread on the same team
#       (mechanically near-duplicate — same game result drives both) and
#   (2) the EMPIRICAL same-game correlation table (sport.correlations) for all
#       other same-game pairs (ML+total, spread+total). The endpoint supplies
#       this table as {<pair_key>: corr}; ~0 => no warning, |corr| >= CORR_WARN
#       => a data-driven correlation note.

def _same_team(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True if two legs come from the same game (not just same team)."""
    return bool(a.get("game_id") and a.get("game_id") == b.get("game_id"))


def _opposing_sides(side_pick_a: Optional[str], side_pick_b: Optional[str]) -> bool:
    """True if two side (ML/spread) legs pick the SAME team (not just same
    side) — used for the near-duplicate block. Compares the team token only."""
    if not side_pick_a or not side_pick_b:
        return False
    a = side_pick_a.split()[0].lower()
    b = side_pick_b.split()[0].lower()
    return a == b


CORR_WARN_THRESHOLD = 0.02   # |corr| >= this => show a correlation note
CORR_STRONG_THRESHOLD = 0.05  # |corr| >= this => "strong" wording


def _pair_keys(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    """Return the correlation-table pair keys for same-game legs a & b.

    ML legs are categorized as ml_fav / ml_dog using favoriteness; totals as
    total_over / total_under; spreads as spread. Returns both canonical
    orderings so the lookup is order-agnostic.
    """
    favorite_side = (a.get("favorite_side") or "").lower()
    keys = []
    for leg in (a, b):
        k = leg.get("kind")
        if k == LEG_ML:
            side = (leg.get("side") or "").lower()
            key = "ml_fav" if (favorite_side and side == favorite_side) else "ml_dog"
        elif k == LEG_TOTAL:
            tot = (leg.get("pick") or "").lower()
            key = "total_over" if tot == "over" else "total_under"
        else:  # spread
            key = "spread"
        keys.append(key)
    return [f"{keys[0]}:{keys[1]}", f"{keys[1]}:{keys[0]}"]


def correlation_effects(
    a: Dict[str, Any],
    b: Dict[str, Any],
    correlations: Dict[str, Dict[str, Any]] | None = None,
) -> Tuple[bool, Optional[str]]:
    """Return (is_block, message_or_None) for the same-game pair a & b.

    - Same-game ML + spread on the SAME team  -> (True, structural block)
    - Same-game pair with an empirical corr above threshold -> (False, note)
    - Otherwise -> (False, None) — treated as independent.
    """
    if not _same_team(a, b):
        return False, None

    ka = a.get("kind")
    kb = b.get("kind")

    # 1) Structural near-duplicate: ML + spread on the same team.
    if {ka, kb} == {LEG_ML, LEG_SPREAD}:
        ml = a if ka == LEG_ML else b
        sp = b if kb == LEG_SPREAD else a
        if _opposing_sides(ml.get("side"), sp.get("side")):
            return True, "ML + spread on the same team are near-duplicates (blocked)"

    # 2) Empirical same-game correlation for all other same-game pairs.
    if correlations:
        for pk in _pair_keys(a, b):
            rec = correlations.get(pk)
            if rec:
                corr = rec.get("corr") or 0.0
                n = rec.get("n") or 0
                if abs(corr) >= CORR_WARN_THRESHOLD and n >= 30:
                    direction = "correlated" if corr > 0 else "negatively correlated"
                    strength = "strongly" if abs(corr) >= CORR_STRONG_THRESHOLD else "mildly"
                    return False, (
                        f"same-game {direction} ({strength}): historical joint-hit "
                        f"offers {abs(corr)*100:.1f}pp vs independence (n={n}) "
                        f"— fair price may understate the true vig"
                    )
                break  # found the pair, no need to try the reversed key

    return False, None


# ---------------------------------------------------------------------------
# Correlated-leg flags on a pair of legs (single-leg vs multi-game)
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    """One leg of a parlay, normalized to a common shape for the math."""
    game_id: Any
    sport: str
    kind: str                 # LEG_ML | LEG_SPREAD | LEG_TOTAL
    label: str                # human label, e.g. "LAD ML (-174)"
    pick: str                 # "home"/"away" for ML; "Over"/"Under" for total; side for spread
    side: Optional[str]       # team abbr/name for ML & spread legs
    prob: float = 0.50           # model probability of this leg winning (0..1)
    odds: Optional[float] = None # American odds the book pays for this leg
    ev: Optional[float] = None   # per-leg EV on $100 stake (dollar), if known
    model_file: Optional[str] = None
    is_calibrated: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def decimal(self) -> float:
        return american_to_decimal(self.odds or 100.0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "sport": self.sport,
            "kind": self.kind,
            "label": self.label,
            "pick": self.pick,
            "side": self.side,
            "prob": self.prob,
            "odds": self.odds,
            "decimal": round(self.decimal, 4),
            "ev": self.ev,
            "model_file": self.model_file,
            "is_calibrated": self.is_calibrated,
            "meta": self.meta,
        }


@dataclass
class ParlayResult:
    legs: List[Leg]
    n_legs: int

    fair_probability: float     # p1 * p2 * ... * pn (independent)
    fair_decimal: float         # 1 / fair_probability
    fair_american: int          # fair_decimal as American odds
    book_decimal: float         # odds1 * odds2 * ... * oddsn (what book pays)
    book_american: int          # book_decimal as American odds
    combined_implied: float     # 1 / book_decimal
    vig_drag: float             # combined_implied - fair_probability (book's compounded edge)
    ev_pct: float               # (book_decimal / fair_decimal - 1) * 100
    ev_dollars: float           # EV on $100 stake
    correlation_warnings: List[str] = field(default_factory=list)
    correlation_blocks: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_legs": self.n_legs,
            "fair_probability": round(self.fair_probability, 6),
            "fair_decimal": round(self.fair_decimal, 4),
            "fair_american": self.fair_american,
            "book_decimal": round(self.book_decimal, 4),
            "book_american": self.book_american,
            "combined_implied": round(self.combined_implied, 6),
            "vig_drag": round(self.vig_drag, 6),
            "ev_pct": round(self.ev_pct, 2),
            "ev_dollars": round(self.ev_dollars, 2),
            "correlation_warnings": self.correlation_warnings,
            "correlation_blocks": self.correlation_blocks,
            "legs": [l.as_dict() for l in self.legs],
        }


def _safe_prob(p: Any) -> float:
    """Clamp a model probability to a sane (0,1) open range for odds math."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.50
    if p is None or p != p:  # None or NaN
        return 0.50
    return max(0.01, min(0.99, p))


def combine(
    legs: List[Leg],
    correlations: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ParlayResult:
    """Combine legs into a parlay ticket with correlation flags.

    Assumes independent legs for the base fair probability, then surfaces
    correlation blocks (same-game ML + spread, structural) and data-driven
    correlation notes for same-game pairs using the empirical `correlations`
    table ({<pair_key>: {corr, n}}).
    """
    # Block same-game near-duplicate legs before math.
    blocks: List[str] = []
    warnings: List[str] = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            effects = correlation_effects(
                {
                    "game_id": legs[i].game_id,
                    "kind": legs[i].kind,
                    "pick": legs[i].pick,
                    "side": legs[i].side,
                    "favorite_side": legs[i].meta.get("favorite_side"),
                },
                {
                    "game_id": legs[j].game_id,
                    "kind": legs[j].kind,
                    "pick": legs[j].pick,
                    "side": legs[j].side,
                    "favorite_side": legs[j].meta.get("favorite_side"),
                },
                correlations=correlations,
            )
            if effects and effects[1]:
                is_block, reason = effects
                if is_block:
                    blocks.append(reason)
                else:
                    warnings.append(reason)

    # Independent fair probability.
    probs = [_safe_prob(l.prob) for l in legs]
    fair_prob = 1.0
    for p in probs:
        fair_prob *= p
    fair_prob = max(1e-6, fair_prob)
    fair_decimal = 1.0 / fair_prob

    # What the book actually pays (product of decimal odds of each leg).
    book_decimal = 1.0
    for l in legs:
        book_decimal *= l.decimal

    combined_implied = 1.0 / book_decimal if book_decimal > 0 else 1.0
    vig_drag = combined_implied - fair_prob
    ev_pct = (book_decimal / fair_decimal - 1.0) * 100.0
    ev_dollars = (book_decimal / fair_decimal - 1.0) * 100.0  # same as ev_pct on $100

    return ParlayResult(
        legs=legs,
        n_legs=len(legs),
        fair_probability=fair_prob,
        fair_decimal=fair_decimal,
        fair_american=decimal_to_american(fair_decimal),
        book_decimal=book_decimal,
        book_american=decimal_to_american(book_decimal),
        combined_implied=combined_implied,
        vig_drag=vig_drag,
        ev_pct=ev_pct,
        ev_dollars=ev_dollars,
        correlation_warnings=warnings,
        correlation_blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Best-EV combo search
# --------------------------------------------------------------------------
# Finds the highest-EV cross-game parlays from a pool of legs. Only positive-EV
# legs are eligible (a negative-EV leg can't be 'value' on its own), and we
# never combine two legs from the same game (same-game pairs are correlated;
# the builder's combo suggestions are the independent cross-game ones).
#
# The pool is capped so the combinatorial search stays fast for a single
# request; results are sorted by EV%.


def top_ev_combos(
    legs: List[Leg],
    n: int = 5,
    max_legs: int = 4,
    min_ev: float = 0.0,
    max_pool: int = 26,
    max_combos: int = 25000,
) -> List[Dict[str, Any]]:
    """Return the top-`n` cross-game parlays by EV% from `legs`.

    - Only legs with positive per-leg EV are candidates (drives the search to
      genuinely positive-EV tickets).
    - At most one leg per game per combo (creates clean independent combos).
    - Each combo has betwen 2 and `max_legs` legs.
    - Pool is capped to `max_pool` legs (top by per-leg EV) and enumeration to
      `max_combos` to bound runtime.
    - Only strictly-EV combos (>= `min_ev`, default 0) are returned.
    """
    eligible = [l for l in legs if l.ev is not None and l.ev > 0]
    if len(eligible) < 2:
        return []
    # cap the pool by per-leg EV (richest value first)
    eligible.sort(key=lambda l: l.ev or 0.0, reverse=True)
    pool = eligible[:max_pool]

    # group legs by game so we never pick two from the same game
    by_game: Dict[Any, List[Leg]] = {}
    for l in pool:
        by_game.setdefault(l.game_id, []).append(l)

    candidates: List[Leg] = []  # order in which enumerate over unique games
    used_games: List[Any] = []
    for l in pool:
        if l.game_id in used_games:
            continue
        used_games.append(l.game_id)
        candidates.append(l)
    per_game = {l.game_id: by_game[l.game_id] for l in candidates}

    results: List[Dict[str, Any]] = []
    seen: set = set()
    count = 0

    def _try_combo(chosen: List[Leg]) -> bool:
        """Record a combo; return False if the limit is exceeded (stop search)."""
        nonlocal count
        count += 1
        if count > max_combos:
            return False
        if len(chosen) < 2:
            return True
        key = tuple(sorted(l.label for l in chosen))
        if key in seen:
            return True
        seen.add(key)
        res = combine(chosen)
        if res.ev_pct >= min_ev and not res.correlation_blocks:
            results.append({
                "legs": [l.as_dict() for l in chosen],
                "n_legs": len(chosen),
                "fair_probability": res.fair_probability,
                "fair_american": res.fair_american,
                "book_decimal": round(res.book_decimal, 3),
                "book_american": res.book_american,
                "ev_pct": round(res.ev_pct, 1),
                "ev_dollars": round(res.ev_dollars, 2),
                "vig_drag": round(res.vig_drag, 4),
            })
        return True

    # enumerate combos of size 2..max_legs over the (distinct-game) candidate
    # legs. DFS guaranteeing at most one leg per game: at each step, only pick
    # legs from games that come AFTER the current candidate's game in ordering
    # so we never mix two legs of the same game.
    def _dfs(start: int, chosen: List[Leg]) -> None:
        if len(chosen) >= 2:
            if not _try_combo(chosen):
                return
        if len(chosen) >= max_legs:
            return
        for i in range(start, len(candidates)):
            leg = candidates[i]
            gid = leg.game_id
            # try every alternate leg from this same game (only one per game,
            # but alternate which leg of the game we pick)
            for alt in per_game[gid]:
                _dfs(i + 1, chosen + [alt])

    _dfs(0, [])

    results.sort(key=lambda r: r["ev_pct"], reverse=True)
    return results[:n]
