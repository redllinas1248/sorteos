import os
import secrets
from werkzeug.security import generate_password_hash
from datetime import (
    datetime,
    timedelta,
    timezone
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from zoneinfo import ZoneInfo

from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    abort,
    jsonify,
    send_from_directory
)

import stripe

from db import get_db


app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "sorteos-clave-local")


# ============================================================
# CONFIGURACIÓN DE SESIONES SEGURAS
# ============================================================

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2)
)


# ============================================================
# STRIPE
# ============================================================

STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

stripe.api_key = STRIPE_SECRET_KEY


# ============================================================
# ZONA HORARIA
# ============================================================

TZ_MEXICO = ZoneInfo("America/Mexico_City")


@app.template_filter("mx")
def filtro_mx(dt):

    if dt is None:
        return None

    return (
        dt
        .replace(tzinfo=timezone.utc)
        .astimezone(TZ_MEXICO)
    )


# ============================================================
# CONFIGURACIÓN DE RESERVAS
# ============================================================

RESERVA_TTL_MINUTOS = int(os.getenv("RESERVA_TTL_MINUTOS", "30"))


# ============================================================
# CREDENCIALES DE ADMINISTRACIÓN
# ============================================================

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


# ============================================================
# DECORADOR: SOLO ADMINISTRADOR
# ============================================================

def admin_required(f):

    @wraps(f)
    def decorated_function(*args, **kwargs):

        if not session.get("admin_logged_in"):

            flash("Debes iniciar sesión como administrador.", "error")

            return redirect(
                url_for("admin_login", next=request.path)
            )

        return f(*args, **kwargs)

    return decorated_function


# ============================================================
# DECORADOR: SOLO SUPER ADMIN
#
# Bloquea el acceso a usuarios restringidos. Si intentan
# entrar a una ruta de super admin, los redirige a su panel.
# ============================================================

def super_admin_required(f):

    @wraps(f)
    def decorated_function(*args, **kwargs):

        if not session.get("admin_logged_in"):

            flash("Debes iniciar sesión como administrador.", "error")

            return redirect(
                url_for("admin_login", next=request.path)
            )

        if session.get("admin_type") != "super":

            flash(
                "No tienes permiso para acceder a esa sección.",
                "error"
            )

            return redirect(url_for("mi_panel"))

        return f(*args, **kwargs)

    return decorated_function



# ============================================================
# HELPER: LIBERAR RESERVAS EXPIRADAS
#
# IMPORTANTE: NO libera boletos que ya generaron un voucher
# de OXXO (tienen stripe_session_id y metodo_pago='oxxo').
# Stripe les da hasta 5 días al usuario para pagar en tienda.
# ============================================================

def liberar_reservas_expiradas(db):

    cursor = db.cursor()

    cutoff = datetime.now() - timedelta(minutes=RESERVA_TTL_MINUTOS)

    cursor.execute("""
        SELECT id
        FROM sp_boletos
        WHERE
            estado = 'reservado'
            AND reservado_en IS NOT NULL
            AND reservado_en < %s
            AND NOT (
                metodo_pago = 'oxxo'
                AND stripe_session_id IS NOT NULL
            )
    """, (cutoff,))

    ids = [f["id"] for f in cursor.fetchall()]

    if not ids:
        return 0

    cursor.execute("""
        UPDATE sp_boletos
        SET
            estado = 'disponible',
            reserva_token = NULL,
            reservado_en = NULL,
            nombre = NULL,
            numero_especial = NULL,
            actualizado_en = NOW()
        WHERE id = ANY(%s)
    """, (ids,))

    return len(ids)


# ============================================================
# INICIO (redirige a la lista de sorteos)
# ============================================================

@app.route("/")
def inicio():
    return redirect(url_for("rifas"))


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    return "OK"


# ============================================================
# LISTADO PÚBLICO DE SORTEOS
# ============================================================

@app.route("/rifas")
def rifas():

    db = get_db()

    try:

        liberadas = liberar_reservas_expiradas(db)

        if liberadas > 0:
            db.commit()

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id,
                titulo,
                descripcion,
                imagen_url,
                cantidad_boletos,
                precio_boleto,
                estado,
                fecha_inicio,
                fecha_fin,
                fecha_sorteo
            FROM sp_rifas
            WHERE estado = 'activa'
            ORDER BY creado_en DESC
        """)

        rifas = cursor.fetchall()

        return render_template("rifas.html", rifas=rifas)

    finally:

        db.close()


# ============================================================
# PARTICIPAR (elegir boleto)
# ============================================================

@app.route("/rifas/participar/<int:rifa_id>")
def participar(rifa_id):

    db = get_db()

    try:

        liberadas = liberar_reservas_expiradas(db)

        if liberadas > 0:
            db.commit()

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id,
                titulo,
                descripcion,
                imagen_url,
                cantidad_boletos,
                precio_boleto,
                estado
            FROM sp_rifas
            WHERE id = %s
              AND estado = 'activa'
        """, (rifa_id,))

        rifa = cursor.fetchone()

        if not rifa:

            flash("El sorteo no existe o ya no está disponible.", "error")

            return redirect(url_for("rifas"))

        cursor.execute("""
            SELECT
                id,
                numero,
                estado
            FROM sp_boletos
            WHERE rifa_id = %s
              AND estado = 'disponible'
            ORDER BY numero ASC
        """, (rifa_id,))

        boletos = cursor.fetchall()

        return render_template(
            "participar.html",
            rifa=rifa,
            boletos=boletos
        )

    finally:

        db.close()


# ============================================================
# RESERVAR BOLETO
# ============================================================

