-- Migration: Register QB cumulative and rolling features in nfl.features
-- All features: is_trainable=true, pick_card=true
-- current_ats/current_ou/live_ats/live_ou = false

DO $$
DECLARE
    next_id INTEGER;
BEGIN
    SELECT COALESCE(MAX(id), 0) + 1 INTO next_id FROM nfl.features;

    -- ============================================================
    -- HOME/AWAY ROLLING 5-GAME QB FEATURES
    -- ============================================================

    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_passer_rating_5',   'QB Rtg 5G H',   'Home QB passer rating over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_passer_rating_5',   'QB Rtg 5G A',   'Away QB passer rating over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_any_a_5',           'QB ANY/A 5G H',  'Home QB adjusted net yards per attempt over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_any_a_5',           'QB ANY/A 5G A',  'Away QB adjusted net yards per attempt over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_ypa_5',             'QB YPA 5G H',    'Home QB yards per pass attempt over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_ypa_5',             'QB YPA 5G A',    'Away QB yards per pass attempt over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_td_pct_5',          'QB TD% 5G H',    'Home QB touchdown percentage over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_td_pct_5',          'QB TD% 5G A',    'Away QB touchdown percentage over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_int_pct_5',         'QB INT% 5G H',   'Home QB interception percentage over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_int_pct_5',         'QB INT% 5G A',   'Away QB interception percentage over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_sack_rate_5',       'QB SCK% 5G H',   'Home QB sack rate over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_sack_rate_5',       'QB SCK% 5G A',   'Away QB sack rate over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_rush_ypg_5',        'QB RuYPG 5G H',  'Home QB rushing yards per game over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_rush_ypg_5',        'QB RuYPG 5G A',  'Away QB rushing yards per game over last 5 games', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_rush_att_5',        'QB RuAtt 5G H',  'Home QB rush attempts over last 5 games (total)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_rush_att_5',        'QB RuAtt 5G A',  'Away QB rush attempts over last 5 games (total)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_games_5',           'QB Gms 5G H',     'Games played by home QB in last 5 (reliability indicator)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_games_5',           'QB Gms 5G A',     'Games played by away QB in last 5 (reliability indicator)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;

    -- ============================================================
    -- HOME/AWAY SEASON-LONG (CUMULATIVE) QB FEATURES
    -- ============================================================

    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_passer_rating_season',  'QB Rtg Seas H',  'Home QB passer rating YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_passer_rating_season',  'QB Rtg Seas A',  'Away QB passer rating YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_any_a_season',          'QB ANY/A Seas H', 'Home QB adjusted net yards per attempt YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_any_a_season',          'QB ANY/A Seas A', 'Away QB adjusted net yards per attempt YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_ypa_season',            'QB YPA Seas H',   'Home QB yards per pass attempt YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_ypa_season',            'QB YPA Seas A',   'Away QB yards per pass attempt YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_td_pct_season',         'QB TD% Seas H',   'Home QB touchdown percentage YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_td_pct_season',         'QB TD% Seas A',   'Away QB touchdown percentage YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_int_pct_season',        'QB INT% Seas H',  'Home QB interception percentage YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_int_pct_season',        'QB INT% Seas A',  'Away QB interception percentage YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_sack_rate_season',      'QB SCK% Seas H',  'Home QB sack rate YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_sack_rate_season',      'QB SCK% Seas A',  'Away QB sack rate YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_rush_ypg_season',       'QB RuYPG Seas H', 'Home QB rushing yards per game YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_rush_ypg_season',       'QB RuYPG Seas A', 'Away QB rushing yards per game YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_rush_att_pg_season',    'QB RuAtt/G Seas H', 'Home QB rush attempts per game YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_rush_att_pg_season',    'QB RuAtt/G Seas A', 'Away QB rush attempts per game YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_games_season',          'QB GP Seas H',  'Games played by home QB this season (experience)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_games_season',          'QB GP Seas A',  'Games played by away QB this season (experience)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;

    -- ============================================================
    -- QB DIFFERENTIAL FEATURES (computed: home - away)
    -- ============================================================

    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'qb_passer_rating_5_diff',        'QB Rtg Diff 5G',     'Home minus away QB passer rating (last 5)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'qb_any_a_5_diff',                'QB ANY/A Diff 5G',   'Home minus away QB ANY/A (last 5)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'qb_passer_rating_season_diff',   'QB Rtg Diff Seas',   'Home minus away QB passer rating YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'qb_any_a_season_diff',           'QB ANY/A Diff Seas', 'Home minus away QB ANY/A YTD', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;

    -- ============================================================
    -- QB TREND FEATURES (computed: recent 5-game minus season average)
    -- ============================================================

    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_passer_rating_trend',    'QB Rtg Trend H',     'Home QB recent-passing-rating minus season avg (hot/cold)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_passer_rating_trend',    'QB Rtg Trend A',     'Away QB recent-passing-rating minus season avg (hot/cold)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'home_qb_ypa_trend',              'QB YPA Trend H',     'Home QB recent YPA minus season avg YPA (hot/cold)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;
    INSERT INTO nfl.features (id, name, display_name, description, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou)
    VALUES
    (next_id,     'away_qb_ypa_trend',              'QB YPA Trend A',     'Away QB recent YPA minus season avg YPA (hot/cold)', TRUE, TRUE, FALSE, FALSE, FALSE, FALSE);
    next_id := next_id + 1;

END $$;
