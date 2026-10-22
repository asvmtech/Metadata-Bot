import os, re, json, asyncio, logging, shutil, subprocess, tempfile
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
from pymongo import MongoClient
from pymongo.errors import PyMongoError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('metadata-bot')

BOT_TOKEN = os.getenv('BOT_TOKEN', '8958744545:AAFz-pS6HpaMDgZ4FppsyimCuSsgde0Hod8').strip()
API_ID = os.getenv('API_ID', '32541562').strip()
API_HASH = os.getenv('API_HASH', 'e37e4432298d5a5eb4a6e32c18804283').strip()
DATABASE_URL = os.getenv('DATABASE_URL', 'mongodb+srv://asvm:incorrectasvm@cluster0.v2z8vnw.mongodb.net/?appName=Cluster0').strip()
MAX_FILE_MB = int(os.getenv('MAX_FILE_MB', '1800'))
WORK_DIR = Path(os.getenv('WORK_DIR', '/tmp/video_metadata_bot'))
WORK_DIR.mkdir(parents=True, exist_ok=True)

if not all([BOT_TOKEN, API_ID, API_HASH, DATABASE_URL]):
    raise RuntimeError('Set BOT_TOKEN, API_ID, API_HASH, and DATABASE_URL as environment variables.')

app = Client('video_metadata_bot', api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN)

mongo = MongoClient(DATABASE_URL, serverSelectionTimeoutMS=8000)
db = mongo['video_metadata_bot']
settings_col = db['user_settings']

# In-memory sessions hold only temporary local paths and are cleared after processing.
sessions = {}

LANGUAGES = [
    ('English', 'eng'), ('Hindi', 'hin'), ('Tamil', 'tam'), ('Telugu', 'tel'),
    ('Malayalam', 'mal'), ('Kannada', 'kan'), ('Bengali', 'ben'), ('Marathi', 'mar'),
    ('Punjabi', 'pan'), ('Urdu', 'urd'), ('French', 'fra'), ('Spanish', 'spa'),
    ('German', 'deu'), ('Japanese', 'jpn'), ('Korean', 'kor'), ('Arabic', 'ara'),
    ('Undetermined', 'und'),
]


def buttons(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows])


def run_cmd(args):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or 'Command failed')[-2500:])
    return proc.stdout


def probe(path):
    raw = run_cmd(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)])
    return json.loads(raw)


def metadata_summary(info):
    fmt = info.get('format', {})
    lines = [f"**Container:** `{fmt.get('format_name', 'Unknown')}`", f"**Duration:** `{round(float(fmt.get('duration', 0)))} sec`", '**Streams:**']
    for i, s in enumerate(info.get('streams', [])):
        typ = s.get('codec_type', 'unknown').title()
        tags = s.get('tags') or {}
        lang = tags.get('language', 'und')
        title = tags.get('title', '—')
        codec = s.get('codec_name', 'unknown')
        lines.append(f"• {i}: **{typ}** — `{codec}` | language: `{lang}` | title: `{title}`")
    tags = fmt.get('tags') or {}
    if tags:
        lines.append('**File metadata:**')
        for k, v in list(tags.items())[:20]:
            lines.append(f'• `{k}` = `{v}`')
    return '\n'.join(lines)


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip().strip('.')
    return name[:180] or 'video'


def edit_metadata(src, dst, cfg):
    info = probe(src)
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src), '-map', '0', '-map_metadata', '0', '-c', 'copy']
    # FFmpeg metadata keys are stream-indexed; preserve all streams and apply chosen values.
    for key in ('title', 'comment', 'description', 'artist', 'album', 'date', 'genre', 'copyright', 'encoder'):
        value = cfg.get('file_' + key, '').strip()
        if value:
            args += ['-metadata', f'{key}={value}']
    for idx, stream in enumerate(info.get('streams', [])):
        typ = stream.get('codec_type')
        if typ == 'audio':
            lang = cfg.get(f'audio_lang_{idx}', cfg.get('audio_lang', '')).strip()
            title = cfg.get(f'audio_title_{idx}', '').strip() or cfg.get('audio_title', '').strip()
        elif typ == 'subtitle':
            lang = cfg.get(f'sub_lang_{idx}', cfg.get('sub_lang', '')).strip()
            title = cfg.get(f'sub_title_{idx}', '').strip() or cfg.get('sub_title', '').strip()
        elif typ == 'video':
            lang = cfg.get(f'video_lang_{idx}', '').strip()
            title = cfg.get(f'video_title_{idx}', '').strip() or cfg.get('video_title', '').strip()
        else:
            continue
        if lang:
            args += ['-metadata:s:' + str(idx), f'language={lang}']
        if title:
            args += ['-metadata:s:' + str(idx), f'title={title}']
    # Avoid writing to same file; stream-copy keeps quality unchanged where supported.
    args += [str(dst)]
    run_cmd(args)
    return probe(dst)