@app.route(
    "/rifas/participar/<int:rifa_id>/reservar",
    methods=["POST"]
)
def reservar_boleto(rifa_id):

    boleto_id = request.form.get("boleto_id", "").strip()

    if not boleto_id:

        flash("Selecciona un boleto.", "error")

        return redirect(url_for("participar", rifa_id=rifa_id))

    db = get_db()

    try:

        liberar_reservas_expiradas(db)

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id,
                rifa_id,
                numero,
                estado,
                reserva_token,
                reservado_en
            FROM sp_boletos
            WHERE id = %s
              AND rifa_id = %s
              AND estado = 'disponible'
            FOR UPDATE
        """, (boleto_id, rifa_id))

        boleto = cursor.fetchone()

        if not boleto:

            db.rollback()

            flash("Ese boleto ya no está disponible. Selecciona otro.", "error")

            return redirect(url_for("participar", rifa_id=rifa_id))

        reserva_token = secrets.token_urlsafe(32)

        cursor.execute("""
            UPDATE sp_boletos
            SET
                estado = 'reservado',
                reserva_token = %s,
                reservado_en = NOW(),
                actualizado_en = NOW()
            WHERE
                id = %s
                AND rifa_id = %s
                AND estado = 'disponible'
        """, (reserva_token, boleto["id"], rifa_id))

        if cursor.rowcount != 1:

            db.rollback()

            flash("No fue posible reservar el boleto.", "error")

            return redirect(url_for("participar", rifa_id=rifa_id))

        db.commit()

        session["reserva_token"] = reserva_token
        session["boleto_id"] = boleto["id"]
        session["rifa_id"] = rifa_id

        return redirect(url_for("pago", reserva_token=reserva_token))

    except Exception as error:

        db.rollback()

        print("ERROR AL RESERVAR BOLETO:", error)

        flash("Ocurrió un error al reservar el boleto.", "error")

        return redirect(url_for("participar", rifa_id=rifa_id))

    finally:

        db.close()


# ============================================================
# PANTALLA DE PAGO (con captura de datos)
# ============================================================

@app.route("/rifas/pago/<reserva_token>")
def pago(reserva_token):

    db = get_db()

    try:

        liberadas = liberar_reservas_expiradas(db)

        if liberadas > 0:
            db.commit()

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                b.id,
                b.rifa_id,
                b.numero,
                b.estado,
                b.reserva_token,
                b.reservado_en,
                b.pagado_en,
                b.metodo_pago,
                b.nombre,
                b.numero_especial,
                b.stripe_session_id,
                r.titulo,
                r.descripcion,
                r.imagen_url,
                r.precio_boleto
            FROM sp_boletos b
            INNER JOIN sp_rifas r
                ON r.id = b.rifa_id
            WHERE b.reserva_token = %s
        """, (reserva_token,))

        boleto = cursor.fetchone()

        if not boleto:

            flash(
                "La reserva expiró o ya no está disponible. "
                "Puedes elegir otro boleto.",
                "error"
            )

            return redirect(url_for("rifas"))

        # Si ya está asignado o pagado, ir directo a la tarjeta
        if boleto["estado"] in ("asignado", "pagado"):

            return redirect(
                url_for("tarjeta_participacion", reserva_token=reserva_token)
            )

        # Si ya generó voucher OXXO → mandarlo a pago_pendiente
        if (
            boleto["estado"] == "reservado"
            and boleto["metodo_pago"] == "oxxo"
            and boleto["stripe_session_id"]
        ):

            return redirect(
                url_for("pago_pendiente", reserva_token=reserva_token)
            )

        # Solo se puede pagar si está reservado
        if boleto["estado"] != "reservado":

            flash("Esta reserva ya no está disponible.", "error")

            return redirect(url_for("rifas"))

        # Calcular timestamp de expiración
        expira_en_ms = None

        if boleto["reservado_en"]:

            reservado_utc = boleto["reservado_en"].replace(
                tzinfo=timezone.utc
            )

            expira_en = reservado_utc + timedelta(
                minutes=RESERVA_TTL_MINUTOS
            )

            expira_en_ms = int(expira_en.timestamp() * 1000)

        return render_template(
            "pago.html",
            boleto=boleto,
            stripe_public_key=STRIPE_PUBLIC_KEY,
            minutos_restantes=RESERVA_TTL_MINUTOS,
            expira_en_ms=expira_en_ms
        )

    finally:

        db.close()


# ============================================================
# INICIAR PAGO CON STRIPE CHECKOUT
# ============================================================

@app.route("/rifas/pago/<reserva_token>/checkout", methods=["POST"])
def iniciar_pago(reserva_token):

    # ----------------------------------------------------
    # Capturar datos del formulario
    # ----------------------------------------------------

    nombre = request.form.get("nombre", "").strip()
    numero_especial = request.form.get("numero_especial", "").strip() or None
    metodo = request.form.get("metodo", "card")

    if metodo not in ("card", "oxxo"):
        metodo = "card"

    # ----------------------------------------------------
    # Validaciones
    # ----------------------------------------------------

    if not nombre:

        flash("El nombre es obligatorio.", "error")

        return redirect(
            url_for("pago", reserva_token=reserva_token)
        )

    if len(nombre) > 200:

        flash("El nombre es demasiado largo (máximo 200 caracteres).", "error")

        return redirect(
            url_for("pago", reserva_token=reserva_token)
        )

    if numero_especial and len(numero_especial) > 50:

        flash("El número especial es demasiado largo.", "error")

        return redirect(
            url_for("pago", reserva_token=reserva_token)
        )

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                b.id,
                b.rifa_id,
                b.numero,
                b.estado,
                b.reserva_token,
                r.titulo,
                r.precio_boleto
            FROM sp_boletos b
            INNER JOIN sp_rifas r
                ON r.id = b.rifa_id
            WHERE b.reserva_token = %s
        """, (reserva_token,))

        boleto = cursor.fetchone()

        if not boleto:

            flash("Reserva no encontrada.", "error")

            return redirect(url_for("rifas"))

        if boleto["estado"] != "reservado":

            flash("Esta reserva ya no puede pagarse.", "error")

            return redirect(
                url_for("pago", reserva_token=reserva_token)
            )

        # ----------------------------------------------------
        # Guardar datos en el boleto
        # ----------------------------------------------------

        cursor.execute("""
            UPDATE sp_boletos
            SET
                nombre = %s,
                numero_especial = %s,
                metodo_pago = %s,
                actualizado_en = NOW()
            WHERE id = %s
        """, (nombre, numero_especial, metodo, boleto["id"]))

        # ----------------------------------------------------
        # Crear sesión de Stripe
        # ----------------------------------------------------

        base_url = request.url_root.rstrip("/")

        checkout_session = stripe.checkout.Session.create(

            line_items=[{
                "price_data": {
                    "currency": "mxn",
                    "product_data": {
                        "name": f"Boleto #{boleto['numero']} - {boleto['titulo']}",
                    },
                    "unit_amount": int(float(boleto["precio_boleto"]) * 100),
                },
                "quantity": 1,
            }],

            mode="payment",

            success_url=(
                f"{base_url}/rifas/pago/{reserva_token}/exito"
                "?session_id={CHECKOUT_SESSION_ID}"
            ),

            cancel_url=f"{base_url}/rifas/pago/{reserva_token}/cancelado",

            metadata={
                "boleto_id": str(boleto["id"]),
                "reserva_token": reserva_token,
                "metodo_preferido": metodo,
            },
        )

        cursor.execute("""
            UPDATE sp_boletos
            SET
                stripe_session_id = %s,
                actualizado_en = NOW()
            WHERE id = %s
        """, (checkout_session.id, boleto["id"]))

        db.commit()

        return redirect(checkout_session.url, code=303)

    except Exception as error:

        db.rollback()

        print("ERROR AL INICIAR PAGO:", error)

        flash(
            "No fue posible iniciar el pago. Inténtalo de nuevo.",
            "error"
        )

        return redirect(
            url_for("pago", reserva_token=reserva_token)
        )

    finally:

        db.close()


# ============================================================
# PAGO EXITOSO (redirect de Stripe)
# ============================================================

@app.route("/rifas/pago/<reserva_token>/exito")
def pago_exito(reserva_token):

    session_id = request.args.get("session_id", "").strip()

    if not session_id:

        flash("No se recibió confirmación del pago.", "error")

        return redirect(url_for("rifas"))

    db = get_db()

    try:

        cursor = db.cursor()

        try:

            checkout_session = stripe.checkout.Session.retrieve(session_id)

        except Exception as error:

            print("ERROR AL CONSULTAR STRIPE:", error)

            flash("No pudimos verificar el pago.", "error")

            return redirect(url_for("rifas"))

        # ----------------------------------------------------
        # Si el pago NO está confirmado (OXXO) → pendiente
        # ----------------------------------------------------

        if checkout_session.payment_status not in ("paid", "no_payment_required"):

            return redirect(
                url_for("pago_pendiente", reserva_token=reserva_token)
            )

        # ----------------------------------------------------
        # Pago confirmado (Tarjeta) → asignado
        # ----------------------------------------------------

        cursor.execute("""
            UPDATE sp_boletos
            SET
                estado = 'asignado',
                pagado_en = NOW(),
                asignado_en = NOW(),
                monto_pagado = %s,
                actualizado_en = NOW()
            WHERE
                reserva_token = %s
                AND estado = 'reservado'
        """, (
            checkout_session.amount_total / 100 if checkout_session.amount_total else None,
            reserva_token
        ))

        db.commit()

        return redirect(
            url_for("tarjeta_participacion", reserva_token=reserva_token)
        )

    finally:

        db.close()


# ============================================================
# PAGO PENDIENTE (OXXO generado, esperando pago en tienda)
# ============================================================

@app.route("/rifas/pago/<reserva_token>/pendiente")
def pago_pendiente(reserva_token):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                b.id,
                b.rifa_id,
                b.numero,
                b.estado,
                b.reserva_token,
                b.nombre,
                b.numero_especial,
                b.metodo_pago,
                b.pagado_en,
                b.stripe_session_id,
                r.titulo,
                r.descripcion,
                r.imagen_url,
                r.precio_boleto
            FROM sp_boletos b
            INNER JOIN sp_rifas r
                ON r.id = b.rifa_id
            WHERE b.reserva_token = %s
        """, (reserva_token,))

        boleto = cursor.fetchone()

        if not boleto:

            flash("La reserva no existe.", "error")

            return redirect(url_for("rifas"))

        # Si ya se pagó, mandarlo a la tarjeta final
        if boleto["estado"] in ("asignado", "pagado"):

            return redirect(
                url_for("tarjeta_participacion", reserva_token=reserva_token)
            )

        return render_template(
            "pago_pendiente.html",
            boleto=boleto
        )

    finally:

        db.close()


