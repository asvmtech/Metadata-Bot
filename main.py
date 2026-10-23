import os, re, json, time, asyncio, logging, shutil, subprocess, threading
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, jsonify
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Configure these in Render Environment Variables. Do not hardcode API secrets.
BOT_TOKEN = os.getenv('BOT_TOKEN', '8958744545:AAFz-pS6HpaMDgZ4FppsyimCuSsgde0Hod8').strip()
API_ID = os.getenv('API_ID', '32541562').strip()
API_HASH = os.getenv('API_HASH', 'e37e4432298d5a5eb4a6e32c18804283').strip()
DATABASE_URL = os.getenv('DATABASE_URL', 'mongodb+srv://asvm:incorrectasvm@cluster0.v2z8vnw.mongodb.net/?appName=Cluster0').strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "4096"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/video_metadata_bot"))
PORT = int(os.getenv("PORT", "10000"))

if not all([BOT_TOKEN, API_ID, API_HASH, DATABASE_URL]):
    raise RuntimeError("Set BOT_TOKEN, API_ID, API_HASH and DATABASE_URL in Render Environment Variables.")

WORK_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("metadata-bot")

# Render Web Service port/health check
web = Flask(__name__)
@web.get("/")
def home():
    return jsonify(status="online", service="Video Metadata Editor Bot"), 200
@web.get("/health")
def health():
    return jsonify(status="healthy"), 200
threading.Thread(
    target=lambda: web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True),
    daemon=True, name="health-server"
).start()

app = Client("video_metadata_bot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN)
mongo = MongoClient(DATABASE_URL, serverSelectionTimeoutMS=8000)
settings_col = mongo["video_metadata_bot"]["user_settings"]
sessions = {}
user_locks = {}

LANGUAGES = [
    ("English", "eng"), ("Hindi", "hin"), ("Urdu", "urd"), ("Tamil", "tam"),
    ("Telugu", "tel"), ("Malayalam", "mal"), ("Kannada", "kan"), ("Bengali", "ben"),
    ("Marathi", "mar"), ("Punjabi", "pan"), ("French", "fra"), ("Spanish", "spa"),
    ("German", "deu"), ("Japanese", "jpn"), ("Korean", "kor"), ("Arabic", "ara"),
    ("Undetermined", "und")
]
LANGUAGE_NAMES = dict(LANGUAGES)
FILE_TAGS = {"title", "artist", "album", "date", "year", "genre", "comment", "description", "copyright", "encoder", "composer", "performer", "publisher", "language", "synopsis", "show", "season_number", "episode_id", "network", "creation_time"}

def buttons(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])

def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip().strip(".")
    return name[:180] or "video"

def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"

def bar(pct, width=14):
    pct = max(0, min(100, int(pct)))
    return "▓" * int(width * pct / 100) + "░" * (width - int(width * pct / 100))

def storage_report():
    u = shutil.disk_usage(WORK_DIR)
    return f"💾 **Storage** — Used `{fmt_size(u.used)}` | Free `{fmt_size(u.free)}` | Total `{fmt_size(u.total)}`"

def run_cmd(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or "Command failed")[-2200:])
    return p.stdout

def probe(path):
    return json.loads(run_cmd(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))

def lang_name(code):
    code = str(code or "und").lower()
    return f"{LANGUAGE_NAMES.get(code, code)} (`{code}`)"

def summary(info):
    f = info.get("format", {})
    dur = float(f.get("duration") or 0)
    lines = [
        f"**Title:** `{(f.get('tags') or {}).get('title', '—')}`",
        f"**Format:** `{f.get('format_name', 'Unknown')}`",
        f"**Duration:** `{int(dur//60)}m {int(dur%60)}s`",
        f"**File size:** `{fmt_size(f.get('size', 0))}`",
        "**Streams:**"
    ]
    for s in info.get("streams", []):
        typ = s.get("codec_type", "unknown")
        tags = s.get("tags") or {}
        extra = ""
        if typ == "video":
            extra = f" | {s.get('width','?')}x{s.get('height','?')}"
        elif typ == "audio":
            extra = f" | {s.get('channels','?')} channels, {s.get('sample_rate','?')} Hz"
        lines.append(
            f"• {typ.title()} `{s.get('codec_name','unknown')}`"
            f" | {lang_name(tags.get('language','und'))}"
            f" | title: `{tags.get('title','—')}`{extra}"
        )
    tags = f.get("tags") or {}
    if tags:
        lines.append("**File metadata:**")
        for k, v in list(tags.items())[:15]:
            lines.append(f"• `{k}` = `{str(v)[:100]}`")
    return "\n".join(lines)

