"""Interactive chat workflows."""
from __future__ import annotations
import asyncio, re, shutil, sys, threading, subprocess, shlex
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Any, Optional
from tgai.ui import (action_menu, clear, display_messages, edit_message, reformulate_flow, select_chat_interactive, 
                     select_chat_text, ACTION_SEND, ACTION_EDIT, ACTION_REFORMULATE, ACTION_REMEMBER, 
                     ACTION_MANUAL, ACTION_SKIP, format_messages, _word_wrap, _fmt_date)
from tgai.chat_view import (CHAT_BOTTOM_GAP, max_scroll as _chat_max_scroll, visible_lines as _chat_visible_lines, window_start as _chat_window_start)

async def _poll_chat_list(tg, dh, ah):
    l_sum = 0
    while True:
        try: await asyncio.sleep(2.0)
        except asyncio.CancelledError: break
        try:
            nd = await tg.get_dialogs(limit=50)
            c_sum = sum(d.id + getattr(d, "unread_count", 0) for d in nd)
            if c_sum == l_sum: continue
            l_sum = c_sum
            dh[0] = nd
            if ah and ah[0] and ah[0].is_running: ah[0].invalidate()
        except Exception: pass

async def _load_entity(tg, ident):
    try: return await tg.resolve_entity(ident)
    except Exception as e: print(f"Ошибка: {e}"); return None

async def _run_chat(tg, cl, st, ent, pers, txt):
    me = await tg.get_me()
    cid = getattr(ent, "id", 0)
    ai = getattr(cl, "provider_name", "") == "yandexgpt"
    cl.set_history(cid, st.load_history(cid))
    msgs, l_msg = [], [None]
    from tgai.telegram import is_broadcast_channel
    chan = is_broadcast_channel(ent)
    try: await tg.mark_read(ent)
    except Exception: pass
    if not txt:
        await _run_chat_fullscreen(tg, cl, st, ent, pers, me, msgs, chan, cid, ai, l_msg)
        return l_msg[0]
    return None

