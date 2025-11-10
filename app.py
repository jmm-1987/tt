import os
from datetime import datetime

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
import requests
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from werkzeug.exceptions import HTTPException


load_dotenv()


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///inbox.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "cambia-esta-clave")

db = SQLAlchemy(app)


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    contact_number = db.Column(db.String(64), nullable=False, index=True)
    contact_name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    messages = db.relationship(
        "Message",
        backref="conversation",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Message.sent_at.asc()",
    )

    def last_message(self):
        return self.messages.order_by(Message.sent_at.desc()).first()


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True
    )
    sender_type = db.Column(db.String(32), nullable=False)  # "customer" o "agent"
    message_text = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    external_id = db.Column(db.String(128), index=True)


GREEN_API_INSTANCE_ID = os.environ.get("GREEN_API_INSTANCE_ID")
GREEN_API_API_TOKEN = os.environ.get("GREEN_API_API_TOKEN")
GREEN_API_BASE_URL = os.environ.get("GREEN_API_BASE_URL", "https://api.green-api.com")


def ensure_database():
    with app.app_context():
        db.create_all()


@app.template_filter("nl2br")
def nl2br(value: str) -> str:
    if value is None:
        return ""
    return Markup(value.replace("\n", "<br>"))


@app.template_filter("chat_display")
def chat_display(value: str) -> str:
    if not value:
        return ""
    if value.endswith("@c.us") or value.endswith("@s.whatsapp.net"):
        return value.split("@", 1)[0]
    return value


def normalize_chat_id(raw_number: str) -> str:
    """
    Devuelve el chatId en formato requerido por Green API.
    """
    if not raw_number:
        raise ValueError("El número de WhatsApp no puede estar vacío")

    raw_number = raw_number.strip()
    if "@" in raw_number:
        return raw_number

    digits = "".join(filter(str.isdigit, raw_number))
    if not digits:
        raise ValueError("El número de WhatsApp debe contener al menos un dígito")

    return f"{digits}@c.us"


def send_whatsapp_message(chat_id: str, message_text: str) -> requests.Response:
    if not (GREEN_API_INSTANCE_ID and GREEN_API_API_TOKEN):
        raise RuntimeError(
            "Faltan credenciales de Green API. Define GREEN_API_INSTANCE_ID y GREEN_API_API_TOKEN en .env"
        )

    url = f"{GREEN_API_BASE_URL}/waInstance{GREEN_API_INSTANCE_ID}/sendMessage/{GREEN_API_API_TOKEN}"
    payload = {"chatId": normalize_chat_id(chat_id), "message": message_text}

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response


@app.route("/")
def dashboard():
    conversations = Conversation.query.order_by(Conversation.updated_at.desc()).all()
    return render_template("index.html", conversations=conversations)


@app.route("/conversation/<int:conversation_id>", methods=["GET", "POST"])
def conversation_detail(conversation_id: int):
    conversation = Conversation.query.get_or_404(conversation_id)

    if request.method == "POST":
        message_text = (request.form.get("message") or "").strip()

        if not message_text:
            abort(400, description="El mensaje no puede estar vacío")

        try:
            response = send_whatsapp_message(conversation.contact_number, message_text)
            data = response.json() if response.headers.get("content-type") == "application/json" else {}
            external_id = data.get("idMessage")
        except requests.exceptions.HTTPError as exc:
            abort(exc.response.status_code, description=f"Error al enviar mensaje: {exc}")
        except Exception as exc:  # noqa: BLE001
            abort(500, description=f"No fue posible enviar el mensaje: {exc}")

        message = Message(
            conversation_id=conversation.id,
            sender_type="agent",
            message_text=message_text,
            sent_at=datetime.utcnow(),
            external_id=external_id,
        )
        conversation.updated_at = datetime.utcnow()

        db.session.add(message)
        db.session.commit()

        return redirect(url_for("conversation_detail", conversation_id=conversation.id))

    messages = conversation.messages.order_by(Message.sent_at.asc()).all()
    return render_template(
        "conversation.html",
        conversation=conversation,
        messages=messages,
    )


