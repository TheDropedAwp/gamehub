import io
import hmac
import re
import secrets
import shutil
import zipfile
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis
from fastapi import (
    FastAPI,
    Request,
    Depends,
    Form,
    HTTPException,
    status,
    UploadFile,
    File,
)
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db, settings
from app.models import (
    User,
    UserRole,
    Game,
    GameStatus,
    RevisionStatus,
    GameRevision,
    GamePlay,
    GameReview,
    Category,
    Tag,
)
from app.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
UPLOADS_DIR = STATIC_DIR / "uploads"
COVERS_DIR = UPLOADS_DIR / "covers"
AVATARS_DIR = UPLOADS_DIR / "avatars"
GAMES_DIR = UPLOADS_DIR / "games"
CSRF_COOKIE_NAME = "csrf_token"
MAX_UNCOMPRESSED_BUILD_SIZE = 750 * 1024 * 1024

for folder in [STATIC_DIR, TEMPLATES_DIR, UPLOADS_DIR, COVERS_DIR, AVATARS_DIR, GAMES_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


app = FastAPI(title=settings.APP_NAME)

Base.metadata.create_all(bind=engine)


@app.on_event("startup")
def seed_on_startup():
    if not settings.AUTO_SEED_ON_STARTUP:
        return

    if not settings.ADMIN_PASSWORD or not settings.MODERATOR_PASSWORD:
        print(
            "AUTO_SEED_ON_STARTUP is enabled, but ADMIN_PASSWORD or "
            "MODERATOR_PASSWORD is not set. Skipping seed."
        )
        return

    from app.seed import run_seed

    try:
        run_seed()
    except Exception as error:
        print(f"Startup seed failed: {error}")

class UnityStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)

        lower_path = path.lower()
        original_path = lower_path

        if lower_path.endswith(".br"):
            response.headers["Content-Encoding"] = "br"
            response.headers["Vary"] = "Accept-Encoding"
            original_path = lower_path[:-3]

        elif lower_path.endswith(".gz"):
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
            original_path = lower_path[:-3]

        if original_path.endswith(".js"):
            response.headers["Content-Type"] = "application/javascript"

        elif original_path.endswith(".wasm"):
            response.headers["Content-Type"] = "application/wasm"

        elif original_path.endswith(".data"):
            response.headers["Content-Type"] = "application/octet-stream"

        elif original_path.endswith(".json"):
            response.headers["Content-Type"] = "application/json"

        return response


app.mount("/static", UnityStaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def get_redis_client():
    if not settings.REDIS_URL:
        return None

    try:
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


redis_client = get_redis_client()


def clear_games_cache():
    if redis_client:
        redis_client.delete("published_games")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-zа-яё0-9]+", "-", text, flags=re.IGNORECASE)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "game"


