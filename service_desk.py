"""Launch beside MR CARE's app.py to use the existing CRM and database."""
import json
import os
from collections import Counter, defaultdict
from html import escape
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
import app as crm


def esc(value):
    return escape(str(value if value is not None else "—"), quote=True)


STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#172c3e;font:15px/1.5 Inter,"Segoe UI",sans-serif}header{background:#113946;color:white;padding:18px max(5vw,22px);display:flex;align-items:center;gap:15px}header b{letter-spacing:1px;font-size:20px}header span{border-left:1px solid #63818a;padding-left:15px;color:#d6e6e7}main{max-width:1220px;margin:auto;padding:22px 4vw 44px}nav,.tools,.metrics,.row,.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}nav{border-bottom:1px solid #ccd7dd;padding-bottom:16px}a,button{font:inherit;color:#075e62;cursor:pointer}nav a,.btn,button{text-decoration:none;border-radius:7px;padding:8px 14px;font-weight:600}nav [aria-current],button.primary{background:#116864;color:white}.export{margin-left:auto}h1{font-size:clamp(28px,4vw,40px);letter-spacing:-1.4px;margin:26px 0 16px}h2{font-size:20px;margin:0}.metrics{align-items:stretch}.metric,.panel{background:white;border:1px solid #dce3e9;border-radius:12px}.metric{flex:1;min-width:125px;padding:16px 20px}.metric strong{display:block;font-size:30px}.metric span,small,dt{color:#536774}.panel{margin-top:18px;padding:20px}.tools{margin:16px 0}.tools input,.tools select{padding:9px 12px;border:1px solid #b9c8ce;border-radius:6px;font:inherit;max-width:100%}.tools input{flex:1;min-width:160px}.row{padding:12px 0;border-bottom:1px solid #e2e8ec;justify-content:space-between}.row:last-child{border:0}.badge{background:#eef1f4;color:#405462;border-radius:20px;padding:3px 10px;font-size:13px;font-weight:600}.Accepted{background:#e0f3ea;color:#126144}.Pending{background:#fff0ce;color:#78560c}.Unassigned{background:#fae6df;color:#84432d}details{border-top:1px solid #dde5e8}summary{cursor:pointer;display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr) auto;align-items:center;gap:14px;padding:15px 0}summary b,dd{overflow-wrap:anywhere}summary small{display:block}summary .badge{justify-self:end}details[open] summary b{color:#116864}dl{display:grid;grid-template-columns:minmax(130px,1fr) 3fr;gap:6px 14px}dd{margin:0}button,.btn{border:1px solid #b8c9cb;background:white}.actions{margin:14px 0}ol{padding-left:22px}li{margin:5px 0}footer{margin-top:16px;color:#536774;font-size:13px}[hidden]{display:none!important}@media(max-width:620px){main{padding:15px}.export{margin:0}nav{gap:0}nav a{padding:7px}.panel{padding:14px}summary{grid-template-columns:1fr auto}summary .assignee{display:none}.metric{min-width:42%}}
"""


def dashboard(db, path):
    tech_view = path == "/tech"
    records = list(db.execute("SELECT * FROM calls ORDER BY rowid DESC"))
    events = defaultdict(list)
    for event in db.execute("SELECT call_id,at,detail FROM events ORDER BY id"):
        events[event["call_id"]].append(event)
    counts = Counter(row["state"] for row in records)
    title = "Technician workspace" if tech_view else "Service dashboard"
    parts = [f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} | MR CARE</title><style>{STYLE}</style><header><b>MR CARE</b><span>Service desk</span></header><main><nav>']
    for url, label in [("/", "Dashboard"), ("/tech", "Technicians")]:
        current = ' aria-current="page"' if url == path else ""
        parts.append(f'<a href="{url}"{current}>{label}</a>')
    parts.append(f'<a class="export" href="/csv">Export CSV ↓</a></nav><h1>{title}</h1>')
    if not tech_view:
        parts.append('<section class="metrics" aria-label="Complaint overview">')
        for label, total in [("Registered complaints", sum(r["job"] is not None for r in records)),
                             ("Pending acceptance", counts["Pending"]), ("Accepted", counts["Accepted"]),
                             ("Unassigned", counts["Unassigned"])]:
            parts.append(f'<div class="metric"><span>{label}</span><strong>{total}</strong></div>')
        parts.append('</section><section class="panel"><h2>Technician workload</h2>')
        for tech in crm.CONFIG["technicians"]:
            name = tech["name"]
            assigned = Counter(r["state"] for r in records if r["tech"] == name)
            parts.append(f'<div class="row"><b>{esc(name)}</b><span>{esc(tech["branch"])} · {assigned["Pending"]} pending · {assigned["Accepted"]} accepted</span></div>')
        parts.append('</section>')
    else:
        records = [r for r in records if r["tech"] and r["state"] in ("Pending", "Accepted")]
        records.sort(key=lambda r: r["state"] != "Pending")
    heading = "Technician offers" if tech_view else "Recent requests"
    parts.append(f'<section class="panel"><h2>{heading}</h2><div class="tools"><input id="query" type="search" aria-label="Search requests" placeholder="Search job, customer, product or model"><select id="state" aria-label="Filter by status"><option value="">All statuses</option>')
    for state in sorted({r["state"] for r in records}):
        parts.append(f'<option>{esc(state)}</option>')
    parts.append('</select><button onclick="location.reload()">Refresh</button></div>')
    for row in records:
        data = json.loads(row["data"])
        state = esc(row["state"])
        job = f'Job #{row["job"]}' if row["job"] is not None else "Intake session"
        parts.append(f'<details data-state="{state}"><summary><span><b>{job} · {esc(data.get("product") or "Product pending")}</b><small>{esc(data.get("name") or "Customer details pending")}</small></span><span class="assignee">{esc(row["tech"])}</span><span class="badge {state}">{state}</span></summary><dl>')
        info = {"Customer token":row["token"], "Previous job":row["previous"], **data,
                "Assignment details":row["reason"], "Warranty coverage":"Requires verification", "Session":row["id"]}
        for label, value in info.items():
            parts.append(f'<dt>{esc(label.replace("_"," ").capitalize())}</dt><dd>{esc(value)}</dd>')
        parts.append('</dl>')
        if tech_view and state == "Pending":
            parts.append(f'<form class="actions" method="post" action="/offer"><input type="hidden" name="call_id" value="{esc(row["id"])}"><input type="hidden" name="tech" value="{esc(row["tech"])}"><button class="primary" name="action" value="Accept">Accept offer</button><button name="action" value="Reject">Reject offer</button></form>')
        parts.append('<h3>Activity</h3><ol>')
        for event in events[row["id"]]:
            parts.append(f'<li><small>{esc(event["at"])} UTC</small> · {esc(event["detail"])}</li>')
        parts.append('</ol></details>')
    parts.append("""<p id="empty" hidden>No matching requests.</p></section><footer>MR CARE demo CRM · SMS and WhatsApp disconnected</footer></main>
<script>
const cards = Array.from(document.querySelectorAll('details'));
const query = document.getElementById('query'), state = document.getElementById('state');
function filter(){
  const term = query.value.trim().toLowerCase();
  for(const card of cards){
    card.hidden = (state.value && card.dataset.state !== state.value) || !card.textContent.toLowerCase().includes(term);
  }
  document.getElementById('empty').hidden = cards.some(card => !card.hidden);
}
query.addEventListener('input',filter);state.addEventListener('change',filter);filter();
</script></html>""")
    return "".join(parts)


if __name__ == "__main__":
    crm.dashboard = dashboard
    crm.serve(False)
