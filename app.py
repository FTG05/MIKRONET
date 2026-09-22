"""
LENDING TRACKER  -  borrower CRM + loan/payment ledger.

Product rule: a loan of P over TERM days accrues a flat RATE% of P.
Default is 20% over 100 days, editable per loan.

    total_due = principal * (1 + rate/100)
    daily_due = total_due / term_days
    balance   = total_due - sum(payments)

Runs behind Cloudflare on create-automation.com. Binds 127.0.0.1 only.
"""
import os, re, json, csv, io, secrets, sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, Response, flash, send_file)

ROOT = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets a container mount the db + config on a persistent volume.
# Unset (the Windows case) it is just the app folder, same as before.
DATA_DIR = os.environ.get("DATA_DIR") or ROOT
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "lending.db")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEFAULTS = {
    "password": secrets.token_urlsafe(9),
    "secret_key": secrets.token_hex(32),
    "port": 8091,
    "currency": "PHP",
    "default_rate": 20.0,
    "default_term_days": 100,
    # Once a loan passes its due date unpaid, monthly interest kicks in.
    # Charged per COMPLETED period, on what was still owed when that period
    # began. Simple, not compounding. Per-loan override lives on loans.overdue_rate_pct.
    "overdue_rate": 20.0,
    "overdue_period_days": 30,
    # shown to internet-plan clients on their payment portal
    "biz_name": "INTERNET",
    "pay_note": "",
}