def make_unique_slug(db: Session, title: str) -> str:
    base = slugify(title)
    slug = base
    counter = 2

    while db.query(Game).filter(Game.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1

    return slug


def parse_tags(tags_text: str) -> list[str]:
    raw_tags = re.split(r"[,\n;#]+", tags_text)
    result = []

    for tag in raw_tags:
        tag = tag.strip().lower()

        if not tag:
            continue

        tag = re.sub(r"\s+", "-", tag)

        if tag not in result:
            result.append(tag)

    return result[:15]
def tags_to_text(tag_names: list[str]) -> str:
    return ",".join(tag_names[:15])


def split_tags(tags_text: str) -> list[str]:
    if not tags_text:
        return []

    return [item.strip() for item in tags_text.split(",") if item.strip()]


def category_ids_to_csv(category_ids: list[int]) -> str:
    return ",".join(str(item) for item in category_ids[:2])


def csv_to_category_ids(value: str) -> list[int]:
    if not value:
        return []

    result = []

    for part in value.split(","):
        part = part.strip()

        if part.isdigit():
            result.append(int(part))

    return result[:2]


def categories_from_csv(db: Session, value: str) -> list[Category]:
    ids = csv_to_category_ids(value)

    if not ids:
        return []

    return db.query(Category).filter(Category.id.in_(ids)).all()
templates.env.globals["split_tags"] = split_tags
templates.env.globals["categories_from_csv"] = categories_from_csv

def get_latest_edit_revision(db: Session, game_id: int) -> GameRevision | None:
    return (
        db.query(GameRevision)
        .filter(
            GameRevision.game_id == game_id,
            GameRevision.status.in_([RevisionStatus.DRAFT, RevisionStatus.REJECTED]),
        )
        .order_by(GameRevision.updated_at.desc())
        .first()
    )


def get_waiting_revision(db: Session, game_id: int) -> GameRevision | None:
    return (
        db.query(GameRevision)
        .filter(
            GameRevision.game_id == game_id,
            GameRevision.status == RevisionStatus.WAITING,
        )
        .order_by(GameRevision.updated_at.desc())
        .first()
    )

def get_or_create_tag(db: Session, name: str) -> Tag:
    tag = db.query(Tag).filter(Tag.name == name).first()

    if tag:
        return tag

    tag = Tag(name=name)
    db.add(tag)
    db.flush()
    return tag


def validate_zip_members(zip_file: zipfile.ZipFile):
    forbidden_extensions = {
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".py",
        ".php",
        ".dll",
        ".msi",
    }

    total_uncompressed_size = 0

    for member in zip_file.infolist():
        filename = member.filename.replace("\\", "/")

        if filename.startswith("/") or ".." in Path(filename).parts:
            raise ValueError("В архиве найден небезопасный путь.")

        suffix = Path(filename).suffix.lower()

        if suffix in forbidden_extensions:
            raise ValueError(f"В архиве запрещён файл: {filename}")

        if not member.is_dir():
            total_uncompressed_size += member.file_size

        if total_uncompressed_size > MAX_UNCOMPRESSED_BUILD_SIZE:
            raise ValueError("Распакованный билд слишком большой. Максимум 750 МБ.")


def find_index_html(game_folder: Path) -> Path | None:
    for file in game_folder.rglob("index.html"):
        return file

    return None


async def save_image_file(
    image: UploadFile | None,
    folder: Path,
    public_prefix: str,
    filename_base: str,
    required: bool = False,
) -> str:
    if not image or not image.filename:
        if required:
            raise ValueError("Нужно загрузить изображение.")
        return ""

    ext = Path(image.filename).suffix.lower()

    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        raise ValueError("Изображение должно быть JPG, PNG или WEBP.")

    content = await image.read()

    if len(content) > 5 * 1024 * 1024:
        raise ValueError("Изображение слишком большое. Максимум 5 МБ.")

    folder.mkdir(parents=True, exist_ok=True)

    path = folder / f"{filename_base}{ext}"
    path.write_bytes(content)

    return f"{public_prefix}/{filename_base}{ext}"


async def save_cover_file(cover: UploadFile | None, slug: str) -> str:
    return await save_image_file(
        image=cover,
        folder=COVERS_DIR,
        public_prefix="/static/uploads/covers",
        filename_base=slug,
        required=False,
    )


async def save_avatar_file(avatar: UploadFile | None, user_id: int) -> str:
    return await save_image_file(
        image=avatar,
        folder=AVATARS_DIR,
        public_prefix="/static/uploads/avatars",
        filename_base=f"user-{user_id}",
        required=False,
    )


async def extract_game_build(
    build_file: UploadFile,
    folder_name: str,
    required: bool = True,
) -> str:
    if not build_file or not build_file.filename:
        if required:
            raise ValueError("Нужно загрузить архив с игрой.")
        return ""

    if Path(build_file.filename).suffix.lower() != ".zip":
        raise ValueError("Билд игры нужно загрузить в формате ZIP.")

    archive_bytes = await build_file.read()

    if len(archive_bytes) > 500 * 1024 * 1024:
        raise ValueError("Архив слишком большой. Максимум 500 МБ.")

    game_folder = GAMES_DIR / folder_name

    if game_folder.exists():
        shutil.rmtree(game_folder)

    game_folder.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zip_file:
            validate_zip_members(zip_file)
            zip_file.extractall(game_folder)

    except zipfile.BadZipFile:
        shutil.rmtree(game_folder, ignore_errors=True)
        raise ValueError("Архив повреждён или не является ZIP-файлом.")

    except ValueError:
        shutil.rmtree(game_folder, ignore_errors=True)
        raise

    except Exception as error:
        shutil.rmtree(game_folder, ignore_errors=True)
        raise ValueError(f"Не удалось распаковать архив: {error}")

    index_file = find_index_html(game_folder)

    if not index_file:
        shutil.rmtree(game_folder, ignore_errors=True)
        raise ValueError("В архиве не найден index.html.")

    relative_index = index_file.relative_to(STATIC_DIR).as_posix()

    return f"/static/{relative_index}"


def get_user_by_token(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get("access_token")

    if not token:
        return None

    user_id = decode_access_token(token)

    if not user_id:
        return None

    user = db.query(User).filter(User.id == int(user_id)).first()

    return user


def get_or_create_csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE_NAME)

    if token:
        return token

    return secrets.token_urlsafe(32)


async def verify_csrf_token(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    submitted_token = request.headers.get("x-csrf-token")

    if not submitted_token:
        form = await request.form()
        submitted_token = form.get("csrf_token")

    if not cookie_token or not submitted_token:
        raise HTTPException(status_code=403, detail="CSRF token missing")

    if not hmac.compare_digest(str(cookie_token), str(submitted_token)):
        raise HTTPException(status_code=403, detail="CSRF token invalid")


def set_auth_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def delete_auth_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(
        "access_token",
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user = get_user_by_token(request, db)

    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    return user


def require_roles(*roles: UserRole):
    def dependency(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user

    return dependency


def render(
    request: Request,
    template_name: str,
    context: dict,
    db: Session,
):
    current_user = get_user_by_token(request, db)
    csrf_token = get_or_create_csrf_token(request)

    base_context = {
        "request": request,
        "current_user": current_user,
        "app_name": settings.APP_NAME,
        "UserRole": UserRole,
        "GameStatus": GameStatus,
        "csrf_token": csrf_token,
    }

    base_context.update(context)

    response = templates.TemplateResponse(request, template_name, base_context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


def attach_review_stats(db: Session, games: list[Game]) -> None:
    if not games:
        return

    game_ids = [game.id for game in games]
    rows = (
        db.query(
            GameReview.game_id,
            func.avg(GameReview.rating),
            func.count(GameReview.id),
        )
        .filter(GameReview.game_id.in_(game_ids))
        .group_by(GameReview.game_id)
        .all()
    )
    stats = {
        game_id: (float(average or 0), int(count))
        for game_id, average, count in rows
    }

    for game in games:
        average, count = stats.get(game.id, (0.0, 0))
        game.average_rating = average
        game.reviews_count = count


def get_review_stats(db: Session, game_id: int) -> tuple[float, int]:
    average, count = (
        db.query(func.avg(GameReview.rating), func.count(GameReview.id))
        .filter(GameReview.game_id == game_id)
        .one()
    )
    return float(average or 0), int(count)


def render_game_detail(
    request: Request,
    db: Session,
    game: Game,
    review_error: str | None = None,
):
    current_user = get_user_by_token(request, db)
    average_rating, reviews_count = get_review_stats(db, game.id)
    reviews = (
        db.query(GameReview)
        .filter(GameReview.game_id == game.id)
        .order_by(GameReview.created_at.desc())
        .all()
    )
    user_review = None

    if current_user:
        user_review = (
            db.query(GameReview)
            .filter(
                GameReview.game_id == game.id,
                GameReview.user_id == current_user.id,
            )
            .first()
        )

    return render(
        request,
        "game.html",
        {
            "game": game,
            "reviews": reviews,
            "average_rating": average_rating,
            "reviews_count": reviews_count,
            "user_review": user_review,
            "review_error": review_error,
        },
        db,
    )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    category_id: int | None = None,
):
    categories = db.query(Category).order_by(Category.name.asc()).all()

    query = db.query(Game).filter(Game.status == GameStatus.PUBLISHED)

    if q:
        search = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Game.title.ilike(search),
                Game.description.ilike(search),
                Game.tags.any(Tag.name.ilike(search)),
                Game.categories.any(Category.name.ilike(search)),
            )
        )

    if category_id:
        query = query.filter(Game.categories.any(Category.id == category_id))

    games = query.order_by(Game.created_at.desc()).all()
    attach_review_stats(db, games)
    top_game = (
        db.query(Game)
        .filter(Game.status == GameStatus.PUBLISHED)
        .order_by(Game.plays_count.desc(), Game.created_at.desc())
        .first()
    )

    return render(
        request,
        "index.html",
        {
            "games": games,
            "top_game": top_game,
            "categories": categories,
            "q": q,
            "category_id": category_id,
        },
        db,
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "register.html", {"error": None}, db)


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    username = username.strip()

    if len(password) < 6:
        return render(request, "register.html", {"error": "Пароль должен быть не короче 6 символов."}, db)

    if db.query(User).filter(User.email == email).first():
        return render(request, "register.html", {"error": "Пользователь с такой почтой уже существует."}, db)

    if db.query(User).filter(User.username == username).first():
        return render(request, "register.html", {"error": "Такой логин уже занят."}, db)

    user = User(
        email=email,
        username=username,
        hashed_password=hash_password(password),
        role=UserRole.USER,
        is_active=True,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(str(user.id))

    response = RedirectResponse(url="/profile", status_code=status.HTTP_302_FOUND)
    set_auth_cookie(response, token)

    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "login.html", {"error": None}, db)


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    if not user or not verify_password(password, user.hashed_password):
        return render(request, "login.html", {"error": "Неверная почта или пароль."}, db)

    if not user.is_active:
        return render(request, "login.html", {"error": "Аккаунт заблокирован."}, db)

    token = create_access_token(str(user.id))

    response = RedirectResponse(url="/profile", status_code=status.HTTP_302_FOUND)
    set_auth_cookie(response, token)

    return response


@app.post("/logout")
def logout(csrf: None = Depends(verify_csrf_token)):
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    delete_auth_cookie(response)
    return response


@app.get("/profile", response_class=HTMLResponse)
def profile(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    published_games = (
        db.query(Game)
        .filter(
            Game.developer_id == user.id,
            Game.status == GameStatus.PUBLISHED,
        )
        .order_by(Game.created_at.desc())
        .all()
    )

    played_games = (
        db.query(GamePlay)
        .filter(GamePlay.user_id == user.id)
        .order_by(GamePlay.last_played_at.desc())
        .all()
    )

    return render(
        request,
        "profile.html",
        {
            "user": user,
            "published_games": published_games,
            "played_games": played_games,
        },
        db,
    )

@app.post("/profile/avatar")
async def update_profile_avatar(
    avatar: UploadFile = File(None),
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        avatar_path = await save_avatar_file(avatar, user.id)

    except ValueError:
        return RedirectResponse(url="/profile", status_code=status.HTTP_302_FOUND)

    if avatar_path:
        user.avatar_path = avatar_path
        db.commit()

    return RedirectResponse(url="/profile", status_code=status.HTTP_302_FOUND)

@app.post("/become-developer")
def become_developer(
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role == UserRole.USER:
        user.role = UserRole.DEVELOPER
        db.commit()

    return RedirectResponse(url="/developer", status_code=status.HTTP_302_FOUND)


@app.get("/developer", response_class=HTMLResponse)
def developer_panel(
    request: Request,
    user: User = Depends(require_roles(UserRole.DEVELOPER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    my_games = (
        db.query(Game)
        .filter(Game.developer_id == user.id)
        .order_by(Game.created_at.desc())
        .all()
    )

    return render(
        request,
        "developer.html",
        {
            "my_games": my_games,
        },
        db,
    )


@app.get("/developer/games/new", response_class=HTMLResponse)
def developer_new_game_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.DEVELOPER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    categories = db.query(Category).order_by(Category.name.asc()).all()

    return render(
        request,
        "developer_game_form.html",
        {
            "categories": categories,
            "error": None,
        },
        db,
    )


@app.post("/developer/games/new")
async def developer_new_game(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category_ids: list[int] = Form(default=[]),
    tags: str = Form(""),
    cover: UploadFile = File(None),
    build_zip: UploadFile = File(None),
    submit_action: str = Form("submit"),
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.DEVELOPER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    categories = db.query(Category).order_by(Category.name.asc()).all()

    title = title.strip()
    description = description.strip()
    tag_names = parse_tags(tags)
    is_draft = submit_action == "draft"

    if len(title) < 2:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Название слишком короткое.",
                "game": None,
                "revision": None,
            },
            db,
        )

    if len(category_ids) == 0:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Выбери хотя бы одну категорию.",
                "game": None,
                "revision": None,
            },
            db,
        )

    if len(category_ids) > 2:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Можно выбрать максимум две категории.",
                "game": None,
                "revision": None,
            },
            db,
        )

    selected_categories = db.query(Category).filter(Category.id.in_(category_ids)).all()

    if len(selected_categories) != len(category_ids):
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Некоторые категории не найдены.",
                "game": None,
                "revision": None,
            },
            db,
        )

    slug = make_unique_slug(db, title)

    try:
        cover_path = await save_cover_file(cover, slug)

        entry_path = await extract_game_build(
            build_zip,
            folder_name=slug,
            required=not is_draft,
        )

    except ValueError as error:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": str(error),
                "game": None,
                "revision": None,
            },
            db,
        )

    game = Game(
        title=title,
        slug=slug,
        description=description,
        cover_path=cover_path,
        entry_path=entry_path,
        status=GameStatus.DRAFT if is_draft else GameStatus.WAITING,
        developer_id=user.id,
        moderation_comment="",
    )

    game.categories = selected_categories
    game.tags = [get_or_create_tag(db, tag_name) for tag_name in tag_names]

    db.add(game)
    db.commit()

    clear_games_cache()

    return RedirectResponse(url="/developer", status_code=status.HTTP_302_FOUND)