def save_settings(user_id, cfg):
    try:
        settings_col.update_one({'_id': user_id}, {'$set': {'settings': cfg, 'updated_at': datetime.now(timezone.utc)}}, upsert=True)
    except PyMongoError as e:
        log.warning('Could not save settings to MongoDB: %s', e)


def load_settings(user_id):
    try:
        row = settings_col.find_one({'_id': user_id})
        return (row or {}).get('settings', {})
    except PyMongoError:
        return {}


@app.on_message(filters.command(['start', 'help']))
async def start(_, m: Message):
    await m.reply_text(
        '🎬 **Video Metadata Editor**\n\n'
        'Send me a video or document video. I will download it and show its current metadata. '
        'Then choose fields to edit, including audio/subtitle languages, stream titles, and file metadata.\n\n'
        'Commands:\n'
        '• /settings — view saved defaults\n'
        '• /cancel — clear current session\n\n'
        'Note: this bot edits metadata without re-encoding when the container supports it. Some formats may not support every tag.',
        reply_markup=buttons([[('⚙️ Settings', 'settings'), ('❌ Cancel', 'cancel')]])
    )


@app.on_message(filters.command('settings'))
async def settings_cmd(_, m: Message):
    cfg = load_settings(m.from_user.id)
    if not cfg:
        return await m.reply_text('No saved defaults yet. Send a video and edit its metadata first.')
    await m.reply_text('**Saved defaults**\n' + '\n'.join(f'• `{k}` = `{v}`' for k, v in cfg.items()))


@app.on_message(filters.command('cancel'))
async def cancel_cmd(_, m: Message):
    s = sessions.pop(m.from_user.id, None)
    if s:
        for p in s.get('paths', []):
            try: Path(p).unlink(missing_ok=True)
            except Exception: pass
    await m.reply_text('Cancelled and cleared the temporary session.')


@app.on_message((filters.video | filters.document) & filters.private)
async def receive_video(_, m: Message):
    media = m.video or m.document
    if not media:
        return
    name = getattr(media, 'file_name', None) or f'video_{m.id}.mp4'
    if m.video and not name.lower().endswith(('.mp4', '.mkv', '.mov', '.webm', '.m4v', '.avi')):
        name += '.mp4'
    if getattr(media, 'file_size', 0) > MAX_FILE_MB * 1024 * 1024:
        return await m.reply_text(f'File is larger than configured limit ({MAX_FILE_MB} MB). Increase MAX_FILE_MB only if your Render plan has enough disk and memory.')
    msg = await m.reply_text('⬇️ Downloading video…')
    uid = m.from_user.id
    user_dir = WORK_DIR / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    src = user_dir / safe_filename(name)
    try:
        downloaded = await app.download_media(m, file_name=str(src))
        if not downloaded:
            raise RuntimeError('Telegram did not return a downloaded file path.')
        src = Path(downloaded)
        await msg.edit_text('🔎 Reading existing metadata…')
        info = await asyncio.to_thread(probe, src)
        sessions[uid] = {'src': str(src), 'name': src.name, 'cfg': load_settings(uid), 'paths': [str(src)], 'info': info, 'prompt': None}
        await msg.edit_text(
            f'✅ **Video loaded:** `{src.name}`\n\n{metadata_summary(info)}\n\nChoose what to edit:',
            reply_markup=buttons([
                [('🌐 Audio language', 'edit_audio_lang'), ('💬 Subtitle language', 'edit_sub_lang')],
                [('🎧 Audio track title', 'edit_audio_title'), ('💬 Subtitle title', 'edit_sub_title')],
                [('🎞 Video stream title', 'edit_video_title'), ('📝 File title', 'edit_file_title')],
                [('🧾 Other file tags', 'other_tags'), ('✏️ Rename output file', 'rename')],
                [('👁 Preview metadata', 'preview'), ('✅ Apply & send', 'apply')],
                [('💾 Save current defaults', 'save'), ('❌ Cancel', 'cancel')]
            ])
        )
    except Exception as e:
        log.exception('Failed processing upload')
        await msg.edit_text(f'❌ Could not read this file. Make sure it is a supported video and try again.\n`{str(e)[-1200:]}`')