def load_cfg():
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH) as f:
            cfg = json.load(f)
        changed = False
        for k, v in DEFAULTS.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed:
            with open(CFG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
        return cfg
    with open(CFG_PATH, "w") as f:
        json.dump(DEFAULTS, f, indent=2)
    return dict(DEFAULTS)

def save_cfg():
    with open(CFG_PATH, "w") as f:
        json.dump(CFG, f, indent=2)


CFG = load_cfg()
SYM = {"PHP": "₱", "USD": "$", "EUR": "€", "GBP": "£"}.get(CFG["currency"], "")

app = Flask(__name__)
app.secret_key = CFG["secret_key"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  MAX_CONTENT_LENGTH=8 * 1024 * 1024)


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS borrowers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            phone      TEXT DEFAULT '',
            email      TEXT DEFAULT '',
            address    TEXT DEFAULT '',
            notes      TEXT DEFAULT '',
            archived   INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS loans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            borrower_id INTEGER NOT NULL REFERENCES borrowers(id) ON DELETE CASCADE,
            principal   REAL NOT NULL,
            rate_pct    REAL NOT NULL DEFAULT 20.0,
            term_days   INTEGER NOT NULL DEFAULT 100,
            start_date  TEXT NOT NULL,
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id    INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
            amount     REAL NOT NULL,
            paid_on    TEXT NOT NULL,
            method     TEXT DEFAULT '',
            note       TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS capital (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,          -- 'in' = you added, 'out' = you withdrew
            amount     REAL NOT NULL,
            moved_on   TEXT NOT NULL,
            note       TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_loans_borrower ON loans(borrower_id);
        CREATE INDEX IF NOT EXISTS idx_payments_loan  ON payments(loan_id);
        """)
        # migration: per-loan late-interest override (NULL = use config default)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(loans)")}
        if "overdue_rate_pct" not in cols:
            con.execute("ALTER TABLE loans ADD COLUMN overdue_rate_pct REAL")
        # sanla / pawn loans charge a flat amount per month, not a percentage.
        # When set, this wins over overdue_rate_pct.
        if "overdue_flat" not in cols:
            con.execute("ALTER TABLE loans ADD COLUMN overdue_flat REAL")
        # Open-ended interest-only loans (sanla, weekly renewals): the borrower
        # pays a fixed charge every period forever and the principal rides until
        # they redeem it. No maturity, so they can never be "overdue" - only
        # behind on the periodic charge.
        if "kind" not in cols:
            con.execute("ALTER TABLE loans ADD COLUMN kind TEXT DEFAULT 'term'")
            con.execute("ALTER TABLE loans ADD COLUMN charge_period_days INTEGER")
            con.execute("ALTER TABLE loans ADD COLUMN charge_amount REAL")

        # ---- internet-plan clients. Separate business, separate tables: these
        # people never appear in the ledger above, they only pay a monthly bill.
        con.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            access_key  TEXT NOT NULL UNIQUE,
            phone       TEXT DEFAULT '',
            address     TEXT DEFAULT '',
            plan        TEXT DEFAULT '',
            monthly     REAL DEFAULT 0,
            due_day     INTEGER DEFAULT 5,
            active      INTEGER DEFAULT 1,
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS client_payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            period       TEXT NOT NULL,           -- 'YYYY-MM', the bill it covers
            amount       REAL NOT NULL,
            method       TEXT DEFAULT '',
            reference    TEXT DEFAULT '',
            proof        TEXT DEFAULT '',         -- filename under uploads/
            status       TEXT DEFAULT 'pending',  -- pending | approved | rejected
            admin_note   TEXT DEFAULT '',
            submitted_at TEXT DEFAULT (datetime('now')),
            reviewed_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS pay_methods (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            label   TEXT NOT NULL,
            account TEXT DEFAULT '',
            number  TEXT DEFAULT '',
            note    TEXT DEFAULT '',
            qr      TEXT DEFAULT '',
            active  INTEGER DEFAULT 1,
            sort    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_cp_client ON client_payments(client_id);
        CREATE INDEX IF NOT EXISTS idx_cp_status ON client_payments(status);

        -- ---- signups from the public MikroNet site's "Apply" form. A lead
        -- lands here with their valid ID; once installed, turn it into a
        -- 'clients' row by hand (Clients -> Add a client) so they can pay.
        CREATE TABLE IF NOT EXISTS applications (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name    TEXT NOT NULL,
            street       TEXT DEFAULT '',
            municipality TEXT DEFAULT '',
            barangay     TEXT DEFAULT '',
            phone        TEXT DEFAULT '',
            email        TEXT DEFAULT '',
            plan         TEXT DEFAULT '',
            agent        TEXT DEFAULT '',
            message      TEXT DEFAULT '',
            id_file      TEXT DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'new',
            submitted_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_appl_status ON applications(status);
        """)
        # GCash has no public "pay this number" deep link - only payment
        # gateways can mint gcash:// checkout links. So this is a free-text
        # field: paste one in if you ever have one, leave it blank otherwise.
        mcols = {r["name"] for r in con.execute("PRAGMA table_info(pay_methods)")}
        if "link" not in mcols:
            con.execute("ALTER TABLE pay_methods ADD COLUMN link TEXT DEFAULT ''")
        # logo = an image the user uploads from the bank's own app. colour is the
        # fallback tint behind the initials when there is no logo yet.
        if "logo" not in mcols:
            con.execute("ALTER TABLE pay_methods ADD COLUMN logo TEXT DEFAULT ''")
            con.execute("ALTER TABLE pay_methods ADD COLUMN color TEXT DEFAULT ''")


# ---------------------------------------------------------------- money/dates
def money(x):
    return round(float(x or 0), 2)


def fmt(x):
    v = money(x)
    return ("-" if v < 0 else "") + SYM + "{:,.2f}".format(abs(v))


def opt_float(v):
    """Blank form field -> None, so the loan falls back to the config default."""
    v = (v or "").strip()
    return float(v) if v else None


def d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def today():
    return date.today()


def late_interest(total_due, due_date, pays, rate_pct, period_days, flat=None):
    """Monthly interest that starts once a loan matures unpaid.

    Two shapes, because sanla (pawn) loans are not percentage deals:

    FLAT (`flat` set) - a fixed amount per completed period, regardless of the
    balance. A 6,000 sanla at 250/month owes 250 for every month it runs on,
    whether 6,000 or 600 is left. This is what pawn tickets actually do.

    PERCENT - rate% of whatever was still owed when each period opened. Simple,
    not compounding: earlier late charges never accrue interest themselves.
    Worked example, 60,000 due at 20%/30d with nothing paid after maturity:
        day 30 -> +12,000    day 60 -> +24,000    day 89 -> still +24,000

    Either way it is recomputed from the payment history on every page load, so
    it is always current with no scheduled job to run or miss.
    """
    days_over = (today() - due_date).days
    periods = days_over // period_days if days_over > 0 else 0
    next_charge = due_date + timedelta(days=(periods + 1) * period_days)
    if periods <= 0:
        return 0.0, periods, next_charge

    if flat is not None and flat > 0:
        return money(flat * periods), periods, next_charge

    if rate_pct <= 0:
        return 0.0, periods, next_charge

    charged = 0.0
    for k in range(periods):
        opened = due_date + timedelta(days=k * period_days)
        paid_by_then = sum(a for pd, a in pays if pd <= opened)
        owed_then = max(0.0, total_due - paid_by_then)
        if owed_then <= 0:
            break                       # cleared before this period opened
        charged += owed_then * rate_pct / 100.0
    return money(charged), periods, next_charge


PERIOD_WORD = {7: "week", 14: "fortnight", 15: "15 days", 30: "month"}


def open_loan_view(row, keys, principal, start, paid):
    """An open-ended, interest-only loan.

    Lola Bebe pays 500 a week and keeps renewing; the 5,000 rides until she
    decides to redeem it. Richmond's sanla is the same deal at 250 a month.
    These have NO maturity date, so the term/late-interest machinery does not
    apply - there is nothing to be late for. The only question each period is
    whether the charge got paid.

        accrued = charge * completed periods
        owed    = principal + accrued - paid
    """
    per_days = int(row["charge_period_days"] or 7)
    charge = money(row["charge_amount"] or 0)
    periods = max(0, (today() - start).days // per_days)
    accrued = money(charge * periods)
    total_now = money(principal + accrued)
    balance = money(total_now - paid)

    # payments cover the periodic charge first; the rest pays down principal
    on_interest = money(min(paid, accrued))
    interest_owed = money(accrued - on_interest)
    principal_left = money(max(0.0, principal - (paid - on_interest)))

    if balance <= 0.005:
        status = "paid"
    elif interest_owed > 0.005:
        status = "behind"
    else:
        status = "current"

    word = PERIOD_WORD.get(per_days, "%dd" % per_days)
    return {
        "id": row["id"], "borrower_id": row["borrower_id"], "kind": "open",
        "principal": principal, "rate_pct": 0.0, "term_days": 0,
        "start_date": start, "due_date": None, "notes": row["notes"] or "",
        "total_due": principal, "interest": accrued,
        "paid": paid, "balance": balance,
        "penalty": 0.0, "periods": periods,
        "next_charge": start + timedelta(days=(periods + 1) * per_days),
        "next_add": charge, "od_rate": 0.0, "period_days": per_days,
        "od_flat": charge, "od_label": fmt(charge),
        "od_rate_raw": None, "od_flat_raw": None,
        "charge": charge, "charge_period_days": per_days, "period_word": word,
        "terms_label": "%s/%s, open-ended" % (fmt(charge), word),
        "accrued": accrued, "interest_owed": interest_owed,
        "principal_left": principal_left,
        "total_now": total_now,
        "days_left": None, "days_elapsed": (today() - start).days,
        "daily_due": money(charge / per_days), "expected": accrued,
        "drift": money(paid - accrued),
        "pct": min(100.0, (paid / total_now * 100.0) if total_now else 0.0),
        "status": status,
    }


def loan_view(row, pays=None):
    """Everything derived about a loan. `pays` is [(date, amount), ...]."""
    pays = pays or []
    keys = row.keys()
    principal = money(row["principal"])
    start = d(row["start_date"])
    paid_total = money(sum(a for _, a in pays))
    if "kind" in keys and (row["kind"] or "term") == "open":
        return open_loan_view(row, keys, principal, start, paid_total)

    rate = float(row["rate_pct"])
    term = int(row["term_days"])
    due_date = start + timedelta(days=term)
    od_rate = row["overdue_rate_pct"] if "overdue_rate_pct" in keys else None
    od_rate = float(od_rate) if od_rate is not None else float(CFG["overdue_rate"])
    od_flat = row["overdue_flat"] if "overdue_flat" in keys else None
    od_flat = float(od_flat) if od_flat is not None else None
    period_days = int(CFG["overdue_period_days"])

    total_due = money(principal * (1 + rate / 100.0))
    interest = money(total_due - principal)
    paid = money(sum(a for _, a in pays))

    penalty, periods, next_charge = late_interest(
        total_due, due_date, pays, od_rate, period_days, od_flat)
    total_now = money(total_due + penalty)
    balance = money(total_now - paid)

    elapsed = (today() - start).days
    elapsed_clamped = max(0, min(elapsed, term))
    days_left = (due_date - today()).days
    daily_due = money(total_due / term) if term else 0.0
    expected = money(daily_due * elapsed_clamped)
    drift = money(paid - expected)          # +ahead of schedule, -behind

    if balance <= 0.005:
        status = "paid"
    elif days_left < 0:
        status = "overdue"
    elif drift < -0.005:
        status = "behind"
    else:
        status = "active"

    # what the next 30-day rollover will add if nothing more is paid
    if status != "overdue":
        next_add = 0.0
    elif od_flat:
        next_add = money(od_flat)
    else:
        next_add = money(balance * od_rate / 100.0)
    od_label = fmt(od_flat) if od_flat else ("%g%%" % od_rate)

    return {
        "id": row["id"], "borrower_id": row["borrower_id"], "kind": "term",
        "charge": 0.0, "charge_period_days": None, "period_word": "",
        "terms_label": "%g%% over %dd" % (rate, term),
        "accrued": 0.0, "interest_owed": 0.0, "principal_left": principal,
        "principal": principal, "rate_pct": rate, "term_days": term,
        "start_date": start, "due_date": due_date, "notes": row["notes"] or "",
        "total_due": total_due, "interest": interest,
        "paid": paid, "balance": balance,
        "penalty": penalty, "periods": periods, "next_charge": next_charge,
        "next_add": next_add, "od_rate": od_rate, "period_days": period_days,
        "od_flat": od_flat, "od_label": od_label,
        "od_rate_raw": (row["overdue_rate_pct"] if "overdue_rate_pct" in keys else None),
        "od_flat_raw": (row["overdue_flat"] if "overdue_flat" in keys else None),
        "total_now": total_now,
        "days_left": days_left, "days_elapsed": elapsed_clamped,
        "daily_due": daily_due, "expected": expected, "drift": drift,
        "pct": min(100.0, (paid / total_now * 100.0) if total_now else 0.0),
        "status": status,
    }


def all_loans(con, borrower_id=None):
    """Late interest needs each payment's DATE, not just the total, so payments
    are pulled once and grouped rather than summed in SQL."""
    pq = "SELECT p.loan_id, p.paid_on, p.amount FROM payments p"
    pargs = []
    if borrower_id:
        pq += " JOIN loans l ON l.id=p.loan_id WHERE l.borrower_id=?"
        pargs.append(borrower_id)
    by_loan = {}
    for r in con.execute(pq, pargs):
        by_loan.setdefault(r["loan_id"], []).append((d(r["paid_on"]), float(r["amount"])))

    q = ("SELECT l.*, b.name AS borrower_name "
         "FROM loans l JOIN borrowers b ON b.id=l.borrower_id")
    args = []
    if borrower_id:
        q += " WHERE l.borrower_id=?"
        args.append(borrower_id)
    q += " ORDER BY date(l.start_date) DESC, l.id DESC"
    out = []
    for r in con.execute(q, args):
        v = loan_view(r, by_loan.get(r["id"], []))
        v["borrower_name"] = r["borrower_name"]
        out.append(v)
    return out


def cash_summary(con):
    """The cash box.

    Collections get re-lent, so the money you have actually put in is NOT the
    sum of every principal ever written. A 36k collection redeployed as a new
    loan adds 36k to `deployed` but zero to `cap_in` - it is the same money
    going around again. Only `capital` rows represent real outside money.

        on_hand   = capital in - capital out + collected - deployed
        recycled  = the slice of `deployed` that collections paid for
    """
    def one(sql, *a):
        return money(con.execute(sql, a).fetchone()[0])

    t = today().isoformat()

    # ALL-TIME totals - the history of the book.
    cap_in = one("SELECT COALESCE(SUM(amount),0) FROM capital WHERE kind='in'")
    cap_out = one("SELECT COALESCE(SUM(amount),0) FROM capital WHERE kind='out'")
    deployed = one("SELECT COALESCE(SUM(principal),0) FROM loans")
    collected = one("SELECT COALESCE(SUM(amount),0) FROM payments")

    # AS OF TODAY - the only thing that can honestly be called cash on hand.
    # Rows dated ahead of today are money that has not moved yet; counting them
    # made the balance read as a forecast rather than what is in the box.
    cap_in_t = one("SELECT COALESCE(SUM(amount),0) FROM capital "
                   "WHERE kind='in' AND date(moved_on)<=date(?)", t)
    cap_out_t = one("SELECT COALESCE(SUM(amount),0) FROM capital "
                    "WHERE kind='out' AND date(moved_on)<=date(?)", t)
    deployed_t = one("SELECT COALESCE(SUM(principal),0) FROM loans "
                     "WHERE date(start_date)<=date(?)", t)
    collected_t = one("SELECT COALESCE(SUM(amount),0) FROM payments "
                      "WHERE date(paid_on)<=date(?)", t)

    on_hand_today = money(cap_in_t - cap_out_t + collected_t - deployed_t)
    on_hand_proj = money(cap_in - cap_out + collected - deployed)

    net_capital = money(cap_in - cap_out)
    from_pocket = money(min(deployed, net_capital) if net_capital > 0 else 0)
    return {
        "cap_in": cap_in, "cap_out": cap_out, "net_capital": net_capital,
        "deployed": deployed, "collected": collected,
        "on_hand": on_hand_today,
        "on_hand_proj": on_hand_proj,
        "pending": money(on_hand_proj - on_hand_today),
        "collected_t": collected_t, "deployed_t": deployed_t,
        "net_capital_t": money(cap_in_t - cap_out_t),
        "from_pocket": from_pocket,
        "recycled": money(max(0.0, deployed - from_pocket)),
    }


def pending_rows(con):
    """Everything dated after today - money that has not actually moved yet."""
    t = today().isoformat()
    out = []
    for r in con.execute("SELECT moved_on d, kind, amount, note FROM capital "
                         "WHERE date(moved_on)>date(?)", (t,)):
        out.append({"d": r["d"], "what": "capital " + r["kind"], "who": r["note"] or "",
                    "amt": money(r["amount"] if r["kind"] == "in" else -r["amount"])})
    for r in con.execute("SELECT l.start_date d, l.principal, b.name FROM loans l "
                         "JOIN borrowers b ON b.id=l.borrower_id "
                         "WHERE date(l.start_date)>date(?)", (t,)):
        out.append({"d": r["d"], "what": "lend", "who": r["name"],
                    "amt": money(-r["principal"])})
    for r in con.execute("SELECT p.paid_on d, p.amount, b.name FROM payments p "
                         "JOIN loans l ON l.id=p.loan_id "
                         "JOIN borrowers b ON b.id=l.borrower_id "
                         "WHERE date(p.paid_on)>date(?)", (t,)):
        out.append({"d": r["d"], "what": "collect", "who": r["name"],
                    "amt": money(r["amount"])})
    out.sort(key=lambda x: x["d"])
    return out


# ---------------------------------------------------------------- auth
def login_required(fn):
    """Admin only. The whole lending ledger sits behind this."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if session.get("role") != "admin":
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def cur_client():
    with db() as con:
        return con.execute("SELECT * FROM clients WHERE id=? AND active=1",
                           (session.get("cid", 0),)).fetchone()


def client_required(fn):
    """A paying client, on their own portal and nowhere else."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if session.get("role") != "client":
            return redirect(url_for("login", next=request.path))
        if not cur_client():        # key deleted or switched off mid-session
            session.clear()
            flash("That access key is no longer active.", "err")
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    """One box. The admin password lands on the ledger, a client key on the
    portal. Keys are random enough that a plain lookup is fine here."""
    err = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if secrets.compare_digest(pw, CFG["password"]):
            session.clear()
            session["role"] = "admin"
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        with db() as con:
            row = con.execute("SELECT id FROM clients WHERE access_key=? AND active=1",
                              (pw.strip().upper(),)).fetchone()
        if row:
            session.clear()
            session["role"] = "client"
            session["cid"] = row["id"]
            session.permanent = True
            return redirect(url_for("portal"))
        err = "Wrong password or access key."
    return render_template_string(LOGIN_HTML, err=err, cfg=CFG)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", app="lending")


# ---------------------------------------------------------------- public site
@app.route("/")
def home():
    """The MikroNet marketing site. Public, no login - this is the front
    door. 'Pay Bill' in its header is the only door into the password box."""
    return render_template_string(MIKRONET_HTML)


@app.route("/pay")
def pay_redirect():
    """Where the site's 'Pay Bill' button goes. Already signed in? skip
    straight past the password box instead of asking again."""
    role = session.get("role")
    if role == "admin":
        return redirect(url_for("dashboard"))
    if role == "client" and cur_client():
        return redirect(url_for("portal"))
    return redirect(url_for("login"))


@app.route("/apply", methods=["POST"])
def apply_submit():
    """The site's sign-up form posts here directly (no more Formspree) -
    one valid ID, one lead, saved straight to the same database and folder
    everything else in this app uses."""
    f = request.form
    full_name = (f.get("fullName") or "").strip()
    if not full_name:
        return jsonify(ok=False, error="Full name is required."), 400
    try:
        id_file = save_upload(request.files.get("idUpload"), "appid", ALLOWED_ID)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    with db() as con:
        con.execute("""INSERT INTO applications
                       (full_name, street, municipality, barangay, phone,
                        email, plan, agent, message, id_file)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (full_name, (f.get("street") or "").strip(),
                     (f.get("municipality") or "").strip(),
                     (f.get("barangay") or "").strip(),
                     (f.get("phone") or "").strip(),
                     (f.get("email") or "").strip(),
                     (f.get("dataPlan") or "").strip(),
                     (f.get("agent") or "").strip(),
                     (f.get("message") or "").strip(), id_file))
    return jsonify(ok=True)


# ---------------------------------------------------------------- dashboard
@app.route("/dashboard")
@login_required
def dashboard():
    with db() as con:
        loans = all_loans(con)
        n_borrowers = con.execute(
            "SELECT COUNT(*) c FROM borrowers WHERE archived=0").fetchone()["c"]
        cash = cash_summary(con)

    k = {
        "principal": money(sum(l["principal"] for l in loans)),
        "collected": money(sum(l["paid"] for l in loans)),
        # payments against loans still running - excludes fully settled ones,
        # which is the figure you check a running book against
        "collected_open": money(sum(l["paid"] for l in loans if l["status"] != "paid")),
        "collected_settled": money(sum(l["paid"] for l in loans if l["status"] == "paid")),
        "outstanding": money(sum(l["balance"] for l in loans if l["balance"] > 0)),
        "interest": money(sum(l["interest"] for l in loans)),
        "penalty": money(sum(l["penalty"] for l in loans)),
        "overdue": sum(1 for l in loans if l["status"] == "overdue"),
        "behind": sum(1 for l in loans if l["status"] == "behind"),
        "active": sum(1 for l in loans if l["status"] != "paid"),
        "borrowers": n_borrowers,
    }
    k["overdue_amt"] = money(sum(l["balance"] for l in loans if l["status"] == "overdue"))

    open_loans = [l for l in loans if l["status"] != "paid"]
    # Open-ended loans have no due date, so days_left is None and cannot be
    # compared against a term loan's integer. Sort those to the end of their
    # status group rather than blowing up the whole dashboard.
    order = {"overdue": 0, "behind": 1, "current": 2, "active": 2}
    open_loans.sort(key=lambda l: (order.get(l["status"], 3),
                                   l["days_left"] if l["days_left"] is not None else 10 ** 6))
    closed = [l for l in loans if l["status"] == "paid"]

    with db() as con:
        people = con.execute(
            "SELECT id,name FROM borrowers WHERE archived=0 ORDER BY name COLLATE NOCASE"
        ).fetchall()

    return render_template_string(DASH_HTML, k=k, c=cash, open_loans=open_loans,
                                  closed=closed, fmt=fmt, today=today(),
                                  people=people, cfg=CFG)


# ---------------------------------------------------------------- capital
@app.route("/capital")
@login_required
def capital():
    with db() as con:
        c = cash_summary(con)
        pend = pending_rows(con)
        rows = con.execute("SELECT * FROM capital ORDER BY date(moved_on) DESC, id DESC").fetchall()
    return render_template_string(CAPITAL_HTML, c=c, rows=rows, pend=pend,
                                  fmt=fmt, today=today())


@app.route("/capital/new", methods=["POST"])
@login_required
def capital_new():
    try:
        amount = abs(float(request.form["amount"]))
        if amount <= 0:
            raise ValueError
    except Exception:
        flash("Amount must be a positive number.", "err")
        return redirect(url_for("capital"))
    kind = "out" if request.form.get("kind") == "out" else "in"
    with db() as con:
        con.execute("INSERT INTO capital (kind,amount,moved_on,note) VALUES (?,?,?,?)",
                    (kind, amount,
                     request.form.get("moved_on") or today().isoformat(),
                     request.form.get("note", "").strip()))
    flash("Capital " + ("withdrawal" if kind == "out" else "top-up") + " recorded.", "ok")
    return redirect(url_for("capital"))


@app.route("/capital/<int:cid>/delete", methods=["POST"])
@login_required
def capital_delete(cid):
    with db() as con:
        con.execute("DELETE FROM capital WHERE id=?", (cid,))
    flash("Entry removed.", "ok")
    return redirect(url_for("capital"))


# ---------------------------------------------------------------- borrowers
@app.route("/borrowers")
@login_required
def borrowers():
    q = (request.args.get("q") or "").strip()
    show_archived = request.args.get("archived") == "1"
    with db() as con:
        sql = ("SELECT b.* FROM borrowers b WHERE b.archived=?")
        args = [1 if show_archived else 0]
        if q:
            sql += " AND (b.name LIKE ? OR b.phone LIKE ? OR b.email LIKE ? OR b.notes LIKE ?)"
            like = "%" + q + "%"
            args += [like, like, like, like]
        sql += " ORDER BY b.name COLLATE NOCASE"
        rows = con.execute(sql, args).fetchall()

        people = []
        for r in rows:
            ls = all_loans(con, r["id"])
            people.append({
                "row": r,
                "balance": money(sum(l["balance"] for l in ls if l["balance"] > 0)),
                "paid": money(sum(l["paid"] for l in ls)),
                "open": sum(1 for l in ls if l["status"] != "paid"),
                "worst": ("overdue" if any(l["status"] == "overdue" for l in ls)
                          else "behind" if any(l["status"] == "behind" for l in ls)
                          else "active" if any(l["status"] != "paid" for l in ls)
                          else "paid"),
            })
    return render_template_string(PEOPLE_HTML, people=people, q=q, fmt=fmt,
                                  show_archived=show_archived)


@app.route("/borrower/new", methods=["POST"])
@login_required
def borrower_new():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name is required.", "err")
        return redirect(url_for("borrowers"))
    with db() as con:
        cur = con.execute(
            "INSERT INTO borrowers (name,phone,email,address,notes) VALUES (?,?,?,?,?)",
            (name, request.form.get("phone", "").strip(),
             request.form.get("email", "").strip(),
             request.form.get("address", "").strip(),
             request.form.get("notes", "").strip()))
        bid = cur.lastrowid
    flash("Added " + name + ".", "ok")
    return redirect(url_for("borrower", bid=bid))


@app.route("/borrower/<int:bid>")
@login_required
def borrower(bid):
    with db() as con:
        b = con.execute("SELECT * FROM borrowers WHERE id=?", (bid,)).fetchone()
        if not b:
            return redirect(url_for("borrowers"))
        loans = all_loans(con, bid)
        pays = con.execute(
            "SELECT p.* FROM payments p JOIN loans l ON l.id=p.loan_id "
            "WHERE l.borrower_id=? ORDER BY date(p.paid_on) DESC, p.id DESC",
            (bid,)).fetchall()
    tot = {
        "principal": money(sum(l["principal"] for l in loans)),
        "due": money(sum(l["total_due"] for l in loans)),
        "paid": money(sum(l["paid"] for l in loans)),
        "balance": money(sum(l["balance"] for l in loans if l["balance"] > 0)),
    }
    return render_template_string(BORROWER_HTML, b=b, loans=loans, pays=pays,
                                  tot=tot, fmt=fmt, today=today(), cfg=CFG)


@app.route("/borrower/<int:bid>/edit", methods=["POST"])
@login_required
def borrower_edit(bid):
    with db() as con:
        con.execute("UPDATE borrowers SET name=?,phone=?,email=?,address=?,notes=?,archived=? "
                    "WHERE id=?",
                    ((request.form.get("name") or "").strip(),
                     request.form.get("phone", "").strip(),
                     request.form.get("email", "").strip(),
                     request.form.get("address", "").strip(),
                     request.form.get("notes", "").strip(),
                     1 if request.form.get("archived") else 0,
                     bid))
    flash("Saved.", "ok")
    return redirect(url_for("borrower", bid=bid))


@app.route("/borrower/<int:bid>/delete", methods=["POST"])
@login_required
def borrower_delete(bid):
    with db() as con:
        con.execute("DELETE FROM borrowers WHERE id=?", (bid,))
    flash("Borrower deleted, along with their loans and payments.", "ok")
    return redirect(url_for("borrowers"))


# ---------------------------------------------------------------- loans
@app.route("/loan/new", methods=["POST"])
@login_required
def loan_new():
    bid = int(request.form["borrower_id"])
    try:
        principal = float(request.form["principal"])
        if principal <= 0:
            raise ValueError
    except Exception:
        flash("Principal must be a positive number.", "err")
        return redirect(url_for("borrower", bid=bid))
    with db() as con:
        con.execute("INSERT INTO loans (borrower_id,principal,rate_pct,term_days,start_date,"
                    "notes,overdue_rate_pct,overdue_flat,kind,charge_period_days,charge_amount) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, principal,
                     float(request.form.get("rate_pct") or CFG["default_rate"]),
                     int(request.form.get("term_days") or CFG["default_term_days"]),
                     request.form.get("start_date") or today().isoformat(),
                     request.form.get("notes", "").strip(),
                     opt_float(request.form.get("overdue_rate_pct")),
                     opt_float(request.form.get("overdue_flat")),
                     "open" if request.form.get("kind") == "open" else "term",
                     int(request.form.get("charge_period_days") or 7),
                     opt_float(request.form.get("charge_amount"))))
    flash("Loan added.", "ok")
    return redirect(url_for("borrower", bid=bid))


@app.route("/loan/<int:lid>/edit", methods=["POST"])
@login_required
def loan_edit(lid):
    with db() as con:
        row = con.execute("SELECT borrower_id FROM loans WHERE id=?", (lid,)).fetchone()
        if not row:
            return redirect(url_for("dashboard"))
        con.execute("UPDATE loans SET principal=?,rate_pct=?,term_days=?,start_date=?,notes=?,"
                    "overdue_rate_pct=?,overdue_flat=?,kind=?,charge_period_days=?,"
                    "charge_amount=? WHERE id=?",
                    (float(request.form["principal"]),
                     float(request.form.get("rate_pct") or 0),
                     int(request.form.get("term_days") or 1),
                     request.form["start_date"],
                     request.form.get("notes", "").strip(),
                     opt_float(request.form.get("overdue_rate_pct")),
                     opt_float(request.form.get("overdue_flat")),
                     "open" if request.form.get("kind") == "open" else "term",
                     int(request.form.get("charge_period_days") or 7),
                     opt_float(request.form.get("charge_amount")), lid))
    flash("Loan updated.", "ok")
    return redirect(url_for("borrower", bid=row["borrower_id"]))


@app.route("/loan/<int:lid>/delete", methods=["POST"])
@login_required
def loan_delete(lid):
    with db() as con:
        row = con.execute("SELECT borrower_id FROM loans WHERE id=?", (lid,)).fetchone()
        con.execute("DELETE FROM loans WHERE id=?", (lid,))
    flash("Loan deleted.", "ok")
    if row:
        return redirect(url_for("borrower", bid=row["borrower_id"]))
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- payments
@app.route("/payment/new", methods=["POST"])
@login_required
def payment_new():
    lid = int(request.form["loan_id"])
    with db() as con:
        row = con.execute("SELECT borrower_id FROM loans WHERE id=?", (lid,)).fetchone()
    if not row:
        return redirect(url_for("dashboard"))
    try:
        amount = float(request.form["amount"])
        if amount == 0:
            raise ValueError
    except Exception:
        flash("Payment amount must be a non-zero number.", "err")
        return redirect(url_for("borrower", bid=row["borrower_id"]))
    with db() as con:
        con.execute("INSERT INTO payments (loan_id,amount,paid_on,method,note) VALUES (?,?,?,?,?)",
                    (lid, amount,
                     request.form.get("paid_on") or today().isoformat(),
                     request.form.get("method", "").strip(),
                     request.form.get("note", "").strip()))
    flash("Payment recorded.", "ok")
    return redirect(url_for("borrower", bid=row["borrower_id"]))


@app.route("/payment/<int:pid>/delete", methods=["POST"])
@login_required
def payment_delete(pid):
    with db() as con:
        row = con.execute("SELECT l.borrower_id AS bid FROM payments p "
                          "JOIN loans l ON l.id=p.loan_id WHERE p.id=?", (pid,)).fetchone()
        con.execute("DELETE FROM payments WHERE id=?", (pid,))
    flash("Payment removed.", "ok")
    if row:
        return redirect(url_for("borrower", bid=row["bid"]))
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- export
@app.route("/export.csv")
@login_required
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["borrower", "phone", "principal", "rate_pct", "term_days", "start_date",
                "due_date", "total_due", "late_rate_pct", "late_periods", "late_interest",
                "payable_now", "paid", "balance", "days_left", "next_late_charge",
                "status", "notes"])
    with db() as con:
        phones = {r["id"]: r["phone"] for r in con.execute("SELECT id,phone FROM borrowers")}
        for l in all_loans(con):
            w.writerow([l["borrower_name"], phones.get(l["borrower_id"], ""),
                        l["principal"], l["rate_pct"], l["term_days"], l["start_date"],
                        l["due_date"], l["total_due"], l["od_label"], l["periods"],
                        l["penalty"], l["total_now"], l["paid"], l["balance"],
                        l["days_left"],
                        l["next_charge"] if l["status"] == "overdue" else "",
                        l["status"], l["notes"]])
    fname = "loans-" + today().isoformat() + ".csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + fname})


# ================================================================ templates
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080c18; --panel:#111830; --panel2:#18203c; --line:#253055;
  --ink:#e9edf9; --dim:#8894b8; --accent:#3b82f6; --accent-d:#2563eb;
  --accent-soft:rgba(59,130,246,.13); --sky:#60a5fa;
  --green:#34d399; --red:#f87171; --amber:#fbbf24;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);
  --glow:0 0 0 1px rgba(59,130,246,.25),0 8px 28px -10px rgba(59,130,246,.45);
}
body{background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  padding-bottom:60px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{background:var(--panel);border-bottom:1px solid var(--line);
  padding:14px 22px;display:flex;align-items:center;gap:22px;
  position:sticky;top:0;z-index:20}
header .brand{font-weight:700;font-size:17px;letter-spacing:.4px}
header .brand span{color:var(--accent)}
header nav{display:flex;gap:18px;margin-left:8px}
header nav a{color:var(--dim);font-size:14px;font-weight:500}
header nav a.on,header nav a:hover{color:var(--ink);text-decoration:none}
header .sp{margin-left:auto}
.wrap{max-width:1180px;margin:0 auto;padding:24px 22px}
h2{font-size:19px;margin:26px 0 12px;font-weight:650}
h2:first-child{margin-top:0}
.sub{color:var(--dim);font-size:13px;font-weight:400;margin-left:8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;
  display:flex;flex-direction:column}
/* sub-captions wrap to different line counts, so pin them to the bottom -
   otherwise the cards in a row read as if they are on different baselines */
.kpi .small{margin-top:auto;padding-top:5px}
.kpi .lab{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.7px}
.kpi .val{font-size:22px;font-weight:680;margin-top:5px;font-variant-numeric:tabular-nums}
.kpi.hot .val{color:var(--red)} .kpi.good .val{color:var(--green)}
.kpi.warn .val{color:var(--amber)} .kpi.o .val{color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:18px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--dim);font-size:11px;text-transform:uppercase;
  letter-spacing:.7px;padding:0 10px 9px;font-weight:600;white-space:nowrap}
th.num{text-align:right}   /* headers must sit over their figures, not left of them */
td{padding:11px 10px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums;
  vertical-align:middle}
tr:hover td{background:var(--panel2)}
td.name{font-weight:600;font-variant-numeric:normal}
.num{text-align:right}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;
  font-weight:650;text-transform:uppercase;letter-spacing:.4px}
.pill.active{background:rgba(62,207,142,.14);color:var(--green)}
.pill.current{background:rgba(62,207,142,.14);color:var(--green)}
.pill.paid{background:rgba(139,145,163,.16);color:var(--dim)}
.pill.behind{background:rgba(255,176,32,.14);color:var(--amber)}
.pill.overdue{background:rgba(255,92,92,.15);color:var(--red)}
.bar{height:5px;background:var(--panel2);border-radius:3px;overflow:hidden;
  min-width:80px;margin-top:5px}
.bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
.bar i.done{background:var(--green)}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
  border-radius:7px;padding:9px 11px;font:inherit;width:100%}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
label{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:5px;font-weight:600}
.row{display:grid;gap:12px;margin-bottom:12px}
.c2{grid-template-columns:1fr 1fr} .c3{grid-template-columns:1fr 1fr 1fr}
.c4{grid-template-columns:repeat(4,1fr)}
.c5{grid-template-columns:repeat(5,1fr)}
button,.btn{background:var(--accent);color:#12130f;border:none;border-radius:7px;
  padding:9px 16px;font:inherit;font-weight:650;cursor:pointer;width:auto;
  display:inline-block}
button:hover,.btn:hover{background:var(--accent-d);text-decoration:none}
button.ghost,.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--dim)}
button.ghost:hover{background:var(--panel2);color:var(--ink)}
button.danger{background:transparent;border:1px solid rgba(255,92,92,.35);color:var(--red);
  padding:5px 11px;font-size:12px}
button.danger:hover{background:rgba(255,92,92,.12)}
button.mini{padding:5px 11px;font-size:12px}
.flash{padding:11px 16px;border-radius:8px;margin-bottom:16px;font-size:14px}
.flash.ok{background:rgba(62,207,142,.12);color:var(--green);
  border:1px solid rgba(62,207,142,.25)}
.flash.err{background:rgba(255,92,92,.12);color:var(--red);
  border:1px solid rgba(255,92,92,.25)}
details{margin-top:10px}
details summary{cursor:pointer;color:var(--dim);font-size:13px;list-style:none;
  padding:6px 0;user-select:none}
details summary:hover{color:var(--accent)}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"+ ";font-weight:700}
details[open] summary::before{content:"- "}
.empty{color:var(--dim);text-align:center;padding:34px;font-size:14px}
.muted{color:var(--dim)} .g{color:var(--green)} .r{color:var(--red)} .a{color:var(--amber)}
.small{font-size:12px}
.badge{background:var(--accent);color:#12130f;border-radius:11px;padding:1px 7px;
  font-size:11px;font-weight:700;margin-left:5px}
.login{max-width:340px;margin:14vh auto;padding:0 20px}
.login .card{padding:26px}
.login h1{font-size:21px;margin-bottom:6px} .login h1 span{color:var(--accent)}
.login p{color:var(--dim);font-size:13px;margin-bottom:20px}
.loan-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.loan-head .amt{font-size:17px;font-weight:680}
.terms{color:var(--dim);font-size:12px}
/* ---- polish: depth, motion, and a bit of light behind the accent ---- */
body{background:
  radial-gradient(1100px 620px at 12% -12%, rgba(59,130,246,.10), transparent 62%),
  radial-gradient(900px 520px at 92% 0%, rgba(96,165,250,.07), transparent 58%),
  var(--bg);
  background-attachment:fixed}
header{background:rgba(17,24,48,.86);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line);box-shadow:0 1px 0 rgba(255,255,255,.03)}
header .brand{background:linear-gradient(96deg,var(--ink),var(--sky));
  -webkit-background-clip:text;background-clip:text;color:transparent}
header .brand span{-webkit-text-fill-color:var(--accent);color:var(--accent)}
header nav a{padding:5px 0;position:relative;transition:color .15s}
header nav a.on::after{content:"";position:absolute;left:0;right:0;bottom:-2px;
  height:2px;border-radius:2px;background:var(--accent)}
.card,.kpi,.pay{box-shadow:var(--shadow);transition:border-color .16s,transform .16s}
.kpi{position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;
  background:var(--accent);opacity:.55}
.kpi.good::before{background:var(--green)} .kpi.hot::before{background:var(--red)}
.kpi.warn::before{background:var(--amber)} .kpi.o::before{background:var(--accent)}
.kpi .lab{padding-left:2px}
.pay:hover{border-color:rgba(59,130,246,.45);transform:translateY(-2px)}
h2{letter-spacing:-.2px}
button,.btn{background:linear-gradient(180deg,var(--accent),var(--accent-d));
  color:#fff;box-shadow:0 1px 0 rgba(255,255,255,.14) inset,
  0 6px 16px -8px rgba(37,99,235,.9);transition:filter .15s,transform .08s}
button:hover,.btn:hover{background:linear-gradient(180deg,var(--sky),var(--accent));
  filter:brightness(1.04)}
button:active,.btn:active{transform:translateY(1px)}
button.ghost,.btn.ghost{background:var(--panel2);box-shadow:none;color:var(--dim)}
button.ghost:hover,.btn.ghost:hover{background:var(--panel2);
  border-color:var(--accent);color:var(--ink);filter:none}
button.danger{background:transparent;box-shadow:none}
button.danger:hover{background:rgba(248,113,113,.12);filter:none}
input:focus,select:focus,textarea:focus{box-shadow:0 0 0 3px var(--accent-soft)}
a:hover{text-decoration:none;color:var(--sky)}
td.name a{color:var(--ink)} td.name a:hover{color:var(--sky)}
.pill{border:1px solid transparent}
.login .card{box-shadow:var(--glow)}
.login h1{font-size:24px;letter-spacing:-.4px;
  background:linear-gradient(96deg,var(--ink),var(--sky));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.login h1 span{-webkit-text-fill-color:var(--accent);color:var(--accent)}

/* ---- tablet: tables still fit, wide forms do not ---- */
@media(max-width:900px){
  .c4,.c5{grid-template-columns:1fr 1fr}
  .wrap{padding:18px 16px}
  th{font-size:10px;padding-left:7px;padding-right:7px}
  td{padding-left:7px;padding-right:7px}
}

/* ---- phone: every table restacks into label/value cards ----
   A 9-column ledger cannot be squeezed into 375px and side-scrolling a table
   of money is miserable, so each row becomes its own card with the column
   header carried down as the row label via data-l. ---- */
@media(max-width:620px){
  .c2,.c3,.c4,.c5{grid-template-columns:1fr}
  .wrap{padding:14px 12px}
  h2{font-size:17px;margin:22px 0 10px}
  header{padding:11px 13px;gap:10px;flex-wrap:wrap}
  header .brand{font-size:16px}
  header nav{order:3;width:100%;margin-left:0;gap:16px;
    overflow-x:auto;white-space:nowrap;padding-bottom:2px}
  header .sp{display:none}
  .kpis{grid-template-columns:1fr 1fr;gap:9px}
  .kpi{padding:12px 13px}
  .kpi .val{font-size:18px}
  .kpi .lab{font-size:10px}
  .card{padding:14px}

  /* 16px stops iOS Safari zooming the page when a field takes focus */
  input,select,textarea{font-size:16px;padding:11px}
  button,.btn{padding:12px 18px}          /* comfortable tap targets */
  button.mini,button.danger{padding:9px 14px}

  table,table>tbody{display:block}
  table tr:first-child{display:none}      /* the <th> header row */
  table tr{display:block;border:1px solid var(--line);border-radius:9px;
    margin-bottom:10px;overflow:hidden;background:var(--panel)}
  table tr:hover td{background:none}
  table td{display:flex;justify-content:space-between;align-items:baseline;
    gap:14px;padding:9px 13px;border:none;border-top:1px solid var(--line);
    text-align:right}
  table td:first-child{border-top:none}
  table td::before{content:attr(data-l);color:var(--dim);font-size:10px;
    font-weight:650;text-transform:uppercase;letter-spacing:.6px;
    text-align:left;flex:0 0 auto}
  table td.name{background:var(--panel2);text-align:left;font-size:15px;
    padding:12px 13px;display:block}
  table td.name::before{display:none}
  table td .bar{min-width:130px}
  table td form{margin:0}
  .loan-head .amt{font-size:16px}
  .loan-head span[style]{margin-left:0 !important}
}
"""

# ================================================================ public site
# The MikroNet marketing site (formerly a standalone mikronet.html). Rendered
# through Jinja like every other page here so its 'Pay Bill' button and its
# Apply form can use url_for() and post straight into this same app/db.
MIKRONET_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MikroNet — Pure Fiber Internet</title>
<meta name="description" content="MikroNet delivers pure fiber internet for homes and businesses. Fast, reliable, always on.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#050B1C;--surface:#0D1E40;--surface-2:#142650;
    --line:rgba(255,255,255,0.08);--text:#E8EDF8;--muted:#7A8BAD;
    --accent:#0090FF;--accent-bright:#00D9FF;--accent-violet:#7B61FF;--accent-dim:rgba(0,144,255,0.13);
    --radius:14px;--maxw:1160px;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  html{scroll-behavior:smooth;}
  section,footer{scroll-margin-top:70px;}
  body{margin:0;background:var(--ink);color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased;overflow-x:hidden;}
  h1,h2,h3{font-family:'Plus Jakarta Sans',sans-serif;line-height:1.12;margin:0;letter-spacing:-0.02em;font-weight:800;}
  a{color:inherit;text-decoration:none;}img{max-width:100%;display:block;}button{font-family:inherit;}ul{margin:0;padding:0;list-style:none;}
  .mono{font-family:'JetBrains Mono',monospace;}
  .wrap{max-width:var(--maxw);margin:0 auto;padding:0 16px;position:relative;}
  @media(min-width:480px){.wrap{padding:0 20px;}}
  @media(min-width:720px){.wrap{padding:0 24px;}}
  section{padding:56px 0;position:relative;}
  @media(min-width:720px){section{padding:88px 0;}}

  body::before{
    content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:0.4;
    background-image:radial-gradient(rgba(255,255,255,0.035) 1px, transparent 1px);
    background-size:26px 26px;
  }
  body > * { position:relative; z-index:1; }

  .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:14px 22px;border-radius:9px;font-weight:700;font-size:0.94rem;border:1.5px solid transparent;cursor:pointer;transition:transform .25s cubic-bezier(.2,.8,.2,1),box-shadow .25s ease,border-color .2s ease,background .2s ease;min-height:48px;position:relative;overflow:hidden;}
  .btn:active{transform:scale(0.96);}
  .btn-primary{background:linear-gradient(135deg,var(--accent),#0066CC);color:#fff;border-color:transparent;box-shadow:0 4px 18px rgba(0,144,255,0.35);}
  .btn-primary::after{content:'';position:absolute;inset:0;background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,0.35) 45%,transparent 60%);transform:translateX(-120%);transition:transform .6s ease;}
  .btn-primary:hover{box-shadow:0 6px 26px rgba(0,217,255,0.5);transform:translateY(-2px);}
  .btn-primary:hover::after{transform:translateX(120%);}
  .btn-ghost{background:transparent;border-color:rgba(255,255,255,0.18);color:var(--text);}
  .btn-ghost:hover{border-color:var(--accent-bright);color:var(--accent-bright);transform:translateY(-2px);}

  header{position:sticky;top:0;z-index:50;background:rgba(5,11,28,0.7);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line);transition:background .3s ease,border-color .3s ease,box-shadow .3s ease;}
  header.scrolled{background:rgba(5,11,28,0.92);box-shadow:0 8px 30px rgba(0,0,0,0.35);}
  .nav{display:flex;align-items:center;justify-content:space-between;height:64px;}
  @media(min-width:720px){.nav{height:70px;}}
  .logo{display:flex;align-items:center;gap:10px;}
  .logo-mark{width:30px;height:30px;flex:none;}
  .logo-mark svg{width:100%;height:100%;}
  .logo-text{font-weight:800;font-size:1.05rem;letter-spacing:-0.01em;}
  .logo-text span{color:var(--accent-bright);}
  .nav-links{display:none;gap:32px;align-items:center;}
  @media(min-width:900px){.nav-links{display:flex;}}
  .nav-links a{font-size:0.93rem;font-weight:600;color:var(--muted);transition:color .15s;position:relative;}
  .nav-links a::after{content:'';position:absolute;left:0;bottom:-4px;width:0;height:1.5px;background:var(--accent-bright);transition:width .25s ease;}
  .nav-links a:hover{color:var(--text);}
  .nav-links a:hover::after{width:100%;}
  .nav-cta{display:flex;align-items:center;gap:10px;}
  .nav-phone{font-size:0.85rem;font-weight:600;color:var(--muted);display:none;align-items:center;gap:6px;}
  @media(min-width:640px){.nav-phone{display:flex;}}
  .nav-cta .btn{padding:11px 18px;font-size:0.86rem;}
  @media(min-width:640px){.nav-cta .btn{padding:11px 20px;font-size:0.90rem;}}
  .burger{display:flex;flex-direction:column;gap:5px;background:none;border:none;padding:8px;cursor:pointer;margin-left:4px;}
  .burger span{width:22px;height:2px;background:var(--text);display:block;border-radius:2px;transition:transform .25s ease,opacity .25s ease;}
  .burger[aria-expanded="true"] span:nth-child(1){transform:translateY(7px) rotate(45deg);}
  .burger[aria-expanded="true"] span:nth-child(2){opacity:0;}
  .burger[aria-expanded="true"] span:nth-child(3){transform:translateY(-7px) rotate(-45deg);}
  @media(min-width:900px){.burger{display:none;}}
  .mobile-menu{display:grid;grid-template-rows:0fr;transition:grid-template-rows .3s ease;border-top:1px solid transparent;background:var(--ink);}
  .mobile-menu.open{grid-template-rows:1fr;border-top-color:var(--line);}
  .mobile-menu-inner{overflow:hidden;padding:0 16px;}
  .mobile-menu.open .mobile-menu-inner{padding:16px;}
  .mobile-menu a{display:block;font-size:1rem;padding:12px 8px;color:var(--text);font-weight:600;border-radius:8px;}
  .mobile-menu a:hover{background:var(--surface);}
  .mobile-menu .btn{margin-top:8px;width:100%;}

  /* ══════════ HERO ══════════ */
  .hero{padding-top:44px;padding-bottom:28px;overflow:hidden;}
  @media(min-width:720px){.hero{padding-top:56px;padding-bottom:32px;}}
  .hero-bg{position:absolute;inset:-10% -10% auto -10%;height:130%;z-index:0;pointer-events:none;}
  .hero-bg svg{width:100%;height:100%;}
  .hero-grid{display:grid;grid-template-columns:1fr;gap:36px;align-items:center;position:relative;z-index:1;}
  @media(min-width:900px){.hero-grid{grid-template-columns:1.1fr 0.9fr;gap:52px;}}
  .eyebrow{display:inline-flex;align-items:center;gap:8px;color:var(--accent-bright);font-size:0.83rem;font-weight:700;letter-spacing:0.06em;margin-bottom:14px;opacity:0;animation:fadeUp .7s ease .1s forwards;}
  .eyebrow .dot{width:7px;height:7px;border-radius:50%;background:var(--accent-bright);flex:none;box-shadow:0 0 0 0 rgba(0,217,255,0.6);animation:pulseDot 2s ease-in-out infinite;}
  @keyframes pulseDot{0%{box-shadow:0 0 0 0 rgba(0,217,255,0.55);}70%{box-shadow:0 0 0 8px rgba(0,217,255,0);}100%{box-shadow:0 0 0 0 rgba(0,217,255,0);}}
  .hero h1{font-size:clamp(2.2rem,6.4vw,3.9rem);font-weight:800;max-width:15ch;opacity:0;animation:fadeUp .8s ease .22s forwards;}
  .hero h1 .grad{background:linear-gradient(100deg,#fff 20%,var(--accent-bright) 60%,var(--accent-violet) 100%);-webkit-background-clip:text;background-clip:text;color:transparent;background-size:200% 100%;animation:sheen 6s ease-in-out infinite;}
  @keyframes sheen{0%,100%{background-position:0% 50%;}50%{background-position:100% 50%;}}
  .hero p.lede{margin-top:14px;font-size:1rem;color:var(--muted);max-width:44ch;line-height:1.7;opacity:0;animation:fadeUp .8s ease .34s forwards;}
  @media(min-width:720px){.hero p.lede{font-size:1.05rem;}}
  .hero-actions{margin-top:26px;display:flex;gap:10px;flex-wrap:wrap;opacity:0;animation:fadeUp .8s ease .46s forwards;}
  .hero-actions .btn{flex:1;min-width:140px;}
  @media(min-width:400px){.hero-actions .btn{flex:none;}}
  @keyframes fadeUp{from{opacity:0;transform:translateY(14px);}to{opacity:1;transform:translateY(0);}}

  .trust-row{margin-top:38px;display:flex;gap:0;flex-wrap:wrap;opacity:0;animation:fadeUp .8s ease .58s forwards;border-top:1px solid var(--line);padding-top:20px;}
  .trust-item{flex:1;min-width:110px;padding-right:20px;}
  .trust-item .num{font-size:1.5rem;font-weight:800;color:var(--accent-bright);font-variant-numeric:tabular-nums;}
  @media(min-width:480px){.trust-item .num{font-size:1.65rem;}}
  .trust-item .label{font-size:0.78rem;color:var(--muted);margin-top:2px;}

  /* ── Connection Visual ── */
  .connect-visual{
    position:relative;height:320px;border-radius:20px;
    background:radial-gradient(ellipse at 75% 45%, rgba(0,120,220,0.16), transparent 60%), linear-gradient(160deg,var(--surface),var(--surface-2));
    border:1px solid var(--line);overflow:hidden;
    opacity:0;animation:fadeIn 1s ease .5s forwards;
  }
  @keyframes fadeIn{from{opacity:0;transform:scale(0.97);}to{opacity:1;transform:scale(1);}}
  @media(min-width:480px){.connect-visual{height:360px;}}
  @media(min-width:900px){.connect-visual{height:390px;border-radius:22px;}}
  .connect-visual svg{width:100%;height:100%;display:block;}
  .speed-badge{position:absolute;top:14px;right:14px;background:rgba(5,15,35,0.65);backdrop-filter:blur(6px);border:1px solid rgba(0,217,255,0.25);border-radius:10px;padding:8px 12px;text-align:right;z-index:2;}
  .speed-badge .lbl{font-size:0.6rem;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;}
  .speed-badge .val{font-family:'JetBrains Mono',monospace;font-weight:700;color:var(--accent-bright);font-size:1.1rem;line-height:1.3;}

  .section-head{margin-bottom:36px;}
  @media(min-width:720px){.section-head{max-width:620px;margin-bottom:44px;}}
  .section-head .eyebrow{opacity:1;animation:none;}
  .section-head h2{font-size:clamp(1.65rem,4vw,2.4rem);}
  .section-head p{margin-top:10px;color:var(--muted);font-size:0.97rem;}

  .bento{display:grid;grid-template-columns:1fr;gap:12px;}
  @media(min-width:560px){.bento{grid-template-columns:1fr 1fr;}}
  @media(min-width:900px){.bento{grid-template-columns:1.3fr 1fr 1fr;grid-template-rows:auto auto;gap:16px;}}
  .bento .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;position:relative;overflow:hidden;transition:transform .35s cubic-bezier(.2,.8,.2,1),border-color .3s ease,box-shadow .35s ease;}
  .bento .card::before{content:'';position:absolute;inset:0;background:radial-gradient(220px circle at var(--mx,50%) var(--my,50%), rgba(0,217,255,0.10), transparent 60%);opacity:0;transition:opacity .3s ease;pointer-events:none;}
  .bento .card:hover::before{opacity:1;}
  .bento .card:hover{transform:translateY(-4px);border-color:rgba(0,191,255,0.35);box-shadow:0 16px 40px rgba(0,20,60,0.4);}
  @media(min-width:720px){.bento .card{padding:26px;}}
  .bento .card.big{display:flex;flex-direction:column;justify-content:space-between;}
  @media(min-width:900px){.bento .card.big{grid-row:span 2;}}
  @media(min-width:560px) and (max-width:899px){.bento .card.big{grid-column:span 2;}}
  .bento .card h3{font-size:1.05rem;font-weight:700;margin-bottom:8px;}
  .bento .card p{color:var(--muted);font-size:0.92rem;}
  .card-icon{width:38px;height:38px;border-radius:10px;background:var(--accent-dim);display:flex;align-items:center;justify-content:center;margin-bottom:12px;font-size:1.1rem;transition:transform .35s cubic-bezier(.2,.8,.2,1),background .3s ease;}
  .bento .card:hover .card-icon{transform:scale(1.12) rotate(-6deg);background:rgba(0,217,255,0.22);}
  .big-stat{font-size:2.4rem;font-weight:800;color:var(--accent-bright);margin:14px 0 4px;font-variant-numeric:tabular-nums;}
  @media(min-width:720px){.big-stat{font-size:2.8rem;}}

  .plans-toggle{display:inline-flex;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:4px;margin-bottom:28px;position:relative;}
  .plans-toggle button{position:relative;z-index:1;border:none;background:none;color:var(--muted);padding:10px 18px;border-radius:999px;font-size:0.86rem;font-weight:700;cursor:pointer;min-height:44px;transition:color .3s ease;}
  @media(min-width:480px){.plans-toggle button{padding:9px 20px;font-size:0.88rem;}}
  .plans-toggle button.active{color:#fff;}
  .toggle-pill{position:absolute;top:4px;bottom:4px;left:4px;border-radius:999px;background:linear-gradient(135deg,var(--accent),#0066CC);transition:transform .35s cubic-bezier(.2,.8,.2,1),width .35s cubic-bezier(.2,.8,.2,1);box-shadow:0 4px 14px rgba(0,144,255,0.4);}

  .plans-grid{display:grid;grid-template-columns:1fr;gap:14px;}
  @media(min-width:560px){.plans-grid{grid-template-columns:1fr 1fr;}}
  @media(min-width:900px){.plans-grid{grid-template-columns:repeat(3,1fr);gap:18px;}}
  .plan{background:var(--surface);border:1.5px solid var(--line);border-radius:var(--radius);padding:24px;position:relative;transition:transform .35s cubic-bezier(.2,.8,.2,1),border-color .3s ease;}
  .plan:hover{transform:translateY(-4px);}
  @media(min-width:720px){.plan{padding:28px;}}
  .plan.featured{border-color:var(--accent);background:linear-gradient(160deg,rgba(0,144,255,0.11),var(--surface) 45%);box-shadow:0 0 0 1px rgba(0,144,255,0.15),0 20px 50px rgba(0,80,200,0.22);}
  .plan.featured::before{content:'';position:absolute;inset:-1px;border-radius:inherit;padding:1px;background:linear-gradient(135deg,var(--accent-bright),transparent 40%,transparent 60%,var(--accent-violet));-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;opacity:0.6;pointer-events:none;}
  @media(max-width:559px){.plan.featured{order:-1;}}
  .plan-tag{position:absolute;top:-12px;left:20px;background:linear-gradient(135deg,var(--accent),#0066CC);color:#fff;font-size:0.72rem;font-weight:700;padding:3px 11px;border-radius:999px;box-shadow:0 4px 12px rgba(0,144,255,0.4);}
  .plan-name{font-size:0.88rem;color:var(--muted);font-weight:700;}
  .plan-speed{font-size:2.3rem;font-weight:800;margin:8px 0 0;font-variant-numeric:tabular-nums;}
  @media(min-width:720px){.plan-speed{font-size:2.5rem;}}
  .plan-speed span{font-size:0.95rem;color:var(--muted);font-weight:600;}
  .plan-price{margin-top:4px;color:var(--muted);font-size:0.93rem;}
  .plan-price b{color:var(--text);font-size:1.2rem;display:inline-block;transition:opacity .18s ease;}
  .plan-price b.swap{opacity:0;}
  .plan-features{margin:18px 0 22px;display:flex;flex-direction:column;gap:9px;}
  .plan-features li{font-size:0.87rem;color:var(--muted);display:flex;gap:10px;align-items:flex-start;}
  .plan-features li::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--accent-bright);margin-top:7px;flex:none;}
  .plan .btn{width:100%;justify-content:center;}

  .stats-band{background:var(--surface);border-top:1px solid var(--line);border-bottom:1px solid var(--line);position:relative;overflow:hidden;}
  .stats-band::before{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(0,217,255,0.06),transparent);background-size:200% 100%;animation:sweep 5s linear infinite;pointer-events:none;}
  @keyframes sweep{from{background-position:200% 0;}to{background-position:-200% 0;}}
  .stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;text-align:center;max-width:520px;margin:0 auto;position:relative;}
  .stats-grid .num{font-size:1.85rem;font-weight:800;color:var(--accent-bright);font-variant-numeric:tabular-nums;}
  @media(min-width:480px){.stats-grid .num{font-size:2.1rem;}}
  .stats-grid .label{color:var(--muted);font-size:0.84rem;margin-top:4px;}

  footer{border-top:1px solid var(--line);padding:44px 0 24px;}
  @media(min-width:720px){footer{padding:52px 0 26px;}}
  .footer-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding-bottom:28px;}
  @media(min-width:720px){.footer-grid{grid-template-columns:1.4fr 1fr 1fr 1fr;gap:28px;padding-bottom:32px;}}
  .footer-brand{grid-column:1/-1;}
  @media(min-width:720px){.footer-brand{grid-column:auto;}}
  .footer-grid h4{font-size:0.8rem;color:var(--muted);font-weight:700;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.06em;}
  .footer-grid ul{display:flex;flex-direction:column;gap:9px;}
  .footer-grid a{font-size:0.9rem;color:var(--text);transition:color .2s ease;}
  .footer-grid a:hover{color:var(--accent-bright);}
  .footer-bottom{border-top:1px solid var(--line);padding-top:20px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:0.8rem;color:var(--muted);}

  .reveal{opacity:0;transform:translateY(16px);transition:opacity .6s cubic-bezier(.2,.8,.2,1),transform .6s cubic-bezier(.2,.8,.2,1);}
  .reveal.in{opacity:1;transform:none;}
  .reveal-stagger.in > *{opacity:1;transform:none;}
  .reveal-stagger > *{opacity:0;transform:translateY(16px);transition:opacity .6s cubic-bezier(.2,.8,.2,1),transform .6s cubic-bezier(.2,.8,.2,1);}
  .reveal-stagger > *:nth-child(1){transition-delay:.05s;}
  .reveal-stagger > *:nth-child(2){transition-delay:.12s;}
  .reveal-stagger > *:nth-child(3){transition-delay:.19s;}
  .reveal-stagger > *:nth-child(4){transition-delay:.26s;}

  /* ══════════ SIGN UP / APPLICATION ══════════ */
  .apply-hero{
    position:relative;border-radius:20px;overflow:hidden;padding:54px 28px;text-align:center;
    background:linear-gradient(160deg,#0B1B3E,#081431 60%,#050B1C);
    border:1px solid var(--line);isolation:isolate;
  }
  @media(min-width:720px){.apply-hero{padding:74px 40px;}}
  .apply-hero::before{
    content:'';position:absolute;inset:0;z-index:-1;
    background:radial-gradient(ellipse 60% 90% at 20% 0%, rgba(0,144,255,0.22), transparent 60%),
               radial-gradient(ellipse 50% 70% at 100% 100%, rgba(123,97,255,0.16), transparent 60%);
  }
  .apply-hero .streak-svg{position:absolute;inset:0;z-index:-1;width:100%;height:100%;opacity:0.5;}
  .apply-hero .eyebrow{justify-content:center;opacity:1;animation:none;}
  .apply-hero h2{font-size:clamp(1.9rem,5vw,3rem);}
  .apply-hero p{color:var(--muted);max-width:46ch;margin:12px auto 0;font-size:1rem;}

  .apply-features{display:grid;grid-template-columns:1fr;gap:14px;margin-top:32px;}
  @media(min-width:720px){.apply-features{grid-template-columns:repeat(3,1fr);gap:18px;}}
  .apply-features .card{text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;}
  @media(min-width:720px){.apply-features .card{padding:26px;}}
  .apply-features .card h3{font-size:1.05rem;font-weight:700;margin-bottom:8px;}
  .apply-features .card p{color:var(--muted);font-size:0.92rem;}

  .apply-form-wrap{
    margin-top:40px;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:26px 20px;position:relative;overflow:hidden;
  }
  @media(min-width:720px){.apply-form-wrap{padding:38px 40px;}}
  .apply-form-wrap::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--accent-bright),var(--accent-violet),transparent);}

  .field{display:flex;flex-direction:column;gap:6px;margin-bottom:16px;}
  .field label{font-size:0.82rem;font-weight:700;color:var(--muted);}
  .field label .req{color:var(--accent-bright);}
  .field-row{display:grid;grid-template-columns:1fr;gap:16px;}
  @media(min-width:640px){.field-row.two{grid-template-columns:1fr 1fr;}}

  .input, select.input, textarea.input{
    width:100%;background:var(--surface-2);border:1.5px solid var(--line);border-radius:10px;
    padding:13px 16px;color:var(--text);font-size:0.94rem;font-family:inherit;
    transition:border-color .2s ease,box-shadow .2s ease,background .2s ease;
  }
  .input::placeholder{color:rgba(122,139,173,0.65);}
  .input:focus, select.input:focus, textarea.input:focus{
    outline:none;border-color:var(--accent-bright);box-shadow:0 0 0 3px rgba(0,217,255,0.15);background:rgba(20,38,80,0.85);
  }
  textarea.input{resize:vertical;min-height:96px;}
  select.input{appearance:none;-webkit-appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%237A8BAD' stroke-width='1.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 16px center;padding-right:38px;cursor:pointer;
  }
  select.input:disabled{opacity:0.5;cursor:not-allowed;}

  .plan-options{display:flex;flex-direction:column;gap:10px;}
  @media(min-width:720px){.plan-options{flex-direction:row;flex-wrap:wrap;}}
  .plan-option{flex:1;min-width:220px;}
  .plan-option input{position:absolute;opacity:0;width:0;height:0;}
  .plan-option .plan-option-card{
    display:flex;align-items:center;gap:12px;border:1.5px solid var(--line);border-radius:12px;
    padding:14px 16px;cursor:pointer;transition:border-color .2s ease,background .2s ease,transform .2s ease;
    background:var(--surface-2);
  }
  .plan-option .plan-option-card:hover{border-color:rgba(0,217,255,0.35);}
  .plan-option input:checked + .plan-option-card{
    border-color:var(--accent-bright);background:rgba(0,144,255,0.1);box-shadow:0 0 0 3px rgba(0,217,255,0.12);
  }
  .plan-radio-dot{width:18px;height:18px;border-radius:50%;border:2px solid rgba(122,139,173,0.5);flex:none;position:relative;transition:border-color .2s ease;}
  .plan-option input:checked + .plan-option-card .plan-radio-dot{border-color:var(--accent-bright);}
  .plan-radio-dot::after{content:'';position:absolute;inset:3px;border-radius:50%;background:var(--accent-bright);transform:scale(0);transition:transform .2s cubic-bezier(.2,.8,.2,1);}
  .plan-option input:checked + .plan-option-card .plan-radio-dot::after{transform:scale(1);}
  .plan-option-text .name{font-weight:700;font-size:0.9rem;}
  .plan-option-text .price{font-size:0.8rem;color:var(--muted);margin-top:1px;}

  .file-field{display:flex;align-items:center;gap:0;border:1.5px solid var(--line);border-radius:10px;overflow:hidden;background:var(--surface-2);}
  .file-field label{
    flex:none;background:rgba(255,255,255,0.06);color:var(--text);font-weight:700;font-size:0.85rem;
    padding:13px 18px;cursor:pointer;transition:background .2s ease;border-right:1.5px solid var(--line);
  }
  .file-field label:hover{background:rgba(255,255,255,0.1);}
  .file-field span{padding:0 16px;font-size:0.88rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .file-field input[type="file"]{display:none;}

  .apply-submit-row{display:flex;justify-content:center;margin-top:8px;}
  .apply-submit-row .btn{min-width:220px;}

  .apply-success{
    display:none;align-items:center;gap:12px;background:rgba(0,200,120,0.1);border:1px solid rgba(0,220,140,0.35);
    color:#5FE8B0;border-radius:12px;padding:14px 18px;font-size:0.9rem;font-weight:600;margin-top:18px;
  }
  .apply-success.show{display:flex;animation:fadeUp .4s ease;}

  @media(prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:0.001ms !important;animation-iteration-count:1 !important;transition-duration:0.001ms !important;}
    .reveal,.hero *{opacity:1 !important;transform:none !important;}
    html{scroll-behavior:auto;}
  }