def load_settings(uid):
    try:
        row = settings_col.find_one({"_id": int(uid)})
        return (row or {}).get("settings", {})
    except PyMongoError:
        log.exception("Settings read failed")
        return {}

def save_settings(uid, cfg):
    try:
        settings_col.update_one(
            {"_id": int(uid)},
            {"$set": {"settings": cfg, "updated_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    except PyMongoError:
        log.exception("Settings save failed")

def lock_for(uid):
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            log.debug("Edit message failed: %s", e)

def menu():
    return buttons([
        [("🌐 Audio language", "edit_audio_lang"), ("💬 Subtitle language", "edit_sub_lang")],
        [("🎧 Audio title", "edit_audio_title"), ("💬 Subtitle title", "edit_sub_title")],
        [("🎞 Video title", "edit_video_title"), ("📝 File title", "edit_file_title")],
        [("🧾 More metadata", "other_tags"), ("✏️ Rename output", "rename")],
        [("🖼️ Thumbnail", "thumbnail"), ("📎 Attachment", "attachment")],
        [("🎬 Add 10s intro", "intro"), ("👁 Preview", "preview")],
        [("✅ Apply & send", "apply")],
        [("💾 Save defaults", "save"), ("❌ Cancel", "cancel")]
    ])

def language_menu(prefix):
    rows = [[(n, f"set_{prefix}_lang:{c}") for n, c in LANGUAGES[i:i+2]] for i in range(0, len(LANGUAGES), 2)]
    rows.append([("Custom language code", f"custom_{prefix}_lang"), ("⬅️ Menu", "menu")])
    return buttons(rows)

async def ask_text(c, key, prompt):
    s = sessions.get(c.from_user.id)
    if not s:
        return await c.answer("Send a video first.", show_alert=True)
    s["prompt"] = key
    await c.message.reply_text(prompt + "\n\nSend /skip to leave unchanged.")
    await c.answer()

async def clear_session(uid):
    s = sessions.pop(uid, None)
    if s:
        for p in s.get("paths", []):
            try: Path(p).unlink(missing_ok=True)
            except OSError: log.exception("File cleanup failed")
    d = WORK_DIR / str(uid)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)

@app.on_message(filters.command(["start", "help"]) & filters.private)
async def start(_, m: Message):
    await m.reply_text(
        "🎬 **Video Metadata Editor**\n\nSend a video/document. Edit audio and subtitle languages, stream titles, file tags or filename.\n\n"
        "/settings — saved defaults\n/cancel — cancel and clear temporary files\n/cleanup — clear temporary files\n\n"
        "Metadata is changed using FFmpeg stream copy where supported (no re-encoding).",
        reply_markup=buttons([[("⚙️ Settings", "settings"), ("❌ Cancel", "cancel")]])
    )

@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(_, m: Message):
    cfg = load_settings(m.from_user.id)
    await m.reply_text("**Saved defaults**\n" + ("\n".join(f"• `{k}` = `{v}`" for k,v in cfg.items()) if cfg else "No saved defaults yet."))

@app.on_message(filters.command(["cancel", "cleanup"]) & filters.private)
async def cleanup_cmd(_, m: Message):
    uid = m.from_user.id
    lock = lock_for(uid)
    if lock.locked():
        return await m.reply_text("⚠️ A task is running. Wait until it finishes before cleanup.")
    await clear_session(uid)
    try:
        settings_col.delete_one({"_id": int(uid)})
    except PyMongoError:
        log.exception("Could not clear this user's MongoDB settings")
        return await m.reply_text("⚠️ Local temporary files cleared, but MongoDB settings could not be deleted.")
    await m.reply_text(
        "🧹 **Cleanup complete**\\n"
        "Temporary files and your saved settings were removed from Render storage and MongoDB.\\n\\n"
        + storage_report()
    )

@app.on_message((filters.video | filters.document) & filters.private)
async def receive_video(_, m: Message):
    # Extra media (thumbnail, attachment, intro) belongs to the active session,
    # not to a new main-video upload.
    active = sessions.get(m.from_user.id) if m.from_user else None
    if active and active.get("awaiting_media"):
        return
    media = m.video or m.document
    if not media:
        return
    name = getattr(media, "file_name", None) or f"video_{m.id}.mp4"
    if m.video and not Path(name).suffix:
        name += ".mp4"
    size = int(getattr(media, "file_size", 0) or 0)
    if size > MAX_FILE_MB * 1024 * 1024:
        return await m.reply_text(f"File exceeds configured limit: {MAX_FILE_MB} MB.")
    uid = m.from_user.id
    lock = lock_for(uid)
    if lock.locked() or uid in sessions:
        return await m.reply_text(
            "⚠️ You already have a video session. Finish editing and upload it, "
            "or use /cancel before sending another video."
        )
    async with lock:
        status = await m.reply_text("⬇️ Preparing download…")
        d = WORK_DIR / str(uid)
        d.mkdir(parents=True, exist_ok=True)
        last = {"t": 0.0}
        async def dl_progress(current, total):
            now = time.monotonic()
            if now-last["t"] < 1 and current < total: return
            last["t"] = now
            pct = current*100/total if total else 0
            await safe_edit(status, f"⬇️ **Downloading**\n\n`{bar(pct)}` **{pct:.1f}%**\n"
                f"Downloaded: `{fmt_size(current)}` / `{fmt_size(total)}`\n"
                f"Remaining: `{fmt_size(max(0,total-current))}`\n\n{storage_report()}")
        try:
            downloaded = await app.download_media(m, file_name=str(d / safe_filename(name)), progress=dl_progress)
            if not downloaded: raise RuntimeError("Telegram returned no downloaded file path.")
            src = Path(downloaded)
            await safe_edit(status, "🔎 Reading file metadata…")
            info = await asyncio.to_thread(probe, src)
            sessions[uid] = {"src": str(src), "name": src.name, "cfg": load_settings(uid),
                             "paths": [str(src)], "info": info, "prompt": None,
                             "thumbnail": None, "attachment": None, "intro": None}
            await safe_edit(status, f"✅ **Loaded:** `{src.name}`\n\n{summary(info)}\n\nChoose what to edit:", menu())
        except Exception as e:
            log.exception("Download/probe failed")
            await safe_edit(status, f"❌ Could not process file.\n`{str(e)[-1000:]}`")

@app.on_callback_query()
async def callbacks(_, c):
    uid, data = c.from_user.id, c.data or ""
    s = sessions.get(uid)
    if data == "cancel":
        if lock_for(uid).locked():
            return await c.answer("A task is running; wait for it to finish.", show_alert=True)
        await clear_session(uid)
        await c.message.reply_text("Cancelled; temporary files cleared.")
        return await c.answer()
    if data == "settings":
        cfg = load_settings(uid)
        await c.message.reply_text("**Saved defaults**\n" + ("\n".join(f"• `{k}` = `{v}`" for k,v in cfg.items()) if cfg else "No saved defaults."))
        return await c.answer()
    if not s:
        return await c.answer("Send a video first.", show_alert=True)
    if data == "edit_audio_lang":
        await safe_edit(c.message, "Choose audio language:", reply_markup=language_menu("audio"))
    elif data == "edit_sub_lang":
        await safe_edit(c.message, "Choose subtitle language:", reply_markup=language_menu("sub"))
    elif data.startswith("set_audio_lang:"):
        s["cfg"]["audio_lang"] = data.split(":",1)[1]
        await c.message.reply_text("Audio language set to " + lang_name(s["cfg"]["audio_lang"]), reply_markup=menu())
    elif data.startswith("set_sub_lang:"):
        s["cfg"]["sub_lang"] = data.split(":",1)[1]
        await c.message.reply_text("Subtitle language set to " + lang_name(s["cfg"]["sub_lang"]), reply_markup=menu())
    elif data == "custom_audio_lang":
        await ask_text(c, "audio_lang", "Send 3-letter language code, e.g. eng, hin, urd, und."); return
    elif data == "custom_sub_lang":
        await ask_text(c, "sub_lang", "Send 3-letter language code, e.g. eng, hin, urd, und."); return
    elif data == "edit_audio_title":
        await ask_text(c, "audio_title", "Send audio title, e.g. Hindi Audio or Urdu AAC."); return
    elif data == "edit_sub_title":
        await ask_text(c, "sub_title", "Send subtitle title, e.g. Hindi Subtitles."); return
    elif data == "edit_video_title":
        await ask_text(c, "video_title", "Send video stream title."); return
    elif data == "edit_file_title":
        await ask_text(c, "file_title", "Send overall file title metadata."); return
    elif data == "other_tags":
        await ask_text(c, "other_tags", "Send tags: artist=Name|album=Movie|year=2026|genre=Drama|comment=Test."); return
    elif data == "rename":
        await ask_text(c, "rename", "Send output filename including extension, e.g. Movie (2026).mkv."); return
    elif data == "thumbnail":
        s["awaiting_media"] = "thumbnail"
        await safe_edit(c.message, "🖼️ Send the photo/image to use as the Telegram upload thumbnail. This sets the Telegram preview thumbnail; it does not guarantee VLC embedded-cover support for every container.", reply_markup=buttons([[("⬅️ Menu", "menu")]]))
    elif data == "attachment":
        s["awaiting_media"] = "attachment"
        await safe_edit(c.message, "📎 Send a document to attach with the final video. It will be sent alongside the edited video.", reply_markup=buttons([[("⬅️ Menu", "menu")]]))
    elif data == "intro":
        s["awaiting_media"] = "intro"
        await safe_edit(c.message, "🎬 Send a video clip of about 10 seconds. It will be joined before the main video. For smooth playback, FFmpeg may need to re-encode the intro and main video; this takes longer and uses extra temporary disk space.", reply_markup=buttons([[("⬅️ Menu", "menu")]]))
    elif data == "preview":
        cfg = s["cfg"]
        await safe_edit(c.message, "**Pending changes**\n" + ("\n".join(f"• `{k}` = `{v}`" for k,v in cfg.items()) if cfg else "No changes selected."), reply_markup=menu())
    elif data == "menu":
        await safe_edit(c.message, "Choose what to edit:", reply_markup=menu())
    elif data == "save":
        save_settings(uid, s["cfg"])
        await c.message.reply_text("💾 Defaults saved to MongoDB.")
    elif data == "apply":
        if not s.get("cfg"):
            await c.answer("Choose at least one metadata change first.", show_alert=True)
            return
        await c.answer("Processing…")
        await apply_and_send(c.message, uid)
        return
    await c.answer()


@app.on_message((filters.photo | filters.video | filters.document) & filters.private)
async def receive_extra_media(_, m: Message):
    uid = m.from_user.id
    s = sessions.get(uid)
    if not s or not s.get("awaiting_media"):
        return
    kind = s.pop("awaiting_media")
    d = WORK_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    status = await m.reply_text(f"⬇️ Downloading {kind}…")
    try:
        if kind == "thumbnail":
            media = m.photo or m.document
            if not media:
                return await safe_edit(status, "Please send an image/photo.")
            ext = ".jpg"
            target = d / f"thumbnail_{uid}.jpg"
        elif kind == "intro":
            media = m.video or (m.document if getattr(m.document, "mime_type", "").startswith("video/") else None)
            if not media:
                s["awaiting_media"] = "intro"
                return await safe_edit(status, "Please send a video clip for the intro.")
            target = d / f"intro_{uid}{Path(getattr(media, 'file_name', '') or 'intro.mp4').suffix or '.mp4'}"
        else:
            media = m.document
            if not media:
                s["awaiting_media"] = "attachment"
                return await safe_edit(status, "Please send the attachment as a document.")
            target = d / safe_filename(getattr(media, "file_name", None) or f"attachment_{uid}")
        downloaded = await app.download_media(m, file_name=str(target))
        if not downloaded:
            raise RuntimeError("Telegram did not return a file path.")
        path = Path(downloaded)
        s["paths"].append(str(path))
        s[kind] = str(path)
        await safe_edit(status, f"✅ {kind.title()} saved: `{path.name}`", reply_markup=menu())
    except Exception as e:
        log.exception("Extra media download failed")
        await safe_edit(status, f"❌ Could not save {kind}: `{str(e)[-500:]}`", reply_markup=menu())

@app.on_message(filters.private & filters.text & ~filters.command(["start","help","settings","cancel","cleanup","skip"]))
async def text_input(_, m: Message):
    s = sessions.get(m.from_user.id)
    if not s or not s.get("prompt"): return
    key, value = s.pop("prompt"), m.text.strip()
    if key == "other_tags":
        tags = {}
        for part in re.split(r"(?<!\\)\|", value):
            if "=" not in part: continue
            k,v = part.split("=",1); k=k.strip().lower(); v=v.replace("\\|","|").strip()
            if k in FILE_TAGS and v:
                if k == "year": k = "date"
                tags["file_"+k] = v
        s["cfg"].update(tags)
        await m.reply_text("Tags updated: " + (", ".join(tags) if tags else "No valid tags found."))
    elif key == "rename":
        if not Path(value).suffix: value += Path(s["name"]).suffix or ".mkv"
        s["out_name"] = safe_filename(value)
        await m.reply_text(f"Output filename set to `{s['out_name']}`.")
    elif key in ("audio_lang","sub_lang"):
        value = value.lower()
        if not re.fullmatch(r"[a-z]{3}", value):
            s["prompt"] = key
            return await m.reply_text("Use a 3-letter code such as eng, hin, urd or und.")
        s["cfg"][key] = value
        await m.reply_text(f"{key.replace('_',' ').title()} set to {lang_name(value)}.")
    else:
        s["cfg"][key] = value
        await m.reply_text(f"Updated `{key}`.")
    await m.reply_text("Continue editing:", reply_markup=menu())

@app.on_message(filters.command("skip") & filters.private)
async def skip_cmd(_, m: Message):
    s = sessions.get(m.from_user.id)
    if s: s["prompt"] = None
    await m.reply_text("Skipped.", reply_markup=menu() if s else None)


def join_intro(intro, main_video, output, progress_callback=None):
    """Prepend intro to main video. Re-encodes both to compatible H.264/AAC for reliable concatenation."""
    # Create normalized temporary files then concat; paths are kept beside the source for cleanup.
    base = output.parent
    part1 = base / "intro_normalized.mp4"
    part2 = base / "main_normalized.mp4"
    concat_list = base / "concat_list.txt"
    try:
        for idx, (input_path, part) in enumerate(((intro, part1), (main_video, part2))):
            run_cmd([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", str(part)
            ])
        concat_list.write_text(f"file '{part1.as_posix()}'\\nfile '{part2.as_posix()}'\\n", encoding="utf-8")
        run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat_list), "-c", "copy", "-movflags", "+faststart", str(output)])
        return output
    finally:
        for p in (part1, part2, concat_list):
            try: p.unlink(missing_ok=True)
            except OSError: pass


