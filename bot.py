import asyncio, hashlib, json, logging, os, re, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatMember
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneCodeHashEmptyError,
)

# ─── config ────────────────────────────────────────────────
API_ID    = 34624990
API_HASH  = "22ca915edd923b06a62e6f3855a2cd02"
BOT_TOKEN = "8754153575:AAF-DrYK6buWoayAqXAuv0hoH7MsS5VZJm0"
OWNER_ID  = 7434333153

FORCE_CHANNEL     = "ᴋᴀᴅᴠᴀ ᴊᴀꜱʜᴀɴ ᴏᴘ"
FORCE_CHANNEL_URL = "https://t.me/KADVAJASHANOP"
DEFAULT_SESSION   = "FYTEREGOSESSION"

BASE_DIR      = Path(__file__).parent.resolve()
USERBOTS_DIR  = BASE_DIR / "userbots"
SESSIONS_DIR  = BASE_DIR / "sessions"
LOGS_DIR      = BASE_DIR / "logs"
REGISTRY_FILE = BASE_DIR / "registry.json"

for d in (USERBOTS_DIR, SESSIONS_DIR, LOGS_DIR):
    d.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("egohost")


def load_registry():
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_registry():
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


registry    = load_registry()
login_state = {}
pair_codes  = {}
id_map      = {}
join_cache  = {}


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def tail_log(name: str, n: int = 40) -> str:
    p = LOGS_DIR / f"{sanitize(name)}.log"
    if not p.exists():
        return "(no logs)"
    size = p.stat().st_size
    with open(p, "rb") as f:
        try:
            f.seek(-min(size, 8192), os.SEEK_END)
        except OSError:
            f.seek(0)
        data = f.read().decode("utf-8", "replace").splitlines()
    return "\n".join(data[-n:])


def short_id(name: str) -> str:
    sid = hashlib.md5(name.encode()).hexdigest()[:10]
    id_map[sid] = name
    return sid


# ─── session helpers ───────────────────────────────────────

def session_path(session_name: str) -> Path:
    return SESSIONS_DIR / session_name


def session_file(session_name: str) -> Path:
    return SESSIONS_DIR / f"{session_name}.session"


def has_session(session_name: str) -> bool:
    return session_file(session_name).exists()


def wipe_session(session_name: str):
    for suffix in (".session", ".session-journal"):
        p = SESSIONS_DIR / f"{session_name}{suffix}"
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            log.warning(f"wipe {p}: {e}")


# ─── process control ───────────────────────────────────────

def spawn(name: str):
    meta = registry.get(name)
    if not meta:
        return False, "not registered"
    fpath = USERBOTS_DIR / meta["file"]
    if not fpath.exists():
        return False, "file missing"
    if pid_alive(meta.get("pid")):
        return False, "already running"

    sess_name = meta.get("session", DEFAULT_SESSION)
    if not has_session(sess_name):
        return False, f"no session '{sess_name}' — pair first"

    logf = open(LOGS_DIR / f"{sanitize(name)}.log", "ab", buffering=0)
    env = os.environ.copy()
    env.update({
        "EGO_SESSION":  str(session_path(sess_name)),
        "EGO_NAME":     name,
        "EGO_API_ID":   str(API_ID),
        "EGO_API_HASH": API_HASH,
        "EGO_OWNER_ID": str(OWNER_ID),
        "PYTHONUNBUFFERED": "1",
    })

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(fpath)],
            stdout=logf, stderr=subprocess.STDOUT,
            cwd=str(USERBOTS_DIR),
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        logf.close()
        return False, str(e)

    meta.update({
        "pid":        proc.pid,
        "status":     "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": None,
    })
    save_registry()
    return True, f"pid={proc.pid}"