</style>
</head>
<body>

<header id="siteHeader">
  <div class="wrap nav">
    <a href="#" class="logo">
      <span class="logo-mark">
        <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="logoGrad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#00D9FF"/>
              <stop offset="100%" stop-color="#0066CC"/>
            </linearGradient>
          </defs>
          <circle cx="16" cy="16" r="15" fill="none" stroke="url(#logoGrad)" stroke-width="2" opacity="0.35"/>
          <path d="M16 6 L16 14 M8 11 L14 15 M24 11 L18 15 M8 21 L14 17 M24 21 L18 17 M16 26 L16 18" stroke="url(#logoGrad)" stroke-width="1.6" stroke-linecap="round"/>
          <circle cx="16" cy="16" r="4" fill="url(#logoGrad)"/>
        </svg>
      </span>
      <span class="logo-text">Mikro<span>Net</span></span>
    </a>
    <nav class="nav-links">
      <a href="#services">Services</a>
      <a href="#plans">Plans</a>
      <a href="#apply">Sign Up</a>
      <a href="#contact">Contact</a>
    </nav>
    <div class="nav-cta">
      <span class="nav-phone">📞 0908-325-5591</span>
      <a href="{{ url_for('pay_redirect') }}" class="btn btn-ghost">Pay Bill</a>
      <a href="#apply" class="btn btn-primary">Sign up in minutes</a>
      <button class="burger" id="burgerBtn" aria-label="Open menu" aria-expanded="false">
        <span></span><span></span><span></span>
      </button>
    </div>
  </div>
  <div class="mobile-menu" id="mobileMenu">
    <div class="mobile-menu-inner">
      <a href="#services">Services</a>
      <a href="#plans">Plans</a>
      <a href="#apply">Sign Up</a>
      <a href="#contact">Contact</a>
      <a href="{{ url_for('pay_redirect') }}" class="btn btn-ghost">Pay Bill</a>
      <a href="#apply" class="btn btn-primary">Sign up in minutes</a>
    </div>
  </div>
