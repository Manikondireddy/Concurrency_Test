import logging
from decimal import Decimal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from .. import config as app_config
from ..deps import get_db
from ..logging_utils import request_id_var
from .auth_routes import get_current_user

router = APIRouter(tags=["Wallet"])
logger = logging.getLogger(__name__)

MAX_OPTIMISTIC_RETRIES = 25


class _OptimisticConflict(Exception):
    pass


async def _optimistic_backoff(attempt: int) -> None:
    # Keep it short; we just want to reduce thundering-herd retries.
    delay_s = min(0.001 * (2**min(attempt, 6)), 0.05)
    await anyio.sleep(delay_s)


def _raise_db_error(action: str, exc: Exception) -> None:
    request_id = request_id_var.get()
    logger.exception("Database error request_id=%s action=%s exc=%s", request_id, action, exc)
    if app_config.IS_PRODUCTION:
        raise HTTPException(status_code=500, detail="Database operation failed")
    raise HTTPException(status_code=500, detail=f"Database operation failed (request_id={request_id}, exc={type(exc).__name__})")


def _wallet_not_found_for_user(user_id: int) -> HTTPException:
    logger.warning("Wallet not found for user_id=%s", user_id)
    return HTTPException(status_code=404, detail="Wallet not found for current user")


async def _get_wallet_row_by_id(db: AsyncSession, wallet_id: int):
    result = await db.execute(
        select(models.Wallet.id, models.Wallet.user_id, models.Wallet.balance, models.Wallet.version).where(
            models.Wallet.id == wallet_id
        )
    )
    return result.one_or_none()


def _wallet_not_authorized(wallet_id: int, user_id: int) -> HTTPException:
    logger.warning("Wallet not authorized wallet_id=%s user_id=%s", wallet_id, user_id)
    return HTTPException(status_code=403, detail="Not authorized for this wallet")


async def _resolve_wallet_id_for_user(db: AsyncSession, user_id: int) -> int | None:
    result = await db.execute(select(models.Wallet.id).where(models.Wallet.user_id == user_id))
    return result.scalar_one_or_none()


async def _credit_wallet_optimistic(
    db: AsyncSession,
    wallet_id: int,
    user_id: int,
    amount,
):
    wallet_row = None
    ledger_row = None

    for attempt in range(1, MAX_OPTIMISTIC_RETRIES + 1):
        try:
            async with db.begin():
                current = await _get_wallet_row_by_id(db, wallet_id)
                if not current:
                    raise HTTPException(status_code=404, detail="Wallet not found")

                wid, owner_id, _balance, version = current
                if owner_id != user_id:
                    raise _wallet_not_authorized(wallet_id, user_id)

                wallet_update = await db.execute(
                    update(models.Wallet)
                    .where(models.Wallet.id == wid, models.Wallet.version == version)
                    .values(balance=models.Wallet.balance + amount, version=version + 1)
                    .returning(models.Wallet.id, models.Wallet.balance, models.Wallet.version)
                )
                updated = wallet_update.one_or_none()
                if not updated:
                    raise _OptimisticConflict()

                wallet_row = updated
                ledger_insert = await db.execute(
                    insert(models.Ledger)
                    .values(wallet_id=wallet_row.id, amount=amount, type="credit")
                    .returning(
                        models.Ledger.id,
                        models.Ledger.wallet_id,
                        models.Ledger.amount,
                        models.Ledger.type,
                        models.Ledger.created_at,
                    )
                )
                ledger_row = ledger_insert.one()
            break
        except _OptimisticConflict:
            logger.info("Optimistic conflict on credit wallet_id=%s user_id=%s attempt=%s", wallet_id, user_id, attempt)
            await _optimistic_backoff(attempt)
            continue

    if wallet_row is None or ledger_row is None:
        raise HTTPException(status_code=409, detail="Concurrent update, please retry")

    return wallet_row, ledger_row