# ============================================================
# PAGO CANCELADO
# ============================================================

@app.route("/rifas/pago/<reserva_token>/cancelado")
def pago_cancelado(reserva_token):

    flash(
        "El pago fue cancelado. Tu boleto sigue reservado, "
        "puedes intentar de nuevo.",
        "error"
    )

    return redirect(
        url_for("pago", reserva_token=reserva_token)
    )


# ============================================================
# WEBHOOK DE STRIPE
# ============================================================

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if not STRIPE_WEBHOOK_SECRET:

        print("⚠️ STRIPE_WEBHOOK_SECRET no configurado")

        return "", 400

    try:

        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )

    except ValueError as e:

        print("Webhook: payload inválido", e)

        return "", 400

    except stripe.error.SignatureVerificationError as e:

        print("Webhook: firma inválida", e)

        return "", 400

    event_dict = event.to_dict() if hasattr(event, "to_dict") else event

    event_type = event_dict["type"]
    session_data = event_dict["data"]["object"]

    # ============================================================
    # CHECKOUT COMPLETED
    # ============================================================

    if event_type == "checkout.session.completed":

        session_id = session_data["id"]
        payment_status = session_data.get("payment_status", "")
        metadata = session_data.get("metadata", {}) or {}
        boleto_id = metadata.get("boleto_id")
        monto = (session_data.get("amount_total") or 0) / 100

        print(
            f"Webhook: session={session_id}, "
            f"status={payment_status}, boleto={boleto_id}"
        )

        if payment_status == "paid" and boleto_id:

            db = get_db()

            try:

                cursor = db.cursor()

                cursor.execute("""
                    UPDATE sp_boletos
                    SET
                        estado = 'asignado',
                        pagado_en = NOW(),
                        asignado_en = NOW(),
                        monto_pagado = %s,
                        actualizado_en = NOW()
                    WHERE
                        id = %s
                        AND estado = 'reservado'
                """, (monto, boleto_id))

                db.commit()

                print(f"✅ Pago confirmado para boleto {boleto_id}")

            except Exception as e:

                db.rollback()

                print("Error al procesar webhook:", e)

            finally:

                db.close()

        else:

            print(
                f"ℹ️ Webhook recibido (status={payment_status}) "
                f"sin marcar como pagado"
            )

    # ============================================================
    # ASYNC PAYMENT SUCCEEDED (OXXO pagado en tienda)
    # ============================================================

    elif event_type == "checkout.session.async_payment_succeeded":

        session_id = session_data["id"]
        metadata = session_data.get("metadata", {}) or {}
        boleto_id = metadata.get("boleto_id")
        monto = (session_data.get("amount_total") or 0) / 100

        print(f"Webhook async: session={session_id}, boleto={boleto_id}")

        if boleto_id:

            db = get_db()

            try:

                cursor = db.cursor()

                cursor.execute("""
                    UPDATE sp_boletos
                    SET
                        estado = 'asignado',
                        pagado_en = NOW(),
                        asignado_en = NOW(),
                        monto_pagado = %s,
                        actualizado_en = NOW()
                    WHERE
                        id = %s
                        AND estado = 'reservado'
                """, (monto, boleto_id))

                db.commit()

                print(f"✅ Pago asíncrono confirmado para boleto {boleto_id}")

            except Exception as e:

                db.rollback()

                print("Error al procesar webhook async:", e)

            finally:

                db.close()

    else:

        print(f"ℹ️ Evento recibido sin manejar: {event_type}")

    return "", 200


# ============================================================
# TARJETA FINAL DE PARTICIPACIÓN
# ============================================================

