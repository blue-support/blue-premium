
import os
import secrets
import logging
from datetime import datetime, timezone

import requests
import stripe
from flask import (
    Flask, render_template, redirect, request,
    session, url_for, flash, abort
)
from urllib.parse import urlencode
from database import connect, init_database

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

# Muss mit app.py übereinstimmen.
GUILD_ID = 1315795487063801916
GUILD_PREMIUM_ABO_ROLE_ID = 1508600436519731320

PRICE_EUR = "0.99"

PREMIUM_FEATURES = [
    {
        "name": "Exklusive Bot-Befehle",
        "description": "Spezielle Premium-Befehle.",
    },
    {
        "name": "Erweiterte Einstellungen",
        "description": "Mehr Möglichkeiten zur Anpassung.",
    },
    {
        "name": "Premium-Badge",
        "description": "Exklusiver Premium-Status.",
    },
    {
        "name": "Priorisierter Support",
        "description": "Bevorzugte Unterstützung.",
    },
]

DISCORD_API = "https://discord.com/api/v10"
logging.basicConfig(level=logging.INFO)


def env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Konfiguration fehlt: {name}")
    return value


def current_user():
    return session.get("user")


def subscription_for(user_id):
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM premium_subscriptions
               WHERE discord_user_id = %s""",
            (int(user_id),),
        ).fetchone()


def discord_member_exists(user_id):
    """Servermitgliedschaft mit Bot-Token überprüfen."""
    response = requests.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/members/{user_id}",
        headers={"Authorization": f"Bot {env('DISCORD_BOT_TOKEN')}"},
        timeout=10,
    )

    if response.status_code == 404:
        return False

    response.raise_for_status()
    return True


def get_oauth_user(code):
    response = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": env("DISCORD_CLIENT_ID"),
            "client_secret": env("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": env("DISCORD_REDIRECT_URI"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    response.raise_for_status()
    token = response.json()["access_token"]

    response = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


@app.before_request
def initialize():
    init_database()


@app.get("/")
def index():
    user = current_user()
    sub = subscription_for(user["id"]) if user else None
    return render_template(
        "index.html",
        user=user,
        sub=sub,
        features=PREMIUM_FEATURES,
        price=PRICE_EUR,
    )


@app.get("/login")
def login():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": env("DISCORD_CLIENT_ID"),
        "redirect_uri": env("DISCORD_REDIRECT_URI"),
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }

    return redirect(
        "https://discord.com/oauth2/authorize?"
        + urlencode(params)
    )


@app.get("/auth/discord/callback")
def discord_callback():
    state = request.args.get("state")
    expected = session.pop("oauth_state", None)

    if not expected or not state or not secrets.compare_digest(
        state, expected
    ):
        abort(400, "Ungültige OAuth-Anfrage.")

    if request.args.get("error"):
        flash("Discord-Login wurde abgebrochen.", "error")
        return redirect(url_for("index"))

    try:
        user = get_oauth_user(request.args["code"])
    except (requests.RequestException, KeyError):
        logging.exception("Discord OAuth fehlgeschlagen")
        abort(502, "Discord-Login gerade nicht verfügbar.")

    session.clear()
    session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "global_name": user.get("global_name") or user["username"],
    }

    return redirect(url_for("dashboard"))


@app.get("/dashboard")
def dashboard():
    user = current_user()

    if not user:
        return redirect(url_for("login"))

    try:
        is_member = discord_member_exists(user["id"])
    except (requests.RequestException, RuntimeError):
        logging.exception("Mitgliedschaft konnte nicht geprüft werden")
        is_member = None

    sub = subscription_for(user["id"])

    return render_template(
        "dashboard.html",
        user=user,
        sub=sub,
        member=is_member,
        features=PREMIUM_FEATURES,
        price=PRICE_EUR,
        now=datetime.now(timezone.utc),
    )


@app.post("/checkout")
def checkout():
    user = current_user()

    if not user:
        return redirect(url_for("login"))

    # Fehler bei der Mitgliedschaftsprüfung führen niemals zu einem Kauf.
    try:
        if not discord_member_exists(user["id"]):
            flash(
                "Du musst Mitglied auf unserem Discord-Server sein.",
                "error",
            )
            return redirect(url_for("dashboard"))
    except (requests.RequestException, RuntimeError):
        logging.exception("Discord-Mitgliedschaftsprüfung fehlgeschlagen")
        flash(
            "Servermitgliedschaft kann momentan nicht geprüft werden. "
            "Es wurde keine Zahlung gestartet.",
            "error",
        )
        return redirect(url_for("dashboard"))

    sub = subscription_for(user["id"])

    if sub and sub["status"] == "active":
        flash("Du hast bereits ein aktives Premium-Abo.", "info")
        return redirect(url_for("dashboard"))

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[
                {
                    "price": env("STRIPE_PRICE_ID"),
                    "quantity": 1,
                }
            ],
            client_reference_id=str(user["id"]),
            metadata={"discord_user_id": str(user["id"])},
            subscription_data={
                "metadata": {"discord_user_id": str(user["id"])}
            },
            success_url=env("SITE_URL") + "/dashboard?checkout=success",
            cancel_url=env("SITE_URL") + "/dashboard?checkout=cancelled",
        )

        return redirect(checkout_session.url, code=303)

    except Exception:
        logging.exception("Stripe-Checkout fehlgeschlagen")
        flash("Checkout konnte nicht gestartet werden.", "error")
        return redirect(url_for("dashboard"))


@app.post("/cancel")
def cancel():
    user = current_user()

    if not user:
        return redirect(url_for("login"))

    sub = subscription_for(user["id"])

    if not sub or sub["status"] != "active":
        flash("Kein aktives Abo gefunden.", "error")
        return redirect(url_for("dashboard"))

    if sub["cancel_at_period_end"]:
        flash("Die Kündigung ist bereits vorgemerkt.", "info")
        return redirect(url_for("dashboard"))

    try:
        stripe.Subscription.modify(
            sub["stripe_subscription_id"],
            cancel_at_period_end=True,
        )
        flash(
            "Kündigung vorgemerkt. Premium bleibt bis zum "
            "Ende des bezahlten Zeitraums aktiv.",
            "success",
        )
    except Exception:
        logging.exception("Abo-Kündigung fehlgeschlagen")
        flash("Kündigung konnte nicht durchgeführt werden.", "error")

    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            env("STRIPE_WEBHOOK_SECRET"),
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return "Invalid signature", 400
    except RuntimeError:
        return "Webhook configuration missing", 500

    # Eine Transaktion schützt vor doppelter Verarbeitung.
    try:
        with connect() as conn:
            with conn.transaction():
                inserted = conn.execute(
                    """INSERT INTO processed_stripe_events(event_id)
                       VALUES (%s)
                       ON CONFLICT DO NOTHING
                       RETURNING event_id""",
                    (event["id"],),
                ).fetchone()

                if not inserted:
                    return "Already processed", 200

                obj = event["data"]["object"]
                event_type = event["type"]

                if event_type == "checkout.session.completed":
                    if obj.get("mode") == "subscription":
                        user_id = (
                            obj.get("client_reference_id")
                            or (obj.get("metadata") or {}).get(
                                "discord_user_id"
                            )
                        )
                        subscription_id = obj.get("subscription")
                        customer_id = obj.get("customer")

                        if user_id and subscription_id:
                            conn.execute(
                                """INSERT INTO premium_subscriptions
                                   (discord_user_id, stripe_customer_id,
                                    stripe_subscription_id, status)
                                   VALUES (%s, %s, %s, 'inactive')
                                   ON CONFLICT (discord_user_id)
                                   DO UPDATE SET
                                     stripe_customer_id = EXCLUDED.stripe_customer_id,
                                     stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                                     updated_at = NOW()""",
                                (
                                    int(user_id),
                                    customer_id,
                                    subscription_id,
                                ),
                            )

                elif event_type.startswith("customer.subscription."):
                    user_id = (
                        (obj.get("metadata") or {}).get("discord_user_id")
                    )
                    subscription_id = obj.get("id")

                    # Metadaten können fehlen; dann über bestehende Zuordnung suchen.
                    existing = conn.execute(
                        """SELECT discord_user_id FROM premium_subscriptions
                           WHERE stripe_subscription_id = %s""",
                        (subscription_id,),
                    ).fetchone()

                    if not user_id and existing:
                        user_id = str(existing["discord_user_id"])

                    if user_id:
                        stripe_status = obj.get("status", "inactive")
                        period_end = obj.get("current_period_end")
                        active = (
                            stripe_status == "active"
                            and period_end is not None
                            and period_end > int(datetime.now(timezone.utc).timestamp())
                        )

                        # past_due, unpaid, canceled usw. deaktivieren Premium.
                        status = "active" if active else "inactive"

                        from datetime import timedelta
                        end_date = (
                            datetime.fromtimestamp(period_end, timezone.utc)
                            if period_end else None
                        )

                        conn.execute(
                            """INSERT INTO premium_subscriptions
                               (discord_user_id, stripe_customer_id,
                                stripe_subscription_id, status,
                                current_period_end, cancel_at_period_end,
                                updated_at)
                               VALUES (%s, %s, %s, %s, %s, %s, NOW())
                               ON CONFLICT (discord_user_id)
                               DO UPDATE SET
                                 stripe_customer_id = EXCLUDED.stripe_customer_id,
                                 stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                                 status = EXCLUDED.status,
                                 current_period_end = EXCLUDED.current_period_end,
                                 cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                                 updated_at = NOW()""",
                            (
                                int(user_id),
                                obj.get("customer"),
                                subscription_id,
                                status,
                                end_date,
                                bool(obj.get("cancel_at_period_end")),
                            ),
                        )

                elif event_type == "invoice.payment_failed":
                    subscription_id = obj.get("subscription")

                    if subscription_id:
                        conn.execute(
                            """UPDATE premium_subscriptions
                               SET status = 'inactive', updated_at = NOW()
                               WHERE stripe_subscription_id = %s""",
                            (subscription_id,),
                        )

                elif event_type == "invoice.paid":
                    # Nicht allein anhand einer Rechnung aktivieren.
                    # Die Subscription muss von Stripe als active bestätigt sein.
                    subscription_id = obj.get("subscription")

                    if subscription_id:
                        stripe_sub = stripe.Subscription.retrieve(
                            subscription_id
                        )

                        user_id = (
                            (stripe_sub.get("metadata") or {}).get(
                                "discord_user_id"
                            )
                        )

                        if not user_id:
                            existing = conn.execute(
                                """SELECT discord_user_id
                                   FROM premium_subscriptions
                                   WHERE stripe_subscription_id = %s""",
                                (subscription_id,),
                            ).fetchone()
                            if existing:
                                user_id = str(existing["discord_user_id"])

                        if user_id and stripe_sub.get("status") == "active":
                            period_end = stripe_sub.get("current_period_end")
                            end_date = (
                                datetime.fromtimestamp(
                                    period_end, timezone.utc
                                )
                                if period_end else None
                            )

                            conn.execute(
                                """UPDATE premium_subscriptions
                                   SET status = 'active',
                                       current_period_end = %s,
                                       updated_at = NOW()
                                   WHERE discord_user_id = %s
                                     AND stripe_subscription_id = %s""",
                                (end_date, int(user_id), subscription_id),
                            )

    except Exception:
        # 500 bewirkt, dass Stripe das Event erneut zustellen kann.
        logging.exception("Stripe-Event konnte nicht verarbeitet werden")
        return "Processing failed", 500

    return "ok", 200


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    init_database()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
