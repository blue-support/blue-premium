
import os
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from dotenv import load_dotenv

load_dotenv()

import requests
import stripe

from database import connect, init_database

# ==================================================
# FLASK
# ==================================================

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "").strip()

if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY muss gesetzt sein.")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

# ==================================================
# 404 FEHLERSEITE
# ==================================================

@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


# ==================================================
# KONFIGURATION
# ==================================================

SITE_URL = os.getenv(
    "SITE_URL", "https://blue-premium.onrender.com"
).rstrip("/")

DISCORD_CLIENT_ID = os.getenv(
    "DISCORD_CLIENT_ID", "1564596575739846668"
).strip()

DISCORD_CLIENT_SECRET = os.getenv(
    "DISCORD_CLIENT_SECRET", ""
).strip()

DISCORD_BOT_TOKEN = os.getenv(
    "DISCORD_BOT_TOKEN", ""
).strip()

STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY", ""
).strip()

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET", ""
).strip()

# Regulärer monatlicher Preis: 1,99 €
STRIPE_MONTHLY_PRICE_ID = os.getenv(
    "STRIPE_MONTHLY_PRICE_ID", ""
).strip()

# Jährlicher Preis: 9,99 €
STRIPE_YEARLY_PRICE_ID = os.getenv(
    "STRIPE_YEARLY_PRICE_ID", ""
).strip()

# Stripe Promotion-Code-ID, nicht der sichtbare Gutscheincode
STRIPE_MONTHLY_PROMOTION_CODE_ID = os.getenv(
    "STRIPE_MONTHLY_PROMOTION_CODE_ID",
    "promo_1UVV3PIYLNbMfHD0JqX3hytH",
).strip()

GUILD_ID = "1315795487063801916"

DISCORD_REDIRECT_URI = f"{SITE_URL}/oauth/callback"

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

_schema_ready = False

# ==================================================
# DATENBANK INITIALISIEREN
# ==================================================

@app.before_request
def ensure_schema():
    global _schema_ready

    if _schema_ready:
        return

    init_database()

    # Tabelle für idempotente Stripe-Webhook-Verarbeitung.
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_stripe_events (
                event_id TEXT PRIMARY KEY,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Bestehende Installationen behalten ihre Daten.
        conn.execute("""
            ALTER TABLE premium_subscriptions
            ADD COLUMN IF NOT EXISTS stripe_price_id TEXT
        """)

    _schema_ready = True


# ==================================================
# SICHERHEITS-HEADER
# ==================================================

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ==================================================
# CSRF-SCHUTZ
# ==================================================

def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def verify_csrf():
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")

    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, "Ungültiges Formular-Token. Bitte Seite neu laden.")


# ==================================================
# DISCORD
# ==================================================