</header>

<section class="hero">
  <div class="hero-bg" aria-hidden="true">
    <svg viewBox="0 0 800 700" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="heroGlow1" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#0090FF" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="#0090FF" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="heroGlow2" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#7B61FF" stop-opacity="0.18"/>
          <stop offset="100%" stop-color="#7B61FF" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle cx="560" cy="260" r="260" fill="url(#heroGlow1)">
        <animate attributeName="r" values="240;280;240" dur="8s" repeatCount="indefinite"/>
      </circle>
      <circle cx="180" cy="480" r="220" fill="url(#heroGlow2)">
        <animate attributeName="r" values="200;240;200" dur="9s" repeatCount="indefinite"/>
      </circle>
      <g stroke="#00D9FF" stroke-width="1" fill="none" opacity="0.16">
        <path d="M -50 120 C 200 60, 400 180, 850 90"/>
        <path d="M -50 300 C 250 340, 450 220, 850 300"/>
        <path d="M -50 520 C 220 460, 480 560, 850 480"/>
      </g>
      <g fill="#00D9FF">
        <circle r="2.2" opacity="0.9">
          <animateMotion dur="7s" repeatCount="indefinite" path="M -50 120 C 200 60, 400 180, 850 90"/>
        </circle>
        <circle r="1.8" opacity="0.75">
          <animateMotion dur="9s" begin="1.5s" repeatCount="indefinite" path="M -50 300 C 250 340, 450 220, 850 300"/>
        </circle>
        <circle r="2" opacity="0.8">
          <animateMotion dur="8s" begin="0.8s" repeatCount="indefinite" path="M -50 520 C 220 460, 480 560, 850 480"/>
        </circle>
      </g>
    </svg>
  </div>

  <div class="wrap hero-grid">
    <div>
      <p class="eyebrow"><span class="dot"></span>Fiber internet, live in your area</p>
      <h1>Stay <span class="grad">connected</span> to what matters.</h1>
      <p class="lede">Work, study, stream, game — MikroNet's pure fiber backbone keeps every device in your home fast and reliable, day and night.</p>
      <div class="hero-actions">
        <a href="#plans" class="btn btn-primary">View plans &amp; pricing</a>
        <a href="#contact" class="btn btn-ghost">Talk to us</a>
      </div>
      <div class="trust-row">
        <div class="trust-item"><div class="num" data-count="99.9" data-suffix="%">0%</div><div class="label">Network uptime</div></div>
        <div class="trust-item"><div class="num" data-count="4" data-prefix="1–" data-suffix=" ms">1–0 ms</div><div class="label">Typical latency</div></div>
        <div class="trust-item"><div class="num" data-count="24" data-suffix="/7">0/7</div><div class="label">Local support</div></div>
      </div>
    </div>

    <div class="connect-visual" aria-hidden="true">
      <div class="speed-badge">
        <div class="lbl">Live throughput</div>
        <div class="val"><span id="liveSpeed">—</span> Mbps</div>
      </div>
      <svg viewBox="0 0 400 370" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <filter id="glow">
            <feGaussianBlur stdDeviation="3" result="blur"/>
            <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
          </filter>
          <filter id="glow-sm">
            <feGaussianBlur stdDeviation="1.5" result="blur"/>
            <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
          </filter>
          <linearGradient id="lg1" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#00D9FF" stop-opacity="0.05"/>
            <stop offset="50%" stop-color="#00D9FF" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="#00D9FF" stop-opacity="0.05"/>
          </linearGradient>
        </defs>
        <g opacity="0.08">
          <circle cx="50" cy="50" r="1" fill="#00D9FF"/><circle cx="150" cy="50" r="1" fill="#00D9FF"/>
          <circle cx="250" cy="50" r="1" fill="#00D9FF"/><circle cx="350" cy="50" r="1" fill="#00D9FF"/>
          <circle cx="50" cy="150" r="1" fill="#00D9FF"/><circle cx="150" cy="150" r="1" fill="#00D9FF"/>
          <circle cx="250" cy="150" r="1" fill="#00D9FF"/><circle cx="350" cy="150" r="1" fill="#00D9FF"/>
          <circle cx="50" cy="250" r="1" fill="#00D9FF"/><circle cx="150" cy="250" r="1" fill="#00D9FF"/>
          <circle cx="250" cy="250" r="1" fill="#00D9FF"/><circle cx="350" cy="250" r="1" fill="#00D9FF"/>
          <circle cx="50" cy="350" r="1" fill="#00D9FF"/><circle cx="150" cy="350" r="1" fill="#00D9FF"/>
          <circle cx="250" cy="350" r="1" fill="#00D9FF"/>
        </g>
        <circle cx="300" cy="185" r="28" fill="none" stroke="rgba(0,217,255,0.6)" stroke-width="1">
          <animate attributeName="r" values="28;78" dur="2.4s" repeatCount="indefinite" calcMode="ease-in"/>
          <animate attributeName="opacity" values="0.6;0" dur="2.4s" repeatCount="indefinite"/>
        </circle>
        <circle cx="300" cy="185" r="28" fill="none" stroke="rgba(0,217,255,0.4)" stroke-width="1">
          <animate attributeName="r" values="28;78" dur="2.4s" begin="0.8s" repeatCount="indefinite" calcMode="ease-in"/>
          <animate attributeName="opacity" values="0.4;0" dur="2.4s" begin="0.8s" repeatCount="indefinite"/>
        </circle>
        <circle cx="300" cy="185" r="28" fill="none" stroke="rgba(0,217,255,0.2)" stroke-width="1">
          <animate attributeName="r" values="28;78" dur="2.4s" begin="1.6s" repeatCount="indefinite" calcMode="ease-in"/>
          <animate attributeName="opacity" values="0.3;0" dur="2.4s" begin="1.6s" repeatCount="indefinite"/>
        </circle>
        <circle cx="300" cy="185" r="26" fill="rgba(0,80,180,0.22)" stroke="rgba(0,217,255,0.5)" stroke-width="1.5" filter="url(#glow)"/>
        <circle cx="300" cy="185" r="14" fill="rgba(0,120,220,0.4)" stroke="rgba(0,217,255,0.65)" stroke-width="1"/>
        <circle cx="300" cy="185" r="7" fill="#00D9FF" filter="url(#glow)">
          <animate attributeName="opacity" values="0.8;1;0.8" dur="2s" repeatCount="indefinite"/>
        </circle>
        <text x="300" y="222" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="8" font-weight="700" fill="rgba(0,217,255,0.75)" letter-spacing="0.08em">MIKRONET</text>
        <path d="M 72 55 C 160 55, 225 110, 274 165" fill="none" stroke="rgba(0,217,255,0.12)" stroke-width="1.4"/>
        <path d="M 72 55 C 160 55, 225 110, 274 165" fill="none" stroke="url(#lg1)" stroke-width="1.4"/>
        <circle cx="72" cy="55" r="22" fill="rgba(0,80,160,0.15)" stroke="rgba(0,217,255,0.3)" stroke-width="1.2"/>
        <text x="72" y="59" text-anchor="middle" dominant-baseline="middle" font-size="18">🏠</text>
        <text x="72" y="83" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="7.5" fill="rgba(200,220,255,0.55)" font-weight="600">Home</text>
        <circle r="3.5" fill="#00D9FF" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.8s" begin="0s" repeatCount="indefinite" path="M 72 55 C 160 55, 225 110, 274 165"/>
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes="0;0.05;0.15;0.7;0.9;1" dur="2.8s" begin="0s" repeatCount="indefinite"/>
        </circle>
        <circle r="2.5" fill="rgba(0,217,255,0.6)" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.1s" begin="1.2s" repeatCount="indefinite" path="M 274 165 C 225 110, 160 55, 72 55"/>
          <animate attributeName="opacity" values="0;0;0.8;0.8;0" keyTimes="0;0.05;0.15;0.85;1" dur="2.1s" begin="1.2s" repeatCount="indefinite"/>
        </circle>
        <path d="M 72 120 C 160 120, 230 145, 274 175" fill="none" stroke="rgba(0,217,255,0.12)" stroke-width="1.4"/>
        <path d="M 72 120 C 160 120, 230 145, 274 175" fill="none" stroke="url(#lg1)" stroke-width="1.4"/>
        <circle cx="72" cy="120" r="22" fill="rgba(0,80,160,0.15)" stroke="rgba(0,217,255,0.3)" stroke-width="1.2"/>
        <text x="72" y="124" text-anchor="middle" dominant-baseline="middle" font-size="18">💻</text>
        <text x="72" y="148" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="7.5" fill="rgba(200,220,255,0.55)" font-weight="600">Work</text>
        <circle r="3.5" fill="#00D9FF" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.2s" begin="0.6s" repeatCount="indefinite" path="M 72 120 C 160 120, 230 145, 274 175"/>
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes="0;0.05;0.15;0.7;0.9;1" dur="2.2s" begin="0.6s" repeatCount="indefinite"/>
        </circle>
        <circle r="2.5" fill="rgba(0,217,255,0.6)" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="1.9s" begin="0s" repeatCount="indefinite" path="M 274 175 C 230 145, 160 120, 72 120"/>
          <animate attributeName="opacity" values="0;0;0.8;0.8;0" keyTimes="0;0.05;0.15;0.85;1" dur="1.9s" begin="0s" repeatCount="indefinite"/>
        </circle>
        <path d="M 72 185 C 160 185, 220 185, 274 185" fill="none" stroke="rgba(0,217,255,0.12)" stroke-width="1.4"/>
        <path d="M 72 185 C 160 185, 220 185, 274 185" fill="none" stroke="url(#lg1)" stroke-width="1.4"/>
        <circle cx="72" cy="185" r="22" fill="rgba(0,80,160,0.15)" stroke="rgba(0,217,255,0.3)" stroke-width="1.2"/>
        <text x="72" y="189" text-anchor="middle" dominant-baseline="middle" font-size="18">🎮</text>
        <text x="72" y="213" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="7.5" fill="rgba(200,220,255,0.55)" font-weight="600">Game</text>
        <circle r="3.5" fill="#00D9FF" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="1.8s" begin="1.0s" repeatCount="indefinite" path="M 72 185 C 160 185, 220 185, 274 185"/>
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes="0;0.05;0.15;0.7;0.9;1" dur="1.8s" begin="1.0s" repeatCount="indefinite"/>
        </circle>
        <circle r="2.5" fill="rgba(0,217,255,0.6)" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="1.6s" begin="0.4s" repeatCount="indefinite" path="M 274 185 C 220 185, 160 185, 72 185"/>
          <animate attributeName="opacity" values="0;0;0.8;0.8;0" keyTimes="0;0.05;0.15;0.85;1" dur="1.6s" begin="0.4s" repeatCount="indefinite"/>
        </circle>
        <path d="M 72 250 C 160 250, 230 225, 274 195" fill="none" stroke="rgba(0,217,255,0.12)" stroke-width="1.4"/>
        <path d="M 72 250 C 160 250, 230 225, 274 195" fill="none" stroke="url(#lg1)" stroke-width="1.4"/>
        <circle cx="72" cy="250" r="22" fill="rgba(0,80,160,0.15)" stroke="rgba(0,217,255,0.3)" stroke-width="1.2"/>
        <text x="72" y="254" text-anchor="middle" dominant-baseline="middle" font-size="18">📚</text>
        <text x="72" y="278" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="7.5" fill="rgba(200,220,255,0.55)" font-weight="600">Study</text>
        <circle r="3.5" fill="#00D9FF" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.6s" begin="1.4s" repeatCount="indefinite" path="M 72 250 C 160 250, 230 225, 274 195"/>
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes="0;0.05;0.15;0.7;0.9;1" dur="2.6s" begin="1.4s" repeatCount="indefinite"/>
        </circle>
        <circle r="2.5" fill="rgba(0,217,255,0.6)" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.0s" begin="0.7s" repeatCount="indefinite" path="M 274 195 C 230 225, 160 250, 72 250"/>
          <animate attributeName="opacity" values="0;0;0.8;0.8;0" keyTimes="0;0.05;0.15;0.85;1" dur="2.0s" begin="0.7s" repeatCount="indefinite"/>
        </circle>
        <path d="M 72 315 C 160 315, 225 260, 274 205" fill="none" stroke="rgba(0,217,255,0.12)" stroke-width="1.4"/>
        <path d="M 72 315 C 160 315, 225 260, 274 205" fill="none" stroke="url(#lg1)" stroke-width="1.4"/>
        <circle cx="72" cy="315" r="22" fill="rgba(0,80,160,0.15)" stroke="rgba(0,217,255,0.3)" stroke-width="1.2"/>
        <text x="72" y="319" text-anchor="middle" dominant-baseline="middle" font-size="18">❤️</text>
        <text x="72" y="343" text-anchor="middle" font-family="Plus Jakarta Sans,sans-serif" font-size="7.5" fill="rgba(200,220,255,0.55)" font-weight="600">Family</text>
        <circle r="3.5" fill="#00D9FF" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="3.0s" begin="0.3s" repeatCount="indefinite" path="M 72 315 C 160 315, 225 260, 274 205"/>
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes="0;0.05;0.15;0.7;0.9;1" dur="3.0s" begin="0.3s" repeatCount="indefinite"/>
        </circle>
        <circle r="2.5" fill="rgba(0,217,255,0.6)" filter="url(#glow-sm)" opacity="0">
          <animateMotion dur="2.3s" begin="1.8s" repeatCount="indefinite" path="M 274 205 C 225 260, 160 315, 72 315"/>
          <animate attributeName="opacity" values="0;0;0.8;0.8;0" keyTimes="0;0.05;0.15;0.85;1" dur="2.3s" begin="1.8s" repeatCount="indefinite"/>
        </circle>
      </svg>
    </div>
  </div>
</section>

<section id="services">
  <div class="wrap">
    <div class="section-head reveal">
      <p class="eyebrow"><span class="dot"></span>What we offer</p>
      <h2>Pure fiber, built for real life</h2>
      <p>From your home to your business, one reliable fiber network — with people-first support.</p>
    </div>
    <div class="bento reveal-stagger reveal">
      <div class="card big" data-tilt>
        <div>
          <div class="card-icon">⌁</div>
          <h3>Residential Fiber</h3>
          <p>Symmetric upload and download built for streaming, gaming, remote work, and online classes — without the evening slowdown.</p>
        </div>
        <div>
          <div class="big-stat" data-count="1" data-prefix="Up to " data-suffix=" Gbps">Up to 0 Gbps</div>
          <p>On all plans. No data caps, ever.</p>
        </div>
      </div>
      <div class="card" data-tilt>
        <div class="card-icon">▣</div>
        <h3>Business Plans</h3>
        <p>Dedicated bandwidth with SLA-backed uptime for offices, stores, and teams that cannot afford a dropped connection.</p>
      </div>
      <div class="card" data-tilt>
        <div class="card-icon">◈</div>
        <h3>IP-VPN &amp; Multi-Site</h3>
        <p>Securely link branches and remote teams over a private, managed fiber network.</p>
      </div>
      <div class="card" data-tilt>
        <div class="card-icon">⌂</div>
        <h3>Managed IT</h3>
        <p>Structured cabling, network monitoring, and on-site technicians for growing partners and customers.</p>
      </div>
    </div>
  </div>
</section>

<section id="plans">
  <div class="wrap">
    <div class="section-head reveal">
      <p class="eyebrow"><span class="dot"></span>Pricing</p>
      <h2>Pick a plan, upgrade anytime</h2>
      <p>Every plan ships with free standard installation and a router on us.</p>
    </div>
    <div class="plans-toggle reveal" role="tablist" aria-label="Billing period">
      <div class="toggle-pill" id="togglePill"></div>
      <button class="active" id="monthlyBtn" role="tab" aria-selected="true">Monthly</button>
      <button id="annualBtn" role="tab" aria-selected="false">Annual · save 10%</button>
    </div>
    <div class="plans-grid reveal">
      <div class="plan">
        <div class="plan-name">Home Basic</div>
        <div class="plan-speed">25<span> Mbps</span></div>
        <div class="plan-price"><b class="price" data-monthly="₱500" data-annual="₱450">₱500</b> / month</div>
        <ul class="plan-features">
          <li>Good for browsing &amp; streaming</li>
          <li>Free router included</li>
          <li>No data cap</li>
          <li>Standard installation</li>
          <li>Email support</li>
        </ul>
        <a href="#apply" class="btn btn-ghost" data-plan="basic">Choose Basic</a>
      </div>
      <div class="plan featured">
        <span class="plan-tag">Most popular</span>
        <div class="plan-name">Home Standard</div>
        <div class="plan-speed">50<span> Mbps</span></div>
        <div class="plan-price"><b class="price" data-monthly="₱800" data-annual="₱720">₱800</b> / month</div>
        <ul class="plan-features">
          <li>Great for work &amp; online classes</li>
          <li>Free Wi-Fi router</li>
          <li>No data cap</li>
          <li>Standard installation</li>
          <li>Priority support line</li>
        </ul>
        <a href="#apply" class="btn btn-primary" data-plan="standard">Choose Standard</a>
      </div>
      <div class="plan">
        <div class="plan-name">Home Pro</div>
        <div class="plan-speed">100<span> Mbps</span></div>
        <div class="plan-price"><b class="price" data-monthly="₱1,000" data-annual="₱900">₱1,000</b> / month</div>
        <ul class="plan-features">
          <li>Best for multiple devices &amp; gaming</li>
          <li>Free mesh Wi-Fi router</li>
          <li>No data cap</li>
          <li>Standard installation</li>
          <li>Priority support line</li>
        </ul>
        <a href="#apply" class="btn btn-ghost" data-plan="pro">Choose Pro</a>
      </div>
    </div>
  </div>
</section>