async def ask_text(c, key, prompt):
    uid = c.from_user.id
    s = sessions.get(uid)
    if not s:
        return await c.answer('Send a video first.', show_alert=True)
    s['prompt'] = key
    await c.message.reply_text(prompt + '\n\nSend `/skip` to leave it unchanged.')
    await c.answer()


@app.on_callback_query()
async def callbacks(_, c):
    uid = c.from_user.id
    s = sessions.get(uid)
    data = c.data
    if data == 'cancel':
        sessions.pop(uid, None)
        await c.message.reply_text('Session cancelled. Use /cancel to clear temporary files.')
        return await c.answer()
    if data == 'settings':
        cfg = load_settings(uid)
        await c.message.reply_text('**Saved defaults**\n' + ('\n'.join(f'• `{k}` = `{v}`' for k, v in cfg.items()) if cfg else 'No saved defaults yet.'))
        return await c.answer()
    if not s:
        return await c.answer('Send a video first.', show_alert=True)
    if data == 'edit_audio_lang':
        rows = [[(name, 'set_audio_lang:' + code) for name, code in LANGUAGES[i:i+2]] for i in range(0, len(LANGUAGES), 2)]
        rows.append([('Custom language code', 'custom_audio_lang')])
        await c.message.reply_text('Choose the language to apply to all audio streams:', reply_markup=buttons(rows))
    elif data.startswith('set_audio_lang:'):
        s['cfg']['audio_lang'] = data.split(':', 1)[1]
        await c.message.reply_text('Audio language set to `' + s['cfg']['audio_lang'] + '`.', reply_markup=buttons([[('⬅️ Menu', 'preview'), ('✅ Apply & send', 'apply')]]))
    elif data == 'custom_audio_lang':
        await ask_text(c, 'audio_lang', 'Send an ISO 639-2/B language code (examples: `eng`, `hin`, `tam`, `und`).')
    elif data == 'edit_sub_lang':
        rows = [[(name, 'set_sub_lang:' + code) for name, code in LANGUAGES[i:i+2]] for i in range(0, len(LANGUAGES), 2)]
        rows.append([('Custom language code', 'custom_sub_lang')])
        await c.message.reply_text('Choose the language to apply to all subtitle streams:', reply_markup=buttons(rows))
    elif data.startswith('set_sub_lang:'):
        s['cfg']['sub_lang'] = data.split(':', 1)[1]
        await c.message.reply_text('Subtitle language set to `' + s['cfg']['sub_lang'] + '`.', reply_markup=buttons([[('⬅️ Menu', 'preview'), ('✅ Apply & send', 'apply')]]))
    elif data == 'custom_sub_lang':
        await ask_text(c, 'sub_lang', 'Send an ISO 639-2/B language code (examples: `eng`, `hin`, `tam`, `und`).')
    elif data == 'edit_audio_title':
        await ask_text(c, 'audio_title', 'Send the title for all audio tracks, e.g. `Hindi Audio` or `English 5.1`.')
    elif data == 'edit_sub_title':
        await ask_text(c, 'sub_title', 'Send the title for all subtitle tracks, e.g. `English Subtitles`.')
    elif data == 'edit_video_title':
        await ask_text(c, 'video_title', 'Send the video stream title, e.g. `1080p Video`.')
    elif data == 'edit_file_title':
        await ask_text(c, 'file_title', 'Send the overall file title metadata.')
    elif data == 'other_tags':
        await ask_text(c, 'other_tags', 'Send tags as `artist=Name|album=Movie|year=2017|genre=Drama`. Supported: artist, album, date, genre, comment, description, copyright, encoder. Use `\|` for a literal pipe.')
    elif data == 'rename':
        await ask_text(c, 'rename', 'Send the output filename including extension, e.g. `The Movie (2017).mkv`.')
    elif data == 'preview':
        await c.message.reply_text('**Pending changes**\n' + ('\n'.join(f'• `{k}` = `{v}`' for k, v in s['cfg'].items()) if s['cfg'] else 'No changes selected yet.'), reply_markup=buttons([[('⬅️ Edit menu', 'menu'), ('✅ Apply & send', 'apply')]]))
    elif data == 'menu':
        await c.message.reply_text('Choose what to edit:', reply_markup=buttons([[('🌐 Audio language', 'edit_audio_lang'), ('💬 Subtitle language', 'edit_sub_lang')], [('🎧 Audio title', 'edit_audio_title'), ('💬 Subtitle title', 'edit_sub_title')], [('🎞 Video title', 'edit_video_title'), ('📝 File title', 'edit_file_title')], [('🧾 Other tags', 'other_tags'), ('✏️ Rename', 'rename')], [('👁 Preview', 'preview'), ('✅ Apply & send', 'apply')], [('💾 Save defaults', 'save')]]))
    elif data == 'save':
        save_settings(uid, s['cfg'])
        await c.message.reply_text('💾 Defaults saved to MongoDB.')
    elif data == 'apply':
        await c.answer('Processing…')
        await apply_and_send(c.message, uid)
        return
    await c.answer()


