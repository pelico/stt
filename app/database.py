"""SQLite 数据库模型与操作。"""
import os
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import settings

Base = declarative_base()


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """序列化为带时区的 ISO 时间，避免前端按本地时间误解。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Transcript(Base):
    """识别结果表。"""
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    text = Column(Text, nullable=False)
    duration_sec = Column(Integer, default=0)
    audio_path = Column(String(500), nullable=True)
    engine = Column(String(50), default="aliyun")

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": iso_utc(self.timestamp),
            "text": self.text,
            "duration_sec": self.duration_sec,
            "audio_path": self.audio_path,
            "engine": self.engine,
        }


# 确保数据库目录存在（音频目录由 audio_dump 按需创建）
os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def save_transcript(
    text: str,
    duration_sec: int = 0,
    audio_path: Optional[str] = None,
    engine: str = "aliyun",
) -> Transcript:
    """保存一条识别结果。"""
    db = SessionLocal()
    try:
        record = Transcript(
            text=text,
            duration_sec=duration_sec,
            audio_path=audio_path,
            engine=engine,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    finally:
        db.close()


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % / _ 时变成全表匹配。"""
    escaped = (
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def count_transcripts(keyword: Optional[str] = None) -> int:
    """匹配的总条数（不受分页影响）。"""
    db = SessionLocal()
    try:
        query = db.query(func.count(Transcript.id))
        if keyword:
            query = query.filter(Transcript.text.like(_like_pattern(keyword), escape="\\"))
        return int(query.scalar() or 0)
    finally:
        db.close()


def list_transcripts(
    limit: int = 100,
    offset: int = 0,
    keyword: Optional[str] = None,
) -> List[Transcript]:
    """查询历史记录。"""
    db = SessionLocal()
    try:
        query = db.query(Transcript)
        if keyword:
            query = query.filter(Transcript.text.like(_like_pattern(keyword), escape="\\"))
        return (
            query.order_by(Transcript.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    finally:
        db.close()
