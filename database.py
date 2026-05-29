"""
database.py
SQLAlchemy ORM models and all state-transition helper functions.

Tables
------
clients_registry        – registered trading clients and their broker creds
historical_option_traps – 75-min trap levels with lifecycle status
trades_ledger           – individual trade records per client
"""

from __future__ import annotations

import enum
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Generator, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from config import DATABASE_URL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    pool_pre_ping=True,
    echo=False,
)

# Enable WAL mode for SQLite so reads don't block writes
@event.listens_for(_engine, "connect")
def _set_sqlite_pragma(dbapi_con, _):
    if "sqlite" in DATABASE_URL:
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionFactory: sessionmaker[Session] = sessionmaker(
    bind=_engine, autoflush=False, autocommit=False
)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Enumerators
# ---------------------------------------------------------------------------

class TrapStatus(str, enum.Enum):
    ACTIVE_UNMITIGATED = "ACTIVE_UNMITIGATED"
    MITIGATED          = "MITIGATED"
    VOIDED             = "VOIDED"
    EXPIRED_VOID       = "EXPIRED_VOID"


class OptionType(str, enum.Enum):
    CE = "CE"
    PE = "PE"


class BrokerName(str, enum.Enum):
    ZERODHA   = "ZERODHA"
    ANGEL_ONE = "ANGEL_ONE"
    ALICE_BLUE = "ALICE_BLUE"
    GROWW     = "GROWW"
    UPSTOX    = "UPSTOX"
    FYERS     = "FYERS"


class ExitCategory(str, enum.Enum):
    TARGET_HIT    = "TARGET_HIT"
    SL_HIT        = "SL_HIT"
    MANUAL_SQUARE = "MANUAL_SQUARE"
    EXPIRY_VOID   = "EXPIRY_VOID"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ClientsRegistry(Base):
    __tablename__ = "clients_registry"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    name                = Column(String(120), nullable=False)
    broker              = Column(Enum(BrokerName), nullable=False)
    api_key             = Column(String(256), nullable=False)
    api_secret          = Column(String(256), nullable=False)
    access_token        = Column(String(1024), default="")
    totp_secret         = Column(String(128), default="")
    max_capital         = Column(Float, default=100_000.0)
    active              = Column(Boolean, default=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    trades = relationship("TradesLedger", back_populates="client", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Client id={self.id} name={self.name!r} broker={self.broker}>"


class HistoricalOptionTraps(Base):
    __tablename__ = "historical_option_traps"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    trade_date      = Column(DateTime, nullable=False, default=datetime.utcnow)
    strike          = Column(Integer, nullable=False)
    option_type     = Column(Enum(OptionType), nullable=False)
    contract_symbol = Column(String(64), nullable=False)
    entry_origin    = Column(Float, nullable=False,
                             comment="75-min seller entry price (origin of the short)")
    target_high     = Column(Float, nullable=False,
                             comment="High of the 75-min trapped candle – exit target")
    candle_low_sl   = Column(Float, nullable=True,
                             comment="5-min trap candle structural low – SL boundary")
    status          = Column(
        Enum(TrapStatus), nullable=False, default=TrapStatus.ACTIVE_UNMITIGATED
    )
    voided_at       = Column(DateTime, nullable=True)
    mitigated_at    = Column(DateTime, nullable=True)
    notes           = Column(String(512), default="")

    def __repr__(self) -> str:
        return (
            f"<Trap id={self.id} {self.option_type} strike={self.strike} "
            f"origin={self.entry_origin} status={self.status}>"
        )


class TradesLedger(Base):
    __tablename__ = "trades_ledger"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    client_id       = Column(Integer, ForeignKey("clients_registry.id"), nullable=False)
    trap_id         = Column(Integer, ForeignKey("historical_option_traps.id"), nullable=True)
    contract_symbol = Column(String(64), nullable=False)
    entry_price     = Column(Float, nullable=False)
    exit_price      = Column(Float, nullable=True)
    quantity        = Column(Integer, default=1)
    pnl             = Column(Float, nullable=True)
    exit_category   = Column(Enum(ExitCategory), nullable=True)
    entered_at      = Column(DateTime, default=datetime.utcnow)
    exited_at       = Column(DateTime, nullable=True)

    client = relationship("ClientsRegistry", back_populates="trades")

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.id} client={self.client_id} "
            f"symbol={self.contract_symbol} pnl={self.pnl}>"
        )