def stop(name: str):
    meta = registry.get(name)
    if not meta:
        return False, "not registered"
    pid = meta.get("pid")
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(12):
                time.sleep(0.25)
                if not pid_alive(pid):
                    break
            if pid_alive(pid):
                os.kill(int(pid), signal.SIGKILL)
        except OSError as e:
            log.warning(f"kill {name}: {e}")
    meta["pid"]    = None
    meta["status"] = "stopped"
    save_registry()
    return True, "stopped"


def stop_all():
    for n in list(registry.keys()):
        if registry[n].get("status") == "running":
            stop(n)


# ─── force join ────────────────────────────────────────────

async def is_joined(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    cached = join_cache.get(user_id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        member = await context.bot.get_chat_member(f"@{FORCE_CHANNEL}", user_id)
        ok = member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception:
        ok = False
    join_cache[user_id] = (ok, time.time() + 60)
    return ok


async def require_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id == OWNER_ID:
        return True
    if await is_joined(context, user.id):
        return True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=FORCE_CHANNEL_URL)],
        [InlineKeyboardButton("✅ I've Joined",  callback_data="force:check")],
    ])
    text = (
        "🚫 <b>Access Restricted</b>\n\n"
        f"Join <b>@{FORCE_CHANNEL}</b> first to use this bot.\n"
        "After joining, tap <b>I've Joined</b>."
    )
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return False
# ─── keyboards ─────────────────────────────────────────────

def kb_main():
    total   = len(registry)
    running = sum(1 for m in registry.values() if m.get("status") == "running")
    owner_ok = has_session(DEFAULT_SESSION)
    sess_label = "🔑 Owner Session OK" if owner_ok else "⚠️ Owner Login"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📁 Files ({total})",     callback_data="menu:files"),
         InlineKeyboardButton(f"🟢 Running ({running})", callback_data="menu:running")],
        [InlineKeyboardButton("➕ Add File",             callback_data="menu:add"),
         InlineKeyboardButton("📖 Template",             callback_data="menu:template")],
        [InlineKeyboardButton(sess_label,                callback_data="menu:ownerlogin"),
         InlineKeyboardButton("🔄 Refresh",              callback_data="menu:main")],
    ])


def kb_files():
    rows = []
    for name, meta in sorted(registry.items()):
        status = meta.get("status", "stopped")
        icon   = {"running": "🟢", "stopped": "⚪", "crashed": "💀"}.get(status, "⚪")
        sid    = short_id(name)
        label  = f"{icon} {meta.get('file', name)}"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"file:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_file(name: str):
    meta     = registry.get(name, {})
    running  = meta.get("status") == "running"
    sess     = meta.get("session", DEFAULT_SESSION)
    sid      = short_id(name)
    first_row = (
        [InlineKeyboardButton("⏹ Stop", callback_data=f"act:{sid}:stop")]
        if running else
        [InlineKeyboardButton("▶️ Start", callback_data=f"act:{sid}:start")]
    )
    return InlineKeyboardMarkup([
        first_row,
        [InlineKeyboardButton("🔄 Restart", callback_data=f"act:{sid}:restart"),
         InlineKeyboardButton("📜 Logs",    callback_data=f"act:{sid}:logs")],
        [InlineKeyboardButton(f"🔑 Pair / Login ({sess})", callback_data=f"act:{sid}:pair"),
         InlineKeyboardButton("🗑 Delete",  callback_data=f"act:{sid}:delete")],
        [InlineKeyboardButton("⬅️ Back",    callback_data="menu:files")],
    ])


# ─── /start ────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 This bot is private.")
        return
    running = sum(1 for m in registry.values() if m.get("status") == "running")
    sess    = "✅" if has_session(DEFAULT_SESSION) else "⚠️"
    await update.message.reply_text(
        "<b>⚡ EGO MULTI-USERBOT HOST</b>\n\n"
        f"Files: <code>{len(registry)}</code>\n"
        f"Running: <code>{running}</code>\n"
        f"Owner Session: {sess} <code>{DEFAULT_SESSION}</code>\n\n"
        "Upload <code>.py</code> files, pair sessions, start them.",
        parse_mode="HTML", reply_markup=kb_main()
    )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await begin_login_for_session(update, DEFAULT_SESSION, edit=False)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = login_state.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass
    await update.message.reply_text("❌ Cancelled.", reply_markup=kb_main())


