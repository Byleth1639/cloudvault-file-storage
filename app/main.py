"""CloudVault: a practical, self-hostable file manager with S3-compatible storage.

Run: uvicorn app.main:app --reload
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").lower()
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "25")))
STORAGE_QUOTA_MB = max(1, int(os.getenv("STORAGE_QUOTA_MB", "500")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
QUOTA_BYTES = STORAGE_QUOTA_MB * 1024 * 1024
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = 7
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'cloudvault.db'}")
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg://" + DB_URL.removeprefix("postgres://")
elif DB_URL.startswith("postgresql://"):
    DB_URL = "postgresql+psycopg://" + DB_URL.removeprefix("postgresql://")

engine = create_engine(DB_URL, pool_pre_ping=True, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d):
    return d.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if d else None


def new_id():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(String(32), primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(100), nullable=False)
    password_hash = Column(String(200), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now)


class SessionToken(Base):
    __tablename__ = "sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    csrf = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class Folder(Base):
    __tablename__ = "folders"
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    parent_id = Column(String(32), ForeignKey("folders.id"), nullable=True, index=True)
    name = Column(String(160), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now)


class StoredFile(Base):
    __tablename__ = "files"
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    folder_id = Column(String(32), ForeignKey("folders.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    content_type = Column(String(150), nullable=False, default="application/octet-stream")
    size = Column(Integer, nullable=False)
    storage_key = Column(String(150), nullable=False, unique=True)
    sha256 = Column(String(64), nullable=False)
    starred = Column(Boolean, nullable=False, default=False)
    trashed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now)


class Share(Base):
    __tablename__ = "shares"
    id = Column(String(32), primary_key=True)
    file_id = Column(String(32), ForeignKey("files.id"), nullable=False, index=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=now)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    downloads = Column(Integer, nullable=False, default=0)


class Activity(Base):
    __tablename__ = "activities"
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    action = Column(String(50), nullable=False)
    item_name = Column(String(200), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now)


class Store:
    def __init__(self):
        self.kind = STORAGE_BACKEND
        if self.kind == "local":
            (DATA_DIR / "blobs").mkdir(parents=True, exist_ok=True)
        elif self.kind == "s3":
            import boto3
            required = ("S3_BUCKET", "S3_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            missing = [k for k in required if not os.getenv(k)]
            if missing:
                raise RuntimeError("Missing S3 configuration: " + ", ".join(missing))
            self.bucket = os.environ["S3_BUCKET"]
            self.client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], region_name=os.getenv("AWS_DEFAULT_REGION", "auto"))
        else:
            raise RuntimeError("STORAGE_BACKEND must be 'local' or 's3'")

    def put(self, key, src):
        if self.kind == "local":
            shutil.copyfile(src, DATA_DIR / "blobs" / key)
        else:
            self.client.upload_file(str(src), self.bucket, key)

    def delete(self, key):
        if self.kind == "local":
            (DATA_DIR / "blobs" / key).unlink(missing_ok=True)
        else:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def response(self, key, filename, mime, download=False):
        safe_inline = mime.startswith("image/") and mime not in ("image/svg+xml",) or mime in ("application/pdf", "text/plain", "audio/mpeg", "video/mp4", "audio/wav")
        if not safe_inline:
            mime = "application/octet-stream"
        disposition = "attachment" if download or not safe_inline else "inline"
        headers = {
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if self.kind == "local":
            path = DATA_DIR / "blobs" / key
            if not path.is_file():
                raise HTTPException(404, "File data unavailable")
            return FileResponse(path, media_type=mime, headers=headers)
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception:
            raise HTTPException(404, "File data unavailable")
        def chunks():
            try:
                for part in obj["Body"].iter_chunks(chunk_size=64 * 1024):
                    if part:
                        yield part
            finally:
                obj["Body"].close()
        return StreamingResponse(chunks(), media_type=mime, headers=headers)


store = Store()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="CloudVault API", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_token(s):
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password, digest):
    try:
        _, salt, expected = digest.split("$", 2)
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except Exception:
        return False


def session_actor(request: Request, db: Session = Depends(db_dep)):
    raw = request.cookies.get("cv_session")
    if not raw:
        raise HTTPException(401, "Please sign in")
    session = db.get(SessionToken, hash_token(raw))
    if not session or session.expires_at < now():
        raise HTTPException(401, "Session expired; please sign in")
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "Invalid session")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied, session.csrf):
            raise HTTPException(403, "Invalid CSRF token")
    return user, session


def login_response(response: Response, user: User, db: Session):
    raw = secrets.token_urlsafe(36)
    session = SessionToken(token_hash=hash_token(raw), user_id=user.id, csrf=secrets.token_urlsafe(30), expires_at=now()+timedelta(days=SESSION_DAYS))
    db.add(session)
    db.commit()
    response.set_cookie("cv_session", raw, httponly=True, secure=COOKIE_SECURE, samesite="strict", max_age=SESSION_DAYS * 86400, path="/")
    return {"user": user_data(user), "csrf": session.csrf}


def user_data(u):
    return {"id": u.id, "email": u.email, "name": u.display_name, "created_at": iso(u.created_at)}


class Credentials(BaseModel):
    email: str
    password: str
    name: str | None = None


@app.post("/api/auth/register", status_code=201)
def register(payload: Credentials, response: Response, db: Session = Depends(db_dep)):
    email = payload.email.strip().lower()
    name = (payload.name or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 255:
        raise HTTPException(400, "Enter a valid email address")
    if len(name) < 2 or len(name) > 100:
        raise HTTPException(400, "Name must be 2–100 characters")
    if len(payload.password) < 8 or len(payload.password) > 128:
        raise HTTPException(400, "Password must be 8–128 characters")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with that email already exists")
    user = User(id=new_id(), email=email, display_name=name, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    return login_response(response, user, db)


@app.post("/api/auth/login")
def login(payload: Credentials, response: Response, db: Session = Depends(db_dep)):
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return login_response(response, user, db)


@app.post("/api/auth/demo")
def demo_account(response: Response, db: Session = Depends(db_dep)):
    # Each visitor gets a private demo account, avoiding shared public credentials.
    suffix = secrets.token_hex(6)
    user = User(id=new_id(), email=f"demo-{suffix}@cloudvault.local", display_name="Demo Explorer", password_hash=hash_password(secrets.token_urlsafe(24)))
    db.add(user)
    db.commit()
    return login_response(response, user, db)


@app.get("/api/me")
def me(actor=Depends(session_actor)):
    user, session = actor
    return {"user": user_data(user), "csrf": session.csrf, "quota_bytes": QUOTA_BYTES, "max_upload_bytes": MAX_UPLOAD_BYTES, "storage_backend": STORAGE_BACKEND}


@app.post("/api/auth/logout")
def logout(response: Response, actor=Depends(session_actor), db: Session = Depends(db_dep)):
    _, session = actor
    db.delete(session)
    db.commit()
    response.delete_cookie("cv_session", path="/")
    return {"ok": True}


def owned_folder(db, user_id, folder_id):
    if folder_id is None:
        return None
    f = db.get(Folder, folder_id)
    if not f or f.user_id != user_id:
        raise HTTPException(404, "Folder not found")
    return f


def owned_file(db, user_id, file_id):
    f = db.get(StoredFile, file_id)
    if not f or f.user_id != user_id:
        raise HTTPException(404, "File not found")
    return f


def file_data(f):
    return {"id": f.id, "kind": "file", "name": f.name, "folder_id": f.folder_id, "size": f.size, "content_type": f.content_type, "starred": f.starred, "trashed": f.trashed, "sha256": f.sha256, "created_at": iso(f.created_at), "updated_at": iso(f.updated_at)}


def folder_data(f):
    return {"id": f.id, "kind": "folder", "name": f.name, "parent_id": f.parent_id, "created_at": iso(f.created_at)}


def log(db, user_id, action, item_name):
    db.add(Activity(id=new_id(), user_id=user_id, action=action, item_name=item_name))


def filename_clean(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip().strip(".")
    if not name or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise HTTPException(400, "Invalid filename (maximum 160 characters)")
    return name


class FolderCreate(BaseModel):
    name: str
    parent_id: str | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: str | None = None


@app.get("/api/folders")
def folders(actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    return [folder_data(f) for f in db.scalars(select(Folder).where(Folder.user_id == user.id).order_by(Folder.name)).all()]


def folder_name_taken(db, user_id, parent_id, name, exclude_id=None):
    q = select(Folder).where(Folder.user_id == user_id, Folder.parent_id == parent_id, func.lower(Folder.name) == name.lower())
    if exclude_id:
        q = q.where(Folder.id != exclude_id)
    return db.scalar(q) is not None


@app.post("/api/folders", status_code=201)
def create_folder(payload: FolderCreate, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    name = filename_clean(payload.name)
    owned_folder(db, user.id, payload.parent_id)
    if folder_name_taken(db, user.id, payload.parent_id, name):
        raise HTTPException(409, "A folder with that name already exists here")
    f = Folder(id=new_id(), user_id=user.id, parent_id=payload.parent_id, name=name)
    db.add(f)
    log(db, user.id, "created folder", name)
    db.commit()
    return folder_data(f)


@app.patch("/api/folders/{folder_id}")
def edit_folder(folder_id: str, payload: FolderPatch, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_folder(db, user.id, folder_id)
    new_name = filename_clean(payload.name) if payload.name is not None else f.name
    parent = payload.parent_id if "parent_id" in payload.model_fields_set else f.parent_id
    owned_folder(db, user.id, parent)
    if parent == f.id:
        raise HTTPException(400, "A folder cannot contain itself")
    cursor = parent
    while cursor:
        if cursor == f.id:
            raise HTTPException(400, "Cannot move a folder inside its descendant")
        cursor = owned_folder(db, user.id, cursor).parent_id
    if folder_name_taken(db, user.id, parent, new_name, f.id):
        raise HTTPException(409, "A folder with that name already exists here")
    f.name, f.parent_id = new_name, parent
    log(db, user.id, "updated folder", new_name)
    db.commit()
    return folder_data(f)


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: str, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_folder(db, user.id, folder_id)
    if db.scalar(select(Folder.id).where(Folder.parent_id == f.id)) or db.scalar(select(StoredFile.id).where(StoredFile.folder_id == f.id, StoredFile.trashed.is_(False))):
        raise HTTPException(409, "Move or delete the items in this folder first")
    # A trashed file may outlive its original folder; restore it to root instead.
    for trashed_file in db.scalars(select(StoredFile).where(StoredFile.folder_id == f.id, StoredFile.trashed.is_(True))).all():
        trashed_file.folder_id = None
    log(db, user.id, "deleted folder", f.name)
    db.delete(f)
    db.commit()
    return {"ok": True}


@app.get("/api/items")
def list_items(folder_id: str | None=None, view: str="all", q: str="", actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    if view not in ("all", "recent", "starred", "shared", "trash"):
        raise HTTPException(400, "Invalid view")
    if view == "all":
        owned_folder(db, user.id, folder_id)
        fs = db.scalars(select(Folder).where(Folder.user_id == user.id, Folder.parent_id == folder_id)).all()
    else:
        fs = []
    stmt = select(StoredFile).where(StoredFile.user_id == user.id, StoredFile.trashed == (view == "trash"))
    if view == "all":
        stmt = stmt.where(StoredFile.folder_id == folder_id)
    if view == "starred":
        stmt = stmt.where(StoredFile.starred.is_(True))
    if view == "shared":
        active_file_ids = select(Share.file_id).where(Share.user_id == user.id, Share.revoked_at.is_(None), or_(Share.expires_at.is_(None), Share.expires_at > now()))
        stmt = stmt.where(StoredFile.id.in_(active_file_ids))
    if q.strip():
        needle = f"%{q.strip()[:120]}%"
        stmt = stmt.where(StoredFile.name.ilike(needle))
        fs = [f for f in fs if q.casefold() in f.name.casefold()]
    files = db.scalars(stmt.order_by(StoredFile.updated_at.desc()).limit(500)).all()
    if view == "recent":
        files = files[:100]
    return {"folders": [folder_data(f) for f in sorted(fs, key=lambda v: v.name.lower())], "files": [file_data(f) for f in files]}


@app.get("/api/search")
def search(q: str="", actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    q = q.strip()[:120]
    if not q:
        return {"folders": [], "files": []}
    fs = db.scalars(select(Folder).where(Folder.user_id == user.id, Folder.name.ilike(f"%{q}%")).limit(100)).all()
    files = db.scalars(select(StoredFile).where(StoredFile.user_id == user.id, StoredFile.trashed.is_(False), StoredFile.name.ilike(f"%{q}%")).limit(200)).all()
    return {"folders": [folder_data(f) for f in fs], "files": [file_data(f) for f in files]}


@app.get("/api/dashboard")
def dashboard(actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    rows = db.scalars(select(StoredFile).where(StoredFile.user_id == user.id, StoredFile.trashed.is_(False))).all()
    counts = {"image": 0, "document": 0, "video": 0, "other": 0}
    for f in rows:
        mime = f.content_type
        category = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "document" if mime.startswith("text/") or mime == "application/pdf" or "word" in mime else "other"
        counts[category] += 1
    usage = db.scalar(select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.user_id == user.id)) or 0
    return {"total_files": len(rows), "total_folders": db.scalar(select(func.count(Folder.id)).where(Folder.user_id == user.id)) or 0, "storage_used": usage, "storage_quota": QUOTA_BYTES, "categories": counts, "recent": [file_data(f) for f in sorted(rows, key=lambda x: x.updated_at, reverse=True)[:5]]}


@app.get("/api/activity")
def activity(actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    rows = db.scalars(select(Activity).where(Activity.user_id == user.id).order_by(Activity.created_at.desc()).limit(30)).all()
    return [{"id": x.id, "action": x.action, "item_name": x.item_name, "created_at": iso(x.created_at)} for x in rows]


@app.post("/api/files", status_code=201)
def upload_file(file: UploadFile=File(...), folder_id: str | None=Form(None), actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    if folder_id in ("", "null", "undefined"):
        folder_id = None
    owned_folder(db, user.id, folder_id)
    name = filename_clean(file.filename)
    total = db.scalar(select(func.coalesce(func.sum(StoredFile.size), 0)).where(StoredFile.user_id == user.id)) or 0
    if total >= QUOTA_BYTES:
        raise HTTPException(413, "Storage quota reached")
    key = new_id()
    size = 0
    digest = hashlib.sha256()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cloudvault-", delete=False) as output:
            tmp = Path(output.name)
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Maximum upload size is {MAX_UPLOAD_MB} MB")
                if total + size > QUOTA_BYTES:
                    raise HTTPException(413, "Storage quota exceeded")
                digest.update(chunk)
                output.write(chunk)
        store.put(key, tmp)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        f = StoredFile(id=new_id(), user_id=user.id, folder_id=folder_id, name=name, content_type=mime, size=size, storage_key=key, sha256=digest.hexdigest())
        db.add(f)
        log(db, user.id, "uploaded", name)
        db.commit()
        return file_data(f)
    except Exception:
        store.delete(key)
        raise
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
        file.file.close()


class FilePatch(BaseModel):
    name: str | None = None
    folder_id: str | None = None
    starred: bool | None = None


@app.patch("/api/files/{file_id}")
def edit_file(file_id: str, payload: FilePatch, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    if f.trashed:
        raise HTTPException(409, "Restore this file before editing")
    if payload.name is not None:
        f.name = filename_clean(payload.name)
    if "folder_id" in payload.model_fields_set:
        owned_folder(db, user.id, payload.folder_id)
        f.folder_id = payload.folder_id
    if payload.starred is not None:
        f.starred = payload.starred
    f.updated_at = now()
    log(db, user.id, "updated", f.name)
    db.commit()
    return file_data(f)


@app.delete("/api/files/{file_id}")
def trash_file(file_id: str, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    f.trashed = True
    f.updated_at = now()
    # Removing a file from the workspace invalidates its public sharing links.
    for sh in db.scalars(select(Share).where(Share.file_id == f.id, Share.revoked_at.is_(None))).all():
        sh.revoked_at = now()
    log(db, user.id, "moved to trash", f.name)
    db.commit()
    return {"ok": True}


@app.post("/api/files/{file_id}/restore")
def restore_file(file_id: str, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    f.trashed = False
    f.updated_at = now()
    log(db, user.id, "restored", f.name)
    db.commit()
    return file_data(f)


@app.delete("/api/files/{file_id}/permanent")
def permanently_delete(file_id: str, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    if not f.trashed:
        raise HTTPException(409, "Move the file to trash first")
    # delete shares before deleting file; keep no dangling links
    for sh in db.scalars(select(Share).where(Share.file_id == f.id)).all():
        db.delete(sh)
    store.delete(f.storage_key)
    log(db, user.id, "permanently deleted", f.name)
    db.delete(f)
    db.commit()
    return {"ok": True}


@app.get("/api/files/{file_id}/content")
def file_content(file_id: str, download: bool=False, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    if f.trashed:
        raise HTTPException(404, "File is in trash")
    return store.response(f.storage_key, f.name, f.content_type, download=download)


class ShareCreate(BaseModel):
    days: int | None = Field(default=7, ge=1, le=365)


def share_data(sh: Share, filename: str):
    return {"id": sh.id, "file_id": sh.file_id, "filename": filename, "created_at": iso(sh.created_at), "expires_at": iso(sh.expires_at), "downloads": sh.downloads, "revoked": sh.revoked_at is not None}


@app.post("/api/files/{file_id}/shares", status_code=201)
def create_share(file_id: str, payload: ShareCreate, request: Request, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    f = owned_file(db, user.id, file_id)
    if f.trashed:
        raise HTTPException(409, "Restore file before sharing")
    token = secrets.token_urlsafe(32)
    sh = Share(id=new_id(), file_id=f.id, user_id=user.id, token_hash=hash_token(token), expires_at=now() + timedelta(days=payload.days) if payload.days else None)
    db.add(sh)
    log(db, user.id, "created share link", f.name)
    db.commit()
    # Prefer externally configured public origin behind a reverse proxy.
    origin = os.getenv("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))
    return {**share_data(sh, f.name), "url": f"{origin.rstrip('/')}/s/{token}"}


@app.get("/api/shares")
def my_shares(file_id: str | None=None, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    stmt = select(Share).where(Share.user_id == user.id, Share.revoked_at.is_(None))
    if file_id:
        owned_file(db, user.id, file_id)
        stmt = stmt.where(Share.file_id == file_id)
    rows = db.scalars(stmt.order_by(Share.created_at.desc())).all()
    result = []
    for sh in rows:
        file = db.get(StoredFile, sh.file_id)
        if file and not file.trashed and (not sh.expires_at or sh.expires_at > now()):
            result.append(share_data(sh, file.name))
    return result


@app.delete("/api/shares/{share_id}")
def revoke_share(share_id: str, actor=Depends(session_actor), db: Session=Depends(db_dep)):
    user, _ = actor
    sh = db.get(Share, share_id)
    if not sh or sh.user_id != user.id:
        raise HTTPException(404, "Share not found")
    sh.revoked_at = now()
    file = db.get(StoredFile, sh.file_id)
    log(db, user.id, "revoked share link", file.name if file else "file")
    db.commit()
    return {"ok": True}


def public_share(token, db):
    sh = db.scalar(select(Share).where(Share.token_hash == hash_token(token)))
    if not sh or sh.revoked_at or sh.expires_at and sh.expires_at <= now():
        raise HTTPException(404, "This link is invalid or has expired")
    f = db.get(StoredFile, sh.file_id)
    if not f or f.trashed:
        raise HTTPException(404, "This file is no longer available")
    return sh, f


@app.get("/api/public/{token}")
def public_info(token: str, db: Session=Depends(db_dep)):
    _, f = public_share(token, db)
    return {"name": f.name, "size": f.size, "content_type": f.content_type}


@app.get("/api/public/{token}/download")
def public_download(token: str, db: Session=Depends(db_dep)):
    sh, f = public_share(token, db)
    sh.downloads += 1
    db.commit()
    return store.response(f.storage_key, f.name, f.content_type, download=True)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": "CloudVault", "storage_backend": STORAGE_BACKEND}


@app.get("/", response_class=HTMLResponse)
@app.get("/s/{token}", response_class=HTMLResponse)
def frontend():
    return FileResponse(STATIC / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})