@app.route("/conversation/new", methods=["GET", "POST"])
def new_conversation():
    if request.method == "POST":
        raw_number = (request.form.get("contact_number") or "").strip()
        contact_name = (request.form.get("contact_name") or "").strip() or None
        initial_message = (request.form.get("initial_message") or "").strip()

        if not raw_number:
            flash("Debes indicar un número de WhatsApp", "danger")
            return render_template("new_conversation.html")

        try:
            chat_id = normalize_chat_id(raw_number)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("new_conversation.html")

        conversation = Conversation.query.filter_by(contact_number=chat_id).first()
        if conversation:
            flash("Ya existe una conversación con ese número. Te redirigimos.", "info")
            return redirect(url_for("conversation_detail", conversation_id=conversation.id))

        conversation = Conversation(
            contact_number=chat_id,
            contact_name=contact_name,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(conversation)
        db.session.flush()

        if initial_message:
            try:
                response = send_whatsapp_message(chat_id, initial_message)
                data = response.json() if response.headers.get("content-type") == "application/json" else {}
                external_id = data.get("idMessage")
            except requests.exceptions.HTTPError as exc:
                db.session.rollback()
                flash(f"Error al enviar el mensaje inicial: {exc}", "danger")
                return render_template("new_conversation.html")
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                flash(f"No fue posible enviar el mensaje inicial: {exc}", "danger")
                return render_template("new_conversation.html")

            message = Message(
                conversation_id=conversation.id,
                sender_type="agent",
                message_text=initial_message,
                sent_at=datetime.utcnow(),
                external_id=external_id,
            )
            db.session.add(message)

        db.session.commit()
        flash("Conversación creada correctamente.", "success")
        return redirect(url_for("conversation_detail", conversation_id=conversation.id))

    return render_template("new_conversation.html")


@app.post("/webhook/green")
def green_webhook():
    payload = request.get_json(silent=True) or {}
    webhook_type = payload.get("typeWebhook")

    if webhook_type == "incomingMessageReceived":
        return handle_incoming_message(payload)
    if webhook_type == "outgoingMessageReceived":
        return handle_outgoing_message(payload)
    if webhook_type == "outgoingMessageStatus":
        return handle_outgoing_status(payload)

    return jsonify({"status": "ignored", "detail": "Evento no manejado"}), 200


def handle_incoming_message(payload: dict):
    message_data = payload.get("messageData", {})
    sender_data = payload.get("senderData", {})
    text_data = message_data.get("textMessageData") or {}
    message_text = text_data.get("textMessage")

    if not message_text:
        return jsonify({"status": "ignored", "detail": "Mensaje sin texto"}), 200

    chat_id = sender_data.get("chatId")
    contact_name = sender_data.get("senderName") or chat_id
    external_id = payload.get("idMessage")
    timestamp = payload.get("timestamp")
    sent_at = datetime.utcfromtimestamp(timestamp) if timestamp else datetime.utcnow()

    conversation = Conversation.query.filter_by(contact_number=chat_id).first()
    if not conversation:
        conversation = Conversation(
            contact_number=chat_id,
            contact_name=contact_name,
            created_at=sent_at,
            updated_at=sent_at,
        )
        db.session.add(conversation)
        db.session.flush()

    message = Message(
        conversation_id=conversation.id,
        sender_type="customer",
        message_text=message_text,
        sent_at=sent_at,
        external_id=external_id,
    )

    conversation.updated_at = datetime.utcnow()

    db.session.add(message)
    db.session.commit()

    return jsonify({"status": "received"}), 200


def handle_outgoing_message(payload: dict):
    message_data = payload.get("messageData", {})
    chat_id = message_data.get("chatId")
    message_text = (
        message_data.get("textMessageData", {}).get("textMessage")
        or message_data.get("extendedTextMessageData", {}).get("text")
    )
    external_id = payload.get("idMessage")
    timestamp = payload.get("timestamp")
    sent_at = datetime.utcfromtimestamp(timestamp) if timestamp else datetime.utcnow()

    if not (chat_id and message_text):
        return jsonify({"status": "ignored", "detail": "Mensaje saliente sin datos"}), 200

    conversation = Conversation.query.filter_by(contact_number=chat_id).first()
    if not conversation:
        conversation = Conversation(
            contact_number=chat_id,
            contact_name=chat_id,
            created_at=sent_at,
            updated_at=sent_at,
        )
        db.session.add(conversation)
        db.session.flush()

    existing_message = (
        Message.query.filter_by(external_id=external_id, conversation_id=conversation.id)
        .order_by(Message.id.desc())
        .first()
    )

    if existing_message:
        existing_message.sent_at = sent_at
    else:
        message = Message(
            conversation_id=conversation.id,
            sender_type="agent",
            message_text=message_text,
            sent_at=sent_at,
            external_id=external_id,
        )
        db.session.add(message)

    conversation.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "stored"}), 200


def handle_outgoing_status(payload: dict):
    external_id = payload.get("idMessage")
    status = payload.get("status")

    if not external_id:
        return jsonify({"status": "ignored", "detail": "Sin id de mensaje"}), 200

    message = Message.query.filter_by(external_id=external_id).first()
    if not message:
        return jsonify({"status": "ignored", "detail": "Mensaje desconocido"}), 200

    # Aquí podríamos guardar estados adicionales (entregado, leído, etc.).
    # Para mantener el ejemplo sencillo, solo devolvemos el estado.
    return jsonify({"status": "acknowledged", "detail": status}), 200


@app.errorhandler(Exception)
def handle_errors(exc):
    if isinstance(exc, HTTPException):
        code = exc.code or 500
        description = exc.description
    else:
        code = 500
        description = str(exc)

    wants_json = request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html
    if wants_json or request.path.startswith("/webhook"):
        return jsonify({"error": description, "status": code}), code

    return render_template("error.html", message=description, code=code), code


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    ensure_database()
    app.run(debug=os.environ.get("FLASK_ENV") == "development", host="0.0.0.0", port=5000)

