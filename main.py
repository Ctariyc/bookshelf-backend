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
if not saved_cover_url and yt_data.get("thumbnail_url"):
saved_cover_url = yt_data.get("thumbnail_url")
except Exception:
pass
if cover_file and cover_file.filename:
c_ext = os.path.splitext(cover_file.filename)[1] or ".jpg"
c_name = f"{uuid.uuid4().hex}{c_ext}"
c_path = os.path.join("uploads/covers", c_name)
with open(c_path, "wb") as f:
shutil.copyfileobj(cover_file.file, f)
saved_cover_url = f"{base_host}/uploads/covers/{c_name}"
if book_file and book_file.filename:
ext = os.path.splitext(book_file.filename)[1].lower()
b_name = f"{uuid.uuid4().hex}{ext}"
b_path = os.path.join("uploads/books", b_name)
with open(b_path, "wb") as f:
shutil.copyfileobj(book_file.file, f)
saved_file_url = f"{base_host}/uploads/books/{b_name}"
try:
if ext in [".mp3", ".m4b", ".m4a", ".ogg", ".wav"]:
audio = MutagenFile(b_path)
if audio:
if audio.info and not calculated_total:
calculated_total = max(1, int(audio.info.length // 60))
tags = audio.tags
if tags:
raw_title = tags.get("TIT2") or tags.get("\xa9nam") or tags.get("title")
if raw_title:
t_str = str(raw_title[0] if isinstance(raw_title, list) else raw_title).strip()
if t_str:
final_title = t_str
raw_author = tags.get("TPE1") or tags.get("\xa9ART") or tags.get("artist")
if raw_author and final_author == "Невідомий автор":
a_str = str(raw_author[0] if isinstance(raw_author, list) else raw_author).strip()
if a_str:
final_author = a_str
if not saved_cover_url:
cover_bytes = None
for k in tags.keys():
if k.startswith("APIC"):
cover_bytes = tags[k].data
break
if not cover_bytes and "covr" in tags and tags["covr"]:
cover_bytes = bytes(tags["covr"][0])
if cover_bytes:
c_name = f"{uuid.uuid4().hex}.jpg"
with open(os.path.join("uploads/covers", c_name), "wb") as cf:
cf.write(cover_bytes)
saved_cover_url = f"{base_host}/uploads/covers/{c_name}"
elif ext in [".pdf", ".epub", ".fb2", ".txt", ".docx", ".rtf", ".html", ".htm", ".md"]:
extracted_text = extract_text_from_file(b_path, ext)
txt_path = f"{b_path}.txt"
with open(txt_path, "w", encoding="utf-8") as tf:
tf.write(extracted_text)
if not calculated_total:
calculated_total = max(1, len(extracted_text) // 1800)
if not saved_cover_url:
extracted_cover = extract_cover_from_ebook(b_path, ext)
if extracted_cover:
saved_cover_url = f"{base_host}/uploads/covers/{extracted_cover}"
except Exception as e:
print("Помилка розбору файлу:", e)
if calculated_total <= 0:
calculated_total = 200
conn = get_db()
cursor = conn.cursor()
sql = """
INSERT INTO books (title, author, format, total_amount, current_progress, status, cover_url, file_url)
VALUES (%s, %s, %s, %s, 0, 'reading', %s, %s)
"""
cursor.execute(sql, (final_title, final_author, format, calculated_total, saved_cover_url, saved_file_url))
conn.commit()
new_id = cursor.lastrowid
cursor.close()
conn.close()
return {"id": new_id, "status": "created"}
@app.get("/api/books/{book_id}/page/{page_num}")
def get_book_page(book_id: int, page_num: int):
conn = get_db()
cursor = conn.cursor(dictionary=True)
cursor.execute("SELECT file_url, total_amount FROM books WHERE id = %s", (book_id,))
book = cursor.fetchone()
cursor.close()
conn.close()
if not book or not book["file_url"]:
raise HTTPException(status_code=404, detail="Файл книги не знайдено")
filename = book["file_url"].split("/uploads/books/")[-1]
txt_path = os.path.join("uploads/books", f"{filename}.txt")
if not os.path.exists(txt_path):
raise HTTPException(status_code=404, detail="Текст книги не розпізнано")
with open(txt_path, "r", encoding="utf-8") as f:
full_text = f.read()
chars_per_page = 1800
total_pages = max(1, (len(full_text) + chars_per_page - 1) // chars_per_page)
safe_page = max(1, min(page_num, total_pages))
start_idx = (safe_page - 1) * chars_per_page
end_idx = start_idx + chars_per_page
page_text = full_text[start_idx:end_idx]
conn = get_db()
cursor = conn.cursor()
cursor.execute(
"UPDATE books SET current_progress = %s, total_amount = %s, updated_at = NOW() WHERE id = %s",
(safe_page, total_pages, book_id)
)
conn.commit()
cursor.close()
conn.close()
return {
"page": safe_page,
"total": total_pages,
"text": page_text
}
@app.put("/api/books/{book_id}/progress")
def update_progress(book_id: int, data: ProgressUpdate):
conn = get_db()
cursor = conn.cursor(dictionary=True)
cursor.execute("SELECT total_amount FROM books WHERE id = %s", (book_id,))
book = cursor.fetchone()
if not book:
cursor.close()
conn.close()
raise HTTPException(status_code=404, detail="Книгу не знайдено")
total = data.total_amount if (data.total_amount and data.total_amount > 0) else book["total_amount"]
if data.total_amount and data.total_amount > 0:
cursor.execute("UPDATE books SET total_amount = %s WHERE id = %s", (total, book_id))
new_prog = min(data.current_progress, total)
is_completed = new_prog >= total
if is_completed:
sql = """
UPDATE books
SET current_progress = %s, status = 'completed', rating = %s, completed_at = %s
WHERE id = %s
"""
cursor.execute(sql, (new_prog, data.rating or 5, date.today(), book_id))
else:
sql = "UPDATE books SET current_progress = %s, updated_at = NOW() WHERE id = %s"
cursor.execute(sql, (new_prog, book_id))
conn.commit()
cursor.close()
conn.close()
return {"status": "ok", "completed": is_completed}
@app.delete("/api/books/{book_id}")
def delete_book(book_id: int):
conn = get_db()
cursor = conn.cursor(dictionary=True)
cursor.execute("SELECT file_url, cover_url FROM books WHERE id = %s", (book_id,))
book = cursor.fetchone()
if not book:
cursor.close()
conn.close()
raise HTTPException(status_code=404, detail="Книгу не знайдено")
if book["file_url"] and "/uploads/books/" in book["file_url"]:
fname = book["file_url"].split("/uploads/books/")[-1]
for p in [os.path.join("uploads/books", fname), os.path.join("uploads/books", f"{fname}.txt")]:
if os.path.exists(p):
try:
os.remove(p)
except Exception:
pass
if book["cover_url"] and "/uploads/covers/" in book["cover_url"]:
cname = book["cover_url"].split("/uploads/covers/")[-1]
cpath = os.path.join("uploads/covers", cname)
if os.path.exists(cpath):
try:
os.remove(cpath)
except Exception:
pass
cursor.execute("DELETE FROM books WHERE id = %s", (book_id,))
conn.commit()
cursor.close()
conn.close()
return {"status": "deleted"}
@app.get("/api/books/{book_id}/notes")
def get_book_notes(book_id: int):
conn = get_db()
cursor = conn.cursor(dictionary=True)
cursor.execute("SELECT * FROM book_notes WHERE book_id = %s ORDER BY id DESC", (book_id,))
notes = cursor.fetchall()
cursor.close()
conn.close()
return notes
@app.post("/api/books/{book_id}/notes")
def add_book_note(book_id: int, data: NoteCreate):
if not data.note_text or not data.note_text.strip():
raise HTTPException(status_code=400, detail="Текст нотатки порожній")
conn = get_db()
cursor = conn.cursor()
cursor.execute(
"INSERT INTO book_notes (book_id, note_text, page_ref) VALUES (%s, %s, %s)",
(book_id, data.note_text.strip(), (data.page_ref or "").strip())
)
conn.commit()
new_id = cursor.lastrowid
cursor.close()
conn.close()
return {"id": new_id, "status": "created"}
@app.delete("/api/notes/{note_id}")
def delete_book_note(note_id: int):
conn = get_db()
cursor = conn.cursor()
cursor.execute("DELETE FROM book_notes WHERE id = %s", (note_id,))
conn.commit()
cursor.close()
conn.close()
return {"status": "deleted"}
@app.put("/api/books/{book_id}")
async def edit_book_details(
request: Request,
book_id: int,
title: str = Form(...),
author: str = Form("Невідомий автор"),
total_amount: int = Form(200),
cover_file: Optional[UploadFile] = File(None)
):
conn = get_db()
cursor = conn.cursor(dictionary=True)
cursor.execute("SELECT cover_url FROM books WHERE id = %s", (book_id,))
book = cursor.fetchone()
if not book:
cursor.close()
conn.close()
raise HTTPException(status_code=404, detail="Книгу не знайдено")
saved_cover = book["cover_url"]
if cover_file and cover_file.filename:
_, c_ext = os.path.splitext(cover_file.filename)
c_ext = c_ext or ".jpg"
c_name = f"{uuid.uuid4().hex}{c_ext}"
c_path = os.path.join("uploads/covers", c_name)
with open(c_path, "wb") as f:
shutil.copyfileobj(cover_file.file, f)
base_host = str(request.base_url).rstrip("/")
saved_cover = f"{base_host}/uploads/covers/{c_name}"
safe_total = max(1, total_amount)
cursor.execute(
"UPDATE books SET title = %s, author = %s, total_amount = %s, cover_url = %s WHERE id = %s",
(title.strip(), author.strip() or "Невідомий автор", safe_total, saved_cover, book_id)
)
conn.commit()
cursor.close()
conn.close()
return {"status": "updated"}
