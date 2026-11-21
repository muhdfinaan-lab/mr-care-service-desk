"""Job completion, technician pay and reviewer sales for MR CARE."""
import cgi
import io
import json
import os
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from urllib.parse import parse_qs, urlencode

from PIL import Image

import app as crm
import technician_portal as portal

TECH = "/technician/closeout"
SALES = "/sales"
SITE = "https://muhammedfinan.pythonanywhere.com"
CHARGES = ("inspection", "repair", "spare")

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_receipts (
    cid TEXT PRIMARY KEY, tech TEXT NOT NULL, work TEXT NOT NULL,
    metres INTEGER, inspection INTEGER NOT NULL, repair INTEGER NOT NULL,
    spare INTEGER NOT NULL, warranty TEXT NOT NULL, photo BLOB NOT NULL,
    pay_job INTEGER, pay_km INTEGER,
    created TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

STYLE = """
body{font:17px system-ui;color:#143e40;background:#f3f7f6;
max-width:52rem;margin:2rem auto;padding:0 1rem}
article{background:white;border:1px solid #cededb;border-radius:12px;
padding:1.25rem;margin:1rem 0}
label{display:block;margin:1rem 0}
input{display:block;max-width:100%;margin-top:.3rem;padding:.4rem}
button{background:#11675e;color:white;padding:.75rem 1rem;
border:0;border-radius:6px;cursor:pointer}
a{color:#09665e}
"""

def money(text):
    try:
        value = Decimal(text)
        if (value.is_finite() and 0 <= value <= 10000000
                and value == value.quantize(Decimal(".01"))):
            return int(value * 100)
    except (InvalidOperation, TypeError):
        pass
    raise ValueError("Enter a valid rupee amount with up to two decimal places.")

def rate(key):
    value = os.environ.get(key, "").strip()
    return money(value) if value else None

def rupees(paise):
    return f"₹{Decimal(paise) / 100:,.2f}"

def repair_photo(form):
    if "photo" not in form or not getattr(form["photo"], "file", None):
        raise ValueError("Choose a repair photo.")
    try:
        with Image.open(form["photo"].file) as image:
            if image.format not in ("JPEG", "PNG") or image.width * image.height > 16000000:
                raise ValueError("Choose a JPEG or PNG up to 16 megapixels.")
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=82)
            return output.getvalue()
    except (OSError, Image.DecompressionBombError):
        raise ValueError("Choose a readable JPEG or PNG photo.") from None

def finish(db, name, form):
    cid = form.getfirst("cid", "")
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT 1 FROM job_receipts WHERE cid=? AND tech=?",
                  (cid, name)).fetchone():
        return
    visit = db.execute("""
        SELECT v.notes, v.metres, c.data FROM field_visits v
        JOIN calls c ON c.id=v.cid
        WHERE v.cid=? AND v.tech=? AND c.tech=? AND c.state='Accepted'
          AND v.stage='Reached' AND v.outcome='Resolved'
    """, (cid, name, name)).fetchone()
    if not visit or not (visit["notes"] or "").strip():
        raise ValueError("Save the resolved outcome and work notes on this visit first.")
    charges = [money(form.getfirst(k, "")) for k in CHARGES]
    warranty = json.loads(visit["data"]).get("warranty_status") or "Unknown"
    db.execute("""
        INSERT INTO job_receipts
        (cid,tech,work,metres,inspection,repair,spare,warranty,photo,pay_job,pay_km)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (cid, name, visit["notes"], visit["metres"], *charges, warranty,
          repair_photo(form), rate("MR_PAY_PER_JOB"), rate("MR_TRAVEL_PER_KM")))
    db.execute("UPDATE calls SET state='Completed' WHERE id=?", (cid,))
    crm.log(db, cid, "Job completed. Repair photo and itemised bill saved.")

def completion_forms(db, name):
    if not db.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_visits'
    """).fetchone():
        return ""
    visits = db.execute("""
        SELECT c.job, v.cid, v.notes FROM field_visits v JOIN calls c ON c.id=v.cid
        WHERE c.tech=? AND v.tech=? AND c.state='Accepted'
          AND v.stage='Reached' AND v.outcome='Resolved'
    """, (name, name))
    cards = []
    for v in visits:
        inputs = "".join(
            f'<label>{k.title()} charge (₹)<input type="number" name="{k}" '
            'min="0" step=".01" required></label>' for k in CHARGES
        )
        cards.append(
            f'<article><h2>Complete job #{int(v["job"])}</h2>'
            f'<p>{escape(v["notes"] or "")}</p>'
            '<form method="post" enctype="multipart/form-data">'
            f'<input type="hidden" name="cid" value="{escape(v["cid"])}">'
            f'{inputs}<label>Repair photo<input type="file" name="photo" '
            'accept="image/jpeg,image/png" required></label>'
            '<button>Complete job and issue bill</button></form></article>'
        )
    return "".join(cards)

