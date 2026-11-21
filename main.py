import os
import re
import uuid
import shutil
import zipfile
import base64
from datetime import date
from typing import Optional

import requests
import mysql.connector
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader
from bs4 import BeautifulSoup
from docx import Document
from striprtf.striprtf import rtf_to_text
from mutagen import File as MutagenFile

app = FastAPI(title="BookShelf Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads/books", exist_ok=True)
os.makedirs("uploads/covers", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


def get_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "S1029384756s"),
        database=os.getenv("DB_NAME", "bookshelf_db"),
        ssl_disabled=False
    )


def init_tables():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                author VARCHAR(255) DEFAULT 'Невідомий автор',
                format VARCHAR(50) DEFAULT 'paper',
                total_amount INT DEFAULT 200,
                current_progress INT DEFAULT 0,
                status VARCHAR(50) DEFAULT 'reading',
                rating INT DEFAULT NULL,
                completed_at DATE DEFAULT NULL,
                cover_url TEXT DEFAULT NULL,
                file_url TEXT DEFAULT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS book_notes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                book_id INT NOT NULL,
                note_text TEXT NOT NULL,
                page_ref VARCHAR(50) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("Помилка ініціалізації таблиць:", e)


init_tables()


class NoteCreate(BaseModel):
    note_text: str
    page_ref: Optional[str] = ""


class ProgressUpdate(BaseModel):
    current_progress: int
    total_amount: Optional[int] = None
    rating: Optional[int] = None


def extract_text_from_file(file_path: str, ext: str) -> str:
    ext = ext.lower()
    text_blocks = []

    if ext == ".pdf":
        reader = PdfReader(file_path)
        for p in reader.pages:
            t = p.extract_text() or ""
            if t.strip():
                text_blocks.append(t.strip())

    elif ext == ".epub":
        with zipfile.ZipFile(file_path, "r") as z:
            html_files = sorted([f for f in z.namelist() if f.endswith((".xhtml", ".html", ".htm"))])
            for hf in html_files:
                soup = BeautifulSoup(z.read(hf), "html.parser")
                for tag in soup(["script", "style", "head"]):
                    tag.decompose()
                t = soup.get_text(separator="\n")
                if t.strip():
                    text_blocks.append(t.strip())

    elif ext == ".fb2":
        with open(file_path, "rb") as f:
            soup = BeautifulSoup(f.read(), "xml")
            body = soup.find("body")
            if body:
                for p in body.find_all("p"):
                    t = p.get_text().strip()
                    if t:
                        text_blocks.append(t)

    elif ext == ".docx":
        doc = Document(file_path)
        for p in doc.paragraphs:
            if p.text.strip():
                text_blocks.append(p.text.strip())

    elif ext == ".rtf":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_blocks.append(rtf_to_text(f.read()))

    elif ext in [".html", ".htm"]:
        with open(file_path, "rb") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            for tag in soup(["script", "style", "head"]):
                tag.decompose()
            text_blocks.append(soup.get_text(separator="\n"))

    else:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_blocks.append(f.read())

    full_text = "\n\n".join(text_blocks)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    return full_text.strip()


def extract_cover_from_ebook(file_path: str, ext: str) -> Optional[str]:
    ext = ext.lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(file_path, "r") as z:
                imgs = [
                    f for f in z.namelist()
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    and not f.startswith("__MACOSX")
                ]
                if not imgs:
                    return None
                cover_candidates = [f for f in imgs if "cover" in f.lower()]
                chosen = cover_candidates[0] if cover_candidates else max(imgs, key=lambda name: z.getinfo(name).file_size)
                img_ext = os.path.splitext(chosen)[1] or ".jpg"
                c_name = f"{uuid.uuid4().hex}{img_ext}"
                with open(os.path.join("uploads/covers", c_name), "wb") as out:
                    out.write(z.read(chosen))
                return c_name

        elif ext == ".fb2":
            with open(file_path, "rb") as f:
                soup = BeautifulSoup(f.read(), "xml")
                binary = soup.find("binary")
                if binary and binary.text:
                    raw = base64.b64decode(binary.text.strip())
                    c_name = f"{uuid.uuid4().hex}.jpg"
                    with open(os.path.join("uploads/covers", c_name), "wb") as out:
                        out.write(raw)
                    return c_name
    except Exception:
        pass
    return None


@app.get("/")
def health_check():
    return {"status": "online", "service": "BookShelf Tracker API"}


