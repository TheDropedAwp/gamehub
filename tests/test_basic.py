from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Game, GameReview, GameStatus, User, UserRole
from app.security import create_access_token, hash_password


client = TestClient(app)


def test_home_page_available():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_register_page_available():
    response = client.get("/register")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_login_page_available():
    response = client.get("/login")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_developer_page_protected_without_auth():
    response = client.get("/developer", follow_redirects=False)

    assert response.status_code in [401, 403]


def test_authorized_user_can_review_game_and_catalog_shows_rating():
    db = SessionLocal()
    suffix = "review-test-game"
    email = f"{suffix}@example.test"

    try:
        db.query(GameReview).filter(GameReview.game.has(slug=suffix)).delete(synchronize_session=False)
        db.query(Game).filter(Game.slug == suffix).delete(synchronize_session=False)
        db.query(User).filter(User.email == email).delete(synchronize_session=False)
        db.commit()

        user = User(
            email=email,
            username=suffix,
            hashed_password=hash_password("password"),
            role=UserRole.USER,
            is_active=True,
        )
        game = Game(
            title="Review Test Game",
            slug=suffix,
            description="Game used for review tests.",
            status=GameStatus.PUBLISHED,
            entry_path="/static/test-game/index.html",
        )
        db.add_all([user, game])
        db.commit()
        db.refresh(user)

        token = create_access_token(str(user.id))
        client.cookies.set("access_token", token)
        csrf_response = client.get(f"/games/{suffix}")
        csrf_token = csrf_response.cookies.get("csrf_token")
        assert csrf_token

        response = client.post(
            f"/games/{suffix}/reviews",
            data={"rating": "4", "text": "Хорошая браузерная игра."},
            headers={"x-csrf-token": csrf_token},
            follow_redirects=False,
        )

        assert response.status_code == 302

        review = db.query(GameReview).filter(GameReview.game_id == game.id).one()
        assert review.rating == 4
        assert review.text == "Хорошая браузерная игра."

        catalog_response = client.get("/")
        assert "Review Test Game" in catalog_response.text
        assert "4.0" in catalog_response.text
    finally:
        db.query(GameReview).filter(GameReview.game.has(slug=suffix)).delete(synchronize_session=False)
        db.query(Game).filter(Game.slug == suffix).delete(synchronize_session=False)
        db.query(User).filter(User.email == email).delete(synchronize_session=False)
        db.commit()
        client.cookies.clear()
        db.close()