<section id="apply">
  <div class="wrap">
    <div class="apply-hero reveal">
      <svg class="streak-svg" viewBox="0 0 800 300" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <g stroke="#00D9FF" stroke-width="1" fill="none" opacity="0.18">
          <path d="M -50 60 C 200 20, 400 100, 850 40"/>
          <path d="M -50 220 C 250 260, 480 180, 850 240"/>
        </g>
        <g fill="#00D9FF">
          <circle r="2" opacity="0.85"><animateMotion dur="6s" repeatCount="indefinite" path="M -50 60 C 200 20, 400 100, 850 40"/></circle>
          <circle r="1.8" opacity="0.7"><animateMotion dur="7.5s" begin="1s" repeatCount="indefinite" path="M -50 220 C 250 260, 480 180, 850 240"/></circle>
        </g>
      </svg>
      <p class="eyebrow"><span class="dot"></span>Bring MikroNet home</p>
      <h2>Sign up in minutes</h2>
      <p>Fill out the form and our team will confirm coverage in your area and schedule installation — usually within 2–5 business days.</p>
    </div>

    <div class="apply-features">
      <div class="card">
        <div class="card-icon">🧾</div>
        <h3>One bill, no surprises</h3>
        <p>The price on the plan is the price on the bill — no overages, no throttling, no hidden fees after the first month.</p>
      </div>
      <div class="card">
        <div class="card-icon">🚪</div>
        <h3>Leave whenever you want</h3>
        <p>No lock-in. If you ever need to cancel, just message our support team — no penalties, no runaround.</p>
      </div>
      <div class="card">
        <div class="card-icon">🎮</div>
        <h3>Tuned for low ping</h3>
        <p>Our network is routed for consistency, not just peak speed. It holds up during busy evening hours when it matters most.</p>
      </div>
    </div>

    <form class="apply-form-wrap reveal" id="applyForm" action="{{ url_for('apply_submit') }}" method="POST" enctype="multipart/form-data">
      <div class="field">
        <label for="fullName">Full Name <span class="req">*</span></label>
        <input class="input" type="text" id="fullName" name="fullName" placeholder="Juan Dela Cruz" required>
      </div>

      <div class="field">
        <label for="street">House No. / Street <span class="req">*</span></label>
        <input class="input" type="text" id="street" name="street" placeholder="e.g. Blk 3 Lot 12, Purok 3" required>
      </div>

      <div class="field-row two">
        <div class="field">
          <label for="municipality">Municipality <span class="req">*</span></label>
          <select class="input" id="municipality" name="municipality" required>
            <option value="" disabled selected>Select municipality</option>
            <option>San Antonio, Nueva Ecija</option>
            <option>Jaen, Nueva Ecija</option>
            <option>Gapan, Nueva Ecija</option>
            <option>Cabiao, Nueva Ecija</option>
            <option>San Isidro, Nueva Ecija</option>
            <option>Other</option>
          </select>
        </div>
        <div class="field">
          <label for="barangay">Barangay <span class="req">*</span></label>
          <select class="input" id="barangay" name="barangay" required disabled>
            <option value="" disabled selected>Select municipality first</option>
          </select>
        </div>
      </div>

      <div class="field-row two">
        <div class="field">
          <label for="phone">Mobile Number <span class="req">*</span></label>
          <input class="input" type="tel" id="phone" name="phone" placeholder="+63 9XX XXX XXXX" required>
        </div>
        <div class="field">
          <label for="email">Email Address</label>
          <input class="input" type="email" id="email" name="email" placeholder="you@email.com">
        </div>
      </div>

      <div class="field">
        <label>Data Plan <span class="req">*</span></label>
        <div class="plan-options">
          <label class="plan-option">
            <input type="radio" name="dataPlan" value="basic" required>
            <span class="plan-option-card">
              <span class="plan-radio-dot"></span>
              <span class="plan-option-text">
                <span class="name">Home Basic — 25 Mbps</span>
                <span class="price">₱500 / month</span>
              </span>
            </span>
          </label>
          <label class="plan-option">
            <input type="radio" name="dataPlan" value="standard" required>
            <span class="plan-option-card">
              <span class="plan-radio-dot"></span>
              <span class="plan-option-text">
                <span class="name">Home Standard — 50 Mbps</span>
                <span class="price">₱800 / month</span>
              </span>
            </span>
          </label>
          <label class="plan-option">
            <input type="radio" name="dataPlan" value="pro" required>
            <span class="plan-option-card">
              <span class="plan-radio-dot"></span>
              <span class="plan-option-text">
                <span class="name">Home Pro — 100 Mbps</span>
                <span class="price">₱1,000 / month</span>
              </span>
            </span>
          </label>
        </div>
      </div>

      <div class="field-row two">
        <div class="field">
          <label>Upload 1 Valid ID <span class="req">*</span></label>
          <div class="file-field">
            <label for="idUpload">Choose File</label>
            <span id="fileNameLabel">No file chosen</span>
            <input type="file" id="idUpload" name="idUpload" accept="image/*,.pdf" required>
          </div>
        </div>
        <div class="field">
          <label for="agent">Referring Agent</label>
          <input class="input" type="text" id="agent" name="agent" placeholder="Optional">
        </div>
      </div>

      <div class="field">
        <label for="message">Instruction / Message</label>
        <textarea class="input" id="message" name="message" placeholder="Anything we should know before we visit — landmarks, best time to call, etc."></textarea>
      </div>

      <div class="apply-submit-row">
        <button type="submit" class="btn btn-primary">Submit Application</button>
      </div>

      <div class="apply-success" id="applySuccess">
        <span>✅</span>
        <span>Application received! We'll text you within 1 business day to confirm coverage and schedule installation.</span>
      </div>
    </form>
  </div>
</section>

<div class="stats-band">
  <div class="wrap" style="padding-top:40px;padding-bottom:40px;">
    <div class="stats-grid reveal">
      <div><div class="num" data-count="99.9" data-suffix="%">0%</div><div class="label">Network uptime</div></div>
      <div><div class="num" data-count="8" data-suffix=" min">0 min</div><div class="label">Avg. support response</div></div>
    </div>
  </div>
</div>

<footer id="contact">
  <div class="wrap">
    <div class="footer-grid">
      <div class="footer-brand">
        <a href="#" class="logo" style="margin-bottom:12px;">
          <span class="logo-mark">
            <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
              <circle cx="16" cy="16" r="15" fill="none" stroke="#00D9FF" stroke-width="2" opacity="0.35"/>
              <path d="M16 6 L16 14 M8 11 L14 15 M24 11 L18 15 M8 21 L14 17 M24 21 L18 17 M16 26 L16 18" stroke="#00D9FF" stroke-width="1.6" stroke-linecap="round"/>
              <circle cx="16" cy="16" r="4" fill="#00D9FF"/>
            </svg>
          </span>
          <span class="logo-text">Mikro<span>Net</span></span>
        </a>
        <p style="color:var(--muted);font-size:0.88rem;max-width:28ch;line-height:1.65;margin:0;">Pure fiber internet for homes and businesses, backed by local support.</p>
      </div>
      <div>
        <h4>Internet</h4>
        <ul>
          <li><a href="#plans">Residential plans</a></li>
          <li><a href="#services">Business plans</a></li>
          <li><a href="#services">IP-VPN &amp; Multi-Site</a></li>
          <li><a href="#services">Managed IT</a></li>
        </ul>
      </div>
      <div>
        <h4>Company</h4>
        <ul>
          <li><a href="#">About us</a></li>
        </ul>
      </div>
      <div>
        <h4>Get in touch</h4>
        <ul>
          <li><a href="tel:09083255591">0908-325-5591</a></li>
          <li><a href="mailto:mikronetph@gmail.com">mikronetph@gmail.com</a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span>© 2026 MikroNet. All rights reserved.</span>
      <span>Stay Connected to What Matters.</span>
    </div>
  </div>
</footer>

<script>
  const BARANGAYS_BY_MUNICIPALITY = {
    "San Antonio, Nueva Ecija": [
      "Buliran","Cama Juan","Julo","Lawang Kupang","Luyos","Maugat","Panabingan",
      "Papaya","Poblacion","San Francisco","San Jose","San Mariano","Santa Barbara",
      "Santa Cruz","Santo Cristo","Tikiw"
    ],
    "Jaen, Nueva Ecija": [
      "Calabasa","Dampulan (Poblacion)","Don Mariano Marcos (Poblacion)","Hilera",
      "Imbunia","Imelda de Poblacion (Doña Aurora)","Lambakin","Langla","Magsalisi",
      "Malabon-Kaingin","Marawa","Niyugan","Ocampo-Rivera (Poblacion)","Pakol",
      "Pamacpacan","Pinanggaan","Putlod","San Jose","San Josef (Nabao)","San Pablo",
      "San Roque","San Vicente","Santa Rita","Santo Tomas North","Santo Tomas South",
      "Sapang","Ulanin-Pitak"
    ],
    "Gapan, Nueva Ecija": [
      "Balante","Bayanihan","Bulak","Bungo","Kapalangan","Mabunga","Maburak",
      "Mahipon","Makabaclay","Malimba","Mangino","Marelo","Pambuan","Parcutela",
      "Puting Tubig","San Lorenzo (Poblacion)","San Nicolas","San Roque",
      "San Vicente (Poblacion)","Santa Cruz","Santo Cristo Norte","Santo Cristo Sur",
      "Santo Niño"
    ],
    "Cabiao, Nueva Ecija": [
      "Bagong Buhay","Bagong Sikat","Bagong Silang","Concepcion","Entablado",
      "Maligaya","Natividad North (Poblacion)","Natividad South (Poblacion)",
      "Palasinan","Polilio","San Antonio","San Carlos","San Fernando Norte",
      "San Fernando Sur","San Gregorio","San Juan North (Poblacion)",
      "San Juan South (Poblacion)","San Roque","San Vicente","Santa Ines",
      "Santa Isabel","Santa Rita","Sinipit"
    ],
    "San Isidro, Nueva Ecija": [
      "Alua","Calaba","Malapit","Mangga","Poblacion","Pulo","San Roque",
      "Santo Cristo","Tabon"
    ]
  };

  const municipalitySelect = document.getElementById('municipality');
  const barangaySelect = document.getElementById('barangay');

  function populateBarangays(municipality){
    barangaySelect.innerHTML = '';
    if(municipality === 'Other'){
      barangaySelect.disabled = true;
      const opt = document.createElement('option');
      opt.value = '';opt.disabled = true;opt.selected = true;
      opt.textContent = 'Type your barangay in the message box below';
      barangaySelect.appendChild(opt);
      return;
    }
    const list = BARANGAYS_BY_MUNICIPALITY[municipality];
    if(!list){
      barangaySelect.disabled = true;
      const opt = document.createElement('option');
      opt.value = '';opt.disabled = true;opt.selected = true;
      opt.textContent = 'Select municipality first';
      barangaySelect.appendChild(opt);
      return;
    }
    barangaySelect.disabled = false;
    const placeholder = document.createElement('option');
    placeholder.value = '';placeholder.disabled = true;placeholder.selected = true;
    placeholder.textContent = 'Select barangay';
    barangaySelect.appendChild(placeholder);
    list.forEach(brgy=>{
      const opt = document.createElement('option');
      opt.value = brgy;opt.textContent = brgy;
      barangaySelect.appendChild(opt);
    });
  }

  municipalitySelect.addEventListener('change', ()=>populateBarangays(municipalitySelect.value));

  const burgerBtn=document.getElementById('burgerBtn');
  const mobileMenu=document.getElementById('mobileMenu');
  burgerBtn.addEventListener('click',()=>{
    const isOpen=mobileMenu.classList.toggle('open');
    burgerBtn.setAttribute('aria-expanded',isOpen);
  });
  mobileMenu.querySelectorAll('a').forEach(a=>{
    a.addEventListener('click',()=>{mobileMenu.classList.remove('open');burgerBtn.setAttribute('aria-expanded','false');});
  });

  const header=document.getElementById('siteHeader');
  const onScroll=()=>header.classList.toggle('scrolled',window.scrollY>10);
  document.addEventListener('scroll',onScroll,{passive:true});
  onScroll();

  const monthlyBtn=document.getElementById('monthlyBtn');
  const annualBtn=document.getElementById('annualBtn');
  const togglePill=document.getElementById('togglePill');
  const priceEls=document.querySelectorAll('.price');
  function positionPill(btn){
    togglePill.style.width=btn.offsetWidth+'px';
    togglePill.style.transform=`translateX(${btn.offsetLeft-4}px)`;
  }
  function setBilling(mode){
    const btn=mode==='annual'?annualBtn:monthlyBtn;
    priceEls.forEach(el=>{
      el.classList.add('swap');
      setTimeout(()=>{el.textContent=mode==='annual'?el.dataset.annual:el.dataset.monthly;el.classList.remove('swap');},160);
    });
    monthlyBtn.classList.toggle('active',mode==='monthly');
    annualBtn.classList.toggle('active',mode==='annual');
    monthlyBtn.setAttribute('aria-selected',mode==='monthly');
    annualBtn.setAttribute('aria-selected',mode==='annual');
    positionPill(btn);
  }
  monthlyBtn.addEventListener('click',()=>setBilling('monthly'));
  annualBtn.addEventListener('click',()=>setBilling('annual'));
  window.addEventListener('load',()=>positionPill(monthlyBtn));
  window.addEventListener('resize',()=>positionPill(monthlyBtn.classList.contains('active')?monthlyBtn:annualBtn));

  const prefersReduced=window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  function animateCount(el){
    const target=parseFloat(el.dataset.count);
    const prefix=el.dataset.prefix||'';
    const suffix=el.dataset.suffix||'';
    const isDecimal=el.dataset.count.includes('.');
    if(prefersReduced){el.textContent=prefix+el.dataset.count+suffix;return;}
    const dur=1100;const start=performance.now();
    function tick(now){
      const p=Math.min(1,(now-start)/dur);
      const eased=1-Math.pow(1-p,3);
      const val=target*eased;
      el.textContent=prefix+(isDecimal?val.toFixed(1):Math.round(val))+suffix;
      if(p<1)requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  const liveSpeedEl=document.getElementById('liveSpeed');
  function tickLiveSpeed(){
    if(!liveSpeedEl)return;
    const base=850+Math.random()*150;
    liveSpeedEl.textContent=Math.round(base).toLocaleString();
  }
  tickLiveSpeed();
  setInterval(tickLiveSpeed,1800);

  if(!prefersReduced && 'IntersectionObserver' in window){
    const io=new IntersectionObserver(entries=>{
      entries.forEach(e=>{
        if(e.isIntersecting){
          e.target.classList.add('in');
          e.target.querySelectorAll('[data-count]').forEach(animateCount);
          if(e.target.matches('[data-count]'))animateCount(e.target);
          io.unobserve(e.target);
        }
      });
    },{threshold:0.15});
    document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
    document.querySelectorAll('.hero .trust-row [data-count]').forEach(el=>{
      setTimeout(()=>animateCount(el),650);
    });
  }else{
    document.querySelectorAll('.reveal').forEach(el=>el.classList.add('in'));
    document.querySelectorAll('[data-count]').forEach(el=>{el.textContent=(el.dataset.prefix||'')+el.dataset.count+(el.dataset.suffix||'');});
  }

  if(!prefersReduced){
    document.querySelectorAll('[data-tilt]').forEach(card=>{
      card.addEventListener('mousemove',e=>{
        const r=card.getBoundingClientRect();
        const x=e.clientX-r.left, y=e.clientY-r.top;
        card.style.setProperty('--mx',x+'px');
        card.style.setProperty('--my',y+'px');
        const rx=((y/r.height)-0.5)*-4;
        const ry=((x/r.width)-0.5)*4;
        card.style.transform=`translateY(-4px) perspective(600px) rotateX(${rx}deg) rotateY(${ry}deg)`;
      });
      card.addEventListener('mouseleave',()=>{card.style.transform='';});
    });
  }

  document.querySelectorAll('a[data-plan]').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const plan=btn.dataset.plan;
      const radio=document.querySelector(`input[name="dataPlan"][value="${plan}"]`);
      if(radio)radio.checked=true;
    });
  });

  const idUpload=document.getElementById('idUpload');
  const fileNameLabel=document.getElementById('fileNameLabel');
  if(idUpload){
    idUpload.addEventListener('change',()=>{
      fileNameLabel.textContent=idUpload.files.length?idUpload.files[0].name:'No file chosen';
    });
  }

  const applyForm=document.getElementById('applyForm');
  const applySuccess=document.getElementById('applySuccess');
  const submitBtn=applyForm?applyForm.querySelector('button[type="submit"]'):null;
  if(applyForm){
    applyForm.addEventListener('submit',async e=>{
      e.preventDefault();
      if(!applyForm.checkValidity()){applyForm.reportValidity();return;}
      if(submitBtn){submitBtn.disabled=true;submitBtn.textContent='Sending…';}
      try{
        const data=new FormData(applyForm);
        const res=await fetch(applyForm.getAttribute('action'),{method:'POST',body:data,headers:{Accept:'application/json'}});
        const json=await res.json().catch(()=>({}));
        if(res.ok && json.ok){
          applySuccess.classList.add('show');
          applySuccess.scrollIntoView({behavior:prefersReduced?'auto':'smooth',block:'center'});
          applyForm.reset();
          municipalitySelect.value='';
          populateBarangays('');
          fileNameLabel.textContent='No file chosen';
        }else{
          alert('Submission failed: '+(json.error||'please try again.'));
        }
      }catch(err){
        alert('Network error — please check your connection and try again.');
      }finally{
        if(submitBtn){submitBtn.disabled=false;submitBtn.textContent='Submit Application';}
      }
    });
  }
</script>
</body>
</html>
"""


HEAD = ("<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>{{ 'Lending Tracker' if session.get('role')=='admin'"
        "   else cfg.get('biz_name','Portal') }}</title>"
        "<style>" + CSS + "</style></head><body>")

NAV = """
<header>
  <div class=brand>LENDING<span>.</span></div>
  <nav>
    <a href="{{ url_for('dashboard') }}" class="{{ 'on' if request.endpoint=='dashboard' }}">Dashboard</a>
    <a href="{{ url_for('borrowers') }}" class="{{ 'on' if request.endpoint in ['borrowers','borrower'] }}">Borrowers</a>
    <a href="{{ url_for('capital') }}" class="{{ 'on' if request.endpoint=='capital' }}">Cash box</a>
    <a href="{{ url_for('clients') }}" class="{{ 'on' if request.endpoint in ['clients','client'] }}">Clients</a>
    <a href="{{ url_for('review') }}" class="{{ 'on' if request.endpoint=='review' }}">Payments{% if pend_n %}<span class=badge>{{ pend_n }}</span>{% endif %}</a>
    <a href="{{ url_for('applications') }}" class="{{ 'on' if request.endpoint in ['applications','application'] }}">Applications{% if appl_n %}<span class=badge>{{ appl_n }}</span>{% endif %}</a>
    <a href="{{ url_for('methods') }}" class="{{ 'on' if request.endpoint=='methods' }}">Methods</a>
    <a href="{{ url_for('export_csv') }}">Export</a>
  </nav>
  <div class=sp></div>
  <a href="{{ url_for('home') }}" class="small muted" target=_blank>View site</a>
  <a href="{{ url_for('logout') }}" class="small muted">Log out</a>
</header>
<div class=wrap>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat,m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
"""

FOOT = "</div></body></html>"

LOGIN_HTML = HEAD + """
<div class=login><div class=card>
  <h1>{{ cfg.get('biz_name','ACCESS') }}<span>.</span></h1>
  <p>Enter your password or access key.</p>
  {% if err %}<div class="flash err">{{ err }}</div>{% endif %}
  <form method=post>
    <div class=row><input type=password name=password placeholder="Password" autofocus></div>
    <button type=submit style="width:100%">Sign in</button>
  </form>
  <p style="text-align:center;margin-top:14px">
    <a href="{{ url_for('home') }}" class="small muted">&larr; Back to site</a></p>
</div></div></body></html>"""

# --- shared row renderer for a loan table
LOAN_ROWS = """
<table>
<tr>
  <th>Borrower</th><th class=num>Principal</th><th class=num>Total due</th>
  <th class=num>Paid</th><th class=num>Balance</th>
  <th>Progress</th><th>Due date</th><th class=num>Days</th><th>Status</th>
</tr>
{% for l in rows %}
<tr>
  <td class=name><a href="{{ url_for('borrower', bid=l.borrower_id) }}">{{ l.borrower_name }}</a></td>
  <td class=num data-l="Principal">{{ fmt(l.principal) }}</td>
  <td class=num data-l="Total due"><span>{{ fmt(l.total_now) }}
    {% if l.penalty %}<div class="small r">+{{ fmt(l.penalty) }} late</div>{% endif %}</span></td>
  <td class="num g" data-l="Paid">{{ fmt(l.paid) }}</td>
  <td class=num data-l="Balance"><b>{{ fmt(l.balance) }}</b></td>
  <td style="min-width:110px" data-l="Progress">
    <div>
      <div class=small>{{ '%.0f'|format(l.pct) }}%</div>
      <div class=bar><i class="{{ 'done' if l.balance<=0 }}" style="width:{{ l.pct }}%"></i></div>
    </div>
  </td>
  <td class="small" data-l="Due date">
    {% if l.kind=='open' %}<span class=muted>open-ended</span>
    {% else %}{{ l.due_date }}{% endif %}</td>
  <td class="num small {{ 'r' if l.days_left is not none and l.days_left<0 else '' }}" data-l="Days">
    {% if l.kind=='open' %}<span class=muted>{{ l.periods }} {{ l.period_word }}s in</span>
    {% elif l.balance<=0 %}<span class=muted>-</span>
    {% elif l.days_left<0 %}{{ -l.days_left }}d late
    {% else %}{{ l.days_left }}d left{% endif %}
  </td>
  <td data-l="Status"><span class="pill {{ l.status }}">{{ l.status }}</span></td>
</tr>
{% endfor %}
</table>
"""

DASH_HTML = HEAD + NAV + """
<div class=kpis>
  <div class="kpi o"><div class=lab>Your capital</div><div class=val>{{ fmt(c.net_capital) }}</div>
    <div class="small muted">money you actually put in</div></div>
  <div class="kpi {{ 'hot' if c.on_hand < 0 else 'good' }}"><div class=lab>Cash on hand</div>
    <div class=val>{{ fmt(c.on_hand) }}</div>
    <div class="small muted">in the box today{% if c.pending %} &middot;
      <span class=a>{{ fmt(c.on_hand_proj) }} after pending</span>{% endif %}</div></div>
  <div class="kpi warn"><div class=lab>On the street</div><div class=val>{{ fmt(k.outstanding) }}</div>
    <div class="small muted">owed back to you</div></div>
  <div class="kpi good"><div class=lab>Collected, open loans</div>
    <div class=val>{{ fmt(k.collected_open) }}</div>
    <div class="small muted">{{ fmt(c.collected) }} all-time{% if k.collected_settled %}
      &middot; {{ fmt(k.collected_settled) }} on settled{% endif %}</div></div>
  <div class="kpi {{ 'hot' if k.overdue }}"><div class=lab>Overdue</div>
    <div class=val>{{ fmt(k.overdue_amt) }}</div>
    <div class="small muted">{{ k.overdue }} loan(s){% if k.behind %} &middot; {{ k.behind }} behind{% endif %}{% if k.penalty %}
      &middot; <span class=r>{{ fmt(k.penalty) }} late int.</span>{% endif %}</div></div>
  <div class="kpi"><div class=lab>Active / Borrowers</div>
    <div class=val>{{ k.active }} / {{ k.borrowers }}</div>
    <div class="small muted">{{ fmt(k.interest) }} interest booked</div></div>
</div>

<div class=card style="margin-top:14px;padding:13px 16px">
  <div class=small>
    <b>{{ fmt(c.deployed) }}</b> lent out all-time =
    <b class=o>{{ fmt(c.from_pocket) }}</b> your money +
    <b class=g>{{ fmt(c.recycled) }}</b> recycled collections re-lent.
    <a href="{{ url_for('capital') }}" style="margin-left:6px">Cash box &rarr;</a>
  </div>
  {% if c.net_capital <= 0 %}
  <div class="small a" style="margin-top:7px">
    No capital recorded yet, so every peso lent looks like recycled money.
    Add what you originally put in on the <a href="{{ url_for('capital') }}">Cash box</a> page.
  </div>
  {% elif c.on_hand < 0 %}
  <div class="small r" style="margin-top:7px">
    Cash on hand is negative &mdash; you have lent out more than your recorded capital
    plus collections. Add the missing top-up on the
    <a href="{{ url_for('capital') }}">Cash box</a> page.
  </div>
  {% endif %}
</div>