@app.get("/games/{slug}", response_class=HTMLResponse)
def game_page(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.slug == slug).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    current_user = get_user_by_token(request, db)

    can_view_unpublished = (
        current_user
        and (
            current_user.role in [UserRole.MODERATOR, UserRole.ADMIN]
            or game.developer_id == current_user.id
        )
    )

    if game.status != GameStatus.PUBLISHED and not can_view_unpublished:
        raise HTTPException(status_code=403, detail="Игра недоступна")

    if game.status == GameStatus.BLOCKED and not (
        current_user and current_user.role == UserRole.ADMIN
    ):
        raise HTTPException(status_code=403, detail="Игра заблокирована")

    if game.status == GameStatus.PUBLISHED:
        game.plays_count += 1

    if current_user:
        gameplay = (
            db.query(GamePlay)
            .filter(
                GamePlay.user_id == current_user.id,
                GamePlay.game_id == game.id,
            )
            .first()
        )

        if gameplay:
            gameplay.launches += 1
            gameplay.last_played_at = datetime.utcnow()
        else:
            gameplay = GamePlay(
                user_id=current_user.id,
                game_id=game.id,
                launches=1,
                last_played_at=datetime.utcnow(),
            )
            db.add(gameplay)

    db.commit()

    return render_game_detail(request, db, game)