# ---------------------------------------------------------------------------
# DDL initialiser
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist yet."""
    Base.metadata.create_all(_engine)
    logger.info("Database schema initialised at %s", DATABASE_URL)


# ---------------------------------------------------------------------------
# Trap state-transition helpers
# ---------------------------------------------------------------------------

def register_trap(
    strike: int,
    option_type: OptionType,
    contract_symbol: str,
    entry_origin: float,
    target_high: float,
    trade_date: Optional[datetime] = None,
) -> HistoricalOptionTraps:
    """Persist a newly confirmed 75-min seller trap."""
    with db_session() as s:
        trap = HistoricalOptionTraps(
            trade_date=trade_date or datetime.utcnow(),
            strike=strike,
            option_type=option_type,
            contract_symbol=contract_symbol,
            entry_origin=entry_origin,
            target_high=target_high,
            status=TrapStatus.ACTIVE_UNMITIGATED,
        )
        s.add(trap)
        s.flush()
        s.expunge(trap)
    logger.info("Trap registered: %s", trap)
    return trap


def set_trap_sl(trap_id: int, candle_low_sl: float) -> None:
    """Attach the 5-min SL candle low to an existing trap."""
    with db_session() as s:
        trap = s.get(HistoricalOptionTraps, trap_id)
        if trap:
            trap.candle_low_sl = candle_low_sl


def void_trap(trap_id: int) -> None:
    """Mark a trap VOIDED (1-min SL close triggered)."""
    with db_session() as s:
        trap = s.get(HistoricalOptionTraps, trap_id)
        if trap and trap.status == TrapStatus.ACTIVE_UNMITIGATED:
            trap.status    = TrapStatus.VOIDED
            trap.voided_at = datetime.utcnow()
            logger.info("Trap %d VOIDED", trap_id)


def mitigate_trap(trap_id: int) -> None:
    """Mark a trap MITIGATED (target reached)."""
    with db_session() as s:
        trap = s.get(HistoricalOptionTraps, trap_id)
        if trap and trap.status == TrapStatus.ACTIVE_UNMITIGATED:
            trap.status       = TrapStatus.MITIGATED
            trap.mitigated_at = datetime.utcnow()
            logger.info("Trap %d MITIGATED", trap_id)


def flush_expired_traps(contract_symbols: Optional[List[str]] = None) -> int:
    """
    Tuesday 03:30 PM routine – expire all ACTIVE_UNMITIGATED traps for the
    expiring weekly series.  Returns number of rows updated.
    """
    with db_session() as s:
        q = s.query(HistoricalOptionTraps).filter(
            HistoricalOptionTraps.status == TrapStatus.ACTIVE_UNMITIGATED
        )
        if contract_symbols:
            q = q.filter(
                HistoricalOptionTraps.contract_symbol.in_(contract_symbols)
            )
        traps = q.all()
        for t in traps:
            t.status    = TrapStatus.EXPIRED_VOID
            t.voided_at = datetime.utcnow()
        logger.info("Flushed %d traps to EXPIRED_VOID", len(traps))
        return len(traps)


def get_active_traps(
    option_type: Optional[OptionType] = None,
) -> List[HistoricalOptionTraps]:
    """Return all ACTIVE_UNMITIGATED traps, optionally filtered by CE/PE."""
    with db_session() as s:
        q = s.query(HistoricalOptionTraps).filter(
            HistoricalOptionTraps.status == TrapStatus.ACTIVE_UNMITIGATED
        )
        if option_type:
            q = q.filter(HistoricalOptionTraps.option_type == option_type)
        traps = q.order_by(HistoricalOptionTraps.entry_origin.desc()).all()
        for t in traps:
            s.expunge(t)
        return traps


def get_next_lower_trap(
    current_entry: float,
    option_type: OptionType,
) -> Optional[HistoricalOptionTraps]:
    """Return the highest trap whose origin sits below current_entry."""
    with db_session() as s:
        trap = (
            s.query(HistoricalOptionTraps)
            .filter(
                HistoricalOptionTraps.status == TrapStatus.ACTIVE_UNMITIGATED,
                HistoricalOptionTraps.option_type == option_type,
                HistoricalOptionTraps.entry_origin < current_entry,
            )
            .order_by(HistoricalOptionTraps.entry_origin.desc())
            .first()
        )
        if trap:
            s.expunge(trap)
        return trap


# ---------------------------------------------------------------------------
# Trade ledger helpers
# ---------------------------------------------------------------------------

def record_trade_entry(
    client_id: int,
    contract_symbol: str,
    entry_price: float,
    quantity: int = 1,
    trap_id: Optional[int] = None,
) -> TradesLedger:
    with db_session() as s:
        trade = TradesLedger(
            client_id=client_id,
            trap_id=trap_id,
            contract_symbol=contract_symbol,
            entry_price=entry_price,
            quantity=quantity,
        )
        s.add(trade)
        s.flush()
        s.expunge(trade)
    return trade


def record_trade_exit(
    trade_id: int,
    exit_price: float,
    exit_category: ExitCategory,
) -> None:
    with db_session() as s:
        trade = s.get(TradesLedger, trade_id)
        if trade:
            trade.exit_price    = exit_price
            trade.exit_category = exit_category
            trade.exited_at     = datetime.utcnow()
            trade.pnl           = (exit_price - trade.entry_price) * trade.quantity


def get_client_trades(client_id: int, limit: int = 50) -> List[TradesLedger]:
    with db_session() as s:
        trades = (
            s.query(TradesLedger)
            .filter(TradesLedger.client_id == client_id)
            .order_by(TradesLedger.entered_at.desc())
            .limit(limit)
            .all()
        )
        for t in trades:
            s.expunge(t)
        return trades


def get_all_active_clients() -> List[ClientsRegistry]:
    with db_session() as s:
        clients = (
            s.query(ClientsRegistry)
            .filter(ClientsRegistry.active == True)
            .all()
        )
        for c in clients:
            s.expunge(c)
        return clients
