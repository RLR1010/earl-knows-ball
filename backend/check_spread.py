import httpx, csv, io

resp = httpx.get('https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv', follow_redirects=True, timeout=60)
text = resp.text
reader = csv.DictReader(io.StringIO(text))

count = 0
for r in reader:
    if r['season'] == '2024' and r['week'] in ('1', '2', '10'):
        hs = r.get('home_score', '')
        aws = r.get('away_score', '')
        if hs and aws and r.get('spread_line'):
            spread = float(r['spread_line'])
            margin = int(hs) - int(aws)
            # If spread from home perspective, positive=home_favored:
            #   home covers if margin > spread (e.g., spread=6, margin=1 => no cover)
            cov1 = margin > spread
            # If spread from home perspective, negative=home_favored (standard):
            #   negative spread means home favored by that amount
            #   e.g., spread=-6 means BAL -6
            cov2 = margin > -spread  # treated as home favored by -spread
            # If spread from away perspective:
            #   positive means away favored
            cov3 = (margin > -spread) if spread > 0 else (margin > spread)
            
            print(f"{r['away_team']} @ {r['home_team']:4s}: spread={spread:+5.1f} margin={margin:+3d} "
                  f"cov_pos_home={cov1} cov_neg_home={cov2}")
            count += 1
            if count >= 25:
                break