@app.route("/rifas/tarjeta/<reserva_token>")
def tarjeta_participacion(reserva_token):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                b.id,
                b.rifa_id,
                b.numero,
                b.estado,
                b.reserva_token,
                b.nombre,
                b.numero_especial,
                b.creado_en,
                b.asignado_en,
                b.pagado_en,
                b.monto_pagado,
                b.metodo_pago,
                r.titulo,
                r.descripcion,
                r.imagen_url,
                r.fecha_sorteo
            FROM sp_boletos b
            INNER JOIN sp_rifas r
                ON r.id = b.rifa_id
            WHERE b.reserva_token = %s
        """, (reserva_token,))

        boleto = cursor.fetchone()

        if not boleto:

            flash("La participación no existe.", "error")

            return redirect(url_for("rifas"))

        if boleto["estado"] not in ("asignado", "pagado"):

            flash("Aún debes completar el pago para ver tu comprobante.", "error")

            return redirect(
                url_for("pago", reserva_token=reserva_token)
            )

        return render_template(
            "tarjeta_participacion.html",
            boleto=boleto
        )

    finally:

        db.close()


# ============================================================
# PÁGINAS LEGALES
# ============================================================

@app.route("/privacidad")
def privacidad():
    return render_template("privacidad.html")


@app.route("/terminos")
def terminos():
    return render_template("terminos.html")


@app.route("/contacto")
def contacto():
    return render_template("contacto.html")


@app.route("/faq")
def faq():
    return render_template("faq.html")


@app.route("/ganadores")
def ganadores():
    return render_template("ganadores.html")


# ============================================================
# PWA
# ============================================================

@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "manifest.json",
        mimetype="application/manifest+json"
    )


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "service-worker.js",
        mimetype="application/javascript"
    )


# ============================================================
# CONSULTAR MI PARTICIPACIÓN
# ============================================================

@app.route("/rifas/consultar", methods=["GET", "POST"])
def consultar_participacion():

    if request.method == "POST":

        codigo = request.form.get("codigo", "").strip()

        if not codigo:

            flash("Ingresa tu código de participación.", "error")

            return render_template("consultar.html")

        db = get_db()

        try:

            cursor = db.cursor()

            cursor.execute("""
                SELECT
                    id,
                    estado,
                    metodo_pago,
                    stripe_session_id
                FROM sp_boletos
                WHERE reserva_token = %s
            """, (codigo,))

            boleto = cursor.fetchone()

            if not boleto:

                flash(
                    "No encontramos ninguna participación con ese código. "
                    "Verifícalo e intenta de nuevo.",
                    "error"
                )

                return render_template("consultar.html")

            # ------------------------------------------------
            # Boleto asignado o pagado → tarjeta final
            # ------------------------------------------------

            if boleto["estado"] in ("asignado", "pagado"):

                return redirect(
                    url_for("tarjeta_participacion", reserva_token=codigo)
                )

            # ------------------------------------------------
            # Boleto reservado
            # ------------------------------------------------

            elif boleto["estado"] == "reservado":

                # Si ya generó voucher OXXO → pantalla de pendiente
                if (
                    boleto["metodo_pago"] == "oxxo"
                    and boleto["stripe_session_id"]
                ):

                    return redirect(
                        url_for("pago_pendiente", reserva_token=codigo)
                    )

                # Si no, mandarlo a la pantalla de pago
                return redirect(
                    url_for("pago", reserva_token=codigo)
                )

            else:

                flash("Esta participación ya no está activa.", "error")

                return render_template("consultar.html")

        finally:

            db.close()

    return render_template("consultar.html")


# ============================================================
# LOGIN DE ADMINISTRACIÓN
#
# Acepta 2 tipos de usuarios:
#   - Super admin (via variables de entorno)
#   - Usuarios restringidos (via tabla sp_admins)
# ============================================================

@app.route("/rifas/admin/login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # ----------------------------------------------------
        # 1. Verificar si es el SUPER ADMIN
        # ----------------------------------------------------

        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):

            session["admin_logged_in"] = True
            session["admin_type"] = "super"
            session["admin_username"] = username
            session["admin_nombre"] = "Super Admin"
            session.permanent = True

            flash("Has iniciado sesión como super admin.", "success")

            next_page = request.args.get("next")

            if next_page and next_page.startswith("/"):
                return redirect(next_page)

            return redirect(url_for("admin_rifas"))

        # ----------------------------------------------------
        # 2. Verificar si es un usuario restringido
        # ----------------------------------------------------

        db = get_db()

        try:

            cursor = db.cursor()

            cursor.execute("""
                SELECT
                    id,
                    username,
                    password_hash,
                    nombre,
                    propietario_slug,
                    activo
                FROM sp_admins
                WHERE username = %s
            """, (username,))

            usuario = cursor.fetchone()

            if usuario and usuario["activo"] and check_password_hash(
                usuario["password_hash"], password
            ):

                # Actualizar último acceso
                cursor.execute("""
                    UPDATE sp_admins
                    SET ultimo_acceso = NOW()
                    WHERE id = %s
                """, (usuario["id"],))

                db.commit()

                session["admin_logged_in"] = True
                session["admin_type"] = "restringido"
                session["admin_username"] = usuario["username"]
                session["admin_nombre"] = usuario["nombre"]
                session["propietario_slug"] = usuario["propietario_slug"]
                session.permanent = True

                flash(
                    f"Bienvenido, {usuario['nombre']}.",
                    "success"
                )

                return redirect(url_for("mi_panel"))

        finally:

            db.close()

        # ----------------------------------------------------
        # Si llegó aquí, las credenciales fueron incorrectas
        # ----------------------------------------------------

        flash("Usuario o contraseña incorrectos.", "error")

    return render_template("admin_login.html")


@app.route("/rifas/admin/logout")
def admin_logout():

    session.pop("admin_logged_in", None)
    session.pop("admin_type", None)
    session.pop("admin_username", None)
    session.pop("admin_nombre", None)
    session.pop("propietario_slug", None)

    flash("Has cerrado sesión.", "success")

    return redirect("/")


# ============================================================
# PANEL ADMINISTRATIVO
# ============================================================

@app.route("/rifas/admin")
@super_admin_required
def admin_rifas():

    db = get_db()

    try:

        cursor = db.cursor()

        # ====================================================
        # BLOQUE 1: ESTADÍSTICAS DE SORTEOS ACTIVOS
        # (activa + pausada + borrador)
        # ====================================================

        cursor.execute("""
            SELECT estado, COUNT(*) AS total
            FROM sp_rifas
            WHERE estado IN ('borrador', 'activa', 'pausada')
            GROUP BY estado
        """)

        stats_rifas_activos = {
            fila["estado"]: fila["total"]
            for fila in cursor.fetchall()
        }

        cursor.execute("""
            SELECT b.estado, COUNT(*) AS total
            FROM sp_boletos b
            INNER JOIN sp_rifas r ON r.id = b.rifa_id
            WHERE r.estado IN ('activa', 'pausada')
            GROUP BY b.estado
        """)

        stats_boletos_activos = {
            fila["estado"]: fila["total"]
            for fila in cursor.fetchall()
        }

        # ====================================================
        # BLOQUE 2: ESTADÍSTICAS HISTÓRICAS
        # (finalizada + cancelada)
        # ====================================================

        cursor.execute("""
            SELECT estado, COUNT(*) AS total
            FROM sp_rifas
            WHERE estado IN ('finalizada', 'cancelada')
            GROUP BY estado
        """)

        stats_rifas_historicos = {
            fila["estado"]: fila["total"]
            for fila in cursor.fetchall()
        }

        cursor.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE b.estado IN ('asignado', 'pagado')
                ) AS vendidos,
                COALESCE(SUM(b.monto_pagado), 0) AS ingresos
            FROM sp_boletos b
            INNER JOIN sp_rifas r ON r.id = b.rifa_id
            WHERE r.estado IN ('finalizada', 'cancelada')
        """)

        stats_historicos = cursor.fetchone()

        # ====================================================
        # STATS POR RIFA (solo de sorteos no históricos)
        # ====================================================

        cursor.execute("""
            SELECT
                r.id,
                COUNT(CASE WHEN b.estado = 'disponible' THEN 1 END) AS disponibles,
                COUNT(CASE WHEN b.estado = 'reservado' THEN 1 END) AS reservados,
                COUNT(CASE WHEN b.estado = 'pagado' THEN 1 END) AS pagados,
                COUNT(CASE WHEN b.estado = 'asignado' THEN 1 END) AS asignados
            FROM sp_rifas r
            LEFT JOIN sp_boletos b ON b.rifa_id = r.id
            WHERE r.estado IN ('borrador', 'activa', 'pausada')
            GROUP BY r.id
        """)

        stats_por_rifa = {
            fila["id"]: {
                "disponibles": fila["disponibles"],
                "reservados": fila["reservados"],
                "pagados": fila["pagados"],
                "asignados": fila["asignados"]
            }
            for fila in cursor.fetchall()
        }

        # ====================================================
        # LISTADO DE RIFAS
        # ====================================================

        cursor.execute("""
            SELECT
                id, titulo, descripcion, imagen_url,
                cantidad_boletos, precio_boleto, estado,
                fecha_inicio, fecha_fin, fecha_sorteo,
                creado_en, actualizado_en
            FROM sp_rifas
            ORDER BY creado_en DESC
        """)

        rifas = cursor.fetchall()

        return render_template(
            "admin_rifas.html",
            rifas=rifas,
            stats_rifas_activos=stats_rifas_activos,
            stats_boletos_activos=stats_boletos_activos,
            stats_rifas_historicos=stats_rifas_historicos,
            stats_historicos=stats_historicos,
            stats_por_rifa=stats_por_rifa
        )

    finally:

        db.close()


