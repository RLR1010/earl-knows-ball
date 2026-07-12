"""
NFL play-by-play data model from nflverse.

Stores key columns from nflverse PBP parquet files for computing
boxscore stats like first downs, third/fourth down conversions,
and time of possession.
"""

from sqlalchemy import Column, BigInteger, Integer, Float, String, Text, UniqueConstraint
from app.database import Base


class PlayByPlay(Base):
    __tablename__ = "play_by_play"
    __table_args__ = (
        UniqueConstraint("old_game_id", "play_id", name="uq_pbp_game_play"),
        {"schema": "nfl"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    game_id = Column(String(50), nullable=False, index=True)
    old_game_id = Column(String(50), index=True)  # ESPN game ID
    season = Column(Integer)
    week = Column(Integer)
    season_type = Column(String(10))
    posteam = Column(String(5), index=True)
    defteam = Column(String(5), index=True)
    posteam_type = Column(String(5))
    down = Column(Integer)
    ydstogo = Column(Float)
    yardline_100 = Column(Float)
    play_type = Column(String(50))
    play_id = Column(BigInteger)
    drive = Column(Float)
    qtr = Column(Integer)
    first_down = Column(Integer)
    third_down_converted = Column(Integer)
    third_down_attempted = Column(Integer)
    fourth_down_converted = Column(Integer)
    fourth_down_attempted = Column(Integer)
    yards_gained = Column(Float)
    game_seconds_remaining = Column(Float)
    quarter_seconds_remaining = Column(Float)
    pass_attempt = Column(Integer)
    rush_attempt = Column(Integer)
    complete_pass = Column(Integer)
    interception = Column(Integer)
    fumble_lost = Column(Integer)
    touchdown = Column(Integer)
    scoring_play = Column(Integer)
    timeout = Column(Integer)
    timeout_team = Column(String(5))
    desc = Column("desc_text", Text)