@app.on_message(filters.private & filters.text & ~filters.command(['start', 'help', 'settings', 'cancel', 'skip']))
async def text_input(_, m: Message):
    s = sessions.get(m.from_user.id)
    if not s or not s.get('prompt'):
        return
    key = s.pop('prompt')
    value = m.text.strip()
    if key == 'other_tags':
        tags = {}
        # Split unescaped pipes; allow \| within values.
        parts = re.split(r'(?<!\\)\|', value)
        allowed = {'artist','album','date','year','genre','comment','description','copyright','encoder'}
        for part in parts:
            if '=' not in part: continue
            k, v = part.split('=', 1)
            k = k.strip().lower(); v = v.replace('\\|', '|').strip()
            if k in allowed and v: tags['file_' + ('date' if k == 'year' else k)] = v
        s['cfg'].update(tags)
        await m.reply_text('Tags added: ' + (', '.join(tags) if tags else 'No valid tags found.'))
    elif key == 'rename':
        s['out_name'] = safe_filename(value)
        await m.reply_text(f'Output filename set to `{s["out_name"]}`.')
    elif key in ('audio_lang', 'sub_lang'):
        code = value.lower()
        if not re.fullmatch(r'[a-z]{3}', code):
            s['prompt'] = key
            return await m.reply_text('Please send a 3-letter ISO language code, e.g. `eng` or `hin`.')
        s['cfg'][key] = code
        await m.reply_text(f'{key.replace("_", " ").title()} set to `{code}`.')
    else:
        s['cfg'][key] = value
        await m.reply_text(f'Updated `{key}`.')
    await m.reply_text('Continue editing or apply changes:', reply_markup=buttons([[('⬅️ Edit menu', 'menu'), ('👁 Preview', 'preview')], [('💾 Save defaults', 'save'), ('✅ Apply & send', 'apply')]]))


@app.on_message(filters.command('skip'))
async def skip_input(_, m: Message):
    s = sessions.get(m.from_user.id)
    if s: s['prompt'] = None
    await m.reply_text('Skipped. Use the buttons to continue.')


async def apply_and_send(message, uid):
    s = sessions.get(uid)
    if not s:
        return await message.reply_text('Session expired. Send the video again.')
    src = Path(s['src'])
    original_suffix = src.suffix or '.mkv'
    out_name = s.get('out_name') or src.name
    if not Path(out_name).suffix:
        out_name += original_suffix
    dst = src.parent / ('edited_' + safe_filename(out_name))
    status = await message.reply_text('⚙️ Applying metadata…')
    try:
        info = await asyncio.to_thread(edit_metadata, src, dst, s['cfg'])
        s['paths'].append(str(dst))
        await status.edit_text('⬆️ Uploading edited video…')
        caption = '✅ **Metadata updated**\n\n' + metadata_summary(info)
        await app.send_document(uid, document=str(dst), file_name=safe_filename(out_name), caption=caption[:1024])
        save_settings(uid, s['cfg'])
        await status.delete()
    except Exception as e:
        log.exception('Metadata edit failed')
        await status.edit_text('❌ Could not apply metadata. The container may not support one of the requested tags, or FFmpeg failed.\n`' + str(e)[-1500:] + '`')
    finally:
        # Remove local copies to avoid filling Render's ephemeral disk.
        for p in s.get('paths', []):
            try: Path(p).unlink(missing_ok=True)
            except Exception: pass
        sessions.pop(uid, None)


if __name__ == '__main__':
    try:
        mongo.admin.command('ping')
        log.info('MongoDB connection successful')
    except Exception as e:
        log.warning('MongoDB connection check failed at startup: %s', e)
    log.info('Starting bot')
    app.run()