# ============================================================
# PARTICIPANTES DE UN SORTEO
# ============================================================

@app.route("/rifas/admin/participantes/<int:rifa_id>")
@super_admin_required
def admin_participantes(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id, titulo, descripcion, estado,
                cantidad_boletos, fecha_sorteo
            FROM sp_rifas
            WHERE id = %s
        """, (rifa_id,))

        rifa = cursor.fetchone()

        if not rifa:

            flash("El sorteo no existe.", "error")

            return redirect(url_for("admin_rifas"))

        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE estado = 'disponible') AS disponibles,
                COUNT(*) FILTER (WHERE estado = 'reservado') AS reservados,
                COUNT(*) FILTER (WHERE estado = 'pagado') AS pagados,
                COUNT(*) FILTER (WHERE estado = 'asignado') AS asignados
            FROM sp_boletos
            WHERE rifa_id = %s
        """, (rifa_id,))

        stats = cursor.fetchone()

        cursor.execute("""
            SELECT
                id, numero, estado, nombre, numero_especial,
                reservado_en, pagado_en, asignado_en, reserva_token,
                metodo_pago, monto_pagado
            FROM sp_boletos
            WHERE rifa_id = %s
              AND estado IN ('reservado', 'pagado', 'asignado')
            ORDER BY numero ASC
        """, (rifa_id,))

        participantes = cursor.fetchall()

        return render_template(
            "admin_participantes.html",
            rifa=rifa,
            participantes=participantes,
            stats=stats
        )

    finally:

        db.close()


# ============================================================
# LIBERAR BOLETO MANUALMENTE
# ============================================================

@app.route(
    "/rifas/admin/liberar-boleto/<int:boleto_id>",
    methods=["POST"]
)
@super_admin_required
def admin_liberar_boleto(boleto_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT id, numero, estado, rifa_id
            FROM sp_boletos
            WHERE id = %s
            FOR UPDATE
        """, (boleto_id,))

        boleto = cursor.fetchone()

        if not boleto:

            db.rollback()

            flash("El boleto no existe.", "error")

            return redirect(url_for("admin_rifas"))

        if boleto["estado"] not in ("reservado", "pagado"):

            db.rollback()

            flash(
                f"El boleto #{boleto['numero']} no puede liberarse "
                f"(estado actual: {boleto['estado']}).",
                "error"
            )

            return redirect(
                url_for("admin_participantes", rifa_id=boleto["rifa_id"])
            )

        cursor.execute("""
            UPDATE sp_boletos
            SET
                estado = 'disponible',
                reserva_token = NULL,
                reservado_en = NULL,
                pagado_en = NULL,
                asignado_en = NULL,
                stripe_session_id = NULL,
                metodo_pago = NULL,
                monto_pagado = NULL,
                nombre = NULL,
                numero_especial = NULL,
                actualizado_en = NOW()
            WHERE id = %s
        """, (boleto["id"],))

        db.commit()

        flash(f"Boleto #{boleto['numero']} liberado correctamente.", "success")

        return redirect(
            url_for("admin_participantes", rifa_id=boleto["rifa_id"])
        )

    except Exception as error:

        db.rollback()

        print("ERROR AL LIBERAR BOLETO:", error)

        flash("Ocurrió un error al liberar el boleto.", "error")

        return redirect(url_for("admin_rifas"))

    finally:

        db.close()


# ============================================================
# GENERAR TICKETS IMPRIMIBLES
# ============================================================

@app.route("/rifas/admin/tickets/<int:rifa_id>")
@super_admin_required
def admin_tickets(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id, titulo, descripcion,
                cantidad_boletos, fecha_sorteo, estado
            FROM sp_rifas
            WHERE id = %s
        """, (rifa_id,))

        rifa = cursor.fetchone()

        if not rifa:

            flash("El sorteo no existe.", "error")

            return redirect(url_for("admin_rifas"))

        cursor.execute("""
            SELECT
                numero, nombre, numero_especial, asignado_en
            FROM sp_boletos
            WHERE rifa_id = %s
              AND estado = 'asignado'
            ORDER BY numero ASC
        """, (rifa_id,))

        boletos = cursor.fetchall()

        return render_template(
            "admin_tickets.html",
            rifa=rifa,
            boletos=boletos
        )

    finally:

        db.close()


# ============================================================
# CREAR RIFA
# ============================================================

