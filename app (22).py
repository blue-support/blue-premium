import asyncio
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import discord
import requests
import stripe
from discord.ext import commands, tasks
from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for

# ============================================================
# KONFIGURATION – HIER EINTRAGEN (KEINE ECHTEN KEYS COMMITTEN!)
# ============================================================

PREMIUM_BASE_URL = ""

DISCORD_BOT_TOKEN = "MTU2NDU5NjU3NTczOTg0NjY2OA.GfEVWJ.e6JVFVJ1e3FdqDrKNTF3DvNldAhalQzW3OazyA"
DISCORD_CLIENT_ID = "1564596575739846668"
DISCORD_CLIENT_SECRET = "4Ds9OaWXpBUirsLdHnTBB06ns_lbmd3z"

DISCORD_GUILD_ID = 1315795487063801916           # Server-ID als Zahl
DISCORD_PREMIUM_ROLE_ID = 1508600436519731320     # Premium-Rollen-ID als Zahl
DISCORD_LOG_CHANNEL_ID = 1564597181476904991       # Premium-Abo-Logs-Kanal-ID als Zahl

STRIPE_SECRET_KEY = "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"
STRIPE_WEBHOOK_SECRET = "whsec_iTY9wjHk4tUBv9afa05Sry0mhnAgmOAy"
FLASK_SECRET_KEY = "1234567890-kuba1447pl1"

PREMIUM_PRICE_CENTS = 99
DATABASE_PATH = "blue_premium.sqlite3"
WEB_PORT = int(os.environ.get("PORT", "10000"))
DISCORD_API = "https://discord.com/api/v10"

# Alle Premium-Features zentral bearbeiten. Diese Liste wird auf der Website
# angezeigt. Die eigentliche Freischaltung der Bot-Funktionen muss dein Bot-Code
# anhand des aktiven Premium-Status bzw. der Premium-Rolle prüfen.
PREMIUM_FEATURES = [
    {
        "name": "Exklusive Premium-Befehle",
        "description": "Zugriff auf ausgewählte Premium-Kommandos des Blue-Bots.",
        "icon": "⚡",
    },
    {
        "name": "Premium-Rolle",
        "description": "Automatische Premium-Rolle auf dem offiziellen Discord-Server.",
        "icon": "👑",
    },
    {
        "name": "Premium-Erweiterungen",
        "description": "Platz für weitere Vorteile, die du in dieser Liste ergänzen kannst.",
        "icon": "✨",
    },
]

# ============================================================
# APP / BOT
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("blue-premium")

app = Flask(__name__, template_folder=".")
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PREMIUM_BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
)

stripe.api_key = STRIPE_SECRET_KEY
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
bot_loop = None
bot_avatar_url = None
db_lock = threading.Lock()

# ============================================================
# DATENBANK
# ============================================================

def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                discord_user_id TEXT PRIMARY KEY,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'inactive',
                current_period_end INTEGER,
                cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                processed_at INTEGER NOT NULL
            )
        """)
        conn.commit()


def get_subscription(discord_user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE discord_user_id = ?",
            (str(discord_user_id),),
        ).fetchone()
        return dict(row) if row else None


def save_subscription(discord_user_id, customer_id=None, subscription_id=None,
                      status="inactive", period_end=None, cancel_at_period_end=False):
    now = int(time.time())
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO subscriptions
              (discord_user_id, stripe_customer_id, stripe_subscription_id,
               status, current_period_end, cancel_at_period_end, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
              stripe_customer_id = COALESCE(excluded.stripe_customer_id, subscriptions.stripe_customer_id),
              stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, subscriptions.stripe_subscription_id),
              status = excluded.status,
              current_period_end = excluded.current_period_end,
              cancel_at_period_end = excluded.cancel_at_period_end,
              updated_at = excluded.updated_at
        """, (
            str(discord_user_id), customer_id, subscription_id, status,
            period_end, int(bool(cancel_at_period_end)), now,
        ))
        conn.commit()


def event_already_processed(event_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)
        ).fetchone() is not None