async def _run_chat_fullscreen(tg, cl, st, ent, pers, me, all_msgs, chan, cid, ai, l_msg_h):
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import ANSI, FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout.dimension import Dimension

    loop = asyncio.get_running_loop()
    app_h = [None]
    lines, l2m = [], []
    scroll = [0]
    loading = [True]
    m_cache = st.load_media_cache()
    pending = {"file_path": None}
    
    def _inv():
        if app_h[0] and app_h[0].is_running: app_h[0].invalidate()
    def _rebuild():
        nonlocal lines, l2m
        lines, l2m = format_messages(list(all_msgs), me.id)

    async def _proc_media(m):
        has_m = (hasattr(m, "photo") and m.photo) or (hasattr(m, "media") and m.media)
        if not has_m: return
        mid = ""
        if hasattr(m, "photo") and m.photo: mid = str(m.photo.id)
        elif hasattr(m, "media") and hasattr(m.media, "photo") and m.media.photo: mid = str(m.media.photo.id)
        elif hasattr(m, "media") and hasattr(m.media, "document") and m.media.document: mid = str(m.media.document.id)
        
        if not mid: return
        if mid in m_cache:
            res = m_cache[mid]
            cur = getattr(m, "text", "") or ""
            if "[медиа]" not in cur:
                setattr(m, "text", res if not cur or cur == "[медиа]" else f"{res}\n{cur}")
                _rebuild(); _inv()
            return
        try:
            bytes = await tg.download_media(m)
            if not bytes: return
            desc, raw_ocr = await asyncio.gather(loop.run_in_executor(None, lambda: cl.describe_image(bytes)), loop.run_in_executor(None, lambda: cl.ocr_image(bytes)))
            clean = await loop.run_in_executor(None, lambda: cl.clean_ocr_text(raw_ocr)) if raw_ocr else ""
            parts = []
            if desc: parts.append(f"\033[32m[медиа]\033[0m: {desc}")
            else: parts.append("\033[32m[медиа]\033[0m")
            if clean: parts.append(f"\033[36m[текст]\033[0m: {clean}")
            res = " | ".join(parts)
            m_cache[mid] = res; st.save_media_cache(m_cache)
            setattr(m, "text", res if not getattr(m, "text", "") or getattr(m, "text", "") == "[медиа]" else f"{res}\n(m.text if hasattr(m, 'text') else '')")
            _rebuild(); _inv()
        except Exception: pass

    async def _init_load():
        try:
            m_list = await tg.get_messages(ent, limit=100)
            all_msgs.extend(m_list)
            now = datetime.now(timezone.utc)
            day_ago = now - timedelta(days=1)
            p_cnt = 0
            for m in m_list[:10]:
                has_m = (hasattr(m, "photo") and m.photo) or (hasattr(m, "media") and m.media)
                if has_m:
                    m_dt = getattr(m, "date", None)
                    if m_dt and m_dt.replace(tzinfo=timezone.utc) > day_ago and p_cnt < 3:
                        asyncio.create_task(_proc_media(m)); p_cnt += 1
            _rebuild(); loading[0] = False; _inv()
        except Exception: loading[0] = False; _inv()

    asyncio.create_task(_init_load())

    async def _do_send(t):
        try:
            f = pending["file_path"]
            if f:
                sent = await tg.send_file(ent, f, caption=t)
                pending["file_path"] = None
                if sent:
                    all_msgs.insert(0, sent)
                    asyncio.create_task(_proc_media(sent))
            else:
                sent = await tg.send_message(ent, t)
                if sent: all_msgs.insert(0, sent)
            _rebuild(); _inv()
        except Exception: pass

    kb = KeyBindings()
    in_buf = Buffer()
    
    def _max_sc(): return _chat_max_scroll(len(lines), shutil.get_terminal_size().lines)
    
    @kb.add("up", eager=True)
    def _(e): scroll[0] = min(scroll[0] + 1, _max_sc()); _inv()
    @kb.add("down", eager=True)
    def _(e): scroll[0] = max(scroll[0] - 1, 0); _inv()
    @kb.add("<scroll-up>", eager=True)
    def _(e): scroll[0] = min(scroll[0] + 3, _max_sc()); _inv()
    @kb.add("<scroll-down>", eager=True)
    def _(e): scroll[0] = max(scroll[0] - 3, 0); _inv()

    def _get_foc():
        _, r = shutil.get_terminal_size()
        vis = _chat_visible_lines(r)
        st_pos = _chat_window_start(len(lines), scroll[0], r)
        for i in range(st_pos + vis - 1, st_pos - 1, -1):
            if i < len(l2m) and l2m[i]:
                m = l2m[i]
                if (hasattr(m, "photo") and m.photo) or (hasattr(m, "media") and m.media): return m
        return None

    @kb.add("c-v")
    async def _(e):
        m = _get_foc()
        if not m: return
        p = await tg.client.download_media(m, file="/tmp/tgai_media")
        if p: subprocess.run(["qlmanage", "-p", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @kb.add("c-c")
    async def _(e):
        m = _get_foc()
        if not m: return
        p = await tg.client.download_media(m, file="/tmp/tgai_media")
        if p:
            script = f'set the clipboard to (read (POSIX file "{p}") as TIFF picture)'
            subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @kb.add("c-p")
    async def _(e):
        dest = shutil.os.path.join("/tmp/tgai_media", f"p_{int(datetime.now().timestamp())}.png")
        shutil.os.makedirs("/tmp/tgai_media", exist_ok=True)
        script = f'try\nset f to open for access "{dest}" with write permission\nset d to (get the clipboard as «class PNGf»)\nwrite d to f\nclose access f\nreturn "ok"\non error\ntry\nclose access "{dest}"\nend try\nreturn "err"\nend try'
        try:
            res = subprocess.check_output(["osascript", "-e", script], stderr=subprocess.DEVNULL).decode().strip()
            if res == "ok": pending["file_path"] = dest; _inv()
        except Exception: pass

    @kb.add("escape")
    def _(e): e.app.exit()
    @kb.add("left")
    def _(e):
        if in_buf.cursor_position == 0: e.app.exit()
        else: in_buf.cursor_left()
    @kb.add("right")
    def _(e): in_buf.cursor_right()
    @kb.add("enter")
    async def _(e):
        t = in_buf.text.strip()
        if t or pending["file_path"]:
            in_buf.text = ""; await _do_send(t)
    @kb.add("backspace")
    def _(e): in_buf.delete_before_cursor(1)
    @kb.add("<any>")
    def _(e):
        if len(e.data) == 1 and e.data.isprintable(): in_buf.insert_text(e.data)

    def _content():
        _, r = shutil.get_terminal_size()
        if not lines and loading[0]: return FormattedText([("italic ansigray", "\n  [Загрузка истории...]")])
        vis = _chat_visible_lines(r)
        pad = lines + ([""] * CHAT_BOTTOM_GAP)
        st_pos = _chat_window_start(len(lines), scroll[0], r)
        return ANSI("".join([l + "\n" for l in pad[st_pos:st_pos+vis]]))

    def _pref():
        p = [("bold", "Вы")]
        if pending["file_path"]: p.append(("bold ansigreen", " [МЕДИА]"))
        p.append(("", ": "))
        return p

    def _pref_width():
        return 22 if pending["file_path"] else 4

    layout = Layout(HSplit([
        Window(FormattedTextControl(_content, focusable=True)),
        Window(height=1, char="─"),
        Window(FormattedTextControl(lambda: FormattedText([("italic", " ↑↓ прокрутка  Ctrl+V глянуть  Ctrl+C копир.  Ctrl+P вставить  Esc назад")])), height=1),
        VSplit([
            Window(FormattedTextControl(_pref), width=_pref_width, height=1),
            Window(BufferControl(in_buf), height=1)
        ])
    ]), focused_element=in_buf)

    app = Application(layout=layout, key_bindings=kb, full_screen=True, mouse_support=True)
    app_h[0] = app
    
    _l_id = [all_msgs[0].id if all_msgs else 0]
    _p_stop = asyncio.Event()
    async def _poll():
        while not _p_stop.is_set():
            await asyncio.sleep(1.0)
            try:
                ms = await tg.get_messages(ent, limit=10)
                nw = [m for m in ms if m.id > _l_id[0]]
                if nw:
                    _l_id[0] = max(m.id for m in nw)
                    ids = {m.id for m in all_msgs[:20]}
                    added = [m for m in nw if m.id not in ids]
                    if added:
                        all_msgs[:0] = added
                        for m in added:
                            has_m = (hasattr(m, "photo") and m.photo) or (hasattr(m, "media") and m.media)
                            if has_m:
                                asyncio.create_task(_proc_media(m))
                        _rebuild(); _inv()
            except Exception: pass

    p_task = asyncio.create_task(_poll())
    try: await app.run_async()
    finally:
        _p_stop.set(); p_task.cancel()
        st.save_history(cid, cl.get_history(cid))

def run(args, conf, st):
    from tgai.telegram import TelegramManager
    from pathlib import Path
    tg = TelegramManager(conf["telegram"]["api_id"], conf["telegram"]["api_hash"], str(Path.home()/".tgai/session"))
    from tgai.claude import create_llm_client
    cl = create_llm_client(conf)
    pers = st.load_persona(getattr(args, "persona", None) or conf.get("defaults",{}).get("persona", "default"))
    async def main():
        await tg.start()
        try:
            if getattr(args, "target", None):
                e = await _load_entity(tg, args.target)
                if e: await _run_chat(tg, cl, st, e, pers, getattr(args, "text", False))
            else:
                dh, fh, ah = [[]], [None], [None]
                async def _fetch():
                    async def fetch_dialogs():
                        try:
                            dh[0] = await tg.get_dialogs(limit=50)
                            if ah[0]: ah[0].invalidate()
                        except Exception: pass
                    async def fetch_folders():
                        try:
                            fh_res = await tg.get_folders()
                            fh[0] = fh_res if fh_res is not None else []
                            if ah[0]: ah[0].invalidate()
                        except Exception as e:
                            fh[0] = []
                    await asyncio.gather(fetch_dialogs(), fetch_folders())
                ft = asyncio.create_task(_fetch())
                while True:
                    pt = asyncio.create_task(_poll_chat_list(tg, dh, ah))
                    try: dlg = await asyncio.get_running_loop().run_in_executor(None, lambda: select_chat_interactive(dh[0], fh[0], dialogs_holder=dh, folders_holder=fh, app_holder=ah))
                    finally: pt.cancel()
                    if not dlg: break
                    await _run_chat(tg, cl, st, dlg.entity, pers, getattr(args, "text", False))
                ft.cancel()
        finally: await tg.stop()
    try: asyncio.run(main())
    except Exception as e: print(f"Ошибка: {e}")