@app.route("/rifas/admin/nueva")
@super_admin_required
def nueva_rifa():
    return render_template("nueva_rifa.html")


@app.route("/rifas/admin/nueva", methods=["POST"])
@super_admin_required
def crear_rifa():

    titulo = request.form.get("titulo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    imagen_url = request.form.get("imagen_url", "").strip()
    cantidad_boletos = request.form.get("cantidad_boletos", "").strip()
    precio_boleto = request.form.get("precio_boleto", "").strip()
    fecha_inicio = request.form.get("fecha_inicio", "").strip()
    fecha_fin = request.form.get("fecha_fin", "").strip()
    fecha_sorteo = request.form.get("fecha_sorteo", "").strip()

    if not titulo:

        flash("El título del sorteo es obligatorio.", "error")

        return redirect(url_for("nueva_rifa"))

    try:

        cantidad_boletos = int(cantidad_boletos)

        if cantidad_boletos <= 0:
            raise ValueError

    except (ValueError, TypeError):

        flash("La cantidad de boletos debe ser mayor que cero.", "error")

        return redirect(url_for("nueva_rifa"))

    try:

        precio_boleto = float(precio_boleto)

        if precio_boleto <= 0:
            raise ValueError

    except (ValueError, TypeError):

        flash("El precio del boleto debe ser mayor que cero.", "error")

        return redirect(url_for("nueva_rifa"))

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            INSERT INTO sp_rifas (
                titulo, descripcion, imagen_url,
                cantidad_boletos, precio_boleto, estado,
                fecha_inicio, fecha_fin, fecha_sorteo,
                creado_en, actualizado_en
            )
            VALUES (
                %s, %s, %s, %s, %s, 'borrador',
                NULLIF(%s, '')::timestamp,
                NULLIF(%s, '')::timestamp,
                NULLIF(%s, '')::timestamp,
                NOW(), NOW()
            )
            RETURNING id
        """, (
            titulo, descripcion, imagen_url,
            cantidad_boletos, precio_boleto,
            fecha_inicio, fecha_fin, fecha_sorteo
        ))

        rifa_id = cursor.fetchone()["id"]

        for numero in range(1, cantidad_boletos + 1):

            cursor.execute("""
                INSERT INTO sp_boletos (
                    rifa_id, numero, estado, origen, creado_en
                )
                VALUES (%s, %s, 'disponible', 'sistema', NOW())
            """, (rifa_id, numero))

        db.commit()

        flash(
            f"Sorteo creado correctamente con {cantidad_boletos} boletos.",
            "success"
        )

        return redirect(url_for("admin_rifas"))

    except Exception as error:

        db.rollback()

        print("ERROR AL CREAR RIFA:", error)

        flash("Ocurrió un error al crear el sorteo.", "error")

        return redirect(url_for("nueva_rifa"))

    finally:

        db.close()


# ============================================================
# EDITAR RIFA
# ============================================================

@app.route("/rifas/admin/editar/<int:rifa_id>")
@super_admin_required
def editar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id, titulo, descripcion, imagen_url,
                cantidad_boletos, precio_boleto, estado,
                fecha_inicio, fecha_fin, fecha_sorteo
            FROM sp_rifas
            WHERE id = %s
        """, (rifa_id,))

        rifa = cursor.fetchone()

        if not rifa:

            flash("El sorteo no existe.", "error")

            return redirect(url_for("admin_rifas"))

        return render_template(
            "nueva_rifa.html",
            rifa=rifa,
            modo_edicion=True
        )

    finally:

        db.close()


@app.route("/rifas/admin/editar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def actualizar_rifa(rifa_id):

    titulo = request.form.get("titulo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    imagen_url = request.form.get("imagen_url", "").strip()
    cantidad_boletos = request.form.get("cantidad_boletos", "").strip()
    precio_boleto = request.form.get("precio_boleto", "").strip()
    fecha_inicio = request.form.get("fecha_inicio", "").strip()
    fecha_fin = request.form.get("fecha_fin", "").strip()
    fecha_sorteo = request.form.get("fecha_sorteo", "").strip()

    if not titulo:

        flash("El título del sorteo es obligatorio.", "error")

        return redirect(url_for("editar_rifa", rifa_id=rifa_id))

    try:

        cantidad_boletos = int(cantidad_boletos)

        if cantidad_boletos <= 0:
            raise ValueError

    except (ValueError, TypeError):

        flash("La cantidad de boletos no es válida.", "error")

        return redirect(url_for("editar_rifa", rifa_id=rifa_id))

    try:

        precio_boleto = float(precio_boleto)

        if precio_boleto <= 0:
            raise ValueError

    except (ValueError, TypeError):

        flash("El precio del boleto no es válido.", "error")

        return redirect(url_for("editar_rifa", rifa_id=rifa_id))

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET
                titulo = %s,
                descripcion = %s,
                imagen_url = %s,
                cantidad_boletos = %s,
                precio_boleto = %s,
                fecha_inicio = NULLIF(%s, '')::timestamp,
                fecha_fin = NULLIF(%s, '')::timestamp,
                fecha_sorteo = NULLIF(%s, '')::timestamp,
                actualizado_en = NOW()
            WHERE id = %s
        """, (
            titulo, descripcion, imagen_url,
            cantidad_boletos, precio_boleto,
            fecha_inicio, fecha_fin, fecha_sorteo,
            rifa_id
        ))

        if cursor.rowcount == 0:

            db.rollback()

            flash("El sorteo no existe.", "error")

            return redirect(url_for("admin_rifas"))

        db.commit()

        flash("Sorteo actualizado correctamente.", "success")

        return redirect(url_for("admin_rifas"))

    except Exception as error:

        db.rollback()

        print("ERROR AL ACTUALIZAR RIFA:", error)

        flash("Ocurrió un error al actualizar el sorteo.", "error")

        return redirect(url_for("editar_rifa", rifa_id=rifa_id))

    finally:

        db.close()


# ============================================================
# PUBLICAR / PAUSAR / REANUDAR / FINALIZAR / CANCELAR
# ============================================================

@app.route("/rifas/admin/publicar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def publicar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET estado = 'activa', actualizado_en = NOW()
            WHERE id = %s AND estado = 'borrador'
        """, (rifa_id,))

        if cursor.rowcount == 0:
            db.rollback()
            flash("El sorteo no existe o no está en borrador.", "error")
        else:
            db.commit()
            flash("Sorteo publicado correctamente.", "success")

    except Exception as error:

        db.rollback()

        print("ERROR AL PUBLICAR RIFA:", error)

        flash("Ocurrió un error al publicar el sorteo.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_rifas"))