def discord_member(user_id):
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN ist nicht konfiguriert.")

    response = requests.get(
        f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        timeout=10,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code in (403, 404):
        return None

    response.raise_for_status()
    return None


# ==================================================
# ABONNEMENT AUS DER DATENBANK
# ==================================================

def get_subscription(user_id):
    with connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM premium_subscriptions
            WHERE discord_user_id = %s
            """,
            (int(user_id),),
        ).fetchone()


def subscription_is_active(sub):
    if not sub or sub["status"] != "active":
        return False

    period_end = sub["current_period_end"]

    if period_end is None:
        return True

    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)

    return period_end > datetime.now(timezone.utc)


# ==================================================
# STARTSEITE
# ==================================================

@app.route("/")
def index():
    return render_template("index.html")


# ==================================================
# DISCORD LOGIN
# ==================================================

@app.route("/login")
def login():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }

    return redirect(
        "https://discord.com/oauth2/authorize?" + urlencode(params)
    )


@app.route("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        flash("Discord-Login wurde abgebrochen.", "error")
        return redirect(url_for("index"))

    state = request.args.get("state", "")
    saved_state = session.pop("oauth_state", "")

    if not saved_state or not secrets.compare_digest(state, saved_state):
        abort(400, "Ungültiger OAuth-State.")

    code = request.args.get("code")

    if not code:
        abort(400, "Discord hat keinen OAuth-Code zurückgegeben.")

    try:
        token_response = requests.post(
            "https://discord.com/api/v10/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            timeout=10,
        )
        token_response.raise_for_status()

        access_token = token_response.json()["access_token"]

        profile_response = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        profile_response.raise_for_status()

    except requests.RequestException:
        app.logger.exception("Discord-OAuth fehlgeschlagen.")
        flash("Discord-Login fehlgeschlagen. Bitte erneut versuchen.", "error")
        return redirect(url_for("index"))

    user = profile_response.json()

    session.clear()
    session["discord_user"] = {
        "id": user["id"],
        "username": user.get("global_name") or user.get("username", "User"),
        "avatar": user.get("avatar"),
    }
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True

    return redirect(url_for("dashboard"))


# ==================================================
# LOGOUT
# ==================================================

@app.route("/logout", methods=["POST"])
def logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("index"))


# ==================================================
# DASHBOARD
# ==================================================

@app.route("/dashboard")
def dashboard():
    user = session.get("discord_user")

    if not user:
        return redirect(url_for("login"))

    try:
        member = discord_member(user["id"])
    except (requests.RequestException, RuntimeError):
        app.logger.exception(
            "Discord-Mitgliedschaft konnte nicht geprüft werden."
        )
        flash("Discord ist momentan nicht erreichbar.", "error")
        member = None

    sub = get_subscription(user["id"])
    active = subscription_is_active(sub)

    return render_template(
        "dashboard.html",
        user=user,
        is_member=member is not None,
        sub=sub,
        active=active,
    )


# ==================================================
# STRIPE CHECKOUT
# ==================================================

@app.route("/checkout", methods=["POST"])
def checkout():
    verify_csrf()

    user = session.get("discord_user")

    if not user:
        return redirect(url_for("login"))

    plan = request.form.get("plan", "monthly").strip().lower()

    if plan not in ("monthly", "yearly"):
        abort(400, "Ungültige Abo-Auswahl.")

    if not all([
        STRIPE_SECRET_KEY,
        SITE_URL,
        STRIPE_MONTHLY_PRICE_ID,
        STRIPE_YEARLY_PRICE_ID,
        STRIPE_MONTHLY_PROMOTION_CODE_ID,
    ]):
        app.logger.error("Stripe-Konfiguration ist unvollständig.")
        flash(
            "Der Checkout ist noch nicht vollständig konfiguriert.",
            "error",
        )
        return redirect(url_for("dashboard"))

    try:
        member = discord_member(user["id"])

        if not member:
            flash(
                "Du musst zuerst dem Blue-Discord-Server beitreten.",
                "error",
            )
            return redirect(url_for("dashboard"))

        existing_sub = get_subscription(user["id"])

        if subscription_is_active(existing_sub):
            flash(
                "Du hast bereits ein aktives Premium-Abo. "
                "Bitte kündige oder verwalte es zuerst.",
                "error",
            )
            return redirect(url_for("dashboard"))

        # Stripe-Kunden wiederverwenden, wenn bereits vorhanden.
        customer_id = (
            existing_sub["stripe_customer_id"]
            if existing_sub and existing_sub["stripe_customer_id"]
            else None
        )

        if not customer_id:
            customer = stripe.Customer.create(
                name=user["username"],
                metadata={"discord_user_id": user["id"]},
            )
            customer_id = customer.id

        price_id = (
            STRIPE_MONTHLY_PRICE_ID
            if plan == "monthly"
            else STRIPE_YEARLY_PRICE_ID
        )

        checkout_params = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [
                {"price": price_id, "quantity": 1}
            ],
            "success_url": f"{SITE_URL}/dashboard?checkout=success",
            "cancel_url": f"{SITE_URL}/dashboard?checkout=cancelled",
            "client_reference_id": user["id"],
            "metadata": {
                "discord_user_id": user["id"],
                "selected_plan": plan,
            },
            "subscription_data": {
                "metadata": {
                    "discord_user_id": user["id"],
                    "selected_plan": plan,
                }
            },
        }

        # Den festen Promotion Code ausschließlich auf das Monatsabo anwenden.
        if plan == "monthly":
            checkout_params["discounts"] = [
                {
                    "promotion_code": STRIPE_MONTHLY_PROMOTION_CODE_ID
                }
            ]

        checkout_session = stripe.checkout.Session.create(
            **checkout_params
        )

        return redirect(checkout_session.url, code=303)

    except requests.RequestException:
        app.logger.exception(
            "Discord-Mitgliedschaft konnte nicht geprüft werden."
        )
        flash(
            "Discord ist gerade nicht erreichbar. Bitte erneut versuchen.",
            "error",
        )

    except stripe.StripeError:
        app.logger.exception("Stripe-Checkout fehlgeschlagen.")
        flash(
            "Der Checkout konnte nicht gestartet werden. "
            "Bitte prüfe die Stripe-Konfiguration.",
            "error",
        )

    return redirect(url_for("dashboard"))


# ==================================================
# ABO KÜNDIGEN
# ==================================================

@app.route("/cancel", methods=["POST"])
def cancel():
    verify_csrf()

    user = session.get("discord_user")

    if not user:
        return redirect(url_for("login"))

    sub = get_subscription(user["id"])

    if not sub or not sub["stripe_subscription_id"]:
        flash("Es wurde kein Abo gefunden.", "error")
        return redirect(url_for("dashboard"))

    try:
        stripe.Subscription.modify(
            sub["stripe_subscription_id"],
            cancel_at_period_end=True,
        )

        flash(
            "Deine Kündigung wurde bei Stripe vorgemerkt. "
            "Dein Premium-Zugang bleibt bis zum Ende der bezahlten "
            "Laufzeit bestehen.",
            "success",
        )

    except stripe.StripeError:
        app.logger.exception("Stripe-Kündigung fehlgeschlagen.")
        flash("Die Kündigung konnte nicht verarbeitet werden.", "error")

    return redirect(url_for("dashboard"))


# ==================================================
# STRIPE WEBHOOK
# ==================================================

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        app.logger.error("Stripe-Webhook ist nicht konfiguriert.")
        abort(500)

    try:
        event = stripe.Webhook.construct_event(
            request.get_data(),
            request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )

    except (ValueError, stripe.SignatureVerificationError):
        abort(400)

    obj = event["data"]["object"]
    event_type = event["type"]

    with connect() as conn:
        inserted = conn.execute(
            """
            INSERT INTO processed_stripe_events (event_id)
            VALUES (%s)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            (event["id"],),
        ).fetchone()

        # Stripe darf Ereignisse erneut zustellen.
        if not inserted:
            return {"received": True}

        if event_type == "checkout.session.completed":
            user_id = (
                (obj.get("metadata") or {}).get("discord_user_id")
                or obj.get("client_reference_id")
            )
            subscription_id = obj.get("subscription")

            if user_id and subscription_id:
                # Der endgültige Status wird durch Subscription-Webhooks aktualisiert.
                conn.execute(
                    """
                    INSERT INTO premium_subscriptions
                    (
                        discord_user_id,
                        stripe_customer_id,
                        stripe_subscription_id,
                        stripe_price_id,
                        status,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'inactive', NOW())
                    ON CONFLICT (discord_user_id) DO UPDATE SET
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                        stripe_price_id = EXCLUDED.stripe_price_id,
                        status = 'inactive',
                        updated_at = NOW()
                    """,
                    (
                        int(user_id),
                        obj.get("customer"),
                        subscription_id,
                        None,
                    ),
                )

        elif event_type.startswith("customer.subscription."):
            metadata = obj.get("metadata") or {}
            user_id = metadata.get("discord_user_id")
            subscription_id = obj.get("id")

            if not user_id and subscription_id:
                row = conn.execute(
                    """
                    SELECT discord_user_id
                    FROM premium_subscriptions
                    WHERE stripe_subscription_id = %s
                    """,
                    (subscription_id,),
                ).fetchone()

                user_id = row["discord_user_id"] if row else None

            if user_id and subscription_id:
                period_end = obj.get("current_period_end")

                end_dt = (
                    datetime.fromtimestamp(period_end, timezone.utc)
                    if period_end
                    else None
                )

                stripe_status = obj.get("status", "inactive")
                status = (
                    "active"
                    if stripe_status == "active"
                    else "inactive"
                )

                price_id = None
                items = (obj.get("items") or {}).get("data") or []

                if items:
                    price_id = (
                        (items[0].get("price") or {}).get("id")
                    )

                conn.execute(
                    """
                    INSERT INTO premium_subscriptions
                    (
                        discord_user_id,
                        stripe_customer_id,
                        stripe_subscription_id,
                        stripe_price_id,
                        status,
                        current_period_end,
                        cancel_at_period_end,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (discord_user_id) DO UPDATE SET
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                        stripe_price_id = EXCLUDED.stripe_price_id,
                        status = EXCLUDED.status,
                        current_period_end = EXCLUDED.current_period_end,
                        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                        updated_at = NOW()
                    """,
                    (
                        int(user_id),
                        obj.get("customer"),
                        subscription_id,
                        price_id,
                        status,
                        end_dt,
                        bool(obj.get("cancel_at_period_end")),
                    ),
                )

        elif event_type == "invoice.paid":
            subscription_id = obj.get("subscription")

            # Bei neueren Stripe-API-Versionen kann die Subscription
            # unter parent.subscription_details.subscription liegen.
            if not subscription_id:
                parent = obj.get("parent") or {}
                details = parent.get("subscription_details") or {}
                subscription_id = details.get("subscription")

            if subscription_id:
                conn.execute(
                    """
                    UPDATE premium_subscriptions
                    SET status = 'active', updated_at = NOW()
                    WHERE stripe_subscription_id = %s
                      AND (
                          current_period_end IS NULL
                          OR current_period_end > NOW()
                      )
                    """,
                    (subscription_id,),
                )

        elif event_type == "invoice.payment_failed":
            subscription_id = obj.get("subscription")

            if not subscription_id:
                parent = obj.get("parent") or {}
                details = parent.get("subscription_details") or {}
                subscription_id = details.get("subscription")

            if subscription_id:
                conn.execute(
                    """
                    UPDATE premium_subscriptions
                    SET status = 'inactive', updated_at = NOW()
                    WHERE stripe_subscription_id = %s
                    """,
                    (subscription_id,),
                )

    return {"received": True}


# ==================================================
# LOKALER START
# ==================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
