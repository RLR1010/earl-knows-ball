FROM earl-knows-football-api:latest
RUN sed -i 's|connect_args={"options": "-c search_path=nfl,public"}|connect_args={"options": "-c search_path='\''"'"'nfl, public"'"'\''"}|' /app/app/database.py && rm -rf /app/app/__pycache__ /app/app/routers/__pycache__ 2>/dev/null; find /app -name '*.pyc' -delete 2>/dev/null; echo "FIXED"
