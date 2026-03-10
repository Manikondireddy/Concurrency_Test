import logging
from datetime import datetime, timedelta, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from .. import config as app_config
from ..config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET_KEY
from ..deps import get_db
from ..logging_utils import request_id_var

router = APIRouter(prefix="/auth", tags=["Auth"])
users_router = APIRouter(tags=["Users"])
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def _raise_db_error(action: str, exc: Exception) -> None:
    request_id = request_id_var.get()
    logger.exception("Database error request_id=%s action=%s exc=%s", request_id, action, exc)
    if app_config.IS_PRODUCTION:
        raise HTTPException(status_code=500, detail="Database operation failed")
    raise HTTPException(status_code=500, detail=f"Database operation failed (request_id={request_id}, exc={type(exc).__name__})")


async def _verify_password(plain_password: str, hashed_password: str) -> bool:
    # passlib/bcrypt is CPU-bound; keep the event loop responsive.
    return await anyio.to_thread.run_sync(pwd_context.verify, plain_password, hashed_password)


async def _hash_password(password: str) -> str:
    return await anyio.to_thread.run_sync(pwd_context.hash, password)


def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db, use_cache=False),
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_exception

    if credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise credentials_exception
    token = credentials.credentials

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        user_id_int = int(user_id)
    except (JWTError, ValueError):
        raise credentials_exception

    try:
        result = await db.execute(select(models.User).where(models.User.id == user_id_int))
        user = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        _raise_db_error("get_current_user", exc)

    if user is None:
        raise credentials_exception
    return user


async def _create_user(payload: schemas.UserRegisterRequest, db: AsyncSession) -> dict:
    try:
        existing = await db.execute(select(models.User).where(models.User.username == payload.username))
        if existing.scalar_one_or_none():
            logger.warning("Create user conflict username=%s", payload.username)
            raise HTTPException(status_code=409, detail="Username already exists")

        user = models.User(username=payload.username, password=await _hash_password(payload.password))
        db.add(user)
        await db.commit()
        await db.refresh(user)
    except HTTPException:
        raise
    except IntegrityError:
        await db.rollback()
        logger.warning("Create user constraint conflict username=%s", payload.username)
        raise HTTPException(status_code=409, detail="Username already exists")
    except SQLAlchemyError as exc:
        await db.rollback()
        _raise_db_error("create_user", exc)

    logger.info("Create user success user_id=%s username=%s", user.id, user.username)
    return {"id": user.id, "username": user.username}


@users_router.post("/users", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(payload: schemas.UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    logger.info("Create user requested username=%s", payload.username)
    return await _create_user(payload, db)


@router.post("/register", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: schemas.UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    logger.info("Register requested username=%s", payload.username)
    return await _create_user(payload, db)


@router.post("/login", response_model=schemas.TokenResponse)
async def login(payload: schemas.TokenRequest, db: AsyncSession = Depends(get_db)):
    logger.info("Login requested username=%s", payload.username)

    try:
        result = await db.execute(select(models.User).where(models.User.username == payload.username))
        user = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        _raise_db_error("login", exc)

    if not user or not await _verify_password(payload.password, user.password):
        logger.warning("Login failed username=%s", payload.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    token = create_access_token(user_id=user.id, username=user.username)
    logger.info("Login success token issued user_id=%s username=%s", user.id, user.username)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/token", response_model=schemas.TokenResponse, include_in_schema=False)
async def token(payload: schemas.TokenRequest, db: AsyncSession = Depends(get_db)):
    # Alias for compatibility with common JWT flows and older docs.
    return await login(payload, db)
