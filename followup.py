"""Reviewer follow-up for completed MR CARE jobs."""
import json
import re
from contextlib import closing
from html import escape
from urllib.parse import parse_qs, quote

import app as crm
import closeout

PATH = "/follow-up"
SITE = "https://muhammedfinan.pythonanywhere.com"


def record_answer(db, job, answer):
    if answer not in ("Yes", "No"):
        raise ValueError("Choose Yes or No.")

    db.execute("BEGIN IMMEDIATE")
    row = db.execute("""
        SELECT c.id, c.state, f.answer
        FROM calls c
        JOIN job_receipts r ON r.cid = c.id
        LEFT JOIN customer_checks f ON f.cid = c.id
        WHERE c.job = ?
    """, (job,)).fetchone()

    if row is None:
        raise ValueError("The completed job could not be found.")
    if row["answer"] == answer:
        return
    if row["answer"] is not None or row["state"] != "Completed":
        raise ValueError("This complaint has already changed. Refresh the page.")

    db.execute(
        "INSERT INTO customer_checks (cid, answer) VALUES (?, ?)",
        (row["id"], answer),
    )
    if answer == "No":
        db.execute("UPDATE calls SET state = 'Reopened' WHERE id = ?", (row["id"],))

    crm.log(db, row["id"], "Customer follow-up: " + answer)


def whatsapp_link(phone, job):
    digits = re.sub(r"[\s()+-]", "", phone or "")
    if re.fullmatch(r"[6-9]\d{9}", digits):
        digits = "91" + digits
    if not re.fullmatch(r"91[6-9]\d{9}", digits):
        return "<p>WhatsApp number unavailable.</p>"

    message = quote(
        f"MR CARE: Service for complaint #{job} is complete. "
        "Thank you for choosing MR CARE."
    )
    return (
        f'<p><a href="https://wa.me/{digits}?text={message}" '
        'target="_blank" rel="noreferrer">Open WhatsApp completion draft</a></p>'
    )


def page(env, start):
    with closing(crm.connection()) as db, db:
        db.execute(closeout.SCHEMA)
        db.execute("""
            CREATE TABLE IF NOT EXISTS customer_checks (
                cid TEXT PRIMARY KEY,
                answer TEXT NOT NULL,
                recorded TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        if env["REQUEST_METHOD"] == "POST":
            if env.get("HTTP_ORIGIN") != SITE:
                return closeout.reply(start, "403 Forbidden", "Open the reviewer dashboard.")
            try:
                size = int(env.get("CONTENT_LENGTH") or 0)
                if not 0 < size <= 2048:
                    raise ValueError("Invalid form.")
                form = parse_qs(env["wsgi.input"].read(size).decode())
                record_answer(
                    db,
                    form.get("job", [""])[0],
                    form.get("answer", [""])[0],
                )
            except ValueError as error:
                db.rollback()
                return closeout.reply(
                    start, "409 Conflict",
                    escape(str(error)) + f' <a href="{PATH}">Back to follow-up</a>'
                )
            return closeout.reply(start, "303 See Other", "", extra=[("Location", PATH)])

        if env["REQUEST_METHOD"] != "GET":
            return closeout.reply(start, "405 Method Not Allowed", "Unsupported method.")

        cards = []
        rows = db.execute("""
            SELECT c.job, c.data, c.state, f.answer,
                   date(r.created, '+330 minutes', '+1 day') AS due
            FROM job_receipts r
            JOIN calls c ON c.id = r.cid
            LEFT JOIN customer_checks f ON f.cid = c.id
            ORDER BY f.answer IS NOT NULL, due, c.job
        """)

        for row in rows:
            job = int(row["job"])
            data = json.loads(row["data"])
            card = (
                f'<article><h2>Job #{job} · {escape(row["state"])}</h2>'
                f'<p>Follow-up due {escape(row["due"])} (IST)</p>'
            )
            if row["answer"]:
                card += f'<p>Customer answer · <strong>{escape(row["answer"])}</strong></p>'
            elif row["state"] == "Completed":
                card += whatsapp_link(data.get("phone"), job)
                card += (
                    '<form method="post">'
                    f'<input type="hidden" name="job" value="{job}">'
                    '<p>Is your appliance working properly now?</p>'
                    '<button name="answer" value="Yes">Yes · keep closed</button> '
                    '<button name="answer" value="No">No · reopen complaint</button>'
                    '</form>'
                )
            cards.append(card + "</article>")

        html = (
            '<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Customer follow-up · MR CARE</title>'
            '<style>body{font:17px system-ui;color:#143e40;background:#f3f7f6;'
            'max-width:52rem;margin:2rem auto;padding:0 1rem}'
            'article{background:white;border:1px solid #cededb;border-radius:12px;'
            'padding:1rem 1.25rem;margin:1rem 0}a{color:#09665e}'
            'button{padding:.65rem;margin:.2rem;cursor:pointer}</style>'
            '<a href="/">Back to dashboard</a><h1>Customer follow-up</h1>'
            + ("".join(cards) or "<p>No completed jobs yet.</p>")
        )
        return closeout.reply(start, "200 OK", html)


def reviewer(existing):
    def wrapped(env, start):
        path = env.get("PATH_INFO")
        if path == PATH:
            return page(env, start)
        if path in ("/", "/tech"):
            return closeout.linked_page(existing, env, start, PATH, "Customer follow-up")
        return existing(env, start)
    return wrapped
