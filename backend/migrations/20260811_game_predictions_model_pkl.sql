-- 20260811: Add model provenance columns to game_predictions across all
-- sport schemas (mlb, nfl, nba).
--
-- Stores the exact pkl model file used for the ATS/run-line pick and the O/U
-- pick so every prediction is auditable and we can detect if live and backtest
-- were ever fed from different model files.
--
-- Columns are NULL until backfilled / recorded on the next prediction run.
ALTER TABLE mlb.game_predictions
    ADD COLUMN IF NOT EXISTS ats_model_file VARCHAR(255),
    ADD COLUMN IF NOT EXISTS ou_model_file   VARCHAR(255);

ALTER TABLE nfl.game_predictions
    ADD COLUMN IF NOT EXISTS ats_model_file VARCHAR(255),
    ADD COLUMN IF NOT EXISTS ou_model_file   VARCHAR(255);

ALTER TABLE nba.game_predictions
    ADD COLUMN IF NOT EXISTS ats_model_file VARCHAR(255),
    ADD COLUMN IF NOT EXISTS ou_model_file   VARCHAR(255);

COMMENT ON COLUMN mlb.game_predictions.ats_model_file IS
    'Filename of the pkl model used for the ATS/run-line pick';
COMMENT ON COLUMN mlb.game_predictions.ou_model_file IS
    'Filename of the pkl model used for the O/U pick';
COMMENT ON COLUMN nfl.game_predictions.ats_model_file IS
    'Filename of the pkl model used for the ATS/spread pick';
COMMENT ON COLUMN nfl.game_predictions.ou_model_file IS
    'Filename of the pkl model used for the O/U pick';
COMMENT ON COLUMN nba.game_predictions.ats_model_file IS
    'Filename of the pkl model used for the ATS/spread pick';
COMMENT ON COLUMN nba.game_predictions.ou_model_file IS
    'Filename of the pkl model used for the O/U pick';
