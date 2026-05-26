"""
╔══════════════════════════════════════════════════════════════╗
║           NatokHub Telegram Bot — bot.py                     ║
║  Railway তে চলবে | GitHub API দিয়ে videos.json update করবে  ║
╚══════════════════════════════════════════════════════════════╝

পরিবেশ চলক (Railway Variables এ সেট করুন):
  BOT_TOKEN          = your_bot_token
  ADMIN_ID           = your_admin_id
  PRIMARY_CHANNEL    = your_primary_channel_id
  BACKUP_CHANNEL     = your_backup_channel_id
  GITHUB_TOKEN       = ghp_xxxxxxxxxxxx   (GitHub Personal Access Token)
  GITHUB_USERNAME    = your_github_username
  GITHUB_REPO        = natok-hub          (repository নাম)
"""

import os, json, asyncio, logging, base64, re
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import httpx

# ═══════════════════════════════════════════════════════════
#  CONFIG — Railway environment variables থেকে পড়বে
# ═══════════════════════════════════════════════════════════
BOT_TOKEN       = os.environ["BOT_TOKEN"]
ADMIN_ID        = int(os.environ["ADMIN_ID"])
PRIMARY_CH      = int(os.environ["PRIMARY_CHANNEL"])
BACKUP_CH       = int(os.environ["BACKUP_CHANNEL"])
GH_TOKEN        = os.environ["GITHUB_TOKEN"]
GH_USER         = os.environ["GITHUB_USERNAME"]
GH_REPO         = os.environ["GITHUB_REPO"]

# GitHub API — videos.json এর path
GH_FILE_PATH    = "videos.json"
GH_API_BASE     = f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/{GH_FILE_PATH}"
GH_HEADERS      = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
#  GITHUB HELPERS
# ═══════════════════════════════════════════════════════════
async def gh_get_json() -> tuple[dict, str]:
    """GitHub থেকে videos.json পড়ে। (data, sha) রিটার্ন করে।"""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(GH_API_BASE, headers=GH_HEADERS)
        r.raise_for_status()
        body = r.json()
        content = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(content), body["sha"]