@app.post("/games/{slug}/reviews")
def submit_game_review(
    slug: str,
    request: Request,
    rating: int = Form(...),
    text: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    db: Session = Depends(get_db),
):
    current_user = get_user_by_token(request, db)

    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    game = db.query(Game).filter(Game.slug == slug).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    if game.status != GameStatus.PUBLISHED:
        raise HTTPException(status_code=403, detail="Отзывы доступны только для опубликованных игр")

    text = text.strip()

    if rating < 1 or rating > 5:
        return render_game_detail(request, db, game, "Выберите оценку от 1 до 5.")

    if not text:
        return render_game_detail(request, db, game, "Напишите текст отзыва.")

    if len(text) > 1200:
        return render_game_detail(request, db, game, "Отзыв должен быть не длиннее 1200 символов.")

    review = (
        db.query(GameReview)
        .filter(
            GameReview.user_id == current_user.id,
            GameReview.game_id == game.id,
        )
        .first()
    )

    if review:
        review.rating = rating
        review.text = text
    else:
        review = GameReview(
            user_id=current_user.id,
            game_id=game.id,
            rating=rating,
            text=text,
        )
        db.add(review)

    db.commit()

    return RedirectResponse(url=f"/games/{game.slug}#reviews", status_code=status.HTTP_302_FOUND)


