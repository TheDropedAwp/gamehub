import enum
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    Integer,
    DateTime,
    Boolean,
    ForeignKey,
    Enum,
    Table,
    Column,
    CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


game_categories = Table(
    "game_categories",
    Base.metadata,
    Column("game_id", ForeignKey("games.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)


game_tags = Table(
    "game_tags",
    Base.metadata,
    Column("game_id", ForeignKey("games.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class UserRole(str, enum.Enum):
    USER = "user"
    DEVELOPER = "developer"
    MODERATOR = "moderator"
    ADMIN = "admin"


class GameStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING = "waiting"
    PUBLISHED = "published"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class RevisionStatus(str, enum.Enum):
    DRAFT = "draft"
    WAITING = "waiting"
    REJECTED = "rejected"
    APPROVED = "approved"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    avatar_path: Mapped[str] = mapped_column(String(500), default="")

    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    games: Mapped[list["Game"]] = relationship(back_populates="developer")
    played_games: Mapped[list["GamePlay"]] = relationship(back_populates="user")
    reviews: Mapped[list["GameReview"]] = relationship(back_populates="user")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)

    games: Mapped[list["Game"]] = relationship(
        secondary=game_categories,
        back_populates="categories",
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)

    games: Mapped[list["Game"]] = relationship(
        secondary=game_tags,
        back_populates="tags",
    )


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)

    description: Mapped[str] = mapped_column(Text, default="")

    cover_path: Mapped[str] = mapped_column(String(500), default="")
    entry_path: Mapped[str] = mapped_column(String(500), default="")

    status: Mapped[GameStatus] = mapped_column(
        Enum(GameStatus),
        default=GameStatus.DRAFT,
        nullable=False,
    )

    moderation_comment: Mapped[str] = mapped_column(Text, default="")
    admin_comment: Mapped[str] = mapped_column(Text, default="")

    plays_count: Mapped[int] = mapped_column(default=0)

    developer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    developer: Mapped[User | None] = relationship(back_populates="games")

    categories: Mapped[list[Category]] = relationship(
        secondary=game_categories,
        back_populates="games",
    )

    tags: Mapped[list[Tag]] = relationship(
        secondary=game_tags,
        back_populates="games",
    )

    revisions: Mapped[list["GameRevision"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
    )

    played_by: Mapped[list["GamePlay"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
    )

    reviews: Mapped[list["GameReview"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameReview.created_at.desc()",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class GameRevision(Base):
    """
    Черновик/обновление игры.

    Если игра ещё не опубликована, можно редактировать саму Game.
    Если игра уже опубликована, новая версия идёт сюда.
    После одобрения модератором данные ревизии копируются в Game.
    """

    __tablename__ = "game_revisions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    game: Mapped[Game] = relationship(back_populates="revisions")

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    cover_path: Mapped[str] = mapped_column(String(500), default="")
    entry_path: Mapped[str] = mapped_column(String(500), default="")

    category_ids_csv: Mapped[str] = mapped_column(String(255), default="")
    tags_text: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[RevisionStatus] = mapped_column(
        Enum(RevisionStatus),
        default=RevisionStatus.DRAFT,
        nullable=False,
    )

    moderation_comment: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class GamePlay(Base):
    __tablename__ = "game_plays"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)

    launches: Mapped[int] = mapped_column(default=1)
    last_played_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="played_games")
    game: Mapped[Game] = relationship(back_populates="played_by")


class GameReview(Base):
    __tablename__ = "game_reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_game_reviews_rating_range"),
        UniqueConstraint("user_id", "game_id", name="uq_game_reviews_user_game"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user: Mapped[User] = relationship(back_populates="reviews")
    game: Mapped[Game] = relationship(back_populates="reviews")
