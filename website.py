
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import stripe
from flask import (
    Flask, abort, flash, redirect, render_template,
    request, session, url_for
)
from database import connect, init_database

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "")

if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY muss gesetzt sein.")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(days=7),
)

SITE_URL = os.getenv("SITE_URL", "").rstrip("/")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "1564596575739846668")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
GUILD_ID = "1315795487063801916"
DISCORD_REDIRECT_URI = f"{SITE_URL}/oauth/callback"

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

_schema_ready = False


@app.before_request
def ensure_schema():
    global _schema_ready
    if not _schema_ready:
        init_database()
        _schema_ready = True


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


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


def discord_member(user_id):
    if not DISCORD_BOT_TOKEN:
        return None
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


def get_subscription(user_id):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM premium_subscriptions WHERE discord_user_id = %s",
            (int(user_id),),
        ).fetchone()


@app.route("/")
def index():
    return render_template("index.html")


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

    token_response = requests.post(
        "https://discord.com/api/v10/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if token_response.status_code != 200:
        abort(400, "Discord-Login fehlgeschlagen.")

    access_token = token_response.json()["access_token"]
    profile_response = requests.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if profile_response.status_code != 200:
        abort(400, "Discord-Profil konnte nicht geladen werden.")

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


@app.route("/logout", methods=["POST"])
def logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))

    member = discord_member(user["id"])
    sub = get_subscription(user["id"])
    active = bool(
        sub
        and sub["status"] == "active"
        and (
            sub["current_period_end"] is None
            or sub["current_period_end"] > datetime.now(timezone.utc)
        )
    )
    return render_template(
        "dashboard.html",
        user=user,
        is_member=member is not None,
        sub=sub,
        active=active,
    )


@app.route("/checkout", methods=["POST"])
def checkout():
    verify_csrf()
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))

    try:
        if not discord_member(user["id"]):
            flash("Du musst zuerst dem Blue-Discord-Server beitreten.", "error")
            return redirect(url_for("dashboard"))

        if not all([STRIPE_SECRET_KEY, STRIPE_PRICE_ID, SITE_URL]):
            abort(500, "Stripe ist noch nicht vollständig konfiguriert.")

        customer = stripe.Customer.create(
            name=user["username"],
            metadata={"discord_user_id": user["id"]},
        )
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer.id,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{SITE_URL}/dashboard?checkout=success",
            cancel_url=f"{SITE_URL}/dashboard?checkout=cancelled",
            client_reference_id=user["id"],
            metadata={"discord_user_id": user["id"]},
            subscription_data={
                "metadata": {"discord_user_id": user["id"]}
            },
        )
        return redirect(checkout_session.url, code=303)

    except requests.RequestException:
        app.logger.exception("Discord-Mitgliedschaft konnte nicht geprüft werden.")
        flash("Discord ist gerade nicht erreichbar. Bitte erneut versuchen.", "error")
    except stripe.StripeError:
        app.logger.exception("Stripe-Checkout fehlgeschlagen.")
        flash("Checkout konnte nicht gestartet werden.", "error")

    return redirect(url_for("dashboard"))


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
        flash("Deine Kündigung wurde bei Stripe vorgemerkt.", "success")
    except stripe.StripeError:
        app.logger.exception("Stripe-Kündigung fehlgeschlagen.")
        flash("Kündigung konnte nicht verarbeitet werden.", "error")

    return redirect(url_for("dashboard"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
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
        # Ereignisse nur einmal verarbeiten.
        inserted = conn.execute(
            """
            INSERT INTO processed_stripe_events (event_id)
            VALUES (%s)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            (event["id"],),
        ).fetchone()

        if not inserted:
            return {"received": True}

        if event_type == "checkout.session.completed":
            user_id = (
                (obj.get("metadata") or {}).get("discord_user_id")
                or obj.get("client_reference_id")
            )
            subscription_id = obj.get("subscription")

            if user_id and subscription_id:
                conn.execute(
                    """
                    INSERT INTO premium_subscriptions
                    (discord_user_id, stripe_customer_id,
                     stripe_subscription_id, status, updated_at)
                    VALUES (%s, %s, %s, 'inactive', NOW())
                    ON CONFLICT (discord_user_id) DO UPDATE SET
                      stripe_customer_id = EXCLUDED.stripe_customer_id,
                      stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                      status = 'inactive',
                      updated_at = NOW()
                    """,
                    (int(user_id), obj.get("customer"), subscription_id),
                )

        elif event_type.startswith("customer.subscription."):
            metadata = obj.get("metadata") or {}
            user_id = metadata.get("discord_user_id")
            sub_id = obj.get("id")

            if not user_id and sub_id:
                row = conn.execute(
                    """
                    SELECT discord_user_id FROM premium_subscriptions
                    WHERE stripe_subscription_id = %s
                    """,
                    (sub_id,),
                ).fetchone()
                user_id = row["discord_user_id"] if row else None

            if user_id and sub_id:
                period_end = obj.get("current_period_end")
                end_dt = (
                    datetime.fromtimestamp(period_end, timezone.utc)
                    if period_end else None
                )
                status = "active" if obj.get("status") == "active" else "inactive"

                conn.execute(
                    """
                    INSERT INTO premium_subscriptions
                    (discord_user_id, stripe_customer_id,
                     stripe_subscription_id, status, current_period_end,
                     cancel_at_period_end, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (discord_user_id) DO UPDATE SET
                      stripe_customer_id = EXCLUDED.stripe_customer_id,
                      stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                      status = EXCLUDED.status,
                      current_period_end = EXCLUDED.current_period_end,
                      cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                      updated_at = NOW()
                    """,
                    (
                        int(user_id), obj.get("customer"), sub_id, status,
                        end_dt, bool(obj.get("cancel_at_period_end")),
                    ),
                )

        elif event_type == "invoice.paid":
            sub_id = obj.get("subscription")
            if sub_id:
                conn.execute(
                    """
                    UPDATE premium_subscriptions
                    SET status = 'active', updated_at = NOW()
                    WHERE stripe_subscription_id = %s
                    """,
                    (sub_id,),
                )

        elif event_type == "invoice.payment_failed":
            sub_id = obj.get("subscription")
            if sub_id:
                conn.execute(
                    """
                    UPDATE premium_subscriptions
                    SET status = 'inactive', updated_at = NOW()
                    WHERE stripe_subscription_id = %s
                    """,
                    (sub_id,),
                )

    return {"received": True}
