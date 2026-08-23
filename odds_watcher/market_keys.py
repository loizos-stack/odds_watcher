"""Candidate market keys for The Odds API.

That provider returns only the featured markets (h2h, spreads, totals) unless
specific keys are named in the request, and it has no endpoint listing which
keys exist. So the keys cannot be discovered by sampling a response the way
they can on odds-api.io — they have to be asked for by name.

These lists are the documented keys per sport. Which of them your plan and
your bookmakers actually return is a separate question, and one the
`markets --discover` command answers by requesting them.
"""

from __future__ import annotations

# Game-level markets beyond the featured three.
EXTRA_GAME_MARKETS = {
    "baseball_mlb": (
        "h2h_1st_5_innings",
        "spreads_1st_5_innings",
        "totals_1st_5_innings",
        "team_totals",
        "alternate_spreads",
        "alternate_totals",
    ),
    "basketball_nba": ("team_totals", "alternate_spreads", "alternate_totals", "h2h_q1", "totals_q1"),
    "americanfootball_nfl": ("team_totals", "alternate_spreads", "alternate_totals", "h2h_h1", "totals_h1"),
    "icehockey_nhl": ("team_totals", "alternate_spreads", "alternate_totals"),
    # Soccer keys are shared across every soccer_* competition.
    "soccer": (
        "btts",
        "draw_no_bet",
        "double_chance",
        "team_totals",
        "alternate_spreads",
        "alternate_totals",
        "totals_h1",
        "h2h_h1",
        "spreads_h1",
    ),
}

# Player prop keys, which are only available from the per-event endpoint.
PROP_MARKETS = {
    "baseball_mlb": (
        "batter_home_runs",
        "batter_hits",
        "batter_total_bases",
        "batter_rbis",
        "batter_runs_scored",
        "batter_hits_runs_rbis",
        "batter_singles",
        "batter_doubles",
        "batter_triples",
        "batter_walks",
        "batter_strikeouts",
        "batter_stolen_bases",
        "pitcher_strikeouts",
        "pitcher_hits_allowed",
        "pitcher_walks",
        "pitcher_earned_runs",
        "pitcher_outs",
        "pitcher_record_a_win",
        "batter_home_runs_alternate",
        "batter_hits_alternate",
        "batter_total_bases_alternate",
        "pitcher_strikeouts_alternate",
    ),
    "basketball_nba": (
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_blocks",
        "player_steals",
        "player_turnovers",
        "player_points_rebounds_assists",
        "player_double_double",
    ),
    "americanfootball_nfl": (
        "player_pass_yds",
        "player_pass_tds",
        "player_rush_yds",
        "player_reception_yds",
        "player_receptions",
        "player_anytime_td",
    ),
    "icehockey_nhl": (
        "player_points",
        "player_goals",
        "player_assists",
        "player_shots_on_goal",
        "player_total_saves",
    ),
    "soccer": (
        "player_goal_scorer_anytime",
        "player_first_goal_scorer",
        "player_last_goal_scorer",
        "player_to_receive_card",
        "player_to_receive_red_card",
        "player_shots_on_target",
        "player_shots",
        "player_assists",
    ),
}


def _lookup(table: dict, sport: str) -> tuple:
    """Exact key first, then the family prefix.

    Soccer competitions each have their own sport key (soccer_epl,
    soccer_spain_la_liga, ...) but share one set of market keys, so a prefix
    entry covers every league without listing them.
    """
    if sport in table:
        return tuple(table[sport])
    family = sport.split("_", 1)[0]
    return tuple(table.get(family, ()))


def candidates(sport: str) -> tuple:
    """Every non-featured key worth trying for a sport."""
    return _lookup(EXTRA_GAME_MARKETS, sport) + _lookup(PROP_MARKETS, sport)


def known_sports() -> tuple:
    return tuple(sorted(set(EXTRA_GAME_MARKETS) | set(PROP_MARKETS)))