async def cmd_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    uid = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/pair CODE</code>", parse_mode="HTML")
        return
    code = args[0].strip().upper()
    entry = pair_codes.get(code)
    if not entry:
        await update.message.reply_text("❌ Invalid or expired pair code.")
        return
    if entry["expires"] < time.time():
        pair_codes.pop(code, None)
        await update.message.reply_text("❌ Pair code expired.")
        return

    name       = entry["file"]
    session_nm = entry["session"]

    await begin_login_for_session(
        update, session_nm, edit=False,
        bound_file=name,
        register_id=uid,
        intro=(
            f"🔑 <b>Pairing for</b> <code>{name}</code>\n\n"
            "Send your Telegram phone number with country code:\n"
            "<code>+919876543210</code>\n\n"
            "/cancel to abort"
        )
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "<b>Commands</b>\n\n"
        "/start — panel\n"
        "/login — owner session login\n"
        "/pair &lt;code&gt; — friend pairs a userbot\n"
        "/cancel — abort login\n"
        "/help — this",
        parse_mode="HTML", reply_markup=kb_main()
    )


# ─── login engine ──────────────────────────────────────────

async def begin_login_for_session(target, session_name: str, edit: bool = False,
                                  bound_file=None,
                                  register_id=None,
                                  intro=None):
    uid = register_id if register_id is not None else OWNER_ID

    wipe_session(session_name)

    login_state[uid] = {
        "stage":        "phone",
        "client":       None,
        "phone":        None,
        "hash":         None,
        "sent_at":      0,
        "session_name": session_name,
        "bound_file":   bound_file,
    }

    text = intro or (
        f"🔑 <b>Login — {session_name}</b>\n\n"
        "Send phone number with country code:\n"
        "<code>+919876543210</code>\n\n"
        "/cancel to abort"
    )

    if edit and hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="HTML")
    elif hasattr(target, "message") and target.message:
        await target.message.reply_text(text, parse_mode="HTML")
    else:
        await target.reply_text(text, parse_mode="HTML")