@app.get("/moderation", response_class=HTMLResponse)
def moderation_panel(
    request: Request,
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    games = (
        db.query(Game)
        .filter(Game.status == GameStatus.WAITING)
        .order_by(Game.created_at.asc())
        .all()
    )

    revisions = (
        db.query(GameRevision)
        .filter(GameRevision.status == RevisionStatus.WAITING)
        .order_by(GameRevision.created_at.asc())
        .all()
    )

    return render(
        request,
        "moderation.html",
        {
            "games": games,
            "revisions": revisions,
        },
        db,
    )


@app.get("/moderation/games/{game_id}", response_class=HTMLResponse)
def moderation_game_detail(
    game_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    return render(request, "moderation_detail.html", {"game": game}, db)


@app.post("/moderation/games/{game_id}/publish")
def moderation_publish_game(
    game_id: int,
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    if game.status != GameStatus.WAITING:
        raise HTTPException(status_code=400, detail="Игра не находится на модерации")

    game.status = GameStatus.PUBLISHED
    game.moderation_comment = "Игра одобрена модератором."

    db.commit()
    clear_games_cache()

    return RedirectResponse(url="/moderation", status_code=status.HTTP_302_FOUND)

@app.get("/moderation/revisions/{revision_id}", response_class=HTMLResponse)
def moderation_revision_detail(
    revision_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    revision = db.query(GameRevision).filter(GameRevision.id == revision_id).first()

    if not revision:
        raise HTTPException(status_code=404, detail="Обновление не найдено")

    return render(
        request,
        "moderation_revision_detail.html",
        {
            "revision": revision,
            "game": revision.game,
        },
        db,
    )


@app.post("/moderation/revisions/{revision_id}/publish")
def moderation_publish_revision(
    revision_id: int,
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    revision = db.query(GameRevision).filter(GameRevision.id == revision_id).first()

    if not revision:
        raise HTTPException(status_code=404, detail="Обновление не найдено")

    if revision.status != RevisionStatus.WAITING:
        raise HTTPException(status_code=400, detail="Обновление не находится на модерации")

    game = revision.game

    game.title = revision.title
    game.description = revision.description
    game.cover_path = revision.cover_path
    game.entry_path = revision.entry_path
    game.status = GameStatus.PUBLISHED
    game.moderation_comment = "Обновление одобрено модератором."

    category_ids = csv_to_category_ids(revision.category_ids_csv)
    game.categories = db.query(Category).filter(Category.id.in_(category_ids)).all()

    tag_names = split_tags(revision.tags_text)
    game.tags = [get_or_create_tag(db, tag_name) for tag_name in tag_names]

    revision.status = RevisionStatus.APPROVED
    revision.moderation_comment = "Обновление опубликовано."

    db.commit()
    clear_games_cache()

    return RedirectResponse(url="/moderation", status_code=status.HTTP_302_FOUND)


@app.post("/moderation/revisions/{revision_id}/reject")
def moderation_reject_revision(
    revision_id: int,
    moderation_comment: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    revision = db.query(GameRevision).filter(GameRevision.id == revision_id).first()

    if not revision:
        raise HTTPException(status_code=404, detail="Обновление не найдено")

    if revision.status != RevisionStatus.WAITING:
        raise HTTPException(status_code=400, detail="Обновление не находится на модерации")

    revision.status = RevisionStatus.REJECTED
    revision.moderation_comment = moderation_comment.strip() or "Обновление отклонено."

    db.commit()

    return RedirectResponse(url="/moderation", status_code=status.HTTP_302_FOUND)

@app.post("/moderation/games/{game_id}/reject")
def moderation_reject_game(
    game_id: int,
    moderation_comment: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.MODERATOR, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    if game.status != GameStatus.WAITING:
        raise HTTPException(status_code=400, detail="Игра не находится на модерации")

    game.status = GameStatus.REJECTED
    game.moderation_comment = moderation_comment.strip() or "Игра отклонена."

    db.commit()
    clear_games_cache()

    return RedirectResponse(url="/moderation", status_code=status.HTTP_302_FOUND)


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(
    request: Request,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
    user_q: str = "",
    game_q: str = "",
):
    users_query = db.query(User)
    games_query = db.query(Game)

    if user_q.strip():
        search = f"%{user_q.strip()}%"
        users_query = users_query.filter(
            or_(
                User.username.ilike(search),
                User.email.ilike(search),
            )
        )

    if game_q.strip():
        search = f"%{game_q.strip()}%"
        games_query = games_query.filter(
            or_(
                Game.title.ilike(search),
                Game.description.ilike(search),
            )
        )

    users = users_query.order_by(User.created_at.desc()).all()
    games = games_query.order_by(Game.created_at.desc()).all()
    categories = db.query(Category).order_by(Category.name.asc()).all()

    return render(
        request,
        "admin.html",
        {
            "users": users,
            "games": games,
            "categories": categories,
            "roles": list(UserRole),
            "user_q": user_q,
            "game_q": game_q,
        },
        db,
    )


@app.post("/admin/users/{user_id}/role")
def admin_change_user_role(
    user_id: int,
    role: UserRole = Form(...),
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    user.role = role
    db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)


@app.post("/admin/users/{user_id}/toggle-active")
def admin_toggle_user_active(
    user_id: int,
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if user.id == admin.id:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    user.is_active = not user.is_active
    db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)


@app.post("/admin/categories/add")
def admin_add_category(
    name: str = Form(...),
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    name = name.strip()

    if name and not db.query(Category).filter(Category.name == name).first():
        db.add(Category(name=name))
        db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)


@app.post("/admin/categories/{category_id}/delete")
def admin_delete_category(
    category_id: int,
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    category = db.query(Category).filter(Category.id == category_id).first()

    if category and len(category.games) == 0:
        db.delete(category)
        db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)


@app.post("/admin/games/{game_id}/block")
def admin_block_game(
    game_id: int,
    admin_comment: str = Form(""),
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    game.status = GameStatus.BLOCKED
    game.admin_comment = admin_comment.strip() or "Игра заблокирована администратором."

    db.commit()
    clear_games_cache()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)


@app.post("/admin/games/{game_id}/unblock")
def admin_unblock_game(
    game_id: int,
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    game.status = GameStatus.WAITING
    game.admin_comment = ""

    db.commit()
    clear_games_cache()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.get("/developer/games/{game_id}/edit", response_class=HTMLResponse)
def developer_edit_game_page(
    game_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.DEVELOPER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    if user.role != UserRole.ADMIN and game.developer_id != user.id:
        raise HTTPException(status_code=403, detail="Это не ваша игра")

    waiting_revision = get_waiting_revision(db, game.id)

    if game.status == GameStatus.WAITING or waiting_revision:
        return render(
            request,
            "developer_game_locked.html",
            {
                "game": game,
                "waiting_revision": waiting_revision,
            },
            db,
        )

    categories = db.query(Category).order_by(Category.name.asc()).all()

    revision = None

    if game.status == GameStatus.PUBLISHED:
        revision = get_latest_edit_revision(db, game.id)

    return render(
        request,
        "developer_game_form.html",
        {
            "categories": categories,
            "error": None,
            "game": game,
            "revision": revision,
        },
        db,
    )


@app.post("/developer/games/{game_id}/edit")
async def developer_edit_game(
    game_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category_ids: list[int] = Form(default=[]),
    tags: str = Form(""),
    submit_action: str = Form("submit"),
    cover: UploadFile = File(None),
    build_zip: UploadFile = File(None),
    csrf: None = Depends(verify_csrf_token),
    user: User = Depends(require_roles(UserRole.DEVELOPER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    if user.role != UserRole.ADMIN and game.developer_id != user.id:
        raise HTTPException(status_code=403, detail="Это не ваша игра")

    waiting_revision = get_waiting_revision(db, game.id)

    if game.status == GameStatus.WAITING or waiting_revision:
        raise HTTPException(
            status_code=403,
            detail="Нельзя редактировать игру, пока версия находится на модерации.",
        )

    categories = db.query(Category).order_by(Category.name.asc()).all()

    title = title.strip()
    description = description.strip()
    tag_names = parse_tags(tags)

    if len(title) < 2:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Название слишком короткое.",
                "game": game,
                "revision": get_latest_edit_revision(db, game.id),
            },
            db,
        )

    if len(category_ids) == 0:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Выбери хотя бы одну категорию.",
                "game": game,
                "revision": get_latest_edit_revision(db, game.id),
            },
            db,
        )

    if len(category_ids) > 2:
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Можно выбрать максимум две категории.",
                "game": game,
                "revision": get_latest_edit_revision(db, game.id),
            },
            db,
        )

    selected_categories = db.query(Category).filter(Category.id.in_(category_ids)).all()

    if len(selected_categories) != len(category_ids):
        return render(
            request,
            "developer_game_form.html",
            {
                "categories": categories,
                "error": "Некоторые категории не найдены.",
                "game": game,
                "revision": get_latest_edit_revision(db, game.id),
            },
            db,
        )

    is_draft = submit_action == "draft"

    # Если игра ещё не опубликована, редактируем саму Game.
    if game.status in [GameStatus.DRAFT, GameStatus.REJECTED]:
        try:
            new_cover_path = await save_cover_file(cover, game.slug)

            new_entry_path = await extract_game_build(
                build_zip,
                folder_name=game.slug,
                required=not bool(game.entry_path) and not is_draft,
            )

        except ValueError as error:
            return render(
                request,
                "developer_game_form.html",
                {
                    "categories": categories,
                    "error": str(error),
                    "game": game,
                    "revision": None,
                },
                db,
            )

        game.title = title
        game.description = description

        if new_cover_path:
            game.cover_path = new_cover_path

        if new_entry_path:
            game.entry_path = new_entry_path

        game.status = GameStatus.DRAFT if is_draft else GameStatus.WAITING
        game.moderation_comment = ""

        game.categories = selected_categories
        game.tags = [get_or_create_tag(db, tag_name) for tag_name in tag_names]

        db.commit()
        clear_games_cache()

        return RedirectResponse(url="/developer", status_code=status.HTTP_302_FOUND)

    # Если игра опубликована, создаём/обновляем отдельную ревизию.
    if game.status == GameStatus.PUBLISHED:
        revision = get_latest_edit_revision(db, game.id)

        if not revision:
            revision = GameRevision(
                game_id=game.id,
                title=game.title,
                description=game.description,
                cover_path=game.cover_path,
                entry_path=game.entry_path,
                category_ids_csv=category_ids_to_csv([item.id for item in game.categories]),
                tags_text=tags_to_text([item.name for item in game.tags]),
                status=RevisionStatus.DRAFT,
            )

            db.add(revision)
            db.flush()

        folder_name = f"{game.slug}-revision-{revision.id}"

        try:
            new_cover_path = await save_image_file(
                image=cover,
                folder=COVERS_DIR,
                public_prefix="/static/uploads/covers",
                filename_base=f"{game.slug}-revision-{revision.id}",
                required=False,
            )

            new_entry_path = await extract_game_build(
                build_zip,
                folder_name=folder_name,
                required=False,
            )

        except ValueError as error:
            return render(
                request,
                "developer_game_form.html",
                {
                    "categories": categories,
                    "error": str(error),
                    "game": game,
                    "revision": revision,
                },
                db,
            )

        revision.title = title
        revision.description = description
        revision.category_ids_csv = category_ids_to_csv(category_ids)
        revision.tags_text = tags_to_text(tag_names)

        if new_cover_path:
            revision.cover_path = new_cover_path

        if new_entry_path:
            revision.entry_path = new_entry_path

        revision.status = RevisionStatus.DRAFT if is_draft else RevisionStatus.WAITING
        revision.moderation_comment = ""

        db.commit()

        return RedirectResponse(url="/developer", status_code=status.HTTP_302_FOUND)

    return RedirectResponse(url="/developer", status_code=status.HTTP_302_FOUND)

@app.post("/admin/games/{game_id}/delete")
def admin_delete_game(
    game_id: int,
    csrf: None = Depends(verify_csrf_token),
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    game = db.query(Game).filter(Game.id == game_id).first()

    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    game_folder = GAMES_DIR / game.slug

    if game_folder.exists():
        shutil.rmtree(game_folder, ignore_errors=True)

    if game.cover_path:
        cover_file = STATIC_DIR / game.cover_path.replace("/static/", "")

        if cover_file.exists():
            cover_file.unlink()

    db.delete(game)
    db.commit()

    clear_games_cache()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