def bills(db, name):
    sql = """
        SELECT c.job, r.cid, r.tech, r.work, r.metres, r.inspection, r.repair,
               r.spare, r.warranty, r.pay_job, r.pay_km, r.created
        FROM job_receipts r JOIN calls c ON c.id=r.cid
    """
    if name:
        sql += " WHERE r.tech=?"
    rows = db.execute(sql + " ORDER BY r.created DESC, c.job DESC",
                      (name,) if name else ()).fetchall()
    cards = []
    total = 0
    for r in rows:
        subtotal = sum(r[k] for k in CHARGES)
        total += subtotal
        items = " · ".join(f"{k.title()} {rupees(r[k])}" for k in CHARGES)
        km = Decimal(r["metres"]) / 1000 if r["metres"] is not None else None
        distance = f"{km} km" if km is not None else "Unavailable"
        pay = "Technician pay · Rates or travel distance pending"
        if km is not None and r["pay_job"] is not None and r["pay_km"] is not None:
            travel = int((km * r["pay_km"]).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            pay = (f'Technician pay · Job {rupees(r["pay_job"])} + travel '
                   f'{rupees(travel)} ({km} km at {rupees(r["pay_km"])}/km) '
                   f'= {rupees(r["pay_job"] + travel)}')
        photo_url = (TECH if name else SALES) + "?" + urlencode({"photo": r["cid"]})
        cards.append(
            f'<article><h3>Bill · Job #{int(r["job"])}</h3>'
            f'<p>{escape(r["tech"])} · {escape(r["created"])} UTC</p>'
            f'<p>{escape(r["work"])}</p><p>{items}</p><b>Total {rupees(subtotal)}</b>'
            f'<p>Reported warranty · {escape(r["warranty"])}<br>'
            f'Estimated road travel · {distance}</p><p>{pay}</p>'
            f'<a href="{escape(photo_url)}">View repair photo</a></article>'
        )
    return f'<p>{len(rows)} completed · Total billed {rupees(total)}</p>' + "".join(cards)

def reply(start, status, body, mime="text/html; charset=utf-8", extra=()):
    start(status, [("Content-Type", mime), ("Cache-Control", "no-store"),
                   ("X-Content-Type-Options", "nosniff"), *extra])
    return [body if isinstance(body, bytes) else body.encode()]

def application(env, start, admin=False):
    name = None
    if not admin:
        identify = getattr(portal, "signed_in_name", None) or portal.identity
        name = identify(env.get("HTTP_AUTHORIZATION", ""))
        if not name:
            return reply(start, "401 Unauthorized", "Please sign in.",
                         extra=[("WWW-Authenticate", 'Basic realm="MR CARE Technician"')])
    with closing(crm.connection()) as db, db:
        db.execute(SCHEMA)
        if env["REQUEST_METHOD"] == "POST" and not admin:
            if env.get("HTTP_ORIGIN") != SITE:
                return reply(start, "403 Forbidden", "Open the technician portal first.")
            try:
                if not 0 < int(env.get("CONTENT_LENGTH") or 0) <= 5 * 1024 * 1024:
                    raise ValueError("Choose a smaller photo (under 5 MB).")
                form = cgi.FieldStorage(fp=env["wsgi.input"], environ=env)
                finish(db, name, form)
            except ValueError as err:
                db.rollback()
                return reply(start, "400 Bad Request",
                             escape(str(err)) + f' <a href="{TECH}">Back</a>')
            return reply(start, "303 See Other", "", extra=[("Location", TECH)])
        if env["REQUEST_METHOD"] != "GET":
            return reply(start, "405 Method Not Allowed", "Unsupported method.")
        cid = parse_qs(env.get("QUERY_STRING", "")).get("photo")
        if cid:
            sql = "SELECT photo FROM job_receipts WHERE cid=?"
            row = db.execute(sql if admin else sql + " AND tech=?",
                             (cid[0],) if admin else (cid[0], name)).fetchone()
            if row:
                return reply(start, "200 OK", row[0], "image/jpeg")
            return reply(start, "404 Not Found", "Photo unavailable.")
        title = "Sales report" if admin else "Job completion and bills"
        back = "/" if admin else "/technician"
        content = ("" if admin else completion_forms(db, name)) + bills(db, name)
        return reply(start, "200 OK",
                     f'<!doctype html><meta charset="utf-8"><meta name="viewport" '
                     f'content="width=device-width, initial-scale=1"><title>{title} · MR CARE</title>'
                     f'<style>{STYLE}</style><a href="{back}">Back to dashboard</a>'
                     f'<h1>{title}</h1>{content}')

def technician_payment_summary():
    with closing(crm.connection()) as db:
        db.execute(SCHEMA)
        receipts = db.execute("""
            SELECT tech, metres, pay_job, pay_km
            FROM job_receipts
            ORDER BY tech, created, cid
        """).fetchall()

    # Include current technicians even before they complete their first job.
    names = [tech["name"] for tech in crm.CONFIG.get("technicians", [])]
    totals = {}

    def new_total():
        return dict(jobs=0, metres=0, job_pay=0, travel_pay=0, pending=0)

    for name in names:
        totals[name] = new_total()

    for row in receipts:
        name = row["tech"]
        if name not in totals:
            totals[name] = new_total()
        entry = totals[name]
        entry["jobs"] += 1

        metres = row["metres"]
        if metres is not None:
            entry["metres"] += metres

        if metres is None or row["pay_job"] is None or row["pay_km"] is None:
            entry["pending"] += 1
            continue

        # Round each job's allowance exactly as on its individual receipt.
        travel = Decimal(metres) * row["pay_km"] / 1000
        entry["job_pay"] += row["pay_job"]
        entry["travel_pay"] += int(travel.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    rows = []
    for name, entry in sorted(totals.items(), key=lambda item: item[0].casefold()):
        earned = entry["job_pay"] + entry["travel_pay"]
        has_calculated_job = entry["jobs"] > entry["pending"]
        def display(amount):
            return rupees(amount) if has_calculated_job or not entry["pending"] else "—"

        rows.append(
            f'<tr><th scope="row">{escape(name)}</th><td>{entry["jobs"]}</td>'
            f'<td>{entry["metres"] / 1000:,.3f}</td>'
            f'<td>{display(entry["job_pay"])}</td><td>{display(entry["travel_pay"])}</td>'
            f'<td><strong>{display(earned)}</strong></td><td>{entry["pending"]}</td></tr>'
        )

    return (
        '<section aria-labelledby="tech-pay-heading">'
        '<h2 id="tech-pay-heading">Technician payments</h2>'
        '<p>Completed jobs · All time</p>'
        '<div style="overflow-x:auto"><table style="width:100%;text-align:left">'
        '<thead><tr><th>Technician</th><th>Jobs</th><th>Recorded km</th>'
        '<th>Job pay</th><th>Travel allowance</th><th>Total earned</th>'
        '<th>Pending calculation</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div></section>'
    )


def linked_page(existing, env, start, url, label):
    if env["REQUEST_METHOD"] != "GET":
        return existing(env, start)
    captured = []
    def capture(status, headers, exc_info=None):
        captured[:] = [status, headers, exc_info]
    stream = existing(env, capture)
    try:
        body = b"".join(stream)
    finally:
        if hasattr(stream, "close"):
            stream.close()
    status, headers, exc_info = captured
    if status.startswith("200") and any(
        k.lower() == "content-type" and "text/html" in v for k, v in headers
    ):
        link = f'<p><a href="{url}">{label}</a></p>'.encode()
        if url == SALES:
            link = technician_payment_summary().encode() + link
        marker = b"</main>" if b"</main>" in body else b"</body>"
        body = body.replace(marker, link + marker, 1) if marker in body else body + link
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
    start(status, headers, exc_info)
    return [body]

def reviewer_desk(existing):
    def wrapped(env, start):
        path = env.get("PATH_INFO")
        if path == SALES:
            return application(env, start, admin=True)
        if path in ("/", "/tech"):
            return linked_page(existing, env, start, SALES, "Sales report and repair bills")
        return existing(env, start)
    return wrapped

def technician_routes(existing):
    def wrapped(env, start):
        path = env.get("PATH_INFO")
        if path == TECH:
            return application(env, start)
        if path == "/technician":
            return linked_page(existing, env, start, TECH, "Complete a job or view bills")
        return existing(env, start)
    return wrapped