async def _debit_wallet_optimistic(
    db: AsyncSession,
    wallet_id: int,
    user_id: int,
    amount,
):
    wallet_row = None
    ledger_row = None

    for attempt in range(1, MAX_OPTIMISTIC_RETRIES + 1):
        try:
            async with db.begin():
                current = await _get_wallet_row_by_id(db, wallet_id)
                if not current:
                    raise HTTPException(status_code=404, detail="Wallet not found")

                wid, owner_id, balance, version = current
                if owner_id != user_id:
                    raise _wallet_not_authorized(wallet_id, user_id)

                if balance < amount:
                    logger.warning("Debit rejected insufficient funds wallet_id=%s user_id=%s amount=%s", wid, user_id, amount)
                    raise HTTPException(status_code=400, detail="Insufficient balance")

                wallet_update = await db.execute(
                    update(models.Wallet)
                    .where(
                        models.Wallet.id == wid,
                        models.Wallet.version == version,
                        models.Wallet.balance >= amount,
                    )
                    .values(balance=models.Wallet.balance - amount, version=version + 1)
                    .returning(models.Wallet.id, models.Wallet.balance, models.Wallet.version)
                )
                updated = wallet_update.one_or_none()
                if not updated:
                    raise _OptimisticConflict()

                wallet_row = updated
                ledger_insert = await db.execute(
                    insert(models.Ledger)
                    .values(wallet_id=wallet_row.id, amount=amount, type="debit")
                    .returning(
                        models.Ledger.id,
                        models.Ledger.wallet_id,
                        models.Ledger.amount,
                        models.Ledger.type,
                        models.Ledger.created_at,
                    )
                )
                ledger_row = ledger_insert.one()
            break
        except _OptimisticConflict:
            logger.info("Optimistic conflict on debit wallet_id=%s user_id=%s attempt=%s", wallet_id, user_id, attempt)
            await _optimistic_backoff(attempt)
            continue

    if wallet_row is None or ledger_row is None:
        raise HTTPException(status_code=409, detail="Concurrent update, please retry")

    return wallet_row, ledger_row