async def handle_login_text(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    text  = (update.message.text or "").strip()
    stage = st["stage"]
    uid   = update.effective_user.id

    if stage == "phone":
        phone = text
        if not phone.startswith("+"):
            return await update.message.reply_text("Include country code, e.g. +91...")

        wipe_session(st["session_name"])

        client = TelegramClient(str(session_path(st["session_name"])), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(phone, force_sms=True)
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            login_state.pop(uid, None)
            return await update.message.reply_text(f"❌ send_code: {e}")

        st.update({
            "stage":   "code",
            "phone":   phone,
            "hash":    sent.phone_code_hash,
            "client":  client,
            "sent_at": time.time(),
        })
        await update.message.reply_text(
            "📨 OTP sent via SMS.\n"
            "Send the code within 90 seconds.\n"
            "Do <b>not</b> enter it in the official app first — that invalidates it here.",
            parse_mode="HTML"
        )

    elif stage == "code":
        client = st["client"]
        if time.time() - st.get("sent_at", 0) > 90:
            try:
                await client.disconnect()
            except Exception:
                pass
            login_state.pop(uid, None)
            return await update.message.reply_text("❌ Code window expired. Start again.")

        code = text.replace(" ", "").replace("-", "")
        try:
            await client.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            st["stage"] = "password"
            return await update.message.reply_text("🔐 2FA enabled. Send password:")
        except (PhoneCodeExpiredError, PhoneCodeHashEmptyError):
            try:
                await client.disconnect()
            except Exception:
                pass
            login_state.pop(uid, None)
            return await update.message.reply_text("❌ Code expired. Start again.")
        except PhoneCodeInvalidError:
            return await update.message.reply_text("❌ Wrong code. Try again:")
        except Exception as e:
            err = str(e)
            if "previously shared" in err.lower() or "expired" in err.lower():
                try:
                    await client.disconnect()
                except Exception:
                    pass
                login_state.pop(uid, None)
                return await update.message.reply_text(
                    "❌ Telegram rejected the code (previously shared). Start again."
                )
            return await update.message.reply_text(f"❌ {err}")

        try:
            await client.disconnect()
        except Exception:
            pass
        login_state.pop(uid, None)
        await finalize_login(update, context, st)

    elif stage == "password":
        client = st["client"]
        try:
            await client.sign_in(password=text)
        except Exception as e:
            return await update.message.reply_text(f"❌ {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        login_state.pop(uid, None)
        await finalize_login(update, context, st)


async def finalize_login(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict):
    sess_name = st["session_name"]
    bound     = st.get("bound_file")
    if bound and bound in registry:
        registry[bound]["session"] = sess_name
        save_registry()
        await update.message.reply_text(
            f"✅ Session <code>{sess_name}</code> paired to <b>{bound}</b>.",
            parse_mode="HTML", reply_markup=kb_file(bound)
        )
    else:
        await update.message.reply_text(
            f"✅ Session saved: <code>{sess_name}</code>",
            parse_mode="HTML", reply_markup=kb_main()
        )
# ─── button handler ────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid  = q.from_user.id

    if data == "force:check":
        await q.answer()
        if await is_joined(context, uid):
            join_cache.pop(uid, None)
            await q.edit_message_text("✅ Verified. Send /start again.")
        else:
            await q.answer("You haven't joined yet.", show_alert=True)
        return

    if not await require_join(update, context):
        return

    if uid != OWNER_ID:
        await q.answer("Not owner", show_alert=True)
        return

    await q.answer()

    if data == "menu:main":
        running = sum(1 for m in registry.values() if m.get("status") == "running")
        sess    = "✅" if has_session(DEFAULT_SESSION) else "⚠️"
        await q.edit_message_text(
            "<b>⚡ EGO MULTI-USERBOT HOST</b>\n\n"
            f"Files: <code>{len(registry)}</code>\n"
            f"Running: <code>{running}</code>\n"
            f"Owner Session: {sess} <code>{DEFAULT_SESSION}</code>",
            parse_mode="HTML", reply_markup=kb_main()
        )

    elif data == "menu:files":
        if not registry:
            await q.edit_message_text("No files yet. Tap ➕ Add File.", reply_markup=kb_main())
        else:
            await q.edit_message_text("<b>📁 Userbot Files</b>", parse_mode="HTML", reply_markup=kb_files())

    elif data == "menu:running":
        running = [n for n, m in registry.items() if m.get("status") == "running"]
        txt = ("<b>🟢 Running</b>\n\n" +
               "\n".join(f"• <code>{n}</code>" for n in running)) if running else "Nothing running."
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb_main())

    elif data == "menu:add":
        await q.edit_message_text(
            "Send a <code>.py</code> file as a document.\nIt'll be saved to <code>userbots/</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:main")]])
        )

    elif data == "menu:ownerlogin":
        await begin_login_for_session(q, DEFAULT_SESSION, edit=True)

    elif data == "menu:template":
        tpl = (
            "<b>📖 Userbot Template</b>\n\n"
            "<pre>"
            "import os\n"
            "from telethon import TelegramClient, events\n\n"
            "API_ID   = int(os.environ[\"EGO_API_ID\"])\n"
            "API_HASH = os.environ[\"EGO_API_HASH\"]\n"
            "SESSION  = os.environ[\"EGO_SESSION\"]\n"
            "OWNER    = int(os.environ[\"EGO_OWNER_ID\"])\n\n"
            "client = TelegramClient(SESSION, API_ID, API_HASH)\n\n"
            "@client.on(events.NewMessage(outgoing=True, pattern=r\"\\\\.ping$\"))\n"
            "async def ping(e):\n"
            "    await e.edit(\"pong\")\n\n"
            "async def _main():\n"
            "    await client.connect()\n"
            "    if not await client.is_user_authorized():\n"
            "        print(\"FATAL: session not authorized\")\n"
            "        return\n"
            "    await client.run_until_disconnected()\n\n"
            "if __name__ == \"__main__\":\n"
            "    import asyncio; asyncio.run(_main())\n"
            "</pre>"
        )
        await q.edit_message_text(
            tpl, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:main")]])
        )

    elif data.startswith("file:"):
        sid  = data.split(":", 1)[1]
        name = id_map.get(sid)
        meta = registry.get(name)
        if not name or not meta:
            return await q.edit_message_text("Gone.", reply_markup=kb_main())
        status  = meta.get("status", "stopped")
        icon    = {"running": "🟢", "stopped": "⚪", "crashed": "💀"}.get(status, "⚪")
        pid     = meta.get("pid") or "-"
        started = meta.get("started_at") or "-"
        sess_nm = meta.get("session", DEFAULT_SESSION)
        sess    = "✅" if has_session(sess_nm) else "❌"
        txt = (
            f"<b>{icon} {meta.get('file', name)}</b>\n\n"
            f"Status: <code>{status}</code>\n"
            f"PID: <code>{pid}</code>\n"
            f"Session: {sess} <code>{sess_nm}</code>\n"
            f"Started: <code>{started}</code>"
        )
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb_file(name))

    elif data.startswith("act:"):
        _, sid, action = data.split(":", 2)
        name = id_map.get(sid)
        meta = registry.get(name)
        if not name or not meta:
            return await q.edit_message_text("Gone.", reply_markup=kb_main())

        if action == "start":
            ok, info = spawn(name)
            await q.answer(info, show_alert=not ok)
            await q.edit_message_text(f"<b>{name}</b>\n<code>{info}</code>",
                                      parse_mode="HTML", reply_markup=kb_file(name))

        elif action == "stop":
            ok, info = stop(name)
            await q.answer(info)
            await q.edit_message_text(f"<b>{name}</b>\n<code>{info}</code>",
                                      parse_mode="HTML", reply_markup=kb_file(name))

        elif action == "restart":
            stop(name)
            await asyncio.sleep(1)
            ok, info = spawn(name)
            await q.answer(info, show_alert=not ok)
            await q.edit_message_text(f"<b>{name}</b>\n<code>{info}</code>",
                                      parse_mode="HTML", reply_markup=kb_file(name))

        elif action == "logs":
            txt = tail_log(name, 30)
            await q.edit_message_text(
                f"<b>📜 {name}</b>\n<pre>{txt[:3500]}</pre>",
                parse_mode="HTML", reply_markup=kb_file(name)
            )

        elif action == "pair":
            # generate a pair code bound to this file + a per-file session
            code = f"{hashlib.md5((name + str(time.time())).encode()).hexdigest()[:6].upper()}"
            sess_nm = sanitize(name)
            pair_codes[code] = {
                "file":    name,
                "session": sess_nm,
                "expires": time.time() + 600,
            }
            await q.edit_message_text(
                f"🔑 <b>Pair Code for</b> <code>{name}</code>\n\n"
                f"Code: <code>{code}</code>\n"
                f"Session will be: <code>{sess_nm}</code>\n\n"
                f"Ask your friend to send:\n<code>/pair {code}</code>\n\n"
                f"<i>Or tap below to login this session yourself (owner).</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Login as Owner", callback_data=f"ownlogin:{sid}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"file:{sid}")],
                ])
            )

        elif action == "delete":
            stop(name)
            for p in (USERBOTS_DIR / meta["file"], LOGS_DIR / f"{sanitize(name)}.log"):
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            registry.pop(name, None)
            save_registry()
            await q.edit_message_text(f"🗑 <b>{name}</b> deleted.",
                                      parse_mode="HTML", reply_markup=kb_files())

    elif data.startswith("ownlogin:"):
        sid  = data.split(":", 1)[1]
        name = id_map.get(sid)
        if not name or name not in registry:
            return await q.edit_message_text("Gone.", reply_markup=kb_main())
        sess_nm = sanitize(name)
        await begin_login_for_session(
            q, sess_nm, edit=True, bound_file=name, register_id=OWNER_ID
        )