def ffmpeg_edit(src, dst, cfg, callback):
    info = probe(src)
    args = ["ffmpeg","-hide_banner","-y","-i",str(src),"-map","0","-map_metadata","0","-c","copy",
            "-progress","pipe:1","-nostats","-loglevel","error"]
    for key in ("title","comment","description","artist","album","date","genre","copyright","encoder",
                "composer","performer","publisher","language","synopsis","show","season_number",
                "episode_id","network","creation_time"):
        value = str(cfg.get("file_"+key,"")).strip()
        if value: args += ["-metadata",f"{key}={value}"]
    counters = {"audio":0,"subtitle":0,"video":0}
    for st in info.get("streams",[]):
        typ=st.get("codec_type")
        if typ not in counters: continue
        i=counters[typ]; counters[typ]+=1
        if typ=="audio":
            lang=cfg.get(f"audio_lang_{i}",cfg.get("audio_lang","")).strip()
            title=cfg.get(f"audio_title_{i}",cfg.get("audio_title","")).strip(); spec=f"a:{i}"
        elif typ=="subtitle":
            lang=cfg.get(f"sub_lang_{i}",cfg.get("sub_lang","")).strip()
            title=cfg.get(f"sub_title_{i}",cfg.get("sub_title","")).strip(); spec=f"s:{i}"
        else:
            lang=cfg.get(f"video_lang_{i}",cfg.get("video_lang","")).strip()
            title=cfg.get(f"video_title_{i}",cfg.get("video_title","")).strip(); spec=f"v:{i}"
        if lang: args += [f"-metadata:s:{spec}",f"language={lang}"]
        if title: args += [f"-metadata:s:{spec}",f"title={title}"]
    args.append(str(dst))
    duration=float((info.get("format") or {}).get("duration") or 0)
    proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    try:
        for line in proc.stdout:
            if "=" not in line: continue
            k,v=line.strip().split("=",1)
            if k == "out_time_ms" and duration and callback:
                try:
                    raw_time = v.strip()
                    if raw_time and raw_time.upper() != "N/A":
                        elapsed = int(raw_time) / 1_000_000
                        callback(min(99.0, max(0.0, elapsed / duration * 100)))
                except (ValueError, TypeError, OverflowError):
                    # FFmpeg sometimes emits N/A for progress fields.
                    pass
        err=proc.stderr.read() if proc.stderr else ""
        rc=proc.wait()
        if rc: raise RuntimeError((err or "FFmpeg failed")[-2000:])
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()
    if callback: callback(100)
    return probe(dst)

