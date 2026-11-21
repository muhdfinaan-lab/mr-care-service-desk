"""MR CARE technician visits. Mounted by the existing WSGI application."""
import io
import json
import math
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import parse_qs, urlencode

import app as crm
import technician_portal as portal

IST = timezone(timedelta(hours=5, minutes=30))
SITE = "https://muhammedfinan.pythonanywhere.com"
identify = getattr(portal, "signed_in_name", None) or portal.identity


def prepare(db):
    db.execute("BEGIN IMMEDIATE")
    prepare_spares(db)
    db.execute("""
        CREATE TABLE IF NOT EXISTS field_visits (
            cid TEXT PRIMARY KEY, tech TEXT NOT NULL, slot TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'Scheduled', origin TEXT,
            destination TEXT, metres INTEGER, notes TEXT, departure_pin TEXT,
            outcome TEXT, UNIQUE(tech, slot)
        )
    """)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(field_visits)")}
    if "departure_pin" not in columns:
        db.execute("ALTER TABLE field_visits ADD COLUMN departure_pin TEXT")
    if "outcome" not in columns:
        db.execute("ALTER TABLE field_visits ADD COLUMN outcome TEXT")



SPARE_STAGES = ("Spare Order", "Follow-up", "Spare Arrived", "Fitted")


