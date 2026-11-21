"""MR CARE technician portal."""
import base64
import hashlib
import hmac
import json
import secrets
from contextlib import closing
from getpass import getpass
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

import app as crm


ACCOUNTS = Path.home() / ".mr-care-technicians"
SITE = "https://muhammedfinan.pythonanywhere.com"


def password_hash(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=16384, r=8, p=1
    ).hex()


def create_account():
    technicians = crm.CONFIG["technicians"]
    for number, tech in enumerate(technicians, 1):
        print(number, tech["name"])

    choice = input("Technician number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(technicians):
        raise SystemExit("Please run again and select a number from the list.")

    name = technicians[int(choice) - 1]["name"]
    login = input("New technician login ID: ").strip()
    if not login.isascii() or not login.isalnum() or len(login) > 128:
        raise SystemExit("Use letters and numbers for the login ID.")

    password = getpass("New password (at least 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Please run again with a longer password.")

    ACCOUNTS.mkdir(mode=0o700, exist_ok=True)
    salt = secrets.token_bytes(16)
    record = [name, salt.hex(), password_hash(password, salt)]
    try:
        with (ACCOUNTS / login).open("x") as file:
            json.dump(record, file)
    except FileExistsError:
        raise SystemExit("This login ID already exists.")
    print(f"Account created for {name}. Login ID: {login}")


def signed_in_name(header):
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        credentials = base64.b64decode(encoded, validate=True).decode()
        login, password = credentials.split(":", 1)
        if not login.isascii() or not login.isalnum() or len(login) > 128:
            return None
        name, salt, expected = json.loads((ACCOUNTS / login).read_text())
        actual = password_hash(password, bytes.fromhex(salt))
        if hmac.compare_digest(actual, expected):
            return name
    except (ValueError, OSError):
        pass
    return None


def application(env, respond):
    def send(status, content, extra=()):
        respond(status, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            *extra,
        ])
        return [content.encode()]

    name = signed_in_name(env.get("HTTP_AUTHORIZATION", ""))
    if not name:
        return send("401 Unauthorized", "Sign in with your technician ID.", [
            ("WWW-Authenticate", 'Basic realm="MR CARE technician", charset="UTF-8"')
        ])

    method = env["REQUEST_METHOD"]
    if method == "POST":
        if env.get("HTTP_ORIGIN") != SITE:
            return send("403 Forbidden", "Open the technician portal to respond.")
        try:
            size = int(env.get("CONTENT_LENGTH") or 0)
            if not 0 < size <= 2048:
                raise ValueError
            form = parse_qs(env["wsgi.input"].read(size).decode())
            crm.update({
                "call_id": form["id"][0],
                "action": form["action"][0],
                "tech": name,
            }, technician=True)
        except (ValueError, KeyError):
            return send("409 Conflict", '<a href="/technician">Refresh your jobs</a>')
        return send("303 See Other", "", [("Location", "/technician")])

    if method != "GET":
        return send("405 Method Not Allowed", "", [("Allow", "GET, POST")])

    with closing(crm.connection()) as db:
        jobs = db.execute(
            "SELECT * FROM calls WHERE tech = ? AND job IS NOT NULL ORDER BY job DESC",
            (name,),
        ).fetchall()

    cards = []
    for row in jobs:
        data = json.loads(row["data"])
        buttons = ""
        if row["state"] == "Pending":
            buttons = f'''
                <form method="post">
                    <input type="hidden" name="id" value="{escape(row['id'])}">
                    <button name="action" value="Accept">Accept job</button>
                    <button name="action" value="Reject">Reject</button>
                </form>'''
        cards.append(f'''
            <article>
                <small>Job #{int(row["job"])} · {escape(row["state"])}</small>
                <h2>{escape(data.get("product", ""))}</h2>
                <p>{escape(data.get("problem", ""))}</p>
                <p>{escape(data.get("service_location", ""))}
                · {escape(data.get("service_pincode", ""))}</p>
                {buttons}
            </article>''')

    content = "".join(cards) or "<p>You have no assigned jobs.</p>"
    return send("200 OK", f'''<!doctype html>
        <html lang="en"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>My jobs · MR CARE</title>
        <style>
        *{{box-sizing:border-box}}
        body{{margin:0;background:#f2f6f5;color:#173337;font:16px/1.5 system-ui}}
        header{{background:#123e43;color:white;padding:1rem 5%}}
        main{{max-width:48rem;margin:auto;padding:1.2rem}}
        article{{background:white;border:1px solid #cfdddb;border-radius:12px;
                 padding:1.2rem;margin:1rem 0}}
        h2{{margin:.3rem 0}}
        button{{padding:.7rem 1rem;border:0;border-radius:6px;
                background:#126961;color:white;cursor:pointer}}
        </style>
        <header>MR CARE · Technician service</header>
        <main><h1>My jobs</h1><p>{escape(name)}</p>{content}</main></html>''')


if __name__ == "__main__":
    create_account()
