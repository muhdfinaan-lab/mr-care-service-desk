"""MR CARE rehearsal server. Run from the extracted project directory."""
import csv
import hmac
import io
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from wsgiref.simple_server import make_server

CONFIG = json.loads(Path("config.json").read_text(encoding="utf8"))
FIELDS = "language role name address phone pincode service_location service_pincode product model serial purchase_date warranty_status warranty_expiry problem".split()
DB = os.getenv("MR_DB", "mrcare.sqlite3")

def greeting(language):
    hour = datetime.now(timezone(timedelta(hours=5, minutes=30))).hour
    period = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    return CONFIG["greetings"][language].format(period=period)

def connection():
    db = sqlite3.connect(DB, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, data TEXT, job INTEGER UNIQUE, tech TEXT, state TEXT, reason TEXT, previous INTEGER, token INTEGER);
    CREATE TABLE IF NOT EXISTS customers(token INTEGER PRIMARY KEY, phone TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, call_id TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP, detail TEXT);
    CREATE TABLE IF NOT EXISTS declined(call_id TEXT, tech TEXT, PRIMARY KEY(call_id,tech));
    """)
    return db

def assign(db, cid, data):
    category = CONFIG["products"].get(data["product"].casefold())
    pin = data["service_pincode"]
    unavailable = {r[0] for r in db.execute("SELECT tech FROM calls WHERE state IN ('Pending','Accepted') UNION SELECT tech FROM declined WHERE call_id=?", (cid,))}
    options = [(t["distance_by_pin"][pin], t["name"], t["branch"]) for t in CONFIG["technicians"]
               if t["working"] and category in t["skills"] and pin in t["distance_by_pin"] and t["name"] not in unavailable]
    if options:
        km, tech, branch = min(options)
        state, reason = "Pending", f"{branch}, available, skilled in {category}, {pin}, approx. {km} km (fictional pincode-centre distance)"
    else:
        state, tech = "Unassigned", None
        reason = "Product master review" if not category else "No free eligible technician for service pincode"
    db.execute("UPDATE calls SET state=?,tech=?,reason=? WHERE id=?", (state,tech,reason,cid))
    log(db,cid,f"{state}: {tech or 'Queue'} / {reason}")

def log(db,cid,msg):
    db.execute("INSERT INTO events(call_id,detail) VALUES(?,?)",(cid,msg))

def update(body, technician=False):
    cid = body["call_id"]
    if not isinstance(cid,str) or not re.fullmatch(r'[\w-]{1,128}',cid):
        raise ValueError("Invalid conversation ID")
    with closing(connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
        action = body["action"]
        if technician:
            if action not in ("Accept","Reject") or not row or row["state"] != "Pending" or row["tech"] != body.get("tech"):
                raise ValueError("Offer changed. Refresh and retry.")
            if action == "Accept":
                db.execute("UPDATE calls SET state='Accepted' WHERE id=?", (cid,))
                log(db,cid,"Accepted by "+row["tech"])
            else:
                db.execute("INSERT INTO declined VALUES(?,?)",(cid,row["tech"]))
                log(db,cid,"Rejected by "+row["tech"])
                assign(db,cid,json.loads(row["data"]))
        elif action in ("start","capture","confirm"):
            if not row:
                db.execute("INSERT INTO calls(id,data,state) VALUES(?,'{}','Intake')",(cid,))
                log(db,cid,"Session opened")
            if not row or not row["job"]:
                data = json.loads(row["data"]) if row else {}
                patch = body.get("fields",{})
                if not isinstance(patch,dict) or any(k not in FIELDS or not isinstance(v,str) or len(v)>500 for k,v in patch.items()):
                    raise ValueError("Invalid fields")
                data.update({k:v.strip() for k,v in patch.items()})
                db.execute("UPDATE calls SET data=? WHERE id=?", (json.dumps(data),cid))
                if patch:
                    log(db,cid,"Updated "+", ".join(patch))
                if action == "confirm":
                    missing = [k for k in FIELDS if not data.get(k)]
                    if missing:
                        raise ValueError("Collect or explicitly mark unknown: "+", ".join(missing))
                    phone = re.sub(r'[\s()-]', '',data["phone"])
                    if not re.fullmatch(r'(?:\+91)?[6-9]\d{9}',phone) or not all(re.fullmatch(r'[1-9]\d{5}',data[k]) for k in ("pincode","service_pincode")):
                        raise ValueError("Confirm Indian mobile and both six-digit pincodes")
                    phone = "+91"+phone[-10:]
                    db.execute("INSERT OR IGNORE INTO customers(phone) VALUES(?)",(phone,))
                    token = db.execute("SELECT token FROM customers WHERE phone=?",(phone,)).fetchone()[0]
                    prior = None
                    for old in db.execute("SELECT job,data FROM calls WHERE token=? ORDER BY job DESC",(token,)):
                        details = json.loads(old["data"])
                        if all(details[k].casefold()==data[k].casefold() for k in ("product","serial","problem")):
                            prior = old["job"]
                            break
                    job = db.execute("SELECT COALESCE(MAX(job),0)+1 FROM calls").fetchone()[0]
                    db.execute("UPDATE calls SET token=?,job=?,previous=? WHERE id=?",(token,job,prior,cid))
                    log(db,cid,"Registered; warranty requires verification")
                    assign(db,cid,data)
        else:
            raise ValueError("Invalid action")
        result = dict(db.execute("SELECT * FROM calls WHERE id=?",(cid,)).fetchone())
        return {**result,"data":json.loads(result["data"]),"greeting":greeting("en"),"coverage":"Requires verification"}

def dashboard(db,path):
    records = list(db.execute("SELECT * FROM calls ORDER BY rowid DESC"))
    page = ['<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><style>body{background:#f3f6f5;color:#14352e;font:16px system-ui;max-width:960px;margin:5vh auto;padding:1rem}article{background:white;border-left:5px solid #17735d;padding:1rem;margin:1rem 0}button,a{margin-right:1em}dt{font-weight:bold}dl{display:grid;grid-template-columns:10rem 1fr;gap:.35rem}dd{margin:0}</style><title>MR CARE Service Desk</title><h1>MR CARE · Service desk</h1><nav><a href="/">Call monitor</a><a href="/tech">Technician</a><a href="/csv">Export CSV</a></nav>']
    for r in records:
        esc = lambda val: escape(str(val or "—"),quote=True)
        data = json.loads(r["data"])
        page.append(f"<article><small>{esc(r['id'])}</small><h2>{esc(data.get('product'))} · {esc(r['state'])}</h2><p>Job {esc(r['job'])} · Customer {esc(r['token'])} · Previous job {esc(r['previous'])}</p><b>{esc(r['tech'])}</b> {esc(r['reason'])}")
        if path == "/tech" and r["state"]=="Pending":
            for act in ("Accept","Reject"):
                page.append(f'<form method=post action="/offer"><input type=hidden name=call_id value="{esc(r["id"])}"><input type=hidden name=tech value="{esc(r["tech"])}"><button name=action value="{act}">{act}</button></form>')
        else:
            page.append("<dl>"+''.join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k,v in data.items())+"</dl>")
        page.append("<p>Coverage requires verification · SMS and WhatsApp disconnected</p>")
        for event in db.execute("SELECT at,detail FROM events WHERE call_id=? ORDER BY id",(r["id"],)):
            page.append(f"<div>{esc(event[0])} UTC · {esc(event[1])}</div>")
        page.append("</article>")
    if not records:
        page.append("<p>Waiting for the first intake event.</p>")
    if path == "/":
        page.append('<meta http-equiv="refresh" content="2">')
    return ''.join(page)

def serve(public):
    from urllib.parse import parse_qs
    secret = os.environ.get("MR_TOOL_SECRET","")
    if public and len(secret)<32:
        sys.exit("Set MR_TOOL_SECRET (32+ characters)")
    def app(env, respond):
        path = env["PATH_INFO"]
        method = env["REQUEST_METHOD"]
        status, mime = "200 OK","application/json"
        try:
            if public:
                if method!="POST" or path!="/tool" or not hmac.compare_digest(env.get("HTTP_AUTHORIZATION",""),"Bearer "+secret):
                    raise PermissionError()
                size=int(env.get("CONTENT_LENGTH") or 0)
                if not 0<size<=32768:
                    raise ValueError("Invalid body size")
                output=json.dumps(update(json.loads(env["wsgi.input"].read(size))))
            elif method=="POST" and path=="/offer":
                if env.get("HTTP_ORIGIN")!="http://127.0.0.1:8010":
                    raise PermissionError()
                body=parse_qs(env["wsgi.input"].read(min(2048,int(env.get("CONTENT_LENGTH") or 0))).decode())
                update({k:v[0] for k,v in body.items()},True)
                respond("303 See Other",[("Location","/tech")])
                return [b""]
            elif method=="GET" and path in ("/","/tech","/csv"):
                with closing(connection()) as db:
                    if path=="/csv":
                        stream=io.StringIO()
                        writer=csv.writer(stream)
                        writer.writerow(["complaint","customer_token",*FIELDS,"coverage","technician","state","previous"])
                        for r in db.execute("SELECT * FROM calls WHERE job IS NOT NULL"):
                            d=json.loads(r["data"])
                            values=[r["job"],r["token"],*[d[k] for k in FIELDS],"Requires verification",r["tech"],r["state"],r["previous"]]
                            writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(("=","+","-","@")) else v for v in values])
                        output,mime=stream.getvalue(),"text/csv; charset=utf-8"
                    else:
                        output,mime=dashboard(db,path),"text/html; charset=utf-8"
            else:
                raise PermissionError()
        except PermissionError:
            status,output="403 Forbidden",'{"error":"Forbidden"}'
        except (ValueError,KeyError,TypeError) as error:
            status,output="400 Bad Request",json.dumps({"error":str(error)})
        headers=[("Content-Type",mime),("Cache-Control","no-store")]
        if path=="/csv":
            headers.append(("Content-Disposition",'attachment; filename="mr-care.csv"'))
        respond(status,headers)
        return [output.encode()]
    port=8001 if public else 8010
    print(f"Listening at http://127.0.0.1:{port}",flush=True)
    make_server("127.0.0.1",port,app).serve_forever()

if __name__=="__main__":
    if "greeting" in sys.argv:
        print(greeting("ml" if "ml" in sys.argv else "en"))
    else:
        serve("tools" in sys.argv)