@app.route("/rifas/admin/pausar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def pausar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET estado = 'pausada', actualizado_en = NOW()
            WHERE id = %s AND estado = 'activa'
        """, (rifa_id,))

        if cursor.rowcount == 0:
            db.rollback()
            flash("El sorteo no existe o no está activo.", "error")
        else:
            db.commit()
            flash("Sorteo pausado correctamente.", "success")

    except Exception as error:

        db.rollback()

        print("ERROR AL PAUSAR RIFA:", error)

        flash("Ocurrió un error al pausar el sorteo.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_rifas"))


@app.route("/rifas/admin/reanudar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def reanudar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET estado = 'activa', actualizado_en = NOW()
            WHERE id = %s AND estado = 'pausada'
        """, (rifa_id,))

        if cursor.rowcount == 0:
            db.rollback()
            flash("El sorteo no existe o no está pausado.", "error")
        else:
            db.commit()
            flash("Sorteo reanudado correctamente.", "success")

    except Exception as error:

        db.rollback()

        print("ERROR AL REANUDAR RIFA:", error)

        flash("Ocurrió un error al reanudar el sorteo.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_rifas"))


@app.route("/rifas/admin/finalizar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def finalizar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET estado = 'finalizada', actualizado_en = NOW()
            WHERE id = %s AND estado IN ('activa', 'pausada')
        """, (rifa_id,))

        if cursor.rowcount == 0:
            db.rollback()
            flash("El sorteo no existe o no puede finalizarse.", "error")
        else:
            db.commit()
            flash("Sorteo finalizado correctamente.", "success")

    except Exception as error:

        db.rollback()

        print("ERROR AL FINALIZAR RIFA:", error)

        flash("Ocurrió un error al finalizar el sorteo.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_rifas"))


@app.route("/rifas/admin/cancelar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def cancelar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_rifas
            SET estado = 'cancelada', actualizado_en = NOW()
            WHERE id = %s
              AND estado IN ('borrador', 'activa', 'pausada')
        """, (rifa_id,))

        if cursor.rowcount == 0:
            db.rollback()
            flash("El sorteo no existe o no puede cancelarse.", "error")
        else:
            db.commit()
            flash("Sorteo cancelado correctamente.", "success")

    except Exception as error:

        db.rollback()

        print("ERROR AL CANCELAR RIFA:", error)

        flash("Ocurrió un error al cancelar el sorteo.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_rifas"))


# ============================================================
# CLONAR / REUTILIZAR RIFA
#
# Crea una copia de una rifa existente con los mismos datos
# (título, descripción, imagen, precio, cantidad de boletos)
# pero en estado "borrador" y con fechas vacías.
#
# Todos los boletos nuevos se crean como "disponible".
# ============================================================

@app.route("/rifas/admin/clonar/<int:rifa_id>", methods=["POST"])
@super_admin_required
def clonar_rifa(rifa_id):

    db = get_db()

    try:

        cursor = db.cursor()

        # ----------------------------------------------------
        # Obtener datos de la rifa original
        # ----------------------------------------------------

        cursor.execute("""
            SELECT
                titulo,
                descripcion,
                imagen_url,
                cantidad_boletos,
                precio_boleto
            FROM sp_rifas
            WHERE id = %s
        """, (rifa_id,))

        original = cursor.fetchone()

        if not original:

            flash("El sorteo no existe.", "error")

            return redirect(url_for("admin_rifas"))

        # ----------------------------------------------------
        # Crear la copia (estado borrador, sin fechas)
        # ----------------------------------------------------

        cursor.execute("""
            INSERT INTO sp_rifas (
                titulo,
                descripcion,
                imagen_url,
                cantidad_boletos,
                precio_boleto,
                estado,
                creado_en,
                actualizado_en
            )
            VALUES (
                %s, %s, %s, %s, %s,
                'borrador',
                NOW(),
                NOW()
            )
            RETURNING id
        """, (
            original["titulo"],
            original["descripcion"],
            original["imagen_url"],
            original["cantidad_boletos"],
            original["precio_boleto"]
        ))

        nueva_rifa_id = cursor.fetchone()["id"]

        # ----------------------------------------------------
        # Crear boletos nuevos
        # ----------------------------------------------------

        for numero in range(1, original["cantidad_boletos"] + 1):

            cursor.execute("""
                INSERT INTO sp_boletos (
                    rifa_id,
                    numero,
                    estado,
                    origen,
                    creado_en
                )
                VALUES (
                    %s,
                    %s,
                    'disponible',
                    'sistema',
                    NOW()
                )
            """, (
                nueva_rifa_id,
                numero
            ))

        db.commit()

        flash(
            "Copia creada correctamente. Ahora edítala para asignar las nuevas fechas.",
            "success"
        )

        # ----------------------------------------------------
        # Redirigir a editar la copia
        # ----------------------------------------------------

        return redirect(
            url_for("editar_rifa", rifa_id=nueva_rifa_id)
        )

    except Exception as error:

        db.rollback()

        print("ERROR AL CLONAR RIFA:", error)

        flash("Ocurrió un error al reutilizar el sorteo.", "error")

        return redirect(url_for("admin_rifas"))

    finally:

        db.close()


# ============================================================
# PANEL DE USUARIO RESTRINGIDO
#
# Muestra solo las rifas del propietario que inició sesión
# y sus estadísticas. Sin botones de acción.
# ============================================================