# ─── upload handler ────────────────────────────────────────

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    fname = doc.file_name
    if not fname.endswith(".py"):
        return await update.message.reply_text("Only <code>.py</code> files accepted.",
                                               parse_mode="HTML")

    safe  = sanitize(fname)
    name  = safe[:-3]
    fpath = USERBOTS_DIR / safe

    if name in registry and registry[name].get("status") == "running":
        stop(name)

    tgfile = await doc.get_file()
    await tgfile.download_to_drive(custom_path=str(fpath))

    prev = registry.get(name, {})
    registry[name] = {
        "file":       safe,
        "session":    prev.get("session", DEFAULT_SESSION),
        "status":     "stopped",
        "pid":        None,
        "started_at": None,
        "last_error": None,
    }
    save_registry()

    sess_nm = registry[name]["session"]
    sess    = "✅" if has_session(sess_nm) else "⚠️ no session"
    await update.message.reply_text(
        f"✅ Uploaded: <b>{fname}</b>\n\n"
        f"Session: {sess} <code>{sess_nm}</code>\n\n"
        "▶️ Start — spawn\n"
        "🔑 Pair / Login — set session",
        parse_mode="HTML", reply_markup=kb_file(name)
    )


# ─── text handler (login + fallthrough) ────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st  = login_state.get(uid)
    if st:
        if not await require_join(update, context):
            return
        return await handle_login_text(update, context, st)


