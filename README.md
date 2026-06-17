# EarlKnowFootball.com

Full-stack NFL analysis platform — fantasy football advice, game handicapping, team articles, and AI-powered chat.

## Quick Start

```bash
# 1. Start PostgreSQL
sudo docker compose up -d db

# 2. Install Python deps
cd backend
pip install -r requirements.txt

# 3. Set up .env (copy from .env.example)
cp .env.example .env
# Edit .env with your actual API keys

# 4. Run the app
uvicorn app.main:app --reload --port 8001
```

The API will be available at http://localhost:8001