def prepare_spares(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS spare_orders (
            id INTEGER PRIMARY KEY,
            cid TEXT NOT NULL,
            part TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 999),
            stage TEXT NOT NULL DEFAULT 'Spare Order',
            request_id TEXT NOT NULL,
            UNIQUE(cid, request_id)
        )
    """)


def change_spare(db, visit, form):
    cid = visit["cid"]
    if visit["stage"] != "Reached" or visit["outcome"] != "Not resolved":
        raise ValueError("Save the visit as Not resolved before updating spare orders.")

    if form["visit"] == "spare_create":
        part = " ".join(form.get("part", "").split())
        qty = form.get("quantity", "")
        request_id = form.get("request_id", "")
        if not part or len(part) > 100:
            raise ValueError("Enter a part name of up to 100 characters.")
        if not qty.isascii() or not qty.isdigit() or len(qty) > 3 or int(qty) < 1:
            raise ValueError("Enter a quantity from 1 to 999.")
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("Refresh My jobs and submit the order again.")
        inserted = db.execute("""
            INSERT INTO spare_orders(cid, part, quantity, request_id)
            VALUES(?,?,?,?) ON CONFLICT(cid, request_id) DO NOTHING
        """, (cid, part, int(qty), request_id))
        if inserted.rowcount:
            crm.log(db, cid, f"Spare Order #{inserted.lastrowid}: {part} × {int(qty)}")
        return

    order = db.execute(
        "SELECT * FROM spare_orders WHERE id=? AND cid=?",
        (form.get("order_id", ""), cid)
    ).fetchone()
    current = form.get("current_stage")
    if not order or current not in SPARE_STAGES[:-1]:
        raise ValueError("Refresh My jobs to see the current spare order.")
    if order["stage"] != current:
        raise ValueError("This order has already changed. Refresh My jobs.")
    next_stage = SPARE_STAGES[SPARE_STAGES.index(current) + 1]
    db.execute("UPDATE spare_orders SET stage=? WHERE id=?", (next_stage, order["id"]))
    crm.log(db, cid, f"Spare order #{order['id']} · {order['part']} · {next_stage}")



def spare_counts(stage, quantity):
    """Counts inferred from the order workflow until inventory movements are tracked."""
    taken = quantity if stage in ("Spare Arrived", "Fitted") else 0
    used = quantity if stage == "Fitted" else 0
    return taken, used, 0


def spare_panel(db, row):
    from uuid import uuid4

    orders = db.execute(
        "SELECT * FROM spare_orders WHERE cid=? ORDER BY id", (row["cid"],)
    ).fetchall()
    editable = row["stage"] == "Reached" and row["outcome"] == "Not resolved"
    if not editable and not orders:
        return ""

    job_input = f'<input type="hidden" name="id" value="{escape(row["cid"], quote=True)}">'
    parts = ["<section><h3>Spare orders</h3>"]
    for order in orders:
        stage = order["stage"]
        parts.append(
            f'<p><strong>{escape(order["part"])}</strong> · Qty {order["quantity"]}'
            f' · {escape(stage)} · Order #{order["id"]}</p>'
        )
        taken, used, returned = spare_counts(stage, order["quantity"])
        parts.append(
            f"<p> Taken {taken} · Used {used} · Returned {returned}</p>"
        )
        if editable and stage in SPARE_STAGES[:-1]:
            next_stage = SPARE_STAGES[SPARE_STAGES.index(stage) + 1]
            parts.append(
                f'<form method="post">{job_input}'
                f'<input type="hidden" name="order_id" value="{order["id"]}">'
                f'<input type="hidden" name="current_stage" value="{escape(stage)}">'
                f'<button name="visit" value="spare_advance">Mark {next_stage}</button></form>'
            )
    if editable:
        parts.append(
            f'<form method="post">{job_input}'
            f'<input type="hidden" name="request_id" value="{uuid4().hex}">'
            '<p><label>Part name <input name="part" maxlength="100" required></label></p>'
            '<p><label>Quantity <input type="number" name="quantity" min="1" max="999" '
            'value="1" required></label></p>'
            '<button name="visit" value="spare_create">Raise spare order</button></form>'
        )
    return "".join(parts) + "</section>"


def schedule(db, name):
    next_slot = datetime.now(IST).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(hours=1)
    jobs = db.execute("""
        SELECT id FROM calls
        WHERE tech=? AND state='Accepted'
          AND id NOT IN (SELECT cid FROM field_visits)
        ORDER BY job
    """, (name,)).fetchall()

    for job in jobs:
        while True:
            if next_slot.hour >= 18:
                next_slot = (next_slot + timedelta(days=1)).replace(hour=9)
            elif next_slot.hour < 9:
                next_slot = next_slot.replace(hour=9)
            slot = next_slot.isoformat(timespec="minutes")
            occupied = db.execute(
                "SELECT 1 FROM field_visits WHERE tech=? AND slot=?", (name, slot)
            ).fetchone()
            next_slot += timedelta(hours=1)
            if not occupied:
                break
        db.execute(
            "INSERT INTO field_visits(cid,tech,slot) VALUES(?,?,?)",
            (job["id"], name, slot),
        )
        crm.log(db, job["id"], "Appointment scheduled: " + slot)


def change_visit(db, name, form):
    cid = form.get("id", "")
    visit = db.execute("""
        SELECT v.*, c.data FROM field_visits v JOIN calls c ON c.id=v.cid
        WHERE v.cid=? AND v.tech=? AND c.tech=? AND c.state='Accepted'
    """, (cid, name, name)).fetchone()
    if not visit:
        raise ValueError("This visit is unavailable. Refresh your jobs.")

    action = form.get("visit")
    if action in ("spare_create", "spare_advance"):
        return change_spare(db, visit, form)
    if action == "depart" and visit["stage"] == "Scheduled":
        origin = form.get("origin", "").strip()
        data = json.loads(visit["data"])
        destination = data.get("service_location", "").strip()
        if not origin or len(origin) > 300 or not destination:
            raise ValueError("Check the starting address and service address.")
        destination = destination + ", " + data.get("service_pincode", "")
        pin = form.get("departure_pin", "").strip()
        if pin:
            try:
                lon, lat = map(float, pin.split(","))
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError
            except ValueError:
                raise ValueError("Capture the starting point again.")
            pin = f"{lon:.6f},{lat:.6f}"
        db.execute("""
            UPDATE field_visits SET stage='On the way', origin=?,
                destination=?, departure_pin=? WHERE cid=?
        """, (origin, destination, pin, cid))
        detail = "On the way from " + origin

    elif action == "arrive" and visit["stage"] == "On the way":
        try:
            km = Decimal(form.get("km", ""))
            if not km.is_finite() or not 0 <= km <= 10000 or km.as_tuple().exponent < -3:
                raise ValueError
            metres = int(km * 1000)
        except (ValueError, InvalidOperation):
            raise ValueError("Enter the map distance in km, with up to three decimals.")
        db.execute(
            "UPDATE field_visits SET stage='Reached', metres=? WHERE cid=?",
            (metres, cid),
        )
        detail = f"Reached site · estimated road distance {km} km"

    elif action == "notes" and visit["stage"] == "Reached":
        outcome = form.get("outcome", "")
        notes = form.get("notes", "").strip()
        if outcome not in ("Resolved", "Not resolved"):
            raise ValueError("Choose Resolved or Not resolved.")
        if not notes or len(notes) > 1000:
            raise ValueError("Enter the work done or reason still open (up to 1000 characters).")
        if outcome == visit["outcome"] and notes == visit["notes"]:
            return
        db.execute(
            "UPDATE field_visits SET outcome=?, notes=? WHERE cid=?",
            (outcome, notes, cid),
        )
        detail = f"Complaint {outcome.lower()} · {notes}"
    else:
        raise ValueError("The visit status changed. Refresh your jobs.")

    crm.log(db, cid, detail)


SCRIPT = """
<script>
function currentPoint() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject();
    navigator.geolocation.getCurrentPosition(
      p => resolve([p.coords.longitude, p.coords.latitude]
        .map(n => n.toFixed(6)).join(',')),
      reject, {enableHighAccuracy:true, timeout:10000}
    );
  });
}
async function saveStart(button) {
  const msg = button.nextElementSibling;
  msg.textContent = ' Finding starting point…';
  try {
    button.form.elements.departure_pin.value = await currentPoint();
    msg.textContent = ' Starting point saved for this trip.';
  } catch {
    msg.textContent = ' Location unavailable. Use the Maps route at arrival.';
  }
}
async function estimateRoad(button) {
  const msg = button.nextElementSibling;
  if (!button.dataset.start) {
    msg.textContent = ' No starting pin saved. Open Maps and enter its estimate.';
    return;
  }
  msg.textContent = ' Finding driving route…';
  try {
    const end = await currentPoint();
    const points = button.dataset.start + ';' + end;
    const response = await fetch(
      'https://router.project-osrm.org/route/v1/driving/' + points + '?overview=false',
      {signal: AbortSignal.timeout(10000)}
    );
    const data = await response.json();
    const metres = data.routes?.[0]?.distance;
    if (!response.ok || data.code !== 'Ok' || !Number.isFinite(metres)) throw Error();
    button.form.elements.km.value = (metres / 1000).toFixed(3);
    msg.textContent = ' Driving estimate ready. Check it before marking arrival.';
  } catch {
    msg.textContent = ' Route unavailable. Open Maps and enter its driving distance.';
  }
}
</script>
"""


def visit_panel(db, name):
    rows = db.execute("""
        SELECT v.*, c.job FROM field_visits v JOIN calls c ON c.id=v.cid
        WHERE v.tech=? AND c.tech=? AND c.state='Accepted' ORDER BY slot
    """, (name, name)).fetchall()
    if not rows:
        return ""
    parts = ["<h2>Scheduled visits</h2>"]
    for row in rows:
        cid = escape(row["cid"], quote=True)
        when = datetime.fromisoformat(row["slot"]).strftime("%d %b %Y · %I:%M %p IST")
        parts.append(
            f'<article><strong>Job #{int(row["job"])} · {escape(row["stage"])}</strong>'
            f'<p>{when}</p><form method="post">'
            f'<input type="hidden" name="id" value="{cid}">'
        )
        if row["stage"] == "Scheduled":
            parts.append("""
                <label>Starting address <input name="origin" maxlength="300" required></label>
                <input type="hidden" name="departure_pin">
                <p><button type="button" onclick="saveStart(this)">Use current starting point</button>
                <output aria-live="polite"></output></p>
                <button name="visit" value="depart">On the way</button>
            """)
        elif row["stage"] == "On the way":
            url = "https://www.google.com/maps/dir/?" + urlencode({
                "api": 1, "origin": row["origin"], "destination": row["destination"],
                "travelmode": "driving",
            })
            pin = escape(row["departure_pin"] or "", quote=True)
            parts.append(
                f'<p><a href="{escape(url)}" target="_blank" rel="noreferrer">'
                'Open driving route in Maps</a></p>'
                f'<button type="button" data-start="{pin}" onclick="estimateRoad(this)">'
                'Calculate km to current location</button><output aria-live="polite"></output>'
                '<p><label>Map estimate (km) <input type="number" name="km" '
                'min="0" max="10000" step=".001" required></label></p>'
                '<button name="visit" value="arrive">Reached site</button>'
            )
        elif row["stage"] == "Reached":
            options = ['<option value="">Choose outcome</option>']
            for value in ("Resolved", "Not resolved"):
                selected = " selected" if row["outcome"] == value else ""
                options.append(f'<option{selected}>{value}</option>')
            parts.append(
                f'<p>Estimated road distance · {row["metres"]/1000:g} km</p>'
                f'<label>Complaint outcome <select name="outcome" required>'
                f'{"".join(options)}</select></label>'
                '<p><label>Work done or reason still open<br>'
                f'<textarea name="notes" maxlength="1000" required>'
                f'{escape(row["notes"] or "")}</textarea></label></p>'
                '<button name="visit" value="notes">Save visit details</button>'
            )
        parts.append("</form>")
        parts.append(spare_panel(db, row))
        parts.append("</article>")
    return "".join(parts) + SCRIPT


def application(env, respond):
    name = identify(env.get("HTTP_AUTHORIZATION", ""))
    if not name:
        return portal.application(env, respond)

    if env["REQUEST_METHOD"] == "POST":
        if env.get("HTTP_ORIGIN") != SITE:
            respond("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"Open the technician portal to update a visit."]
        try:
            size = int(env.get("CONTENT_LENGTH") or 0)
            if not 0 < size <= 16384:
                raise ValueError
        except ValueError:
            respond("400 Bad Request", [("Content-Type", "text/plain")])
            return [b"Invalid form size."]
        raw = env["wsgi.input"].read(size)
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf8", "replace")).items()}
        if "visit" in form:
            try:
                with closing(crm.connection()) as db, db:
                    prepare(db)
                    change_visit(db, name, form)
            except ValueError as err:
                respond("409 Conflict", [("Content-Type", "text/html; charset=utf-8")])
                return [(escape(str(err)) + ' <a href="/technician">My jobs</a>').encode()]
            respond("303 See Other", [("Location", "/technician")])
            return [b""]
        env = dict(env)
        env["wsgi.input"] = io.BytesIO(raw)
        return portal.application(env, respond)

    if env["REQUEST_METHOD"] != "GET":
        return portal.application(env, respond)

    with closing(crm.connection()) as db, db:
        prepare(db)
        schedule(db, name)
        panel = visit_panel(db, name).encode()
    body = b"".join(portal.application(env, respond))
    return [body.replace(b"</main>", panel + b"</main>", 1)]