# ─── watchdog / boot / main ────────────────────────────────

async def watchdog(context: ContextTypes.DEFAULT_TYPE):
    changed = False
    for name, meta in list(registry.items()):
        if meta.get("status") != "running":
            continue
        if pid_alive(meta.get("pid")):
            continue
        meta["status"]     = "crashed"
        meta["pid"]        = None
        meta["last_error"] = tail_log(name, 6)[-400:]
        changed = True
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"💀 <b>{name}</b> died.\n"
                f"<pre>{(meta['last_error'] or '')[:400]}</pre>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    if changed:
        save_registry()


def reconcile_on_boot():
    changed = False
    for meta in registry.values():
        if meta.get("status") == "running" and not pid_alive(meta.get("pid")):
            meta["status"] = "crashed"
            meta["pid"]    = None
            changed = True
    if changed:
        save_registry()
    login_state.clear()


async def post_init(app: Application):
    if not has_session(DEFAULT_SESSION):
        try:
            await app.bot.send_message(
                OWNER_ID,
                "⚠️ No owner session found.\nUse /login to bind one.",
                parse_mode="HTML"
            )
        except Exception:
            pass


def main():
    reconcile_on_boot()
    print("╔══════════════════════════════════════════════╗")
    print("║  ⚡ EGO MULTI-USERBOT HOST — single file ⚡  ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Files: {len(registry)}  |  Dir: {BASE_DIR}")

    req = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(req)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("login",  cmd_login))
    app.add_handler(CommandHandler("pair",   cmd_pair))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        app.job_queue.run_repeating(watchdog, interval=5, first=5)

    print("✅ Bot ready. Send /start in Telegram.")
    app.run_polling(drop_pending_updates=True, poll_interval=2.0, timeout=30)


if __name__ == "__main__":
    main()