def mark_event_processed(event_id):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events(event_id, processed_at) VALUES (?, ?)",
            (event_id, int(time.time())),
        )
        conn.commit()


# ============================================================
# DISCORD REST: MITGLIEDSCHAFT, ROLLE UND LOGS
# ============================================================

def bot_headers():
    return {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}


def discord_member_exists(discord_user_id):
    if not DISCORD_GUILD_ID or not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.startswith("HIER_"):
        return False
    try:
        response = requests.get(
            f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}",
            headers=bot_headers(), timeout=12,
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        logger.error("Mitgliedschaftsprüfung fehlgeschlagen: %s %s",
                     response.status_code, response.text[:500])
        return False
    except requests.RequestException:
        logger.exception("Discord-Mitgliedschaftsprüfung fehlgeschlagen")
        return False


def discord_set_premium_role(discord_user_id, enabled):
    if not all([DISCORD_GUILD_ID, DISCORD_PREMIUM_ROLE_ID]):
        logger.error("Guild-ID oder Premium-Rollen-ID fehlt.")
        return False, "Die Discord-Serverkonfiguration ist unvollständig."
    if not discord_member_exists(discord_user_id):
        return False, "Du bist nicht auf dem erforderlichen Discord-Server oder der Bot kann deine Mitgliedschaft nicht prüfen."
    url = (f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/members/{discord_user_id}"
           f"/roles/{DISCORD_PREMIUM_ROLE_ID}")
    try:
        response = requests.request(
            "PUT" if enabled else "DELETE", url,
            headers=bot_headers(), timeout=12,
        )
        if response.status_code in (200, 204):
            return True, None
        logger.error("Rollenänderung fehlgeschlagen: %s %s",
                     response.status_code, response.text[:500])
        return False, "Discord konnte die Premium-Rolle nicht ändern. Prüfe Bot-Rang und „Rollen verwalten“."
    except requests.RequestException:
        logger.exception("Discord-Rollenänderung fehlgeschlagen")
        return False, "Discord ist momentan nicht erreichbar."


