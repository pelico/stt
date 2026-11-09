"""SQLite 数据库模型与操作。"""
import os
from datetime import datetime
from typing import List, Optional

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import settings

Base = declarative_base()


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
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "text": self.text,
            "duration_sec": self.duration_sec,
            "audio_path": self.audio_path,
            "engine": self.engine,
        }


# 确保数据目录存在
os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
if settings.save_audio:
    os.makedirs(settings.audio_dir, exist_ok=True)

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
            query = query.filter(Transcript.text.like(f"%{keyword}%"))
        return (
            query.order_by(Transcript.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    finally:
        db.close()
