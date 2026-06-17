from sqlalchemy import Column, Integer, Float, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class MLBPitchingStats(Base):
    """Per-season pitching statistics for MLB players."""
    __tablename__ = "pitching_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_mlb_pitching_player_season"),
        {"schema": "mlb"},
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("mlb.players.id"), nullable=False, index=True)
    season_id = Column(Integer, ForeignKey("mlb.seasons.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("mlb.teams.id"), nullable=True)

    # Games
    games_played = Column(Integer, default=0)
    games_started = Column(Integer, default=0)
    games_finished = Column(Integer, default=0)
    complete_games = Column(Integer, default=0)
    shutouts = Column(Integer, default=0)

    # Innings
    innings_pitched = Column(Float, default=0.0)
    outs = Column(Integer, default=0)

    # Results
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    saves = Column(Integer, default=0)
    blown_saves = Column(Integer, default=0)
    save_opportunities = Column(Integer, default=0)
    holds = Column(Integer, default=0)
    win_percentage = Column(Float, nullable=True)

    # Batting against
    hits = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    earned_runs = Column(Integer, default=0)
    home_runs = Column(Integer, default=0)
    doubles = Column(Integer, default=0)
    triples = Column(Integer, default=0)
    at_bats = Column(Integer, default=0)
    batters_faced = Column(Integer, default=0)

    # Walks & strikeouts
    base_on_balls = Column(Integer, default=0)
    intentional_walks = Column(Integer, default=0)
    strikeouts = Column(Integer, default=0)
    hit_by_pitch = Column(Integer, default=0)

    # Rate stats
    era = Column(Float, nullable=True)  # Earned run average
    whip = Column(Float, nullable=True)  # Walks + hits / IP
    avg = Column(Float, nullable=True)  # Opponent batting average
    obp = Column(Float, nullable=True)  # Opponent on-base percentage
    slg = Column(Float, nullable=True)  # Opponent slugging percentage
    ops = Column(Float, nullable=True)  # Opponent OPS

    # Per-9 stats
    hits_per_9 = Column(Float, nullable=True)
    home_runs_per_9 = Column(Float, nullable=True)
    strikeouts_per_9 = Column(Float, nullable=True)
    walks_per_9 = Column(Float, nullable=True)
    strikeout_walk_ratio = Column(Float, nullable=True)

    # Other
    ground_outs = Column(Integer, default=0)
    air_outs = Column(Integer, default=0)
    ground_into_double_play = Column(Integer, default=0)
    wild_pitches = Column(Integer, default=0)
    balks = Column(Integer, default=0)
    pickoffs = Column(Integer, default=0)
    pitches_thrown = Column(Integer, default=0)
    strikes = Column(Integer, default=0)
    strike_percentage = Column(Float, nullable=True)
    pitches_per_inning = Column(Float, nullable=True)

    # Baserunning against
    stolen_bases = Column(Integer, default=0)
    caught_stealing = Column(Integer, default=0)
    caught_stealing_percentage = Column(Float, nullable=True)

    player = relationship("MLBPlayer", back_populates="pitching_stats")
    season = relationship("MLBSeason")
    team = relationship("MLBTeam")