@app.get("/api/search")
def search_books(q: str):
    if not q or len(q.strip()) < 2:
        return []

    query_text = q.strip()
    results = []

    try:
        g_url = "https://googleapis.com"
        g_res = requests.get(g_url, params={"q": query_text, "maxResults": 6}, timeout=5)
        if g_res.status_code == 200:
            for item in g_res.json().get("items", []):
                info = item.get("volumeInfo", {})
                authors = info.get("authors", ["Невідомий автор"])
                images = info.get("imageLinks", {})
                cover = images.get("thumbnail") or images.get("smallThumbnail")
                if cover:
                    cover = cover.replace("http://", "https://")
                results.append({
                    "title": info.get("title", "Без назви"),
                    "author": ", ".join(authors[:2]),
                    "pages": info.get("pageCount") or 240,
                    "cover_url": cover
                })
    except Exception:
        pass

    if len(results) < 4:
        try:
            ol_url = "https://openlibrary.org"
            ol_res = requests.get(ol_url, params={"q": query_text, "limit": 8}, timeout=5)
            for doc in ol_res.json().get("docs", []):
                authors = doc.get("author_name", ["Невідомий автор"])
                cover_id = doc.get("cover_i")
                cover_url = f"https://openlibrary.org{cover_id}-M.jpg" if cover_id else None
                results.append({
                    "title": doc.get("title", "Без назви"),
                    "author": ", ".join(authors[:2]),
                    "pages": doc.get("number_of_pages_median") or 240,
                    "cover_url": cover_url
                })
        except Exception:
            pass

    results.sort(key=lambda x: x["cover_url"] is not None, reverse=True)
    return results[:6]


@app.get("/api/books")
def get_books():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM books ORDER BY updated_at DESC, id DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


@app.post("/api/books")
async def add_book(
    request: Request,
    title: str = Form("Нова книга"),
    author: str = Form("Невідомий автор"),
    format: str = Form("paper"),
    total_amount: int = Form(0),
    cover_url: Optional[str] = Form(None),
    external_url: Optional[str] = Form(None),
    book_file: Optional[UploadFile] = File(None),
    cover_file: Optional[UploadFile] = File(None)
):
    saved_file_url = external_url.strip() if external_url and external_url.strip() else None
    saved_cover_url = cover_url if cover_url else None
    calculated_total = total_amount
    final_title = title
    final_author = author
    base_host = str(request.base_url).rstrip("/")

    if saved_file_url and ("youtube.com" in saved_file_url or "youtu.be" in saved_file_url):
        try:
            yt_oembed = "https://youtube.com"
            r = requests.get(yt_oembed, params={"url": saved_file_url, "format": "json"}, timeout=5)
            if r.status_code == 200:
                yt_data = r.json()
                if not final_title or final_title == "Нова книга":
                    final_title = yt_data.get("title") or final_title
                if final_author == "Невідомий автор" and yt_data.get("author_name"):
                    final_author = yt_data.get("author_name")
import os
import re
import uuid
import shutil
import zipfile
import base64
from datetime import date
from typing import Optional

import requests
import mysql.connector
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader
from bs4 import BeautifulSoup
from docx import Document
from striprtf.striprtf import rtf_to_text
from mutagen import File as MutagenFile

app = FastAPI(title="BookShelf Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads/books", exist_ok=True)
os.makedirs("uploads/covers", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


def get_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "S1029384756s"),
        database=os.getenv("DB_NAME", "bookshelf_db"),
        ssl_disabled=False
    )


def init_tables():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                author VARCHAR(255) DEFAULT 'Невідомий автор',
                format VARCHAR(50) DEFAULT 'paper',
                total_amount INT DEFAULT 200,
                current_progress INT DEFAULT 0,
                status VARCHAR(50) DEFAULT 'reading',
                rating INT DEFAULT NULL,
                completed_at DATE DEFAULT NULL,
                cover_url TEXT DEFAULT NULL,
                file_url TEXT DEFAULT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS book_notes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                book_id INT NOT NULL,
                note_text TEXT NOT NULL,
                page_ref VARCHAR(50) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("Помилка ініціалізації таблиць:", e)


init_tables()


class NoteCreate(BaseModel):
    note_text: str
    page_ref: Optional[str] = ""


class ProgressUpdate(BaseModel):
    current_progress: int
    total_amount: Optional[int] = None
    rating: Optional[int] = None


def extract_text_from_file(file_path: str, ext: str) -> str:
    ext = ext.lower()
    text_blocks = []

    if ext == ".pdf":
        reader = PdfReader(file_path)
        for p in reader.pages:
            t = p.extract_text() or ""
            if t.strip():
                text_blocks.append(t.strip())

    elif ext == ".epub":
        with zipfile.ZipFile(file_path, "r") as z:
            html_files = sorted([f for f in z.namelist() if f.endswith((".xhtml", ".html", ".htm"))])
            for hf in html_files:
                soup = BeautifulSoup(z.read(hf), "html.parser")
                for tag in soup(["script", "style", "head"]):
                    tag.decompose()
                t = soup.get_text(separator="\n")
                if t.strip():
                    text_blocks.append(t.strip())

    elif ext == ".fb2":
        with open(file_path, "rb") as f:
            soup = BeautifulSoup(f.read(), "xml")
            body = soup.find("body")
            if body:
                for p in body.find_all("p"):
                    t = p.get_text().strip()
                    if t:
                        text_blocks.append(t)

    elif ext == ".docx":
        doc = Document(file_path)
        for p in doc.paragraphs:
            if p.text.strip():
                text_blocks.append(p.text.strip())

    elif ext == ".rtf":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_blocks.append(rtf_to_text(f.read()))

    elif ext in [".html", ".htm"]:
        with open(file_path, "rb") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            for tag in soup(["script", "style", "head"]):
                tag.decompose()
            text_blocks.append(soup.get_text(separator="\n"))

    else:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_blocks.append(f.read())

    full_text = "\n\n".join(text_blocks)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)
    return full_text.strip()