<h2>New loan</h2>
<div class=card>
{% if people %}
<form method=post action="{{ url_for('loan_new') }}">
  <div class="row c5">
    <div><label>Borrower</label>
      <select name=borrower_id required>
        {% for p in people %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
      </select></div>
    <div><label>Amount lent</label>
      <input name=principal type=number step=0.01 min=0.01 required placeholder="10000"></div>
    <div><label>Interest %</label>
      <input name=rate_pct type=number step=0.01 value="{{ cfg.default_rate }}"></div>
    <div><label>Term (days)</label>
      <input name=term_days type=number min=1 value="{{ cfg.default_term_days }}"></div>
    <div><label>Start date</label>
      <input name=start_date type=date value="{{ today }}"></div>
  </div>
  <div class="row c3">
    <div><label>Loan type</label>
      <select name=kind>
        <option value=term>Term &mdash; % over a fixed number of days</option>
        <option value=open>Open-ended &mdash; fixed charge per period, no maturity</option>
      </select></div>
    <div><label>Charge per period (open only)</label>
      <input name=charge_amount type=number step=0.01 placeholder="e.g. 500"></div>
    <div><label>Period (open only)</label>
      <select name=charge_period_days>
        <option value=7>Weekly</option>
        <option value=15>Every 15 days</option>
        <option value=30>Monthly</option>
      </select></div>
  </div>
  <div class="row c3">
    <div><label>Note</label><input name=notes placeholder="Optional"></div>
    <div><label>Late interest %/{{ cfg.overdue_period_days }}d</label>
      <input name=overdue_rate_pct type=number step=0.01
             placeholder="{{ cfg.overdue_rate }} (default)"></div>
    <div><label>or flat amount/{{ cfg.overdue_period_days }}d (sanla)</label>
      <input name=overdue_flat type=number step=0.01 placeholder="e.g. 250"></div>
  </div>
  <button type=submit>Add loan</button>
</form>
{% else %}
<div class=empty>Add a borrower first &mdash;
  <a href="{{ url_for('borrowers') }}">go to Borrowers</a>.</div>
{% endif %}
</div>

<h2>Open loans <span class=sub>{{ open_loans|length }}</span></h2>
<div class=card>
{% if open_loans %}{% set rows = open_loans %}""" + LOAN_ROWS + """
{% else %}<div class=empty>No open loans.</div>{% endif %}
</div>

{% if closed %}
<h2>Settled <span class=sub>{{ closed|length }}</span></h2>
<div class=card>{% set rows = closed %}""" + LOAN_ROWS + """</div>
{% endif %}
""" + FOOT

PEOPLE_HTML = HEAD + NAV + """
<h2>Add borrower</h2>
<div class=card>
<form method=post action="{{ url_for('borrower_new') }}">
  <div class="row c3">
    <div><label>Name</label><input name=name required placeholder="Full name"></div>
    <div><label>Phone</label><input name=phone placeholder="09xx xxx xxxx"></div>
    <div><label>Email</label><input name=email type=email></div>
  </div>
  <div class="row c2">
    <div><label>Address</label><input name=address></div>
    <div><label>Notes</label><input name=notes placeholder="Referred by, work, etc."></div>
  </div>
  <button type=submit>Add borrower</button>
</form>
</div>

<h2>Borrowers <span class=sub>{{ people|length }}</span></h2>
<div class=card>
<form method=get style="display:flex;gap:10px;margin-bottom:16px">
  <input name=q value="{{ q }}" placeholder="Search name, phone, email, notes">
  {% if show_archived %}<input type=hidden name=archived value=1>{% endif %}
  <button type=submit class=ghost>Search</button>
  {% if q %}<a class="btn ghost" href="{{ url_for('borrowers') }}">Clear</a>{% endif %}
</form>
{% if people %}
<table>
<tr><th>Name</th><th>Phone</th><th class=num>Open loans</th><th class=num>Paid to date</th>
    <th class=num>Balance</th><th>Status</th><th>Notes</th></tr>
{% for p in people %}
<tr>
  <td class=name><a href="{{ url_for('borrower', bid=p.row.id) }}">{{ p.row.name }}</a></td>
  <td class=small data-l="Phone">{{ p.row.phone or '-' }}</td>
  <td class=num data-l="Open loans">{{ p.open }}</td>
  <td class="num g" data-l="Paid to date">{{ fmt(p.paid) }}</td>
  <td class=num data-l="Balance"><b>{{ fmt(p.balance) }}</b></td>
  <td data-l="Status"><span class="pill {{ p.worst }}">{{ p.worst }}</span></td>
  <td class="small muted" data-l="Notes">{{ p.row.notes[:44] }}</td>
</tr>
{% endfor %}
</table>
{% else %}<div class=empty>No borrowers{{ ' match that search' if q }}.</div>{% endif %}
<div style="margin-top:14px" class=small>
  {% if show_archived %}<a href="{{ url_for('borrowers') }}">&larr; Back to active</a>
  {% else %}<a href="{{ url_for('borrowers', archived=1) }}">View archived</a>{% endif %}
</div>
</div>
""" + FOOT

BORROWER_HTML = HEAD + NAV + """
<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">
  <h2 style="margin:0">{{ b.name }}</h2>
  {% if b.archived %}<span class="pill paid">archived</span>{% endif %}
  <a href="{{ url_for('borrowers') }}" class="small muted">&larr; all borrowers</a>
</div>

<div class=kpis style="margin:16px 0">
  <div class="kpi o"><div class=lab>Lent</div><div class=val>{{ fmt(tot.principal) }}</div></div>
  <div class="kpi"><div class=lab>Total due</div><div class=val>{{ fmt(tot.due) }}</div></div>
  <div class="kpi good"><div class=lab>Paid</div><div class=val>{{ fmt(tot.paid) }}</div></div>
  <div class="kpi warn"><div class=lab>Balance</div><div class=val>{{ fmt(tot.balance) }}</div></div>
</div>

<div class=card>
<form method=post action="{{ url_for('borrower_edit', bid=b.id) }}">
  <div class="row c3">
    <div><label>Name</label><input name=name value="{{ b.name }}" required></div>
    <div><label>Phone</label><input name=phone value="{{ b.phone }}"></div>
    <div><label>Email</label><input name=email value="{{ b.email }}"></div>
  </div>
  <div class="row c2">
    <div><label>Address</label><input name=address value="{{ b.address }}"></div>
    <div><label>Notes</label><input name=notes value="{{ b.notes }}"></div>
  </div>
  <label style="display:flex;gap:8px;align-items:center;text-transform:none;font-size:13px">
    <input type=checkbox name=archived value=1 style="width:auto" {{ 'checked' if b.archived }}>
    Archived
  </label>
  <div style="margin-top:14px;display:flex;gap:10px">
    <button type=submit>Save details</button>
  </div>
</form>
<details>
  <summary>Delete this borrower</summary>
  <form method=post action="{{ url_for('borrower_delete', bid=b.id) }}"
        onsubmit="return confirm('Delete {{ b.name }} and ALL their loans and payments? This cannot be undone.')">
    <p class="small muted" style="margin:8px 0">
      Removes the borrower, their {{ loans|length }} loan(s) and every payment on record.</p>
    <button type=submit class=danger>Delete permanently</button>
  </form>
</details>
</div>

<h2>New loan</h2>
<div class=card>
<form method=post action="{{ url_for('loan_new') }}">
  <input type=hidden name=borrower_id value="{{ b.id }}">
  <div class="row c4">
    <div><label>Amount lent</label>
      <input name=principal type=number step=0.01 min=0.01 required></div>
    <div><label>Interest %</label>
      <input name=rate_pct type=number step=0.01 value="{{ cfg.default_rate }}"></div>
    <div><label>Term (days)</label>
      <input name=term_days type=number min=1 value="{{ cfg.default_term_days }}"></div>
    <div><label>Start date</label>
      <input name=start_date type=date value="{{ today }}"></div>
  </div>
  <div class="row c3">
    <div><label>Loan type</label>
      <select name=kind>
        <option value=term>Term &mdash; % over a fixed number of days</option>
        <option value=open>Open-ended &mdash; fixed charge per period, no maturity</option>
      </select></div>
    <div><label>Charge per period (open only)</label>
      <input name=charge_amount type=number step=0.01 placeholder="e.g. 500"></div>
    <div><label>Period (open only)</label>
      <select name=charge_period_days>
        <option value=7>Weekly</option>
        <option value=15>Every 15 days</option>
        <option value=30>Monthly</option>
      </select></div>
  </div>
  <div class="row c3">
    <div><label>Note</label><input name=notes placeholder="Optional"></div>
    <div><label>Late interest %/{{ cfg.overdue_period_days }}d</label>
      <input name=overdue_rate_pct type=number step=0.01
             placeholder="{{ cfg.overdue_rate }} (default)"></div>
    <div><label>or flat amount/{{ cfg.overdue_period_days }}d (sanla)</label>
      <input name=overdue_flat type=number step=0.01 placeholder="e.g. 250"></div>
  </div>
  <button type=submit>Add loan</button>
</form>
</div>

<h2>Loans <span class=sub>{{ loans|length }}</span></h2>
{% if not loans %}<div class=card><div class=empty>No loans yet.</div></div>{% endif %}
{% for l in loans %}
<div class=card>
  <div class=loan-head>
    <span class=amt>{{ fmt(l.principal) }}</span>
    <span class="pill {{ l.status }}">{{ l.status }}</span>
    {% if l.kind=='open' %}
    <span class=terms>{{ fmt(l.charge) }} per {{ l.period_word }} &middot;
      interest-only, no maturity &middot; since {{ l.start_date }}</span>
    <span style="margin-left:auto" class=terms>
      {% if l.balance<=0 %}<span class=g>redeemed</span>
      {% elif l.interest_owed %}<span class=a>{{ fmt(l.interest_owed) }} behind</span>
      {% else %}<span class=g>paid up</span>{% endif %}</span>
    {% else %}
    <span class=terms>{{ l.rate_pct }}% over {{ l.term_days }}d &middot;
      {{ l.start_date }} &rarr; {{ l.due_date }} &middot;
      {{ fmt(l.daily_due) }}/day &middot;
      then {{ l.od_label }}/{{ l.period_days }}d late</span>
    <span style="margin-left:auto" class=terms>
      {% if l.balance<=0 %}<span class=g>settled</span>
      {% elif l.days_left<0 %}<span class=r>{{ -l.days_left }} days late</span>
      {% else %}{{ l.days_left }} days left{% endif %}</span>
    {% endif %}
  </div>
  {% if l.notes %}<div class="small muted" style="margin-bottom:8px">{{ l.notes }}</div>{% endif %}

  {% if l.kind=='open' %}
  <table style="margin-top:10px">
  <tr><th class=num>Principal</th><th class=num>Charge</th>
      <th class=num>{{ l.period_word|capitalize }}s</th>
      <th class=num>Interest so far</th><th class=num>Paid</th>
      <th class=num>Owed now</th><th>Next charge</th></tr>
  <tr>
    <td class=num data-l="Principal">{{ fmt(l.principal) }}</td>
    <td class=num data-l="Charge">{{ fmt(l.charge) }}/{{ l.period_word }}</td>
    <td class=num data-l="Periods">{{ l.periods }}</td>
    <td class=num data-l="Interest so far">{{ fmt(l.accrued) }}</td>
    <td class="num g" data-l="Paid">{{ fmt(l.paid) }}</td>
    <td class=num data-l="Owed now"><b>{{ fmt(l.balance) }}</b></td>
    <td class="small" data-l="Next charge">{{ l.next_charge }}</td>
  </tr>
  </table>
  <div class="small muted" style="margin-top:9px">
    {% if l.interest_owed %}
      <span class=a>{{ fmt(l.interest_owed) }} of interest unpaid.</span>
    {% else %}
      <span class=g>Interest paid up.</span>
    {% endif %}
    Principal outstanding <b>{{ fmt(l.principal_left) }}</b> &mdash; it rides until
    she redeems it. Nothing here ever falls due, so no late interest applies.
  </div>
  {% else %}
  <table style="margin-top:10px">
  <tr><th class=num>Total due</th><th class=num>Late interest</th>
      <th class=num>Payable now</th><th class=num>Paid</th>
      <th class=num>Balance</th><th class=num>On schedule</th>
      <th style="width:22%">Progress</th></tr>
  <tr>
    <td class=num data-l="Total due">{{ fmt(l.total_due) }}</td>
    <td class="num {{ 'r' if l.penalty else 'muted' }}" data-l="Late interest">
      <span>{% if l.penalty %}+{{ fmt(l.penalty) }}
        <div class=small>{{ l.periods }} &times; {{ l.od_label }}</div>
      {% else %}&mdash;{% endif %}</span></td>
    <td class=num data-l="Payable now"><b>{{ fmt(l.total_now) }}</b></td>
    <td class="num g" data-l="Paid">{{ fmt(l.paid) }}</td>
    <td class=num data-l="Balance"><b>{{ fmt(l.balance) }}</b></td>
    <td class="num {{ 'g' if l.drift>=0 else 'a' }}" data-l="On schedule">
      {{ '+' if l.drift>=0 }}{{ fmt(l.drift) }}</td>
    <td data-l="Progress">
      <div>
        <div class=small>{{ '%.0f'|format(l.pct) }}% &middot; day {{ l.days_elapsed }}/{{ l.term_days }}</div>
        <div class=bar><i class="{{ 'done' if l.balance<=0 }}" style="width:{{ l.pct }}%"></i></div>
      </div>
    </td>
  </tr>
  </table>
  {% endif %}

  {% if l.status=='overdue' %}
  <div style="margin-top:12px;padding:11px 14px;border-radius:8px;
              background:rgba(255,92,92,.08);border:1px solid rgba(255,92,92,.22)">
    <div class="small r"><b>Past due {{ -l.days_left }} days.</b>
      Late interest of {{ l.od_label }} per {{ l.period_days }} days is running{% if l.od_flat %}
      (flat, sanla-style){% endif %}.</div>
    <div class="small muted" style="margin-top:5px">
      {% if l.periods %}{{ l.periods }} period(s) charged so far, totalling
        {{ fmt(l.penalty) }}.{% else %}Nothing charged yet &mdash; the first
        {{ l.period_days }}-day period has not closed.{% endif %}
      Next charge <b>{{ l.next_charge }}</b>{% if l.next_add %}, about
      <b class=r>{{ fmt(l.next_add) }}</b> more if nothing is paid before then{% endif %}.
    </div>
  </div>
  {% endif %}

  <form method=post action="{{ url_for('payment_new') }}" style="margin-top:16px">
    <input type=hidden name=loan_id value="{{ l.id }}">
    <div class="row c4" style="margin-bottom:10px">
      <div><label>Payment amount</label>
        <input name=amount type=number step=0.01 required
               placeholder="{{ '%.2f'|format(l.daily_due) }}"></div>
      <div><label>Date paid</label><input name=paid_on type=date value="{{ today }}"></div>
      <div><label>Method</label><input name=method placeholder="cash, gcash, bank"></div>
      <div><label>Note</label><input name=note></div>
    </div>
    <button type=submit class=mini>Record payment</button>
  </form>

  <details>
    <summary>Edit loan terms</summary>
    <form method=post action="{{ url_for('loan_edit', lid=l.id) }}" style="margin-top:10px">
      <div class="row c4">
        <div><label>Principal</label>
          <input name=principal type=number step=0.01 value="{{ l.principal }}" required></div>
        <div><label>Interest %</label>
          <input name=rate_pct type=number step=0.01 value="{{ l.rate_pct }}" required></div>
        <div><label>Term (days)</label>
          <input name=term_days type=number min=1 value="{{ l.term_days }}" required></div>
        <div><label>Start date</label>
          <input name=start_date type=date value="{{ l.start_date }}" required></div>
      </div>
      <div class="row c3">
        <div><label>Loan type</label>
          <select name=kind>
            <option value=term {{ 'selected' if l.kind!='open' }}>Term &mdash; % over N days</option>
            <option value=open {{ 'selected' if l.kind=='open' }}>Open-ended &mdash; no maturity</option>
          </select></div>
        <div><label>Charge per period (open only)</label>
          <input name=charge_amount type=number step=0.01
                 value="{{ l.charge if l.kind=='open' else '' }}" placeholder="e.g. 500"></div>
        <div><label>Period (open only)</label>
          <select name=charge_period_days>
            <option value=7 {{ 'selected' if l.charge_period_days==7 }}>Weekly</option>
            <option value=15 {{ 'selected' if l.charge_period_days==15 }}>Every 15 days</option>
            <option value=30 {{ 'selected' if l.charge_period_days==30 }}>Monthly</option>
          </select></div>
      </div>
      <div class="row c3">
        <div><label>Note</label><input name=notes value="{{ l.notes }}"></div>
        <div><label>Late interest %/{{ l.period_days }}d</label>
          <input name=overdue_rate_pct type=number step=0.01
                 value="{{ l.od_rate_raw if l.od_rate_raw is not none else '' }}"
                 placeholder="{{ cfg.overdue_rate }} (default)"></div>
        <div><label>or flat amount/{{ l.period_days }}d (sanla)</label>
          <input name=overdue_flat type=number step=0.01
                 value="{{ l.od_flat_raw if l.od_flat_raw is not none else '' }}"
                 placeholder="overrides the %"></div>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <button type=submit class=mini>Save loan</button>
        <span style="margin-left:auto"></span>
      </div>
    </form>
    <form method=post action="{{ url_for('loan_delete', lid=l.id) }}" style="margin-top:10px"
          onsubmit="return confirm('Delete this loan and its payments?')">
      <button type=submit class=danger>Delete loan</button>
    </form>
  </details>
</div>
{% endfor %}

<h2>Payment history <span class=sub>{{ pays|length }}</span></h2>
<div class=card>
{% if pays %}
<table>
<tr><th>Date</th><th class=num>Amount</th><th>Method</th><th>Note</th><th></th></tr>
{% for p in pays %}
<tr>
  <td class=small data-l="Date">{{ p.paid_on }}</td>
  <td class="num g" data-l="Amount"><b>{{ fmt(p.amount) }}</b></td>
  <td class=small data-l="Method">{{ p.method or '-' }}</td>
  <td class="small muted" data-l="Note">{{ p.note or '' }}</td>
  <td class=num data-l="">
    <form method=post action="{{ url_for('payment_delete', pid=p.id) }}"
          onsubmit="return confirm('Remove this payment?')">
      <button type=submit class=danger>Remove</button>
    </form>
  </td>
</tr>
{% endfor %}
</table>
{% else %}<div class=empty>No payments recorded yet.</div>{% endif %}
</div>
""" + FOOT

CAPITAL_HTML = HEAD + NAV + """
<h2>Cash box <span class=sub>what you actually invested, vs. money going round again</span></h2>

<div class=kpis>
  <div class="kpi o"><div class=lab>Your capital</div><div class=val>{{ fmt(c.net_capital) }}</div>
    <div class="small muted">{{ fmt(c.cap_in) }} in{% if c.cap_out %} &minus; {{ fmt(c.cap_out) }} out{% endif %}</div></div>
  <div class="kpi {{ 'hot' if c.on_hand < 0 else 'good' }}"><div class=lab>Cash on hand</div>
    <div class=val>{{ fmt(c.on_hand) }}</div>
    <div class="small muted">in the box today</div></div>
  <div class="kpi {{ 'warn' if c.pending else '' }}"><div class=lab>After pending</div>
    <div class=val>{{ fmt(c.on_hand_proj) }}</div>
    <div class="small muted">{% if c.pending %}{{ fmt(c.pending) }} still to move{% else %}nothing pending{% endif %}</div></div>
  <div class="kpi"><div class=lab>Lent out all-time</div><div class=val>{{ fmt(c.deployed) }}</div>
    <div class="small muted">across every loan ever written</div></div>
  <div class="kpi good"><div class=lab>Collected</div><div class=val>{{ fmt(c.collected) }}</div>
    <div class="small muted">all payments received</div></div>
</div>

<div class=card style="margin-top:16px">
  <div style="font-weight:650;margin-bottom:8px">Where the {{ fmt(c.deployed) }} came from</div>
  <div class=bar style="height:11px">
    <i style="width:{{ (c.from_pocket / c.deployed * 100) if c.deployed else 0 }}%"></i>
  </div>
  <div class=small style="margin-top:9px">
    <span class=o>&#9632;</span> <b>{{ fmt(c.from_pocket) }}</b> your own capital
    &nbsp;&middot;&nbsp;
    <span class=muted>&#9632;</span> <b>{{ fmt(c.recycled) }}</b> collections re-lent
  </div>
  <div class="small muted" style="margin-top:9px">
    Renewals cost you nothing new. When a collection is rolled straight into the
    next loan &mdash; for one borrower or fifty &mdash; it adds to <i>lent out</i>
    but not to <i>your capital</i>. That is why the two drift further apart the
    longer the book runs, and why <i>lent out</i> is a poor measure of what you
    are actually risking.
  </div>
</div>

{% if pend %}
<h2>Pending <span class=sub>dated after today &mdash; not in the box yet</span></h2>
<div class=card>
<table>
<tr><th>Date</th><th>What</th><th>Who</th><th class=num>Amount</th></tr>
{% for p in pend %}
<tr>
  <td class=small data-l="Date">{{ p.d }}</td>
  <td class=small data-l="What">{{ p.what }}</td>
  <td class=small data-l="Who">{{ p.who }}</td>
  <td class="num {{ 'g' if p.amt>=0 else 'a' }}" data-l="Amount">
    {{ '+' if p.amt>=0 }}{{ fmt(p.amt) }}</td>
</tr>
{% endfor %}
</table>
<div class="small muted" style="margin-top:11px">
  These count toward <b>After pending</b> but not <b>Cash on hand</b>. If any of
  this money has already changed hands, change its date to today or earlier and
  it will move into the box.
</div>
</div>
{% endif %}

<h2>Record a movement</h2>
<div class=card>
<form method=post action="{{ url_for('capital_new') }}">
  <div class="row c4">
    <div><label>Type</label>
      <select name=kind>
        <option value=in>Money in &mdash; capital you invested</option>
        <option value=out>Money out &mdash; profit you took</option>
      </select></div>
    <div><label>Amount</label>
      <input name=amount type=number step=0.01 min=0.01 required placeholder="5000"></div>
    <div><label>Date</label><input name=moved_on type=date value="{{ today }}"></div>
    <div><label>Note</label><input name=note placeholder="starting capital, top-up, drawings"></div>
  </div>
  <button type=submit>Record</button>
</form>
</div>

<h2>Movements <span class=sub>{{ rows|length }}</span></h2>
<div class=card>
{% if rows %}
<table>
<tr><th>Date</th><th>Type</th><th class=num>Amount</th><th>Note</th><th></th></tr>
{% for r in rows %}
<tr>
  <td class=small data-l="Date">{{ r.moved_on }}</td>
  <td data-l="Type"><span class="pill {{ 'active' if r.kind=='in' else 'behind' }}">
    {{ 'invested' if r.kind=='in' else 'withdrawn' }}</span></td>
  <td class="num {{ 'g' if r.kind=='in' else 'a' }}" data-l="Amount"><b>
    {{ '+' if r.kind=='in' else '-' }}{{ fmt(r.amount) }}</b></td>
  <td class="small muted" data-l="Note">{{ r.note or '' }}</td>
  <td class=num data-l="">
    <form method=post action="{{ url_for('capital_delete', cid=r.id) }}"
          onsubmit="return confirm('Remove this entry?')">
      <button type=submit class=danger>Remove</button>
    </form>
  </td>
</tr>
{% endfor %}
</table>
{% else %}
<div class=empty>
  Nothing recorded yet. Add the cash you originally put in &mdash; everything
  after that is just the same money circulating.
</div>
{% endif %}
</div>
""" + FOOT



# ---------------------------------------------------------------- client chrome
EXTRA_CSS = """
.pill.approved,.pill.paid{background:rgba(62,207,142,.14);color:var(--green)}
.pill.pending{background:rgba(255,176,32,.14);color:var(--amber)}
.pill.rejected,.pill.unpaid{background:rgba(255,92,92,.15);color:var(--red)}
.pill.off{background:rgba(139,145,163,.16);color:var(--dim)}
.key{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
  letter-spacing:1px;background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:4px 9px;display:inline-block;color:var(--accent)}
.pays{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.pay{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px}
.pay h3{font-size:15px;margin-bottom:10px}
.pay .acct{font-size:12px;color:var(--dim);margin-bottom:3px}
.pay .no{font-size:17px;font-weight:680;letter-spacing:.5px;
  font-variant-numeric:tabular-nums;word-break:break-all}
.pay img{margin-top:12px;width:100%;max-width:240px;border-radius:8px;
  background:#fff;padding:8px}
/* both the name and the number are things they retype by hand, so both copy */
.pay button.cp{display:flex;justify-content:space-between;align-items:center;
  gap:10px;width:100%;text-align:left;background:var(--panel2);
  border:1px solid var(--line);border-radius:9px;padding:10px 12px;
  margin-bottom:8px;color:var(--ink);cursor:pointer;font:inherit;
  box-shadow:none;transition:border-color .15s,background .15s}
.pay button.cp:hover{border-color:var(--accent);background:var(--panel2);
  filter:none}
.pay button.cp.acct{font-size:13px;font-weight:600;color:var(--dim)}
.pay button.cp.no{font-size:17px;font-weight:680;letter-spacing:.5px;
  font-variant-numeric:tabular-nums}
.pay button.cp i{font-style:normal;font-size:10px;font-weight:650;color:var(--dim);
  text-transform:uppercase;letter-spacing:.6px;white-space:nowrap;opacity:.85}
.pay button.cp.done{border-color:var(--green);background:rgba(52,211,153,.08)}
.pay button.cp.done i{color:var(--green);opacity:1}
.pay .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.pay{border-top:3px solid var(--tint,var(--accent))}
.brandrow{display:flex;align-items:center;gap:11px;margin-bottom:13px}
.brandrow h3{margin:0;font-size:19px;font-weight:750;letter-spacing:-.3px;
  color:var(--ink)}
.brandrow .logo{margin:0;width:42px;height:42px;flex:0 0 42px;object-fit:contain;
  border-radius:10px;background:#fff;padding:5px;box-shadow:none}
.brandrow .badgemark{width:42px;height:42px;flex:0 0 42px;border-radius:10px;
  background:var(--tint,var(--accent));color:#fff;display:flex;
  align-items:center;justify-content:center;font-size:15px;font-weight:750;
  letter-spacing:.5px}
.mlogo{width:38px;height:38px;object-fit:contain;border-radius:9px;
  background:#fff;padding:4px}
.mbadge{width:38px;height:38px;border-radius:9px;color:#fff;display:flex;
  align-items:center;justify-content:center;font-size:14px;font-weight:750}
.pay img{box-shadow:0 6px 20px -10px rgba(0,0,0,.8)}
.proof{display:block;width:72px;height:72px;object-fit:cover;border-radius:7px;
  border:1px solid var(--line);background:var(--panel2)}
.rev{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.rev .meta{flex:1;min-width:200px}
.rev .shot{width:190px;flex:0 0 auto}
.rev .shot img{width:100%;border-radius:9px;border:1px solid var(--line);display:block}
@media(max-width:620px){
  .pays{grid-template-columns:1fr}
  .rev .shot{width:100%}
}
"""

CNAV = """
<header>
  <div class=brand>{{ cfg.get('biz_name','INTERNET') }}<span>.</span></div>
  <div class=sp></div>
  <a href="{{ url_for('home') }}" class="small muted">MikroNet site</a>
  <a href="{{ url_for('logout') }}" class="small muted">Log out</a>
</header>
<div class=wrap>
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat,m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
"""

PORTAL_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + CNAV + """
<h2 style="margin-top:4px">Hi {{ c['name'] }}<span class=sub>{{ c['plan'] }}</span></h2>

<div class=kpis>
  <div class="kpi o"><div class=lab>Monthly</div><div class=val>{{ fmt(c['monthly']) }}</div>
    <div class="small muted">due day {{ c['due_day'] }} of the month</div></div>
  <div class="kpi {{ 'good' if st['status']=='paid' else ('warn' if st['status']=='pending' else 'hot') }}">
    <div class=lab>{{ plab(per) }}</div>
    <div class=val style="font-size:17px">
      {% if st['status']=='paid' %}Paid
      {% elif st['status']=='pending' %}Checking
      {% else %}Not yet paid{% endif %}</div>
    <div class="small muted">
      {% if st['status']=='pending' %}Your proof is with us{% else %}this month{% endif %}</div>
  </div>
</div>

{% if cfg.get('pay_note') %}
  <div class=card style="margin-top:16px">{{ cfg['pay_note'] }}</div>
{% endif %}

<h2>How to pay</h2>
{% if methods %}
<div class=pays>
  {% for m in methods %}
  <div class=pay style="--tint:{{ tint(m) }}">
    <div class=brandrow>
      {% if m['logo'] %}
        <img class=logo src="{{ url_for('uploaded', name=m['logo']) }}"
             alt="{{ m['label'] }}">
      {% else %}
        <span class=badgemark>{{ initials(m['label']) }}</span>
      {% endif %}
      <h3>{{ m['label'] }}</h3>
    </div>
    {% if m['account'] %}
      <button type=button class="cp acct" data-copy="{{ m['account'] }}"
              data-idle="copy name">
        <span>{{ m['account'] }}</span><i>copy name</i></button>
    {% endif %}
    {% if m['number'] %}
      <button type=button class="cp no" data-copy="{{ m['number'] }}"
              data-idle="copy number">
        <span>{{ m['number'] }}</span><i>copy number</i></button>
    {% endif %}
    {% if m['note'] %}<div class="small muted" style="margin-top:7px">{{ m['note'] }}</div>{% endif %}
    {% if m['qr'] %}
      <img src="{{ url_for('uploaded', name=m['qr']) }}" alt="{{ m['label'] }} QR">
    {% endif %}
    <div class=acts>
      {% if m['qr'] %}
        <a class="btn mini" href="{{ url_for('uploaded', name=m['qr'], dl=1) }}"
           download>Save QR</a>
      {% endif %}
      {% if m['link'] %}
        <a class="btn ghost mini" href="{{ m['link'] }}"
           target=_blank rel="noopener noreferrer">Open {{ m['label'] }}</a>
      {% endif %}
    </div>
    {% if m['qr'] %}
      <div class="small muted" style="margin-top:9px">
        Paying from this phone? Save the QR, then in {{ m['label'] }} tap
        <b>QR</b> and upload it from your gallery.</div>
    {% endif %}
  </div>
  {% endfor %}
</div>
{% else %}
  <div class="card empty">No payment methods set up yet.</div>
{% endif %}

<h2>Send your proof</h2>
<div class=card>
  <form method=post action="{{ url_for('portal_pay') }}" enctype=multipart/form-data>
    <div class="row c3">
      <div><label>For which month</label>
        <select name=period>
          {% for p in periods %}<option value="{{ p }}">{{ plab(p) }}</option>{% endfor %}
        </select></div>
      <div><label>Amount sent</label>
        <input name=amount type=number step=0.01 value="{{ '%.2f'|format(c['monthly'] or 0) }}"></div>
      <div><label>Paid via</label>
        <select name=method>
          {% for m in methods %}<option value="{{ m['label'] }}">{{ m['label'] }}</option>{% endfor %}
          <option value="Other">Other</option>
        </select></div>
    </div>
    <div class=row>
      <div><label>Screenshot of payment</label>
        <input name=proof type=file accept="image/*" required></div>
    </div>
    <button type=submit>Send proof</button>
  </form>
</div>

<h2>Your payments</h2>
{% if st['rows'] %}
<table>
  <tr><th>Proof</th><th>Month</th><th class=num>Amount</th><th>Via</th>
      <th>Status</th><th>Note</th></tr>
  {% for p in st['rows'] %}
  <tr>
    <td data-l="Proof">{% if p['proof'] %}
      <a href="{{ url_for('uploaded', name=p['proof']) }}" target=_blank>
        <img class=proof src="{{ url_for('uploaded', name=p['proof']) }}" alt=proof></a>
      {% endif %}</td>
    <td class=name data-l="Month">{{ plab(p['period']) }}</td>
    <td class=num data-l="Amount">{{ fmt(p['amount']) }}</td>
    <td data-l="Via">{{ p['method'] }}</td>
    <td data-l="Status"><span class="pill {{ p['status'] }}">{{ p['status'] }}</span></td>
    <td data-l="Note" class=small>{{ p['admin_note'] or '' }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}
  <div class="card empty">Nothing sent yet.</div>
{% endif %}

<script>
// Clipboard API needs https and a focused document, and quietly rejects when it
// does not have both. Every failure path has to fall through to execCommand and
// then say so - a copy button that does nothing is worse than no button.
document.addEventListener("click", function (e) {
  var b = e.target.closest("button.cp");
  if (!b) return;
  var txt = b.dataset.copy, i = b.querySelector("i");
  var idle = b.dataset.idle || "tap to copy";
  var flash = function (msg, ok) {
    if (ok) b.classList.add("done");
    i.textContent = msg;
    setTimeout(function () {
      b.classList.remove("done");
      i.textContent = idle;
    }, 1600);
  };
  var legacy = function () {
    var t = document.createElement("textarea");
    t.value = txt; t.style.position = "fixed"; t.style.opacity = 0;
    document.body.appendChild(t); t.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(t);
    flash(ok ? "copied" : "copy failed", ok);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(function () {
      flash("copied", true);
    }, legacy);
  } else {
    legacy();
  }
});
</script>
""" + FOOT


CLIENTS_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + NAV + """
<h2 style="margin-top:4px">Clients<span class=sub>{{ per }}</span></h2>
<div class=kpis>
  <div class=kpi><div class=lab>Active</div><div class=val>{{ k['n'] }}</div></div>
  <div class="kpi o"><div class=lab>Monthly billing</div><div class=val>{{ fmt(k['mrr']) }}</div></div>
  <div class="kpi good"><div class=lab>Paid this month</div><div class=val>{{ k['paid'] }}</div></div>
  <div class="kpi hot"><div class=lab>Not yet paid</div><div class=val>{{ k['unpaid'] }}</div></div>
</div>

<div class=card style="margin-top:16px">
  <form method=get style="display:flex;gap:10px">
    <input name=q value="{{ q }}" placeholder="Search name, phone or plan">
    <button type=submit class=ghost>Search</button>
    {% if q %}<a class="btn ghost" href="{{ url_for('clients') }}">Clear</a>{% endif %}
  </form>
</div>

<details>
  <summary>Add a client</summary>
  <div class=card>
    <form method=post action="{{ url_for('client_new') }}">
      <div class="row c3">
        <div><label>Name</label><input name=name required></div>
        <div><label>Phone</label><input name=phone></div>
        <div><label>Plan</label><input name=plan placeholder="e.g. Fiber 50Mbps"></div>
      </div>
      <div class="row c3">
        <div><label>Monthly</label><input name=monthly type=number step=0.01 value="0"></div>
        <div><label>Due day</label><input name=due_day type=number min=1 max=31 value="5"></div>
        <div><label>Access key</label><input name=access_key placeholder="blank = generate"></div>
      </div>
      <div class=row><div><label>Address</label><input name=address></div></div>
      <div class=row><div><label>Notes</label><input name=notes></div></div>
      <button type=submit>Add client</button>
    </form>
  </div>
</details>

{% if rows %}
<table>
  <tr><th>Name</th><th>Plan</th><th class=num>Monthly</th><th>Access key</th>
      <th>{{ per }}</th><th>Sent</th><th></th></tr>
  {% for o in rows %}
  <tr>
    <td class=name data-l="Name">
      <a href="{{ url_for('client', cid=o['c']['id']) }}">{{ o['c']['name'] }}</a>
      {% if not o['c']['active'] %}<span class="pill off">off</span>{% endif %}
      {% if o['c']['phone'] %}<div class="small muted">{{ o['c']['phone'] }}</div>{% endif %}
    </td>
    <td data-l="Plan">{{ o['c']['plan'] or '-' }}</td>
    <td class=num data-l="Monthly">{{ fmt(o['c']['monthly']) }}</td>
    <td data-l="Access key"><span class=key>{{ o['c']['access_key'] }}</span></td>
    <td data-l="{{ per }}"><span class="pill {{ o['st']['status'] }}">{{ o['st']['status'] }}</span></td>
    <td class=num data-l="Sent">{{ o['st']['n'] }}</td>
    <td data-l=""><a class="btn ghost mini" href="{{ url_for('client', cid=o['c']['id']) }}">Open</a></td>
  </tr>
  {% endfor %}
</table>
{% else %}
  <div class="card empty">No clients yet. Add one above and hand them the key.</div>
{% endif %}
""" + FOOT


CLIENT_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + NAV + """
<h2 style="margin-top:4px">{{ c['name'] }}
  <span class=sub>{{ c['plan'] or 'no plan set' }}</span></h2>

<div class=card>
  <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <div>
      <label>Access key</label>
      <span class=key>{{ c['access_key'] }}</span>
    </div>
    <form method=post action="{{ url_for('client_rekey', cid=c['id']) }}"
          onsubmit="return confirm('Issue a new key? The current one stops working.')">
      <button class="ghost mini" type=submit>New key</button>
    </form>
    <div class="small muted" style="flex:1;min-width:200px">
      They sign in at the same login box with this key.
    </div>
  </div>
</div>

<div class=card>
  <form method=post action="{{ url_for('client_edit', cid=c['id']) }}">
    <div class="row c3">
      <div><label>Name</label><input name=name value="{{ c['name'] }}" required></div>
      <div><label>Phone</label><input name=phone value="{{ c['phone'] }}"></div>
      <div><label>Plan</label><input name=plan value="{{ c['plan'] }}"></div>
    </div>
    <div class="row c3">
      <div><label>Monthly</label>
        <input name=monthly type=number step=0.01 value="{{ '%.2f'|format(c['monthly'] or 0) }}"></div>
      <div><label>Due day</label>
        <input name=due_day type=number min=1 max=31 value="{{ c['due_day'] }}"></div>
      <div><label>Access</label>
        <select name=active>
          <option value="1" {{ 'selected' if c['active'] }}>Active</option>
          <option value="" {{ 'selected' if not c['active'] }}>Switched off</option>
        </select></div>
    </div>
    <div class=row><div><label>Address</label><input name=address value="{{ c['address'] }}"></div></div>
    <div class=row><div><label>Notes</label><input name=notes value="{{ c['notes'] }}"></div></div>
    <button type=submit>Save</button>
    <a class="btn ghost" href="{{ url_for('clients') }}">Back</a>
  </form>
</div>

<h2>Payments<span class=sub>{{ st['n'] }} sent</span></h2>
{% if st['rows'] %}
<table>
  <tr><th>Proof</th><th>Month</th><th class=num>Amount</th><th>Via</th>
      <th>Reference</th><th>Status</th><th></th></tr>
  {% for p in st['rows'] %}
  <tr>
    <td data-l="Proof">{% if p['proof'] %}
      <a href="{{ url_for('uploaded', name=p['proof']) }}" target=_blank>
        <img class=proof src="{{ url_for('uploaded', name=p['proof']) }}" alt=proof></a>
      {% endif %}</td>
    <td class=name data-l="Month">{{ plab(p['period']) }}</td>
    <td class=num data-l="Amount">{{ fmt(p['amount']) }}</td>
    <td data-l="Via">{{ p['method'] }}</td>
    <td data-l="Reference" class=small>{{ p['reference'] or '-' }}</td>
    <td data-l="Status"><span class="pill {{ p['status'] }}">{{ p['status'] }}</span></td>
    <td data-l="">
      {% if p['status'] != 'approved' %}
      <form method=post action="{{ url_for('review_act', pid=p['id'], action='approve') }}"
            style="display:inline">
        <input type=hidden name=back value="{{ url_for('client', cid=c['id']) }}">
        <button class=mini type=submit>Approve</button>
      </form>
      {% endif %}
      {% if p['status'] != 'rejected' %}
      <form method=post action="{{ url_for('review_act', pid=p['id'], action='reject') }}"
            style="display:inline">
        <input type=hidden name=back value="{{ url_for('client', cid=c['id']) }}">
        <button class=danger type=submit>Reject</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% else %}
  <div class="card empty">No payments sent yet.</div>
{% endif %}

<details>
  <summary>Delete this client</summary>
  <div class=card>
    <p class="small muted" style="margin-bottom:12px">
      Removes {{ c['name'] }}, their key and every payment and screenshot they sent.
      This cannot be undone.</p>
    <form method=post action="{{ url_for('client_delete', cid=c['id']) }}"
          onsubmit="return confirm('Delete {{ c['name'] }} and all their payments?')">
      <button class=danger type=submit>Delete permanently</button>
    </form>
  </div>
</details>
""" + FOOT


REVIEW_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + NAV + """
<h2 style="margin-top:4px">Waiting on me
  <span class=sub>{{ pend|length }} proof{{ '' if pend|length == 1 else 's' }}</span></h2>

{% if pend %}
  {% for p in pend %}
  <div class=card>
    <div class=rev>
      {% if p['proof'] %}
      <div class=shot>
        <a href="{{ url_for('uploaded', name=p['proof']) }}" target=_blank>
          <img src="{{ url_for('uploaded', name=p['proof']) }}" alt="proof"></a>
      </div>
      {% endif %}
      <div class=meta>
        <div class=loan-head>
          <span class=amt>{{ fmt(p['amount']) }}</span>
          <span class="pill pending">{{ plab(p['period']) }}</span>
        </div>
        <div class=terms>
          <a href="{{ url_for('client', cid=p['client_id']) }}">{{ p['name'] }}</a>
          {% if p['plan'] %} - {{ p['plan'] }}{% endif %}
        </div>
        <div class="small muted" style="margin-top:6px">
          via {{ p['method'] or 'unspecified' }}
          {% if p['reference'] %} - ref {{ p['reference'] }}{% endif %}<br>
          sent {{ p['submitted_at'] }}
        </div>
        <form method=post action="{{ url_for('review_act', pid=p['id'], action='approve') }}"
              style="margin-top:12px">
          <div class=row><input name=admin_note placeholder="Note back to them (optional)"></div>
          <button type=submit>Approve</button>
          <button type=submit class=danger
                  formaction="{{ url_for('review_act', pid=p['id'], action='reject') }}">Reject</button>
        </form>
      </div>
    </div>
  </div>
  {% endfor %}
{% else %}
  <div class="card empty">Nothing waiting. All proofs handled.</div>
{% endif %}

<h2>Recently handled</h2>
{% if done %}
<table>
  <tr><th>Proof</th><th>Client</th><th>Month</th><th class=num>Amount</th>
      <th>Via</th><th>Status</th><th></th></tr>
  {% for p in done %}
  <tr>
    <td data-l="Proof">{% if p['proof'] %}
      <a href="{{ url_for('uploaded', name=p['proof']) }}" target=_blank>
        <img class=proof src="{{ url_for('uploaded', name=p['proof']) }}" alt=proof></a>
      {% endif %}</td>
    <td class=name data-l="Client">
      <a href="{{ url_for('client', cid=p['client_id']) }}">{{ p['name'] }}</a></td>
    <td data-l="Month">{{ plab(p['period']) }}</td>
    <td class=num data-l="Amount">{{ fmt(p['amount']) }}</td>
    <td data-l="Via">{{ p['method'] }}</td>
    <td data-l="Status"><span class="pill {{ p['status'] }}">{{ p['status'] }}</span></td>
    <td data-l="">
      <form method=post action="{{ url_for('review_delete', pid=p['id']) }}"
            onsubmit="return confirm('Delete this entry and its screenshot?')">
        <input type=hidden name=back value="{{ url_for('review') }}">
        <button class=danger type=submit>Delete</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
{% else %}
  <div class="card empty">Nothing handled yet.</div>
{% endif %}
""" + FOOT


APPLICATIONS_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + NAV + """
<h2 style="margin-top:4px">Applications
  <span class=sub>signups from the MikroNet site, valid ID included</span></h2>

{% if rows %}
  {% for a in rows %}
  <div class=card>
    <div class=rev>
      {% if a['id_file'] %}
      <div class=shot>
        <a href="{{ url_for('uploaded', name=a['id_file']) }}" target=_blank>
          {% if a['id_file'].lower().endswith('.pdf') %}
            <div class="card empty" style="padding:20px">Open ID (PDF)</div>
          {% else %}
            <img src="{{ url_for('uploaded', name=a['id_file']) }}" alt="valid id">
          {% endif %}
        </a>
      </div>
      {% endif %}
      <div class=meta>
        <div class=loan-head>
          <span class=amt style="font-size:18px">{{ a['full_name'] }}</span>
          <span class="pill {{ 'pending' if a['status']=='new' else ('good' if a['status']=='converted' else ('off' if a['status']=='declined' else 'ok')) }}">{{ a['status'] }}</span>
        </div>
        <div class=terms>{{ plan_label(a['plan']) }}</div>
        <div class="small muted" style="margin-top:6px">
          {{ a['phone'] }}{% if a['email'] %} - {{ a['email'] }}{% endif %}<br>
          {{ a['street'] }}{% if a['street'] %}, {% endif %}{{ a['barangay'] }}
          {% if a['barangay'] %}, {% endif %}{{ a['municipality'] }}<br>
          {% if a['agent'] %}referred by {{ a['agent'] }}<br>{% endif %}
          {% if a['message'] %}"{{ a['message'] }}"<br>{% endif %}
          applied {{ a['submitted_at'] }}
        </div>
        <form method=post action="{{ url_for('application_status', aid=a['id']) }}"
              style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select name=status>
            {% for s in statuses %}
            <option value="{{ s }}" {{ 'selected' if a['status']==s }}>{{ s }}</option>
            {% endfor %}
          </select>
          <button class=mini type=submit>Save status</button>
        </form>
        <form method=post action="{{ url_for('application_delete', aid=a['id']) }}"
              style="margin-top:8px" onsubmit="return confirm('Delete this application?')">
          <button class=danger type=submit>Delete</button>
        </form>
      </div>
    </div>
  </div>
  {% endfor %}
{% else %}
  <div class="card empty">No applications yet. They will show up here the moment someone applies on the site.</div>
{% endif %}
""" + FOOT


METHODS_HTML = HEAD + "<style>" + EXTRA_CSS + "</style>" + NAV + """
<h2 style="margin-top:4px">Payment methods
  <span class=sub>what clients see on their portal</span></h2>

<div class=card>
  <form method=post action="{{ url_for('method_settings') }}">
    <div class="row c2">
      <div><label>Business name</label>
        <input name=biz_name value="{{ cfg.get('biz_name','INTERNET') }}"></div>
      <div><label>Note at the top of the portal</label>
        <input name=pay_note value="{{ cfg.get('pay_note','') }}"
               placeholder="e.g. Send proof before the 5th to avoid cutoff"></div>
    </div>
    <button type=submit class=ghost>Save settings</button>
  </form>
</div>

{% for m in rows %}
<div class=card>
  <form method=post action="{{ url_for('method_edit', mid=m['id']) }}"
        enctype=multipart/form-data>
    <div class="row c4">
      <div><label>Label</label><input name=label value="{{ m['label'] }}" required></div>
      <div><label>Account name</label><input name=account value="{{ m['account'] }}"></div>
      <div><label>Number</label><input name=number value="{{ m['number'] }}"></div>
      <div><label>Shown</label>
        <select name=active>
          <option value="1" {{ 'selected' if m['active'] }}>Visible</option>
          <option value="" {{ 'selected' if not m['active'] }}>Hidden</option>
        </select></div>
    </div>
    <div class="row c2">
      <div><label>Note</label><input name=note value="{{ m['note'] }}"></div>
      <div><label>Sort order</label>
        <input name=sort type=number value="{{ m['sort'] }}"></div>
    </div>
    <div class=row>
      <div><label>App link (optional)</label>
        <input name=link value="{{ m['link'] or '' }}"
               placeholder="a payment link, if you have one - otherwise leave blank">
      </div>
    </div>
    <div class="row c3">
      <div><label>{{ 'Replace logo' if m['logo'] else 'Logo image' }}</label>
        <input name=logo type=file accept="image/*"></div>
      <div><label>Brand colour</label>
        <input name=color value="{{ m['color'] or '' }}"
               placeholder="{{ tint(m) }}"></div>
      <div><label>Now showing</label>
        <div style="display:flex;align-items:center;gap:9px;padding-top:2px">
          {% if m['logo'] %}
            <img class=mlogo src="{{ url_for('uploaded', name=m['logo']) }}" alt="">
            <label style="display:inline;text-transform:none;letter-spacing:0;
                          margin:0;font-size:12px">
              <input type=checkbox name=drop_logo style="width:auto"> remove</label>
          {% else %}
            <span class=mbadge style="background:{{ tint(m) }}">{{ initials(m['label']) }}</span>
            <span class="small muted">no logo yet</span>
          {% endif %}
        </div>
      </div>
    </div>
    <div class="row c2">
      <div><label>{{ 'Replace QR' if m['qr'] else 'QR image' }}</label>
        <input name=qr type=file accept="image/*"></div>
      <div>
        {% if m['qr'] %}
          <label>Current QR</label>
          <img src="{{ url_for('uploaded', name=m['qr']) }}"
               style="width:120px;border-radius:8px;background:#fff;padding:6px">
          <div class=small style="margin-top:6px">
            <label style="display:inline;text-transform:none;letter-spacing:0">
              <input type=checkbox name=drop_qr style="width:auto"> remove QR</label>
          </div>
        {% endif %}
      </div>
    </div>
    <button type=submit>Save</button>
    <button class=danger type=submit
            formaction="{{ url_for('method_delete', mid=m['id']) }}"
            formnovalidate
            onclick="return confirm('Delete {{ m['label'] }}?')">Delete</button>
  </form>
</div>
{% endfor %}

<details {{ 'open' if not rows }}>
  <summary>Add a payment method</summary>
  <div class=card>
    <form method=post action="{{ url_for('method_new') }}" enctype=multipart/form-data>
      <div class="row c3">
        <div><label>Label</label><input name=label placeholder="GCash" required></div>
        <div><label>Account name</label><input name=account placeholder="Juan D."></div>
        <div><label>Number</label><input name=number placeholder="0917 123 4567"></div>
      </div>
      <div class="row c3">
        <div><label>Note</label><input name=note placeholder="optional"></div>
        <div><label>Sort order</label><input name=sort type=number value="0"></div>
        <div><label>QR image</label><input name=qr type=file accept="image/*"></div>
      </div>
      <div class="row c3">
        <div><label>App link (optional)</label>
          <input name=link placeholder="a payment link, if you have one"></div>
        <div><label>Logo image</label>
          <input name=logo type=file accept="image/*"></div>
        <div><label>Brand colour</label>
          <input name=color placeholder="#0057ff - blank picks one for you"></div>
      </div>
      <button type=submit>Add method</button>
    </form>
  </div>
</details>
""" + FOOT


# ================================================================ clients
# Internet-plan clients never touch the ledger above. A key opens exactly one
# page for them: the portal, where they see how to pay, send a screenshot as
# proof, and watch their own history. I approve or reject from /review.
#
# Keys are stored in the clear on purpose - I have to be able to read one back
# to a client over the phone. They guard a payment upload page, nothing more.

ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_ID = ALLOWED_IMG | {".pdf"}   # the site's "Upload valid ID" also takes a PDF scan
KEY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no O/0 or I/1 - keys get read aloud


def new_key():
    """AB12-CD34-EF56: readable over the phone, unique in the table."""
    with db() as con:
        while True:
            k = "-".join("".join(secrets.choice(KEY_CHARS) for _ in range(4))
                         for _ in range(3))
            if not con.execute("SELECT 1 FROM clients WHERE access_key=?",
                               (k,)).fetchone():
                return k


def save_upload(fs, prefix, allowed=None):
    """Store an uploaded image (or, for prefix='appid', a PDF) under uploads/,
    return the stored filename."""
    if not fs or not fs.filename:
        return ""
    allowed = allowed or ALLOWED_IMG
    ext = os.path.splitext(fs.filename)[1].lower()
    if ext not in allowed:
        if allowed is ALLOWED_ID:
            raise ValueError("ID must be a PNG, JPG, WEBP, GIF or PDF.")
        raise ValueError("Image must be PNG, JPG, WEBP or GIF.")
    name = "%s-%s%s" % (prefix, secrets.token_hex(8), ext)
    fs.save(os.path.join(UPLOAD_DIR, name))
    return name


def drop_upload(name):
    if name:
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass


# Starting tints only - not official brand colours, and every one of them is
# editable per method. A real logo, once uploaded, covers the badge entirely.
BRAND_TINT = {
    "gcash": "#0057ff", "maya": "#16c79a", "paymaya": "#16c79a",
    "gotyme": "#2a3446", "gotyme bank": "#2a3446",
    "china bank": "#c8102e", "chinabank": "#c8102e", "cbc": "#c8102e",
    "bpi": "#b01116", "bdo": "#00539f", "metrobank": "#00539f",
    "unionbank": "#ff6b00", "seabank": "#1a73e8", "landbank": "#0f7b3e",
}


def tint(m):
    """Whatever the user set, else a known brand tint, else the app accent.

    Matched as a substring so real-world labels land too - "China Banking
    Corporation" should pick up the same tint as "China Bank". Longest key
    first, so "gotyme bank" beats "gotyme"."""
    c = (m["color"] or "").strip()
    if c:
        return c
    label = (m["label"] or "").strip().lower()
    for key in sorted(BRAND_TINT, key=len, reverse=True):
        if key in label:
            return BRAND_TINT[key]
    return "#3b82f6"


def initials(label):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", label or "") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


PLAN_LABELS = {
    "basic": "Home Basic - 25 Mbps (₱500/mo)",
    "standard": "Home Standard - 50 Mbps (₱800/mo)",
    "pro": "Home Pro - 100 Mbps (₱1,000/mo)",
}
APPL_STATUSES = ["new", "contacted", "scheduled", "converted", "declined"]


def plan_label(v):
    return PLAN_LABELS.get((v or "").strip().lower(), v or "-")


def this_period():
    return today().strftime("%Y-%m")


def plab(p):
    """'2026-09' -> 'Sep 2026'."""
    try:
        return datetime.strptime(p, "%Y-%m").strftime("%b %Y")
    except (ValueError, TypeError):
        return p or ""


def period_choices(n=6):
    """This month and the five before it, for the portal dropdown."""
    y, m = today().year, today().month
    out = []
    for _ in range(n):
        out.append("%04d-%02d" % (y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def client_state(con, c):
    """Where this client stands on the current month's bill."""
    rows = con.execute("""SELECT * FROM client_payments WHERE client_id=?
                          ORDER BY period DESC, id DESC""", (c["id"],)).fetchall()
    per = this_period()
    paid = any(r["period"] == per and r["status"] == "approved" for r in rows)
    pend = any(r["period"] == per and r["status"] == "pending" for r in rows)
    return {"rows": rows, "n": len(rows),
            "status": "paid" if paid else ("pending" if pend else "unpaid")}


@app.context_processor
def inject_common():
    """cfg for the page title, pend_n / appl_n for the nav badges."""
    if session.get("role") != "admin":
        return {"pend_n": 0, "appl_n": 0, "cfg": CFG}
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM client_payments "
                        "WHERE status='pending'").fetchone()[0]
        a = con.execute("SELECT COUNT(*) FROM applications "
                        "WHERE status='new'").fetchone()[0]
    return {"pend_n": n, "appl_n": a, "cfg": CFG}


@app.errorhandler(413)
def too_big(e):
    if request.path == "/apply":
        return jsonify(ok=False, error="That file is too large - keep it under 8 MB."), 413
    flash("That image is too large - keep it under 8 MB.", "err")
    return redirect(request.referrer or url_for("portal"))


@app.route("/file/<name>")
def uploaded(name):
    """Proofs belong to whoever sent them; QR images are visible to any key.

    ?dl=1 sends it as a download. That is the whole trick behind paying from
    the same phone: you cannot scan a QR that is on your own screen, but GCash
    will happily take one out of your gallery."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name or ""):
        return "bad name", 400
    role = session.get("role")
    if role not in ("admin", "client"):
        return redirect(url_for("login"))
    if role == "client":
        with db() as con:
            mine = con.execute("SELECT 1 FROM client_payments WHERE client_id=? "
                               "AND proof=?",
                               (session.get("cid", 0), name)).fetchone()
            qr = con.execute("SELECT 1 FROM pay_methods WHERE qr=? OR logo=?",
                             (name, name)).fetchone()
        if not (mine or qr):
            return "not found", 404
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.exists(path):
        return "not found", 404
    if request.args.get("dl"):
        with db() as con:
            m = con.execute("SELECT label FROM pay_methods WHERE qr=?",
                            (name,)).fetchone()
        # a name they will recognise in their gallery
        label = re.sub(r"[^A-Za-z0-9 _-]", "", m["label"]) if m else "payment"
        ext = os.path.splitext(name)[1]
        return send_file(path, as_attachment=True,
                         download_name="%s QR%s" % (label.strip() or "payment", ext))
    return send_file(path)


# ---------------------------------------------------------------- client portal
@app.route("/portal")
@client_required
def portal():
    c = cur_client()
    with db() as con:
        methods = con.execute("SELECT * FROM pay_methods WHERE active=1 "
                              "ORDER BY sort, id").fetchall()
        st = client_state(con, c)
    return render_template_string(PORTAL_HTML, c=c, methods=methods, st=st, fmt=fmt,
                                  per=this_period(), plab=plab, cfg=CFG,
                                  periods=period_choices(),
                                  tint=tint, initials=initials)


@app.route("/portal/pay", methods=["POST"])
@client_required
def portal_pay():
    c = cur_client()
    try:
        proof = save_upload(request.files.get("proof"), "proof")
    except ValueError as e:
        flash(str(e), "err")
        return redirect(url_for("portal"))
    if not proof:
        flash("Attach a screenshot of your payment.", "err")
        return redirect(url_for("portal"))
    amount = money(request.form.get("amount") or c["monthly"])
    with db() as con:
        con.execute("""INSERT INTO client_payments
                       (client_id, period, amount, method, reference, proof)
                       VALUES (?,?,?,?,?,?)""",
                    (c["id"], request.form.get("period") or this_period(), amount,
                     request.form.get("method", "").strip(),
                     request.form.get("reference", "").strip(), proof))
    flash("Sent. I will confirm it shortly.", "ok")
    return redirect(url_for("portal"))


# ---------------------------------------------------------------- admin: clients
@app.route("/clients")
@login_required
def clients():
    q = (request.args.get("q") or "").strip()
    with db() as con:
        if q:
            rows = con.execute("""SELECT * FROM clients
                                  WHERE name LIKE ? OR phone LIKE ? OR plan LIKE ?
                                  ORDER BY active DESC, name""",
                               ("%" + q + "%",) * 3).fetchall()
        else:
            rows = con.execute("SELECT * FROM clients "
                               "ORDER BY active DESC, name").fetchall()
        out = [{"c": c, "st": client_state(con, c)} for c in rows]
    live = [o for o in out if o["c"]["active"]]
    k = {"n": len(live),
         "mrr": sum(money(o["c"]["monthly"]) for o in live),
         "unpaid": sum(1 for o in live if o["st"]["status"] == "unpaid"),
         "paid": sum(1 for o in live if o["st"]["status"] == "paid")}
    return render_template_string(CLIENTS_HTML, rows=out, fmt=fmt, k=k, q=q,
                                  per=plab(this_period()))


@app.route("/clients/new", methods=["POST"])
@login_required
def client_new():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name is required.", "err")
        return redirect(url_for("clients"))
    key = (request.form.get("access_key") or "").strip().upper() or new_key()
    with db() as con:
        if con.execute("SELECT 1 FROM clients WHERE access_key=?", (key,)).fetchone():
            flash("That key is already taken.", "err")
            return redirect(url_for("clients"))
        con.execute("""INSERT INTO clients
                       (name, access_key, phone, address, plan, monthly, due_day, notes)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (name, key, request.form.get("phone", "").strip(),
                     request.form.get("address", "").strip(),
                     request.form.get("plan", "").strip(),
                     money(request.form.get("monthly")),
                     int(request.form.get("due_day") or 5),
                     request.form.get("notes", "").strip()))
    flash("%s added. Key: %s" % (name, key), "ok")
    return redirect(url_for("clients"))


@app.route("/client/<int:cid>")
@login_required
def client(cid):
    with db() as con:
        c = con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        if not c:
            flash("No such client.", "err")
            return redirect(url_for("clients"))
        st = client_state(con, c)
    return render_template_string(CLIENT_HTML, c=c, st=st, fmt=fmt, plab=plab)


@app.route("/client/<int:cid>/edit", methods=["POST"])
@login_required
def client_edit(cid):
    with db() as con:
        con.execute("""UPDATE clients SET name=?, phone=?, address=?, plan=?,
                       monthly=?, due_day=?, active=?, notes=? WHERE id=?""",
                    (request.form.get("name", "").strip(),
                     request.form.get("phone", "").strip(),
                     request.form.get("address", "").strip(),
                     request.form.get("plan", "").strip(),
                     money(request.form.get("monthly")),
                     int(request.form.get("due_day") or 5),
                     1 if request.form.get("active") else 0,
                     request.form.get("notes", "").strip(), cid))
    flash("Saved.", "ok")
    return redirect(url_for("client", cid=cid))


@app.route("/client/<int:cid>/rekey", methods=["POST"])
@login_required
def client_rekey(cid):
    """The old key stops working the moment this runs."""
    key = new_key()
    with db() as con:
        con.execute("UPDATE clients SET access_key=? WHERE id=?", (key, cid))
    flash("New key: %s - the old one no longer works." % key, "ok")
    return redirect(url_for("client", cid=cid))


@app.route("/client/<int:cid>/delete", methods=["POST"])
@login_required
def client_delete(cid):
    with db() as con:
        for r in con.execute("SELECT proof FROM client_payments WHERE client_id=?",
                             (cid,)).fetchall():
            drop_upload(r["proof"])
        con.execute("DELETE FROM clients WHERE id=?", (cid,))
    flash("Client and their payment history deleted.", "ok")
    return redirect(url_for("clients"))


# ---------------------------------------------------------------- admin: proofs
@app.route("/review")
@login_required
def review():
    with db() as con:
        pend = con.execute("""SELECT p.*, c.name, c.plan FROM client_payments p
                              JOIN clients c ON c.id = p.client_id
                              WHERE p.status='pending'
                              ORDER BY p.submitted_at""").fetchall()
        done = con.execute("""SELECT p.*, c.name, c.plan FROM client_payments p
                              JOIN clients c ON c.id = p.client_id
                              WHERE p.status!='pending'
                              ORDER BY p.reviewed_at DESC, p.id DESC
                              LIMIT 40""").fetchall()
    return render_template_string(REVIEW_HTML, pend=pend, done=done,
                                  fmt=fmt, plab=plab)


@app.route("/review/<int:pid>/<action>", methods=["POST"])
@login_required
def review_act(pid, action):
    if action not in ("approve", "reject"):
        return redirect(url_for("review"))
    with db() as con:
        con.execute("""UPDATE client_payments SET status=?,
                       reviewed_at=datetime('now'), admin_note=? WHERE id=?""",
                    ("approved" if action == "approve" else "rejected",
                     request.form.get("admin_note", "").strip(), pid))
    flash("Payment %sd." % action, "ok")
    return redirect(request.form.get("back") or url_for("review"))


@app.route("/review/<int:pid>/delete", methods=["POST"])
@login_required
def review_delete(pid):
    with db() as con:
        row = con.execute("SELECT proof FROM client_payments WHERE id=?",
                          (pid,)).fetchone()
        if row:
            drop_upload(row["proof"])
        con.execute("DELETE FROM client_payments WHERE id=?", (pid,))
    flash("Entry deleted.", "ok")
    return redirect(request.form.get("back") or url_for("review"))


# ---------------------------------------------------------------- admin: applications
@app.route("/applications")
@login_required
def applications():
    with db() as con:
        rows = con.execute("""SELECT * FROM applications
                              ORDER BY (status='new') DESC, submitted_at DESC""").fetchall()
    return render_template_string(APPLICATIONS_HTML, rows=rows,
                                  plan_label=plan_label, statuses=APPL_STATUSES)


@app.route("/application/<int:aid>/status", methods=["POST"])
@login_required
def application_status(aid):
    status = request.form.get("status", "")
    if status not in APPL_STATUSES:
        flash("Unknown status.", "err")
        return redirect(url_for("applications"))
    with db() as con:
        con.execute("UPDATE applications SET status=? WHERE id=?", (status, aid))
    flash("Updated.", "ok")
    return redirect(url_for("applications"))


@app.route("/application/<int:aid>/delete", methods=["POST"])
@login_required
def application_delete(aid):
    with db() as con:
        row = con.execute("SELECT id_file FROM applications WHERE id=?",
                          (aid,)).fetchone()
        if row:
            drop_upload(row["id_file"])
        con.execute("DELETE FROM applications WHERE id=?", (aid,))
    flash("Application deleted.", "ok")
    return redirect(url_for("applications"))


# ---------------------------------------------------------------- admin: methods
@app.route("/methods")
@login_required
def methods():
    with db() as con:
        rows = con.execute("SELECT * FROM pay_methods ORDER BY sort, id").fetchall()
    return render_template_string(METHODS_HTML, rows=rows, cfg=CFG,
                                  tint=tint, initials=initials)


@app.route("/methods/new", methods=["POST"])
@login_required
def method_new():
    label = (request.form.get("label") or "").strip()
    if not label:
        flash("Give it a name, e.g. GCash.", "err")
        return redirect(url_for("methods"))
    try:
        qr = save_upload(request.files.get("qr"), "qr")
        logo = save_upload(request.files.get("logo"), "logo")
    except ValueError as e:
        flash(str(e), "err")
        return redirect(url_for("methods"))
    with db() as con:
        con.execute("""INSERT INTO pay_methods
                       (label, account, number, note, qr, logo, color, link, sort)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (label, request.form.get("account", "").strip(),
                     request.form.get("number", "").strip(),
                     request.form.get("note", "").strip(), qr, logo,
                     request.form.get("color", "").strip(),
                     request.form.get("link", "").strip(),
                     int(request.form.get("sort") or 0)))
    flash("%s added." % label, "ok")
    return redirect(url_for("methods"))


@app.route("/methods/<int:mid>/edit", methods=["POST"])
@login_required
def method_edit(mid):
    with db() as con:
        cur = con.execute("SELECT qr, logo FROM pay_methods WHERE id=?",
                          (mid,)).fetchone()
        qr = cur["qr"] if cur else ""
        logo = cur["logo"] if cur else ""
        try:
            fresh = save_upload(request.files.get("qr"), "qr")
            fresh_logo = save_upload(request.files.get("logo"), "logo")
        except ValueError as e:
            flash(str(e), "err")
            return redirect(url_for("methods"))
        if fresh:
            drop_upload(qr)
            qr = fresh
        elif request.form.get("drop_qr"):
            drop_upload(qr)
            qr = ""
        if fresh_logo:
            drop_upload(logo)
            logo = fresh_logo
        elif request.form.get("drop_logo"):
            drop_upload(logo)
            logo = ""
        con.execute("""UPDATE pay_methods SET label=?, account=?, number=?, note=?,
                       qr=?, logo=?, color=?, link=?, active=?, sort=? WHERE id=?""",
                    (request.form.get("label", "").strip(),
                     request.form.get("account", "").strip(),
                     request.form.get("number", "").strip(),
                     request.form.get("note", "").strip(), qr, logo,
                     request.form.get("color", "").strip(),
                     request.form.get("link", "").strip(),
                     1 if request.form.get("active") else 0,
                     int(request.form.get("sort") or 0), mid))
    flash("Saved.", "ok")
    return redirect(url_for("methods"))


@app.route("/methods/<int:mid>/delete", methods=["POST"])
@login_required
def method_delete(mid):
    with db() as con:
        row = con.execute("SELECT qr, logo FROM pay_methods WHERE id=?",
                          (mid,)).fetchone()
        if row:
            drop_upload(row["qr"])
            drop_upload(row["logo"])
        con.execute("DELETE FROM pay_methods WHERE id=?", (mid,))
    flash("Deleted.", "ok")
    return redirect(url_for("methods"))


@app.route("/methods/settings", methods=["POST"])
@login_required
def method_settings():
    CFG["biz_name"] = (request.form.get("biz_name") or "").strip() or "INTERNET"
    CFG["pay_note"] = (request.form.get("pay_note") or "").strip()
    save_cfg()
    flash("Settings saved.", "ok")
    return redirect(url_for("methods"))


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    from waitress import serve
    port = int(os.environ.get("PORT", CFG["port"]))
    host = os.environ.get("HOST", "0.0.0.0")
    print("Lending tracker on http://%s:%d" % (host, port))
    print("DB: %s" % DB_PATH)
    serve(app, host=host, port=port, threads=8)