@app.route("/rifas/mi-panel")
@admin_required
def mi_panel():

    # ----------------------------------------------------
    # Si es super admin, mándalo al panel completo
    # ----------------------------------------------------

    if session.get("admin_type") == "super":

        return redirect(url_for("admin_rifas"))

    # ----------------------------------------------------
    # Obtener el slug del propietario
    # ----------------------------------------------------

    propietario_slug = session.get("propietario_slug")

    if not propietario_slug:

        flash("Sesión inválida.", "error")

        return redirect(url_for("admin_logout"))

    db = get_db()

    try:

        cursor = db.cursor()

        # ------------------------------------------------
        # Rifas del propietario
        # ------------------------------------------------

        cursor.execute("""
            SELECT
                id,
                titulo,
                descripcion,
                imagen_url,
                cantidad_boletos,
                precio_boleto,
                estado,
                fecha_inicio,
                fecha_fin,
                fecha_sorteo,
                creado_en
            FROM sp_rifas
            WHERE propietario_slug = %s
            ORDER BY creado_en DESC
        """, (propietario_slug,))

        rifas = cursor.fetchall()

        # ------------------------------------------------
        # Stats globales del propietario
        # (solo sorteos en curso)
        # ------------------------------------------------

        cursor.execute("""
            SELECT b.estado, COUNT(*) AS total
            FROM sp_boletos b
            INNER JOIN sp_rifas r ON r.id = b.rifa_id
            WHERE r.propietario_slug = %s
              AND r.estado IN ('activa', 'pausada')
            GROUP BY b.estado
        """, (propietario_slug,))

        stats_boletos = {
            fila["estado"]: fila["total"]
            for fila in cursor.fetchall()
        }

        # ------------------------------------------------
        # Ingresos totales del propietario (todos sus sorteos)
        # ------------------------------------------------

        cursor.execute("""
            SELECT
                COALESCE(SUM(b.monto_pagado), 0) AS ingresos_totales,
                COUNT(*) FILTER (
                    WHERE b.estado IN ('asignado', 'pagado')
                ) AS boletos_vendidos
            FROM sp_boletos b
            INNER JOIN sp_rifas r ON r.id = b.rifa_id
            WHERE r.propietario_slug = %s
        """, (propietario_slug,))

        stats_vendedor = cursor.fetchone()

        # ------------------------------------------------
        # Stats por rifa (solo las del propietario)
        # ------------------------------------------------

        cursor.execute("""
            SELECT
                r.id,
                COUNT(CASE WHEN b.estado = 'disponible' THEN 1 END) AS disponibles,
                COUNT(CASE WHEN b.estado = 'reservado' THEN 1 END) AS reservados,
                COUNT(CASE WHEN b.estado = 'pagado' THEN 1 END) AS pagados,
                COUNT(CASE WHEN b.estado = 'asignado' THEN 1 END) AS asignados
            FROM sp_rifas r
            LEFT JOIN sp_boletos b ON b.rifa_id = r.id
            WHERE r.propietario_slug = %s
            GROUP BY r.id
        """, (propietario_slug,))

        stats_por_rifa = {
            fila["id"]: {
                "disponibles": fila["disponibles"],
                "reservados": fila["reservados"],
                "pagados": fila["pagados"],
                "asignados": fila["asignados"]
            }
            for fila in cursor.fetchall()
        }

        return render_template(
            "mi_panel.html",
            rifas=rifas,
            stats_boletos=stats_boletos,
            stats_vendedor=stats_vendedor,
            stats_por_rifa=stats_por_rifa
        )

    finally:

        db.close()

# ============================================================
# ADMIN: GESTIÓN DE USUARIOS RESTRINGIDOS
# Solo el super admin puede crear/editar usuarios para
# que vean las estadísticas de sus propias rifas.
# ============================================================

@app.route("/rifas/admin/admins")
@super_admin_required
def admin_usuarios():

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            SELECT
                id,
                username,
                nombre,
                propietario_slug,
                activo,
                ultimo_acceso,
                creado_en
            FROM sp_admins
            ORDER BY creado_en DESC
        """)

        usuarios = cursor.fetchall()

        return render_template(
            "admin_usuarios.html",
            usuarios=usuarios
        )

    finally:

        db.close()


@app.route("/rifas/admin/admins/nuevo")
@super_admin_required
def nuevo_admin():
    return render_template("nuevo_admin.html")


@app.route("/rifas/admin/admins/nuevo", methods=["POST"])
@super_admin_required
def crear_admin():

    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "").strip()
    nombre = request.form.get("nombre", "").strip()
    propietario_slug = request.form.get("propietario_slug", "").strip().lower()

    # Validaciones
    if not username or not password or not nombre or not propietario_slug:
        flash("Todos los campos son obligatorios.", "error")
        return redirect(url_for("nuevo_admin"))

    if len(username) < 3 or len(username) > 50:
        flash("El usuario debe tener entre 3 y 50 caracteres.", "error")
        return redirect(url_for("nuevo_admin"))

    if len(password) < 6:
        flash("La contraseña debe tener al menos 6 caracteres.", "error")
        return redirect(url_for("nuevo_admin"))

    if not username.replace("-", "").replace("_", "").isalnum():
        flash("El usuario solo puede tener letras, números, guiones y guiones bajos.", "error")
        return redirect(url_for("nuevo_admin"))

    if not propietario_slug.replace("-", "").replace("_", "").isalnum():
        flash("El slug del propietario solo puede tener letras, números, guiones y guiones bajos.", "error")
        return redirect(url_for("nuevo_admin"))

    db = get_db()

    try:

        cursor = db.cursor()

        # Verificar que el username no exista
        cursor.execute(
            "SELECT id FROM sp_admins WHERE username = %s",
            (username,)
        )

        if cursor.fetchone():
            flash("Ese nombre de usuario ya existe.", "error")
            return redirect(url_for("nuevo_admin"))

        # Verificar que el slug no exista
        cursor.execute(
            "SELECT id FROM sp_admins WHERE propietario_slug = %s",
            (propietario_slug,)
        )

        if cursor.fetchone():
            flash("Ya existe un usuario con ese slug de propietario.", "error")
            return redirect(url_for("nuevo_admin"))

        # Hashear el password
        password_hash = generate_password_hash(password)

        cursor.execute("""
            INSERT INTO sp_admins (
                username,
                password_hash,
                nombre,
                propietario_slug,
                activo
            )
            VALUES (%s, %s, %s, %s, TRUE)
        """, (
            username,
            password_hash,
            nombre,
            propietario_slug
        ))

        db.commit()

        flash(
            f"Usuario '{username}' creado correctamente.",
            "success"
        )

        return redirect(url_for("admin_usuarios"))

    except Exception as error:

        db.rollback()

        print("ERROR AL CREAR ADMIN:", error)

        flash("Ocurrió un error al crear el usuario.", "error")

        return redirect(url_for("nuevo_admin"))

    finally:

        db.close()


@app.route("/rifas/admin/admins/toggle/<int:admin_id>", methods=["POST"])
@super_admin_required
def toggle_admin(admin_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute("""
            UPDATE sp_admins
            SET activo = NOT activo
            WHERE id = %s
            RETURNING activo, username
        """, (admin_id,))

        resultado = cursor.fetchone()

        if resultado:

            db.commit()

            estado = "activado" if resultado["activo"] else "desactivado"

            flash(
                f"Usuario '{resultado['username']}' {estado}.",
                "success"
            )

        else:

            db.rollback()

            flash("Usuario no encontrado.", "error")

    except Exception as error:

        db.rollback()

        print("ERROR AL TOGGLE ADMIN:", error)

        flash("Ocurrió un error.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_usuarios"))


@app.route("/rifas/admin/admins/eliminar/<int:admin_id>", methods=["POST"])
@super_admin_required
def eliminar_admin(admin_id):

    db = get_db()

    try:

        cursor = db.cursor()

        cursor.execute(
            "DELETE FROM sp_admins WHERE id = %s RETURNING username",
            (admin_id,)
        )

        resultado = cursor.fetchone()

        if resultado:

            db.commit()

            flash(
                f"Usuario '{resultado['username']}' eliminado.",
                "success"
            )

        else:

            db.rollback()

            flash("Usuario no encontrado.", "error")

    except Exception as error:

        db.rollback()

        print("ERROR AL ELIMINAR ADMIN:", error)

        flash("Ocurrió un error al eliminar el usuario.", "error")

    finally:

        db.close()

    return redirect(url_for("admin_usuarios"))

# ============================================================
# EJECUCIÓN LOCAL
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)