def send_discord_log(title, description, color=0x5865F2, fields=None):
    if not DISCORD_LOG_CHANNEL_ID or not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.startswith("HIER_"):
        logger.info("Discord-Log nicht gesendet (Kanal/Token nicht konfiguriert): %s", title)
        return
    embed = {
        "title": title[:256],
        "description": description[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Powered by Blue ⚡"},
    }
    if bot_avatar_url:
        embed["footer"]["icon_url"] = bot_avatar_url
    if fields:
        embed["fields"] = [
            {"name": str(name)[:256], "value": str(value)[:1024], "inline": bool(inline)}
            for name, value, inline in fields[:25]
        ]
    try:
        response = requests.post(
            f"{DISCORD_API}/channels/{DISCORD_LOG_CHANNEL_ID}/messages",
            headers=bot_headers(), json={"embeds": [embed]}, timeout=12,
        )
        if response.status_code not in (200, 201):
            logger.error("Discord-Logging fehlgeschlagen: %s %s",
                         response.status_code, response.text[:500])
    except requests.RequestException:
        logger.exception("Discord-Logging fehlgeschlagen")


def sync_role_and_log(discord_user_id, enabled, reason):
    ok, error = discord_set_premium_role(discord_user_id, enabled)
    if ok:
        send_discord_log(
            "Premium-Rolle vergeben" if enabled else "Premium-Rolle entfernt",
            f"**Discord-Nutzer:** <@{discord_user_id}> (`{discord_user_id}`)\n**Grund:** {reason}",
            0x2ECC71 if enabled else 0xE67E22,
        )
    else:
        send_discord_log(
            "Fehler bei Premium-Rolle",
            f"**Discord-Nutzer:** <@{discord_user_id}> (`{discord_user_id}`)\n"
            f"**Aktion:** {'Vergeben' if enabled else 'Entfernen'}\n**Fehler:** {error or 'Unbekannt'}",
            0xE74C3C,
        )
    return ok, error


# ============================================================
# HTML
# ============================================================

PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0d14">
<title>Blue Premium – Mehr aus deinem Discord</title>
<meta name="description" content="Blue Premium für 0,99 € im Monat. Exklusive Bot-Funktionen und automatische Rollenverwaltung.">
<style>
:root{color-scheme:dark;--bg:#090b12;--panel:#121725;--panel2:#171e30;--blue:#6574ff;--cyan:#73e0ff;--text:#f6f7ff;--muted:#aab3ca;--line:#ffffff15}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -15%,#323d91 0,transparent 48%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}a{color:inherit;text-decoration:none}.wrap{width:min(1120px,calc(100% - 36px));margin:auto}
header{height:84px;display:flex;align-items:center;justify-content:space-between;gap:18px;border-bottom:1px solid var(--line)}.brand{font-size:21px;font-weight:900;letter-spacing:-.7px}.logo{display:inline-grid;place-items:center;width:39px;height:39px;border-radius:13px;margin-right:8px;background:linear-gradient(135deg,var(--blue),#a36bff);box-shadow:0 8px 30px #6574ff40}.nav{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:12px 17px;border:1px solid var(--line);border-radius:12px;font-weight:750;transition:.2s;cursor:pointer}.btn:hover{transform:translateY(-2px);filter:brightness(1.1)}.primary{background:linear-gradient(135deg,#5865f2,#8870ff);border:0;box-shadow:0 8px 28px #5865f233}.ghost{background:#ffffff08}.muted{color:var(--muted)}.hero{text-align:center;padding:86px 0 58px}.pill{display:inline-flex;gap:8px;align-items:center;border:1px solid #8c98ff55;border-radius:99px;padding:8px 13px;background:#6574ff18;color:#d6daff;font-size:12px;font-weight:850;letter-spacing:1px;text-transform:uppercase}.hero h1{font-size:clamp(42px,7vw,76px);line-height:1.03;letter-spacing:-3.8px;max-width:900px;margin:24px auto 20px}.gradient{background:linear-gradient(90deg,#b9c0ff,#78e2ff);background-clip:text;-webkit-background-clip:text;color:transparent}.intro{max-width:680px;margin:auto;color:var(--muted);line-height:1.8;font-size:17px}.price{font-size:55px;font-weight:950;letter-spacing:-2px;margin:27px 0 3px}.price small{font-size:15px;letter-spacing:0;color:var(--muted);font-weight:600}.actions{display:flex;justify-content:center;gap:12px;flex-wrap:wrap;margin:26px 0 16px}.trust{font-size:13px;line-height:2;color:#c4cbe0}.notice{padding:15px 18px;border-radius:13px;margin:20px 0;background:#ffb84a12;border:1px solid #ffb84a50;color:#ffdda3;line-height:1.6}.notice.ok{background:#36d99b12;border-color:#36d99b50;color:#b5f7d7}.section{padding:45px 0}.heading{text-align:center;margin-bottom:30px}.heading h2{font-size:clamp(29px,4vw,43px);letter-spacing:-1.5px;margin:13px 0}.heading p{color:var(--muted);line-height:1.7}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.feature,.step{padding:25px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(145deg,var(--panel2),var(--panel))}.ico{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:#6574ff20;color:#cbd0ff;font-size:21px;margin-bottom:17px}.feature h3,.step h3{margin:0 0 10px}.feature p,.step p{margin:0;color:var(--muted);line-height:1.75}.buybox{margin:22px 0;padding:28px;border-radius:20px;border:1px solid #7785ff70;background:linear-gradient(120deg,#6574ff19,#73e0ff08);display:flex;align-items:center;justify-content:space-between;gap:24px}.buybox p{color:var(--muted);line-height:1.7;max-width:620px}.steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.num{font-size:12px;font-weight:900;color:#b8c0ff;margin-bottom:18px}.faq{max-width:780px;margin:auto}details{padding:19px;margin:12px 0;border:1px solid var(--line);border-radius:14px;background:var(--panel)}summary{cursor:pointer;font-weight:750}details p{color:var(--muted);line-height:1.75;margin-bottom:0}footer{border-top:1px solid var(--line);padding:28px 0;color:var(--muted);font-size:13px;margin-top:40px}.foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media(max-width:760px){.grid{grid-template-columns:1fr}.steps{grid-template-columns:1fr 1fr}.hero{padding:55px 0 35px}.hero h1{letter-spacing:-2px}.buybox{flex-direction:column;align-items:stretch}header{height:auto;padding:18px 0;align-items:flex-start;flex-direction:column}}@media(max-width:460px){.steps{grid-template-columns:1fr}.price{font-size:47px}}
</style>
</head>
<body><div class="wrap">
<header><a class="brand" href="/"><span class="logo">⚡</span>Blue <span class="gradient">Premium</span></a>
<nav class="nav">
{% if user %}<span class="muted">Angemeldet als {{ user.username|e }}</span><a class="btn ghost" href="/manage">Abo verwalten</a><a class="btn ghost" href="/logout">Abmelden</a>
{% else %}<a class="btn ghost" href="/login">Mit Discord anmelden</a>{% endif %}
</nav></header>
{% if error %}<div class="notice">⚠️ {{ error }}</div>{% endif %}
{% if success %}<div class="notice ok">✓ {{ success }}</div>{% endif %}
<main>
<section class="hero"><span class="pill">✦ Das Blue-Upgrade</span>
<h1>Dein Discord.<br><span class="gradient">Deine Premium-Vorteile.</span></h1>
<p class="intro">Schalte exklusive Blue-Bot-Funktionen frei. Einfach mit Discord anmelden, abonnieren und die Premium-Rolle automatisch erhalten.</p>
<div class="price">0,99 € <small>/ Monat</small></div><div class="muted">Monatliche Abrechnung · Jederzeit kündbar</div>
<div class="actions">{% if user %}<a class="btn primary" href="/checkout">⚡ Premium abonnieren</a><a class="btn ghost" href="/manage">Abo verwalten</a>{% else %}<a class="btn primary" href="/login">Mit Discord fortfahren →</a>{% endif %}</div>
<div class="trust">✓ Sichere Zahlungsabwicklung über Stripe<br>✓ Mitgliedschaft auf dem Blue-Discord-Server erforderlich<br>✓ Premium-Rolle nach bestätigter Zahlung</div></section>
<section class="section"><div class="heading"><span class="pill">Deine Vorteile</span><h2>Das steckt in Premium</h2><p>Die Feature-Liste wird direkt aus der app.py geladen.</p></div>
<div class="grid">{% for feature in features %}<article class="feature"><div class="ico">{{ feature.icon|e }}</div><h3>{{ feature.name|e }}</h3><p>{{ feature.description|e }}</p></article>{% endfor %}</div>
<div class="buybox"><div><h3>Bereit für Blue Premium?</h3><p>0,99 € pro Monat. Du musst Mitglied des zugehörigen Discord-Servers sein. Ohne bestätigte Mitgliedschaft kannst du den Kauf nicht starten.</p></div>{% if user %}<a class="btn primary" href="/checkout">Jetzt abonnieren →</a>{% else %}<a class="btn primary" href="/login">Discord Login →</a>{% endif %}</div></section>
<section class="section"><div class="heading"><span class="pill">Einfach & transparent</span><h2>In vier Schritten zu Premium</h2></div><div class="steps">
<article class="step"><div class="num">SCHRITT 01</div><h3>Discord-Login</h3><p>Dein Discord-Konto wird über OAuth2 authentifiziert.</p></article>
<article class="step"><div class="num">SCHRITT 02</div><h3>Mitgliedschaft prüfen</h3><p>Vor dem Checkout prüfen wir, ob du auf dem erforderlichen Server bist.</p></article>
<article class="step"><div class="num">SCHRITT 03</div><h3>Sicher bezahlen</h3><p>Stripe verarbeitet dein monatliches Abo für 0,99 €.</p></article>
<article class="step"><div class="num">SCHRITT 04</div><h3>Rolle erhalten</h3><p>Nach bestätigter Zahlung wird deine Premium-Rolle synchronisiert.</p></article>
</div></section>
<section class="section"><div class="heading"><span class="pill">FAQ</span><h2>Häufige Fragen</h2></div><div class="faq">
<details><summary>Warum muss ich Mitglied des Servers sein?</summary><p>Die Premium-Funktionen werden über eine Discord-Rolle freigeschaltet. Ohne Mitgliedschaft ist kein Kauf möglich.</p></details>
<details><summary>Was kostet Premium?</summary><p>0,99 € pro Monat. Das Abonnement verlängert sich monatlich, bis du es kündigst.</p></details>
<details><summary>Wie kündige ich?</summary><p>Öffne „Abo verwalten“, um das Stripe-Kundenportal aufzurufen und dein Abonnement zu kündigen.</p></details>
<details><summary>Wann wird die Rolle entfernt?</summary><p>Wenn das Abonnement tatsächlich endet oder Stripe den Status auf inaktiv setzt. Bei einer Kündigung zum Periodenende bleibt Premium bis zum Ende des bezahlten Zeitraums aktiv.</p></details>
<details><summary>Was passiert, wenn die Rolle nicht vergeben werden kann?</summary><p>Die Website zeigt eine Fehlermeldung und protokolliert den Fehler. Eine erfolgreiche Checkout-Rückleitung allein gilt nicht als Bestätigung der Premium-Aktivierung.</p></details>
</div></section>
</main><footer><div class="foot"><span>© Blue Premium · Powered by Blue ⚡</span><span>0,99 € monatlich · Zahlungsabwicklung durch Stripe</span></div></footer>
</div></body></html>"""


# ============================================================
# FLASK ROUTES
# ============================================================

def redirect_error(message):
    return redirect(url_for("premium_page", error=message))


def logged_in_user():
    return session.get("user")


@app.route("/")
@app.route("/premium-abo")
def premium_page():
    messages = {
        "login": "Du bist jetzt mit Discord angemeldet.",
        "checkout": "Stripe hat den Checkout abgeschlossen. Die Premium-Aktivierung erfolgt nach bestätigter Stripe-Zahlung.",
    }
    return render_template(
        "index.html", user=logged_in_user(), features=PREMIUM_FEATURES,
        error={"checkout_cancelled": "Der Checkout wurde abgebrochen. Es wurde nichts berechnet."}.get(
            request.args.get("error"), request.args.get("error")
        ), success=messages.get(request.args.get("success")),
    )


@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID or DISCORD_CLIENT_ID.startswith("HIER_"):
        return "Discord OAuth ist noch nicht konfiguriert.", 500
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": PREMIUM_BASE_URL.rstrip("/") + "/auth/discord/callback",
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


@app.route("/auth/discord/callback")
def discord_callback():
    if request.args.get("error"):
        return redirect_error("Discord-Login wurde abgebrochen.")
    code = request.args.get("code")
    state = request.args.get("state")
    expected = session.pop("oauth_state", None)
    if not code or not expected or not secrets.compare_digest(state or "", expected):
        abort(400, "Ungültige OAuth-Anfrage.")
    try:
        token_response = requests.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": PREMIUM_BASE_URL.rstrip("/") + "/auth/discord/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        user_response = requests.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        user_response.raise_for_status()
        discord_user = user_response.json()
        session.clear()
        session.permanent = True
        session["user"] = {
            "id": str(discord_user["id"]),
            "username": discord_user.get("global_name") or discord_user["username"],
        }
        return redirect(url_for("premium_page", success="login"))
    except (requests.RequestException, KeyError, ValueError):
        logger.exception("Discord-OAuth fehlgeschlagen")
        return redirect_error("Discord-Login fehlgeschlagen. Bitte versuche es erneut.")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("premium_page"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    user = logged_in_user()
    if not user:
        return redirect(url_for("login"))
    if not DISCORD_GUILD_ID or not DISCORD_PREMIUM_ROLE_ID:
        return redirect_error("Das Premium-System ist noch nicht vollständig konfiguriert.")
    if not discord_member_exists(user["id"]):
        send_discord_log(
            "Premium-Kauf blockiert",
            f"<@{user['id']}> (`{user['id']}`) wollte abonnieren, ist aber kein bestätigtes Servermitglied.",
            0xE67E22,
        )
        return redirect_error("Du musst zuerst Mitglied auf dem Blue-Discord-Server werden. Es wurde nichts gekauft oder berechnet.")
    if not STRIPE_SECRET_KEY or STRIPE_SECRET_KEY.startswith("sk_live_HIER_"):
        return redirect_error("Stripe ist noch nicht konfiguriert.")
    try:
        existing = get_subscription(user["id"])
        if existing and existing.get("status") in ("active", "trialing"):
            return redirect_error("Für dein Konto ist bereits ein aktives Premium-Abo hinterlegt.")
        customer_id = existing.get("stripe_customer_id") if existing else None
        params = {
            "mode": "subscription",
            "line_items": [{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": PREMIUM_PRICE_CENTS,
                    "recurring": {"interval": "month"},
                    "product_data": {
                        "name": "Blue Premium",
                        "description": "Blue Premium – monatliches Abonnement",
                    },
                },
                "quantity": 1,
            }],
            "client_reference_id": user["id"],
            "metadata": {"discord_user_id": user["id"]},
            "subscription_data": {"metadata": {"discord_user_id": user["id"]}},
            "success_url": PREMIUM_BASE_URL.rstrip("/") + "/success?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": PREMIUM_BASE_URL.rstrip("/") + "/premium-abo?error=checkout_cancelled",
        }
        if customer_id:
            params["customer"] = customer_id
        # Stripe erstellt den Customer bei Bedarf automatisch für das Abo.
        checkout_session = stripe.checkout.Session.create(**params)
        send_discord_log(
            "Premium-Checkout gestartet",
            f"<@{user['id']}> (`{user['id']}`) hat den Stripe-Checkout für 0,99 €/Monat gestartet.",
            0x5865F2,
        )
        return redirect(checkout_session.url, code=303)
    except Exception:
        logger.exception("Stripe Checkout fehlgeschlagen")
        return redirect_error("Der Checkout konnte nicht gestartet werden. Bitte später erneut versuchen.")


@app.route("/success")
def checkout_success():
    user = logged_in_user()
    if not user:
        return redirect(url_for("login"))
    session_id = request.args.get("session_id", "")
    if not session_id or not session_id.startswith("cs_"):
        return redirect_error("Ungültige Checkout-Sitzung.")
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        if str(checkout_session.get("client_reference_id") or "") != str(user["id"]):
            abort(403, "Diese Checkout-Sitzung gehört nicht zu deinem Konto.")
        # Keine Rolle auf der Success-Seite vergeben. Ausschließlich Webhooks aktivieren Premium.
        return redirect(url_for("premium_page", success="checkout"))
    except stripe.error.StripeError:
        logger.exception("Stripe Checkout-Status konnte nicht gelesen werden")
        return redirect_error("Der Zahlungsstatus konnte nicht geprüft werden. Bitte warte kurz und lade die Seite neu.")


@app.route("/manage")
def manage():
    user = logged_in_user()
    if not user:
        return redirect(url_for("login"))
    sub = get_subscription(user["id"])
    if not sub or not sub.get("stripe_customer_id"):
        return redirect_error("Für dein Konto wurde noch kein Stripe-Abo gefunden.")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url=PREMIUM_BASE_URL.rstrip("/") + "/premium-abo",
        )
        return redirect(portal.url, code=303)
    except stripe.error.StripeError:
        logger.exception("Stripe Customer Portal fehlgeschlagen")
        return redirect_error("Die Abo-Verwaltung ist momentan nicht erreichbar.")


# ============================================================
# STRIPE WEBHOOK – SIGNATURPRÜFUNG + IDEMPOTENZ
# ============================================================

def get_subscription_user_id(subscription):
    metadata = subscription.get("metadata") or {}
    return str(metadata.get("discord_user_id") or "")


def process_subscription(subscription, event_name):
    user_id = get_subscription_user_id(subscription)
    if not user_id:
        logger.error("Discord-ID fehlt in Stripe-Abo-Metadaten: %s", subscription.get("id"))
        send_discord_log("Stripe-Zuordnung fehlt",
                         f"Für Stripe-Abo `{subscription.get('id')}` fehlt die Discord-Nutzer-ID.",
                         0xE74C3C)
        return
    status = str(subscription.get("status") or "inactive")
    customer_id = subscription.get("customer")
    period_end = subscription.get("current_period_end")
    cancel_at_period_end = bool(subscription.get("cancel_at_period_end", False))
    save_subscription(
        user_id, str(customer_id) if customer_id else None,
        str(subscription.get("id")) if subscription.get("id") else None,
        status, int(period_end) if period_end else None, cancel_at_period_end,
    )

    # Bei Kündigung zum Periodenende bleibt die Rolle bis zum tatsächlichen Ende.
    is_active = status in ("active", "trialing")
    if period_end and int(period_end) <= int(time.time()):
        is_active = False
    ok, err = sync_role_and_log(user_id, is_active, f"Stripe-Ereignis: {event_name}; Status: {status}")
    if not ok:
        # Webhook darf fehlschlagen, damit Stripe erneut zustellt.
        raise RuntimeError(err or "Discord-Rolle konnte nicht synchronisiert werden")

    send_discord_log(
        "Premium-Abo aktualisiert",
        f"**Nutzer:** <@{user_id}> (`{user_id}`)\n"
        f"**Status:** `{status}`\n"
        f"**Kündigung zum Periodenende:** {'Ja' if cancel_at_period_end else 'Nein'}\n"
        f"**Abo-Ende (Unix):** `{period_end or 'unbekannt'}`",
        0x2ECC71 if is_active else 0xE67E22,
    )


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET or STRIPE_WEBHOOK_SECRET.startswith("whsec_HIER_"):
        return jsonify(error="Webhook ist nicht konfiguriert"), 500
    try:
        event = stripe.Webhook.construct_event(
            payload=request.get_data(),
            sig_header=request.headers.get("Stripe-Signature", ""),
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return jsonify(error="Ungültiger Request-Body"), 400
    except stripe.error.SignatureVerificationError:
        return jsonify(error="Ungültige Stripe-Signatur"), 400

    event_id = event["id"]
    if event_already_processed(event_id):
        return jsonify(received=True, duplicate=True), 200

    event_type = event["type"]
    data = event["data"]["object"]
    try:
        if event_type == "checkout.session.completed":
            # Checkout allein aktiviert Premium nicht, falls die Zahlung noch offen ist.
            if data.get("mode") == "subscription":
                subscription_id = data.get("subscription")
                if subscription_id and data.get("payment_status") in ("paid", "no_payment_required"):
                    sub = stripe.Subscription.retrieve(subscription_id)
                    process_subscription(sub, event_type)

        elif event_type == "invoice.paid":
            subscription_id = data.get("subscription")
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                process_subscription(sub, event_type)

        elif event_type == "invoice.payment_failed":
            subscription_id = data.get("subscription")
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                user_id = get_subscription_user_id(sub)
                if user_id:
                    save_subscription(
                        user_id,
                        str(sub.get("customer")) if sub.get("customer") else None,
                        str(sub.get("id")) if sub.get("id") else None,
                        "past_due",
                        int(sub.get("current_period_end")) if sub.get("current_period_end") else None,
                        bool(sub.get("cancel_at_period_end", False)),
                    )
                    ok, err = sync_role_and_log(
                        user_id, False, "Stripe-Zahlung fehlgeschlagen"
                    )
                    send_discord_log(
                        "Premium-Zahlung fehlgeschlagen",
                        f"<@{user_id}> (`{user_id}`): Stripe meldet eine fehlgeschlagene Zahlung. "
                        f"Premium-Rolle entfernt. Rollensynchronisierung: {'OK' if ok else err}",
                        0xE67E22 if ok else 0xE74C3C,
                    )
                    if not ok:
                        raise RuntimeError(err or "Premium-Rolle konnte nach fehlgeschlagener Zahlung nicht entfernt werden")

        elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
            process_subscription(data, event_type)

        # Andere Stripe-Events werden absichtlich ignoriert.
        mark_event_processed(event_id)
        return jsonify(received=True), 200
    except Exception:
        logger.exception("Stripe-Webhook konnte nicht verarbeitet werden: %s", event_type)
        # Nicht als verarbeitet markieren: Stripe soll bei Fehlern erneut zustellen.
        return jsonify(error="Webhook-Verarbeitung fehlgeschlagen"), 500


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


# ============================================================
# PERIODISCHE ABLAUFKONTROLLE
# ============================================================

@tasks.loop(minutes=10)
async def reconcile_expired_subscriptions():
    now = int(time.time())
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT discord_user_id, status, current_period_end
            FROM subscriptions
            WHERE status IN ('active', 'trialing')
              AND current_period_end IS NOT NULL
              AND current_period_end <= ?
        """, (now,)).fetchall()
    for row in rows:
        user_id = row["discord_user_id"]
        try:
            ok, error = await asyncio.to_thread(
                sync_role_and_log, user_id, False, "Abonnementzeitraum abgelaufen"
            )
            if ok:
                with db_connect() as conn:
                    conn.execute("""
                        UPDATE subscriptions SET status='expired', updated_at=?
                        WHERE discord_user_id=?
                    """, (now, user_id))
                    conn.commit()
            else:
                logger.error("Ablauf-Synchronisierung für %s fehlgeschlagen: %s", user_id, error)
        except Exception:
            logger.exception("Ablaufprüfung fehlgeschlagen für %s", user_id)


@reconcile_expired_subscriptions.before_loop
async def before_reconcile():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    global bot_loop, bot_avatar_url
    bot_loop = asyncio.get_running_loop()
    if bot.user:
        bot_avatar_url = bot.user.display_avatar.url
    logger.info("Blue Premium Bot bereit als %s", bot.user)
    if not reconcile_expired_subscriptions.is_running():
        reconcile_expired_subscriptions.start()


def validate_configuration():
    missing = []
    for name, value in [
        ("PREMIUM_BASE_URL", PREMIUM_BASE_URL),
        ("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN),
        ("DISCORD_CLIENT_ID", DISCORD_CLIENT_ID),
        ("DISCORD_CLIENT_SECRET", DISCORD_CLIENT_SECRET),
        ("DISCORD_GUILD_ID", DISCORD_GUILD_ID),
        ("DISCORD_PREMIUM_ROLE_ID", DISCORD_PREMIUM_ROLE_ID),
        ("DISCORD_LOG_CHANNEL_ID", DISCORD_LOG_CHANNEL_ID),
        ("STRIPE_SECRET_KEY", STRIPE_SECRET_KEY),
        ("STRIPE_WEBHOOK_SECRET", STRIPE_WEBHOOK_SECRET),
        ("FLASK_SECRET_KEY", FLASK_SECRET_KEY),
    ]:
        if not value or (isinstance(value, str) and
                         (value.startswith("HIER_") or value == "https://DEINE-PREMIUM-DOMAIN")):
            missing.append(name)
    if missing:
        logger.warning("Bitte Konfiguration in app.py ergänzen: %s", ", ".join(missing))


def run_website():
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", WEB_PORT, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    init_db()
    validate_configuration()
    website_thread = threading.Thread(target=run_website, name="blue-premium-web", daemon=True)
    website_thread.start()
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.startswith("HIER_"):
        logger.error("Bot-Token fehlt. Die Website startet, aber Discord-Rollen/Logs und Ablaufprüfung benötigen den Bot.")
        while True:
            time.sleep(3600)
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
