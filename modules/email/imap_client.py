"""IMAP-клиент: синхронные функции, вызываются через asyncio.to_thread."""
import email as email_lib
import imaplib
from email.header import decode_header, make_header

import config


def _dec(value: str) -> str:
    return str(make_header(decode_header(value or "")))


def fetch_email(limit: int, unread_only: bool, query: str) -> str:
    box = imaplib.IMAP4_SSL(config.EMAIL_IMAP_HOST)
    try:
        box.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
        box.select("INBOX", readonly=True)
        crit = ["UNSEEN"] if unread_only else ["ALL"]
        if query:
            crit += ["TEXT", f'"{query.replace(chr(34), "")}"']
        _, data = box.search("UTF-8", *crit)
        ids = data[0].split()[-limit:][::-1]
        out = []
        for i in ids:
            _, msg = box.fetch(i, "(BODY.PEEK[])")
            m = email_lib.message_from_bytes(msg[0][1])
            part = next((p for p in m.walk() if p.get_content_type() == "text/plain"), None)
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace") if part else ""
            out.append(f"От: {_dec(m['From'])}\nТема: {_dec(m['Subject'])}\nДата: {m['Date']}\n{body.strip()[:1500]}")
        return "\n\n---\n".join(out) or "Писем нет."
    finally:
        try:
            box.logout()
        except Exception:
            pass


def fetch_new_emails(last_uid: int) -> tuple[list[dict], int]:
    """Письма с UID > last_uid. При last_uid < 0 только возвращает текущий максимальный UID."""
    box = imaplib.IMAP4_SSL(config.EMAIL_IMAP_HOST)
    try:
        box.login(config.EMAIL_USER, config.EMAIL_PASSWORD)
        box.select("INBOX", readonly=True)
        _, data = box.uid("search", None, "ALL" if last_uid < 0 else f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in data[0].split())
        if last_uid < 0:
            return [], (uids[-1] if uids else 0)
        uids = [u for u in uids if u > last_uid][:5]
        out = []
        for u in uids:
            _, msg = box.uid("fetch", str(u), "(BODY.PEEK[])")
            m = email_lib.message_from_bytes(msg[0][1])
            part = next((p for p in m.walk() if p.get_content_type() == "text/plain"), None)
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace") if part else ""
            out.append({"uid": u, "from": _dec(m["From"]), "subject": _dec(m["Subject"]), "body": body.strip()[:2000]})
        return out, (uids[-1] if uids else last_uid)
    finally:
        try:
            box.logout()
        except Exception:
            pass
