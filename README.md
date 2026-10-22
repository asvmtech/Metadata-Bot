# Telegram Video Metadata Editor Bot

A Telegram bot that downloads a video, displays existing container/stream metadata, lets the user change file title/tags, audio language/title, subtitle language/title, video stream title, and output filename, then sends the edited file back. It uses FFmpeg stream-copy (`-c copy`) to avoid re-encoding when the format supports metadata changes.

## Render deployment
1. Push these files to a GitHub repository.
2. In Render, choose **New + → Blueprint** and select the repository (uses `render.yaml`), or create a Background Worker manually.
3. Add environment variables in Render: `BOT_TOKEN`, `API_ID`, `API_HASH`, `DATABASE_URL`.
4. Deploy. FFmpeg is installed by the build command.

## Environment variables
- `BOT_TOKEN`: token from BotFather.
- `API_ID` / `API_HASH`: Telegram API credentials from my.telegram.org.
- `DATABASE_URL`: MongoDB Atlas connection string. Ensure Atlas Network Access allows Render's outbound connection (for quick testing, `0.0.0.0/0`; use stronger restrictions where practical).
- `MAX_FILE_MB`: max accepted file size (default 1800 MB). Telegram, Pyrogram, and Render limits still apply.

## Important limitations
- Render's local disk is ephemeral; files are deleted after processing. This bot does not persist videos.
- Metadata support depends on the container and codec. Some MP4/MKV tags may be ignored or normalized by FFmpeg/player software.
- Language is set as a language tag (e.g. `eng`, `hin`, `und`); it does not translate audio or subtitles, and it does not detect the spoken language automatically.
- Stream metadata indices can differ from player numbering; inspect the preview after processing.
- Keep your bot token, API hash, and MongoDB password private. If any credentials have been posted publicly, rotate them before deployment.