async def apply_and_send(message, uid):
    s=sessions.get(uid)
    if not s: return await message.reply_text("Session expired. Send the video again.")
    lock=lock_for(uid)
    if lock.locked(): return await message.reply_text("A task is already running.")
    async with lock:
        s=sessions.get(uid)
        if not s: return await message.reply_text("Session expired.")
        src=Path(s["src"])
        if not s.get("cfg"):
            return await message.reply_text(
                "⚠️ No metadata changes selected. Choose at least one edit before uploading."
            )
        if not src.exists():
            sessions.pop(uid,None)
            return await message.reply_text("Source file is missing. Send it again.")
        out_name=s.get("out_name") or src.name
        if not Path(out_name).suffix: out_name += src.suffix or ".mkv"
        dst=src.parent/("edited_"+safe_filename(out_name))
        status=await message.reply_text("⚙️ Applying metadata…")
        loop=asyncio.get_running_loop()
        last={"t":0.0}
        def proc_progress(pct):
            now=time.monotonic()
            if now-last["t"]<1 and pct<100: return
            last["t"]=now
            asyncio.run_coroutine_threadsafe(
                safe_edit(status,f"⚙️ **Applying metadata**\n\n`{bar(pct)}` **{pct:.1f}%**"),
                loop
            )
        try:
            source_for_edit = src
            joined = None
            if s.get("intro") and Path(s["intro"]).exists():
                joined = src.parent / ("intro_joined_" + safe_filename(src.stem) + ".mp4")
                await safe_edit(status, "🎬 Joining intro + main video… This re-encodes for smooth playback and may take a while.")
                await asyncio.to_thread(join_intro, Path(s["intro"]), src, joined)
                s["paths"].append(str(joined))
                source_for_edit = joined
            info=await asyncio.to_thread(ffmpeg_edit,source_for_edit,dst,s["cfg"],proc_progress)
            total=dst.stat().st_size
            up_last={"t":0.0}
            async def upload_progress(current,total_bytes):
                now=time.monotonic()
                if now-up_last["t"]<1 and current<total_bytes: return
                up_last["t"]=now
                pct=current*100/total_bytes if total_bytes else 0
                await safe_edit(status,f"⬆️ **Uploading edited video**\n\n`{bar(pct)}` **{pct:.1f}%**\n"
                    f"Uploaded: `{fmt_size(current)}` / `{fmt_size(total_bytes)}`\n"
                    f"Remaining: `{fmt_size(max(0,total_bytes-current))}`")
            caption="✅ **Metadata updated**\n\n"+summary(info)
            thumb = s.get("thumbnail")
            thumb_path = Path(thumb) if thumb else None
            if thumb_path and not thumb_path.exists():
                thumb_path = None
            # Telegram thumbnails are only used for the upload preview; supported formats/sizes are enforced by Telegram.
            await app.send_document(
                uid, document=str(dst), file_name=safe_filename(out_name),
                caption=caption[:1024], thumb=str(thumb_path) if thumb_path else None,
                progress=upload_progress
            )
            attachment = s.get("attachment")
            if attachment and Path(attachment).exists():
                await app.send_document(uid, document=attachment, caption="📎 Attachment")
            save_settings(uid,s["cfg"])
            await safe_edit(status,"✅ **Done!** Edited video uploaded successfully.")
        except Exception as e:
            log.exception("Metadata edit/upload failed")
            await safe_edit(status,f"❌ Processing failed.\n`{str(e)[-1200:]}`")
        finally:
            current=sessions.pop(uid,None)
            for p in ((current or s).get("paths",[])+[str(dst)]):
                try: Path(p).unlink(missing_ok=True)
                except OSError: log.exception("Temporary file cleanup failed")
            shutil.rmtree(WORK_DIR/str(uid),ignore_errors=True)

if __name__ == "__main__":
    try:
        mongo.admin.command("ping")
        log.info("MongoDB connection successful")
    except Exception:
        log.exception("MongoDB connection check failed")
    log.info("Starting bot; HTTP health server listening on port %s", PORT)
    app.run()