async def gh_put_json(data: dict, sha: str, message: str) -> bool:
    """videos.json GitHub এ আপডেট করে।"""
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    payload = {
        "message": message,
        "content": encoded,
        "sha": sha,
        "branch": "main",
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.put(GH_API_BASE, headers=GH_HEADERS, json=payload)
        if r.status_code in (200, 201):
            return True
        log.error("GitHub PUT failed: %s — %s", r.status_code, r.text[:300])
        return False


# ═══════════════════════════════════════════════════════════
#  THUMBNAIL — GITHUB এ UPLOAD করে PERMANENT URL নাও
# ═══════════════════════════════════════════════════════════
async def upload_thumb_to_github(bot, file_id: str, msg_id: int) -> str | None:
    """
    Telegram থেকে thumbnail download করে GitHub এ upload করে।
    GitHub raw URL রিটার্ন করে — এটা permanent এবং website এ সবসময় দেখাবে।
    """
    try:
        # Telegram থেকে file download করো
        f = await bot.get_file(file_id)
        thumb_bytes = await f.download_as_bytearray()

        encoded  = base64.b64encode(bytes(thumb_bytes)).decode("utf-8")
        filename = f"thumbs/thumb_{msg_id}.jpg"
        api_url  = f"https://api.github.com/repos/{GH_USER}/{GH_REPO}/contents/{filename}"

        # আগে আছে কিনা check করো (update এর জন্য sha লাগবে)
        sha = None
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(api_url, headers=GH_HEADERS)
            if r.status_code == 200:
                sha = r.json().get("sha")

        payload = {
            "message": f"🖼️ Thumb for msg {msg_id}",
            "content": encoded,
            "branch":  "main",
        }
        if sha:
            payload["sha"] = sha  # existing file update

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.put(api_url, headers=GH_HEADERS, json=payload)
            if r.status_code in (200, 201):
                permanent_url = (
                    f"https://raw.githubusercontent.com/{GH_USER}/{GH_REPO}/main/{filename}"
                )
                log.info("🖼️ Thumbnail uploaded: %s", permanent_url)
                return permanent_url
            else:
                log.error("Thumb upload failed: %s — %s", r.status_code, r.text[:200])

    except Exception as e:
        log.error("upload_thumb_to_github error: %s", e)

    return None


# ═══════════════════════════════════════════════════════════
#  VIDEO ADD / REMOVE HELPERS
# ═══════════════════════════════════════════════════════════
def _detect_category(text: str) -> str:
    """Caption থেকে category অনুমান করে।"""
    t = (text or "").lower()
    if any(w in t for w in ["সিরিজ", "series", "web"]): return "series"
    if any(w in t for w in ["টেলিফিল্ম", "telefilm"]):  return "telefilm"
    if any(w in t for w in ["শর্ট", "short"]):           return "short"
    return "natok"


def _detect_episode(text: str):
    """Caption থেকে episode নম্বর বের করে।"""
    m = re.search(r"(?:ep|episode|পর্ব)[^\d]*(\d+)", (text or ""), re.I)
    return int(m.group(1)) if m else None


async def add_video(msg_id: int, title: str, thumb_url: str | None,
                    category: str, episode) -> bool:
    """videos.json এ নতুন ভিডিও যোগ করে।"""
    try:
        data, sha = await gh_get_json()
    except Exception as e:
        log.error("gh_get_json error: %s", e)
        return False

    videos: list = data.get("videos", [])

    # duplicate check
    if any(v.get("msgId") == msg_id for v in videos):
        return False

    # নতুন id বানাও
    new_id = max((v.get("id", 0) for v in videos), default=1000) + 1

    new_video = {
        "id":       new_id,
        "msgId":    msg_id,
        "title":    title,
        "thumb":    thumb_url,
        "category": category,
        "episode":  episode,
        "date":     datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    videos.insert(0, new_video)          # সবার উপরে রাখো
    data["videos"] = videos
    data["total"]  = len(videos)
    data["updated"] = datetime.now(timezone.utc).isoformat()

    ok = await gh_put_json(data, sha, f"➕ Add: {title[:60]}")
    if ok:
        log.info("✅ Added video id=%s title=%s", new_id, title)
    return ok


async def remove_video(msg_id: int) -> bool:
    """videos.json থেকে ভিডিও সরায়।"""
    try:
        data, sha = await gh_get_json()
    except Exception as e:
        log.error("gh_get_json error: %s", e)
        return False

    before  = len(data.get("videos", []))
    videos  = [v for v in data.get("videos", []) if v.get("msgId") != msg_id]
    if len(videos) == before:
        return False                    # পাওয়া যায়নি

    data["videos"] = videos
    data["total"]  = len(videos)
    data["updated"] = datetime.now(timezone.utc).isoformat()

    ok = await gh_put_json(data, sha, f"🗑️ Remove msgId={msg_id}")
    if ok:
        log.info("🗑️ Removed video msgId=%s", msg_id)
    return ok


async def get_video_by_id(vid_id: int) -> dict | None:
    """video id দিয়ে ভিডিও খোঁজে।"""
    try:
        data, _ = await gh_get_json()
        for v in data.get("videos", []):
            if v.get("id") == vid_id:
                return v
    except Exception as e:
        log.error("get_video_by_id error: %s", e)
    return None


# ═══════════════════════════════════════════════════════════
#  CHANNEL POST HANDLER
#  Primary ও Backup channel এ video আসলে চালায়
# ═══════════════════════════════════════════════════════════
async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return

    chat_id = msg.chat_id
    if chat_id not in (PRIMARY_CH, BACKUP_CH):
        return

    # ── ভিডিও বা ডকুমেন্ট (video file) চেক ──
    video    = msg.video or msg.document
    is_video = bool(msg.video or (msg.document and msg.document.mime_type and
                                  msg.document.mime_type.startswith("video")))
    if not is_video:
        return

    msg_id  = msg.message_id
    caption = msg.caption or (msg.video.file_name if msg.video else "") or ""
    title   = caption.strip()[:200] or f"ভিডিও — {msg_id}"

    # ── Thumbnail — GitHub এ upload করে permanent URL নাও ──
    thumb_url = None
    if msg.video and msg.video.thumbnail:
        thumb_url = await upload_thumb_to_github(ctx.bot, msg.video.thumbnail.file_id, msg_id)
    elif msg.document and msg.document.thumbnail:
        thumb_url = await upload_thumb_to_github(ctx.bot, msg.document.thumbnail.file_id, msg_id)

    if thumb_url:
        log.info("🖼️ Thumbnail ready: %s", thumb_url)
    else:
        log.warning("⚠️ No thumbnail for msg_id=%s", msg_id)

    category = _detect_category(caption)
    episode  = _detect_episode(caption)

    log.info("📹 New video detected | chat=%s msg_id=%s title=%s", chat_id, msg_id, title[:40])

    ok = await add_video(msg_id, title, thumb_url, category, episode)
    if ok:
        log.info("🌐 Website updated for msg_id=%s", msg_id)


# ═══════════════════════════════════════════════════════════
#  USER START — deep link handler
#  User website থেকে ad দেখার পর bot link করলে video পাঠায়
#  Link format: t.me/bot?start=get_<video_id>
# ═══════════════════════════════════════════════════════════
async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    user = update.effective_user

    if not args:
        # Normal start — welcome message
        await update.message.reply_text(
            f"👋 স্বাগতম {user.first_name}!\n\n"
            "🎬 NatokHub থেকে বাংলা নাটক দেখুন ফ্রিতে।\n\n"
            f"🌐 Website: https://{GH_USER}.github.io/{GH_REPO}/\n\n"
            "📢 Channel: @bachelor_point_bd",
            disable_web_page_preview=True
        )
        return

    arg = args[0]   # e.g. "get_1003"

    # ── video send ──
    if arg.startswith("get_"):
        try:
            vid_id = int(arg.split("_", 1)[1])
        except ValueError:
            await update.message.reply_text("❌ লিংকটি সঠিক নয়।")
            return

        await update.message.reply_text("⏳ ভিডিও খোঁজা হচ্ছে...")

        video = await get_video_by_id(vid_id)
        if not video:
            await update.message.reply_text("❌ ভিডিওটি পাওয়া যায়নি।")
            return

        msg_id = video.get("msgId")

        # Primary channel থেকে forward করার চেষ্টা
        sent = False
        for ch in (PRIMARY_CH, BACKUP_CH):
            try:
                await ctx.bot.forward_message(
                    chat_id=user.id,
                    from_chat_id=ch,
                    message_id=msg_id,
                )
                sent = True
                log.info("✅ Video sent | user=%s vid=%s ch=%s", user.id, vid_id, ch)
                break
            except Exception as e:
                log.warning("Forward failed from %s: %s", ch, e)

        if not sent:
            await update.message.reply_text(
                "❌ ভিডিওটি পাঠানো যায়নি।\n"
                "সরাসরি channel এ দেখুন: t.me/bachelor_point_bd"
            )
        return

    await update.message.reply_text("❓ অজানা লিংক।")


# ═══════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════
def admin_only(func):
    """Decorator — শুধু ADMIN_ID চালাতে পারবে।"""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ আপনার permission নেই।")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


@admin_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/stats — কতটা ভিডিও আছে দেখায়"""
    try:
        data, _ = await gh_get_json()
        total   = data.get("total", 0)
        updated = data.get("updated", "N/A")
        await update.message.reply_text(
            f"📊 *NatokHub Stats*\n\n"
            f"🎬 মোট ভিডিও: *{total}*\n"
            f"🕐 শেষ আপডেট: `{updated}`\n"
            f"🌐 Website: [দেখুন](https://{GH_USER}.github.io/{GH_REPO}/)",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


@admin_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/remove <msg_id> — manually ভিডিও সরায়"""
    if not ctx.args:
        await update.message.reply_text("Usage: /remove <telegram_message_id>")
        return
    try:
        msg_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ সঠিক message ID দিন।")
        return

    await update.message.reply_text("⏳ সরানো হচ্ছে...")
    ok = await remove_video(msg_id)
    if ok:
        await update.message.reply_text(f"✅ msg_id={msg_id} website থেকে সরানো হয়েছে।")
    else:
        await update.message.reply_text(f"❌ msg_id={msg_id} পাওয়া যায়নি বা error হয়েছে।")


@admin_only
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/list — সর্বশেষ ১০টি ভিডিও দেখায়"""
    try:
        data, _ = await gh_get_json()
        videos  = data.get("videos", [])[:10]
        if not videos:
            await update.message.reply_text("কোনো ভিডিও নেই।")
            return
        lines = ["📋 *সর্বশেষ ভিডিও:*\n"]
        for v in videos:
            lines.append(
                f"🎬 `{v['id']}` | msg:`{v['msgId']}` | {v['title'][:35]}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


@admin_only
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sync — manually GitHub থেকে sync check করে"""
    try:
        data, sha = await gh_get_json()
        await update.message.reply_text(
            f"✅ GitHub sync OK\n"
            f"📄 SHA: `{sha[:12]}...`\n"
            f"🎬 Videos: {data.get('total', 0)}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ GitHub sync failed: {e}")


@admin_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help — admin commands"""
    await update.message.reply_text(
        "🤖 *NatokHub Bot — Admin Commands*\n\n"
        "/stats — মোট ভিডিও ও status দেখুন\n"
        "/list — সর্বশেষ ১০টি ভিডিও\n"
        "/remove <msg\\_id> — ভিডিও সরান\n"
        "/sync — GitHub connection চেক করুন\n"
        "/help — এই তালিকা",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start", on_start))

    # Admin commands
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("sync",   cmd_sync))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Channel video detector (new + edited posts)
    ch_filter = filters.ChatType.CHANNEL & (
        filters.VIDEO | filters.Document.VIDEO
    )
    app.add_handler(MessageHandler(ch_filter, on_channel_post))

    log.info("🚀 NatokHub Bot চালু হয়েছে...")
    log.info("📡 Channels: Primary=%s | Backup=%s", PRIMARY_CH, BACKUP_CH)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
