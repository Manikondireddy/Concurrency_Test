from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class UserRegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=6, max_length=128)


class UserResponse(BaseModel):
    id: int
    username: str


class TokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class WalletResponse(BaseModel):
    wallet_id: int
    user_id: int
    balance: Decimal


class WalletBalanceResponse(BaseModel):
    wallet_id: int
    user_id: int
    balance: Decimal


class TransactionRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class LedgerEntryResponse(BaseModel):
    id: int
    wallet_id: int
    amount: Decimal
    type: str
    created_at: datetime


class TransactionResponse(BaseModel):
    wallet_id: int
    balance: Decimal
    ledger_entry: LedgerEntryResponse