@router.post("/wallets", response_model=schemas.WalletResponse, status_code=status.HTTP_201_CREATED)
async def create_wallet(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Create wallet requested user_id=%s", current_user.id)

    try:
        wallet_result = await db.execute(select(models.Wallet).where(models.Wallet.user_id == current_user.id))
        existing_wallet = wallet_result.scalar_one_or_none()
        if existing_wallet:
            logger.info(
                "Create wallet idempotent hit user_id=%s existing_wallet_id=%s",
                current_user.id,
                existing_wallet.id,
            )
            response.status_code = status.HTTP_200_OK
            return {
                "wallet_id": existing_wallet.id,
                "user_id": existing_wallet.user_id,
                "balance": existing_wallet.balance,
            }

        wallet = models.Wallet(user_id=current_user.id, balance=Decimal("0.00"))
        db.add(wallet)
        await db.commit()
        await db.refresh(wallet)
    except IntegrityError:
        await db.rollback()
        wallet_result = await db.execute(select(models.Wallet).where(models.Wallet.user_id == current_user.id))
        existing_wallet = wallet_result.scalar_one_or_none()
        if existing_wallet:
            logger.info(
                "Create wallet idempotent race resolved user_id=%s existing_wallet_id=%s",
                current_user.id,
                existing_wallet.id,
            )
            response.status_code = status.HTTP_200_OK
            return {
                "wallet_id": existing_wallet.id,
                "user_id": existing_wallet.user_id,
                "balance": existing_wallet.balance,
            }
        raise HTTPException(status_code=409, detail="Wallet already exists for this user")
    except SQLAlchemyError as exc:
        await db.rollback()
        _raise_db_error("create_wallet", exc)

    logger.info("Wallet created wallet_id=%s user_id=%s", wallet.id, wallet.user_id)
    return {
        "wallet_id": wallet.id,
        "user_id": wallet.user_id,
        "balance": wallet.balance,
    }


@router.post("/wallets/me/credit", response_model=schemas.TransactionResponse)
async def credit_wallet(
    payload: schemas.TransactionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Credit requested amount=%s requester_user_id=%s", payload.amount, current_user.id)

    try:
        wallet_id = await _resolve_wallet_id_for_user(db, current_user.id)
        if wallet_id is None:
            raise _wallet_not_found_for_user(current_user.id)
        wallet_row, ledger_row = await _credit_wallet_optimistic(db, wallet_id, current_user.id, payload.amount)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("credit_wallet", exc)

    logger.info(
        "Credit success wallet_id=%s new_balance=%s ledger_id=%s",
        wallet_row.id,
        wallet_row.balance,
        ledger_row.id,
    )
    return {
        "wallet_id": wallet_row.id,
        "balance": wallet_row.balance,
        "ledger_entry": {
            "id": ledger_row.id,
            "wallet_id": ledger_row.wallet_id,
            "amount": ledger_row.amount,
            "type": ledger_row.type,
            "created_at": ledger_row.created_at,
        },
    }


@router.post("/wallets/me/debit", response_model=schemas.TransactionResponse)
async def debit_wallet(
    payload: schemas.TransactionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Debit requested amount=%s requester_user_id=%s", payload.amount, current_user.id)

    try:
        wallet_id = await _resolve_wallet_id_for_user(db, current_user.id)
        if wallet_id is None:
            raise _wallet_not_found_for_user(current_user.id)
        wallet_row, ledger_row = await _debit_wallet_optimistic(db, wallet_id, current_user.id, payload.amount)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("debit_wallet", exc)

    logger.info("Debit success wallet_id=%s new_balance=%s ledger_id=%s", wallet_row.id, wallet_row.balance, ledger_row.id)
    return {
        "wallet_id": wallet_row.id,
        "balance": wallet_row.balance,
        "ledger_entry": {
            "id": ledger_row.id,
            "wallet_id": ledger_row.wallet_id,
            "amount": ledger_row.amount,
            "type": ledger_row.type,
            "created_at": ledger_row.created_at,
        },
    }


@router.post("/wallets/{wallet_id}/credit", response_model=schemas.TransactionResponse)
async def credit_wallet_by_id(
    wallet_id: int,
    payload: schemas.TransactionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Credit requested amount=%s wallet_id=%s requester_user_id=%s", payload.amount, wallet_id, current_user.id)

    try:
        wallet_row, ledger_row = await _credit_wallet_optimistic(db, wallet_id, current_user.id, payload.amount)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("credit_wallet_by_id", exc)

    logger.info("Credit success wallet_id=%s new_balance=%s ledger_id=%s", wallet_row.id, wallet_row.balance, ledger_row.id)
    return {
        "wallet_id": wallet_row.id,
        "balance": wallet_row.balance,
        "ledger_entry": {
            "id": ledger_row.id,
            "wallet_id": ledger_row.wallet_id,
            "amount": ledger_row.amount,
            "type": ledger_row.type,
            "created_at": ledger_row.created_at,
        },
    }


@router.post("/wallets/{wallet_id}/debit", response_model=schemas.TransactionResponse)
async def debit_wallet_by_id(
    wallet_id: int,
    payload: schemas.TransactionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Debit requested amount=%s wallet_id=%s requester_user_id=%s", payload.amount, wallet_id, current_user.id)

    try:
        wallet_row, ledger_row = await _debit_wallet_optimistic(db, wallet_id, current_user.id, payload.amount)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("debit_wallet_by_id", exc)

    logger.info("Debit success wallet_id=%s new_balance=%s ledger_id=%s", wallet_row.id, wallet_row.balance, ledger_row.id)
    return {
        "wallet_id": wallet_row.id,
        "balance": wallet_row.balance,
        "ledger_entry": {
            "id": ledger_row.id,
            "wallet_id": ledger_row.wallet_id,
            "amount": ledger_row.amount,
            "type": ledger_row.type,
            "created_at": ledger_row.created_at,
        },
    }


@router.get("/wallets/me/balance", response_model=schemas.WalletBalanceResponse)
async def get_balance(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Balance requested requester_user_id=%s", current_user.id)

    try:
        wallet_id = await _resolve_wallet_id_for_user(db, current_user.id)
        if wallet_id is None:
            raise _wallet_not_found_for_user(current_user.id)
        result = await db.execute(select(models.Wallet).where(models.Wallet.id == wallet_id))
        wallet = result.scalar_one_or_none()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("get_balance", exc)

    logger.info("Balance response wallet_id=%s balance=%s", wallet.id, wallet.balance)
    return {"wallet_id": wallet.id, "user_id": wallet.user_id, "balance": wallet.balance}


@router.get("/wallets/{wallet_id}/balance", response_model=schemas.WalletBalanceResponse)
async def get_balance_by_id(
    wallet_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Balance requested wallet_id=%s requester_user_id=%s", wallet_id, current_user.id)

    try:
        result = await db.execute(select(models.Wallet).where(models.Wallet.id == wallet_id))
        wallet = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        _raise_db_error("get_balance_by_id", exc)

    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if wallet.user_id != current_user.id:
        raise _wallet_not_authorized(wallet_id, current_user.id)

    logger.info("Balance response wallet_id=%s balance=%s", wallet.id, wallet.balance)
    return {"wallet_id": wallet.id, "user_id": wallet.user_id, "balance": wallet.balance}


@router.get("/wallets/me/ledger", response_model=list[schemas.LedgerEntryResponse])
async def get_ledger(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info(
        "Ledger requested limit=%s offset=%s requester_user_id=%s",
        limit,
        offset,
        current_user.id,
    )

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    try:
        wallet_id = await _resolve_wallet_id_for_user(db, current_user.id)
        if wallet_id is None:
            raise _wallet_not_found_for_user(current_user.id)
        rows_result = await db.execute(
            select(models.Ledger)
            .where(models.Ledger.wallet_id == wallet_id)
            .order_by(models.Ledger.created_at.desc(), models.Ledger.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = rows_result.scalars().all()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("get_ledger", exc)

    logger.info("Ledger response wallet_id=%s count=%s", wallet_id, len(rows))
    return [
        {
            "id": row.id,
            "wallet_id": row.wallet_id,
            "amount": row.amount,
            "type": row.type,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/wallets/{wallet_id}/ledger", response_model=list[schemas.LedgerEntryResponse])
async def get_ledger_by_id(
    wallet_id: int,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    logger.info("Ledger requested wallet_id=%s limit=%s offset=%s requester_user_id=%s", wallet_id, limit, offset, current_user.id)

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    try:
        wallet_result = await db.execute(select(models.Wallet).where(models.Wallet.id == wallet_id))
        wallet = wallet_result.scalar_one_or_none()
        if wallet is None:
            raise HTTPException(status_code=404, detail="Wallet not found")
        if wallet.user_id != current_user.id:
            raise _wallet_not_authorized(wallet_id, current_user.id)

        rows_result = await db.execute(
            select(models.Ledger)
            .where(models.Ledger.wallet_id == wallet_id)
            .order_by(models.Ledger.created_at.desc(), models.Ledger.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = rows_result.scalars().all()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _raise_db_error("get_ledger_by_id", exc)

    logger.info("Ledger response wallet_id=%s count=%s", wallet_id, len(rows))
    return [
        {
            "id": row.id,
            "wallet_id": row.wallet_id,
            "amount": row.amount,
            "type": row.type,
            "created_at": row.created_at,
        }
        for row in rows
    ]
