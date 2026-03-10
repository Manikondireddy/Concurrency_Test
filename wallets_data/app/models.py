from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.sql import func
from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False, default="")


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallet_balance_non_negative"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    balance = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    # Optimistic locking for concurrent balance updates.
    version = Column(Integer, nullable=False, default=0, server_default="0")


class Ledger(Base):
    __tablename__ = "ledger"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),)

    id = Column(Integer, primary_key=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    type = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