def extract_cover_from_ebook(file_path: str, ext: str) -> Optional[str]:
    ext = ext.lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(file_path, "r") as z:
                imgs = [
                    f for f in z.namelist()
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    and not f.startswith("__MACOSX")
                ]
                if not imgs:
                    return None
                cover_candidates = [f for f in imgs if "cover" in f.lower()]
                chosen = cover_candidates[0] if cover_candidates else max(imgs, key=lambda name: z.getinfo(name).file_size)
                img_ext = os.path.splitext(chosen)[1] or ".jpg"
                c_name = f"{uuid.uuid4().hex}{img_ext}"
                with open(os.path.join("uploads/covers", c_name), "wb") as out:
                    out.write(z.read(chosen))
                return c_name

        elif ext == ".fb2":
            with open(file_path, "rb") as f:
                soup = BeautifulSoup(f.read(), "xml")
                binary = soup.find("binary")
                if binary and binary.text:
                    raw = base64.b64decode(binary.text.strip())
                    c_name = f"{uuid.uuid4().hex}.jpg"
                    with open(os.path.join("uploads/covers", c_name), "wb") as out:
                        out.write(raw)
                    return c_name
    except Exception:
        pass
    return None


@app.get("/")
def health_check():
    return {"status": "online", "service": "BookShelf Tracker API"}


@app.get("/api/search")
def search_books(q: str):
    if not q or len(q.strip()) < 2:
        return []

    query_text = q.strip()
    results = []

    try:
        g_url = "https://googleapis.com"
        g_res = requests.get(g_url, params={"q": query_text, "maxResults": 6}, timeout=5)
        if g_res.status_code == 200:
            for item in g_res.json().get("items", []):
                info = item.get("volumeInfo", {})
                authors = info.get("authors", ["Невідомий автор"])
                images = info.get("imageLinks", {})
                cover = images.get("thumbnail") or images.get("smallThumbnail")
                if cover:
                    cover = cover.replace("http://", "https://")
                results.append({
                    "title": info.get("title", "Без назви"),
                    "author": ", ".join(authors[:2]),
                    "pages": info.get("pageCount") or 240,
                    "cover_url": cover
                })
    except Exception:
        pass

    if len(results) < 4:
        try:
            ol_url = "https://openlibrary.org"
            ol_res = requests.get(ol_url, params={"q": query_text, "limit": 8}, timeout=5)
            for doc in ol_res.json().get("docs", []):
                authors = doc.get("author_name", ["Невідомий автор"])
                cover_id = doc.get("cover_i")
                cover_url = f"https://openlibrary.org{cover_id}-M.jpg" if cover_id else None
                results.append({
                    "title": doc.get("title", "Без назви"),
                    "author": ", ".join(authors[:2]),
                    "pages": doc.get("number_of_pages_median") or 240,
                    "cover_url": cover_url
                })
        except Exception:
            pass

    results.sort(key=lambda x: x["cover_url"] is not None, reverse=True)
    return results[:6]


@app.get("/api/books")
def get_books():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM books ORDER BY updated_at DESC, id DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


@app.post("/api/books")
async def add_book(
    request: Request,
    title: str = Form("Нова книга"),
    author: str = Form("Невідомий автор"),
    format: str = Form("paper"),
    total_amount: int = Form(0),
    cover_url: Optional[str] = Form(None),
    external_url: Optional[str] = Form(None),
    book_file: Optional[UploadFile] = File(None),
    cover_file: Optional[UploadFile] = File(None)
):
    saved_file_url = external_url.strip() if external_url and external_url.strip() else None
    saved_cover_url = cover_url if cover_url else None
    calculated_total = total_amount
    final_title = title
    final_author = author
    base_host = str(request.base_url).rstrip("/")

    if saved_file_url and ("youtube.com" in saved_file_url or "youtu.be" in saved_file_url):
        try:
            yt_oembed = "https://youtube.com"
            r = requests.get(yt_oembed, params={"url": saved_file_url, "format": "json"}, timeout=5)
            if r.status_code == 200:
                yt_data = r.json()
                if not final_title or final_title == "Нова книга":
                    final_title = yt_data.get("title") or final_title
                if final_author == "Невідомий автор" and yt_data.get("author_name"):
                    final_author = yt_data.get("author_name")
