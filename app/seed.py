from sqlalchemy.orm import Session

from app.database import SessionLocal, engine, Base, settings
from app.models import User, UserRole, Category
from app.security import hash_password


DEFAULT_CATEGORIES = [
    "Аркады",
    "Экшен",
    "Гонки",
    "Головоломки",
    "Стратегии",
    "Симуляторы",
    "Спорт",
    "Казуальные",
    "Платформеры",
    "Хоррор",
    "Для двоих",
    "Unity WebGL",
    "HTML5",
]


def get_or_create_user(
    db: Session,
    email: str,
    username: str,
    password: str,
    role: UserRole,
):
    user = db.query(User).filter(User.email == email).first()

    if user:
        user.username = username
        user.role = role
        user.is_active = True
        return user

    user = User(
        email=email,
        username=username,
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )

    db.add(user)
    return user


def get_or_create_category(db: Session, name: str):
    category = db.query(Category).filter(Category.name == name).first()

    if category:
        return category

    category = Category(name=name)
    db.add(category)
    return category


def run_seed():
    Base.metadata.create_all(bind=engine)

    if not settings.ADMIN_PASSWORD or not settings.MODERATOR_PASSWORD:
        raise RuntimeError("Set ADMIN_PASSWORD and MODERATOR_PASSWORD before running seed.")

    db = SessionLocal()

    try:
        get_or_create_user(
            db=db,
            email=settings.ADMIN_EMAIL,
            username="admin",
            password=settings.ADMIN_PASSWORD,
            role=UserRole.ADMIN,
        )

        get_or_create_user(
            db=db,
            email=settings.MODERATOR_EMAIL,
            username="moderator",
            password=settings.MODERATOR_PASSWORD,
            role=UserRole.MODERATOR,
        )

        for category_name in DEFAULT_CATEGORIES:
            get_or_create_category(db, category_name)

        db.commit()

        print("Seed completed.")
        print(f"Admin: {settings.ADMIN_EMAIL}")
        print(f"Moderator: {settings.MODERATOR_EMAIL}")

    finally:
        db.close()


if __name__ == "__main__":
    run_seed()
