# GameHub

GameHub is a FastAPI web app for publishing, moderating, and launching browser games.

## Features

- Public catalog with search, categories, tags, and game pages.
- Developer panel for uploading HTML5 and Unity WebGL builds.
- Moderation flow for new games and published-game revisions.
- Admin panel for users, games, categories, and tags.
- Launch counters and player launch history.

## Tech Stack

- Python 3.10+
- FastAPI
- SQLAlchemy
- Jinja2 templates
- Redis
- PostgreSQL

## Local Setup

1. Create and activate a virtual environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies.

```powershell
pip install -r requirements.txt
```

3. Create local environment variables.

```powershell
Copy-Item .env.example .env
```

Update `.env` with your database, Redis, and account settings.

4. Seed base users and categories.

```powershell
python -m app.seed
```

5. Run the app.

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Tests

Install development dependencies first.

```powershell
pip install -r requirements-dev.txt
```

```powershell
pytest
```

## Deploy To Render

The project includes `render.yaml`, so Render can create a free web service and a free PostgreSQL database from a GitHub repository.

1. Push the project to GitHub. Do not commit `.env`, `.pytest_cache`, virtual environments, local database files, logs, or uploaded user content.
2. In Render, create a new Blueprint or Web Service from the GitHub repository.
3. If you create the service manually, use these commands:

```powershell
pip install -r requirements.txt
```

```powershell
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

4. Set environment variables in Render:

- `DATABASE_URL`: Render PostgreSQL internal connection string.
- `SECRET_KEY`: long random secret.
- `COOKIE_SECURE`: `true` on Render.
- `AUTO_SEED_ON_STARTUP`: `true` on Render if shell access is unavailable.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD`: initial administrator credentials.
- `MODERATOR_EMAIL` and `MODERATOR_PASSWORD`: initial moderator credentials.
- `REDIS_URL`: optional. Leave empty on the free setup if you do not use Render Key Value.

5. On the free Render plan, keep `AUTO_SEED_ON_STARTUP=true`. The app will create or update the administrator, moderator, and categories during startup. If shell access is available, you can also run seed manually:

```powershell
python -m app.seed
```

6. Check the main pages: `/`, `/register`, `/login`, `/developer`, `/moderation`, `/admin`, and `/healthz`.

### Render Free Limitations

- Free web services sleep after inactivity, so the first request after a pause can be slow.
- Free PostgreSQL databases are temporary on Render. Use a paid database or another persistent PostgreSQL provider for a long-lived public demo.
- Free Render web services do not provide persistent disks. Files uploaded to `app/static/uploads` are suitable only for temporary demonstrations and can disappear after a redeploy or restart. For a real public site, move uploads to S3-compatible storage, Supabase Storage, Cloudinary, Cloudflare R2, or a paid Render Disk.

## GitHub Notes

The repository intentionally ignores local secrets, virtual environments, caches, database files, logs, and uploaded user content. Keep `.env` private and publish only `.env.example`.
