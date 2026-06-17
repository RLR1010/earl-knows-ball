from sqlalchemy import Column, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class PlayerWeeklyStats(Base):
    __tablename__ = "player_weekly_stats"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("nfl.players.id"), nullable=False, index=True)
    game_id = Column(Integer, ForeignKey("nfl.games.id"), nullable=True)
    season_id = Column(Integer, ForeignKey("nfl.seasons.id"), nullable=False)
    week = Column(Integer, nullable=False)
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False)
    opponent_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False)

    # Passing
    pass_attempts = Column(Integer, default=0)
    pass_completions = Column(Integer, default=0)
    pass_yards = Column(Float, default=0.0)
    pass_tds = Column(Integer, default=0)
    pass_int = Column(Integer, default=0)
    passer_rating = Column(Float, nullable=True)

    # Rushing
    rush_attempts = Column(Integer, default=0)
    rush_yards = Column(Float, default=0.0)
    rush_tds = Column(Integer, default=0)
    rush_long = Column(Integer, nullable=True)

    # Receiving
    targets = Column(Integer, default=0)
    receptions = Column(Integer, default=0)
    receiving_yards = Column(Float, default=0.0)
    receiving_tds = Column(Integer, default=0)
    receiving_long = Column(Integer, nullable=True)

    # Fumbles
    fumbles = Column(Integer, default=0)
    fumbles_lost = Column(Integer, default=0)

    # Defense / Special Teams (for DST)
    sacks = Column(Float, default=0.0)
    interceptions = Column(Integer, default=0)
    fumbles_recovered = Column(Integer, default=0)
    defensive_tds = Column(Integer, default=0)
    special_teams_tds = Column(Integer, default=0)
    points_allowed = Column(Integer, nullable=True)
    yards_allowed = Column(Integer, nullable=True)

    # Kicking
    field_goals_made = Column(Integer, default=0)
    field_goals_attempted = Column(Integer, default=0)
    extra_points_made = Column(Integer, default=0)
    extra_points_attempted = Column(Integer, default=0)

    # Fantasy points
    fantasy_points_ppr = Column(Float, default=0.0)
    fantasy_points_half = Column(Float, default=0.0)
    fantasy_points_std = Column(Float, default=0.0)
    fantasy_points_dk = Column(Float, default=0.0)  # DraftKings scoring

    # Snaps
    snaps_offense = Column(Integer, nullable=True)
    snaps_defense = Column(Integer, nullable=True)
    snaps_special = Column(Integer, nullable=True)

    player = relationship("Player", backref="weekly_stats")
    game = relationship("Game", backref="player_stats")
    team = relationship("Team", foreign_keys=[team_id])
    opponent = relationship("Team", foreign_keys=[opponent_id])

    __table_args__ = (
        UniqueConstraint("player_id", "game_id", name="uq_player_game"),
        {"schema": "nfl"},
    )
