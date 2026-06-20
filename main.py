"""
main.py — Agendify Pedidos
Bot SaaS multi-tenant de toma de pedidos por WhatsApp.
"""
import os
import re
import json
import html
import smtplib
import requests
import datetime
import time
from email.mime.text import MIMEText
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain.tools import tool
import database as db
import geo
import pagos

load_dotenv()

app = FastAPI(title="Agendify Pedidos - Bot de Taqueria SaaS")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "agendify_pedidos")
MAX_HISTORIAL = 10
TZ_MX = datetime.timezone(datetime.timedelta(hours=-6))

# Cache de negocios en memoria (se carga al arrancar)
_negocios_cache: dict = {}   # phone_number_id -> dict negocio
_menu_cache:     dict = {}   # negocio_id -> list de items


def _cargar_negocios():
    global _negocios_cache, _menu_cache
    negocios = db.cargar_negocios()
    for n in negocios:
        pid = n["phone_number_id"]
        _negocios_cache[pid] = n
        _menu_cache[n["id"]] = db.cargar_menu(n["id"])
    print(f"[Startup] {len(_negocios_cache)} negocio(s) cargado(s).")


@app.on_event("startup")
async def startup():
    _cargar_negocios()


@app.get("/health")
def health():
    return {"status": "ok"}


# ── HELPERS DE WHATSAPP ──────────────────────────────────────────────────────

def enviar_whatsapp(telefono: str, mensaje: str, token: str, phone_id: str,
                     intentos: int = 3) -> bool:
    """Envia un mensaje de WhatsApp. Reintenta hasta `intentos` veces con
    backoff simple si falla por un problema momentaneo de red o de la API
    (timeouts, 5xx). Errores claramente no-recuperables (401, 400 por
    numero invalido, etc.) no se reintentan, para no perder tiempo."""
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    }
    ultimo_error = ""
    for intento in range(1, intentos + 1):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=10)
            if r.ok:
                return True
            ultimo_error = f"{r.status_code} {r.text[:200]}"
            # 401 (token invalido/expirado) o 400 (numero invalido) no se
            # arreglan reintentando — fallamos rapido.
            if r.status_code in (400, 401):
                break
        except Exception as e:
            ultimo_error = str(e)
        if intento < intentos:
            time.sleep(1.5 * intento)  # backoff: 1.5s, 3s...

    print(f"!!! FALLO DEFINITIVO enviando WhatsApp a {telefono} tras {intento} intento(s): {ultimo_error}")
    return False


# ── HELPERS DE CORREO ────────────────────────────────────────────────────────

def _html_correo_pedido(negocio: str, nombre: str, telefono: str, items: list,
                         total: float, tipo: str, direccion: str, metodo_pago: str,
                         notas: str = "") -> str:
    color = "#2563eb"
    emoji = "🛵" if tipo == "envio" else "🏪"
    tipo_texto = "Envío a domicilio" if tipo == "envio" else "Para recoger en local"
    filas = "".join(
        f"<tr><td style='padding:4px 8px'>{html.escape(i['nombre'])}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{i['cantidad']}</td>"
        f"<td style='padding:4px 8px;text-align:right'>${i['precio']*i['cantidad']:.2f}</td></tr>"
        for i in items
    )
    dir_html = (
        f"<tr><td style='color:#6b7280;padding:4px 8px'>Dirección</td>"
        f"<td colspan='2' style='padding:4px 8px'><b>{html.escape(direccion)}</b></td></tr>"
        if direccion else ""
    )
    notas_html = (
        f"<tr><td style='color:#6b7280;padding:4px 8px'>Notas</td>"
        f"<td colspan='2' style='padding:4px 8px;color:#dc2626'><b>{html.escape(notas)}</b></td></tr>"
        if notas else ""
    )
    return f"""
    <div style='font-family:sans-serif;max-width:520px;margin:0 auto'>
      <div style='background:{color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0'>
        <b>{emoji} Nuevo pedido en {html.escape(negocio)}</b>
      </div>
      <div style='border:1px solid #e5e7eb;border-top:none;padding:16px 20px;border-radius:0 0 8px 8px'>
        <table style='width:100%;border-collapse:collapse;margin-bottom:12px'>
          <tr><td style='color:#6b7280;padding:4px 8px'>Cliente</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(nombre)}</b></td></tr>
          <tr><td style='color:#6b7280;padding:4px 8px'>Teléfono</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(telefono)}</b></td></tr>
          <tr><td style='color:#6b7280;padding:4px 8px'>Tipo</td>
              <td colspan='2' style='padding:4px 8px'><b>{tipo_texto}</b></td></tr>
          {dir_html}
          {notas_html}
          <tr><td style='color:#6b7280;padding:4px 8px'>Pago</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(metodo_pago or '—')}</b></td></tr>
        </table>
        <table style='width:100%;border-collapse:collapse;background:#f9fafb;border-radius:6px'>
          <tr style='background:#f3f4f6'>
            <th style='padding:6px 8px;text-align:left'>Producto</th>
            <th style='padding:6px 8px'>Cant.</th>
            <th style='padding:6px 8px;text-align:right'>Subtotal</th>
          </tr>
          {filas}
          <tr style='border-top:2px solid #e5e7eb'>
            <td colspan='2' style='padding:8px;font-weight:bold'>TOTAL</td>
            <td style='padding:8px;text-align:right;font-weight:bold;font-size:1.1em'>${total:.2f}</td>
          </tr>
        </table>
        <p style='font-size:11px;color:#9ca3af;margin-top:16px;text-align:center'>
          Enviado automáticamente por Agendify Pedidos.
        </p>
      </div>
    </div>"""


def _enviar_correo_worker(email: str, asunto: str, html_body: str):
    """Worker que corre en hilo separado — nunca bloquea la respuesta al cliente."""
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_pass  = os.getenv("SMTP_APP_PASSWORD", "")
    if not smtp_email or not smtp_pass:
        return
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = asunto
        msg["From"]    = smtp_email
        msg["To"]      = email
        # Timeout de 10s para no bloquear si el servidor no responde
        with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                               context=None,
                               timeout=10) as s:
            s.login(smtp_email, smtp_pass)
            s.sendmail(smtp_email, [email], msg.as_string())
        print(f"   [Correo] Enviado a {email}: {asunto}")
    except Exception as e:
        print(f"!!! Error enviando correo: {e}")


def notificar_dueno(email: str, asunto: str, html_body: str):
    """Lanza el envio de correo en un hilo separado para no bloquear
    la respuesta al cliente aunque el SMTP falle o tarde."""
    if not email:
        return
    import threading
    threading.Thread(
        target=_enviar_correo_worker,
        args=(email, asunto, html_body),
        daemon=True,
    ).start()


# ── HELPERS DEL CARRITO ──────────────────────────────────────────────────────

# Promociones de tacos — Super Tacos George's (confirmadas con el dueño).
# Cada entrada define como se cobra un PAQUETE de N piezas a precio fijo;
# las piezas que sobran fuera del paquete se cobran a precio individual.
# Ej: Bistec 3x$60 -> si piden 7, son 2 paquetes de 3 ($120) + 1 individual
# ($25) = $145, no 7 x $25 = $175.
_PROMOS_TACOS = {
    "Taco de Pastor":  {"paquete": 2, "precio_paquete": 26.00},  # 2x1: paga 1 ($26), lleva 2
    "Taco de Bistec":  {"paquete": 3, "precio_paquete": 60.00},  # 3x$60
    "Taco de Sirloin": {"paquete": 2, "precio_paquete": 50.00},  # 2x$50
    "Taco de Chorizo": {"paquete": 3, "precio_paquete": 55.00},  # 3x$55
}


def _precio_linea(nombre: str, cantidad: int, precio_individual: float) -> float:
    """Calcula el precio de una linea del carrito, aplicando la promo por
    paquete si el producto tiene una (ver _PROMOS_TACOS). Las piezas que
    no completan un paquete se cobran a precio individual normal."""
    promo = _PROMOS_TACOS.get(nombre)
    if not promo:
        return precio_individual * cantidad
    tam_paquete = promo["paquete"]
    precio_paquete = promo["precio_paquete"]
    paquetes_completos = cantidad // tam_paquete
    piezas_sueltas = cantidad % tam_paquete
    return (paquetes_completos * precio_paquete) + (piezas_sueltas * precio_individual)


def _extraer_pago_de_notas(notas_raw: str) -> tuple:
    """Extrae el metodo de pago guardado con el prefijo '[PAGO:X]' del
    campo de notas, si existe. Devuelve (metodo_pago, notas_limpias).
    Esta es la UNICA funcion que debe usarse para esto — antes habia
    lugares que hacian este parseo a mano y otros que se les olvidaba,
    lo que causaba que '[PAGO:Tarjeta]' se mostrara crudo al cliente."""
    if not notas_raw or not notas_raw.startswith("[PAGO:"):
        return "", notas_raw
    cierre = notas_raw.find("]")
    if cierre == -1:
        return "", notas_raw
    metodo = notas_raw[6:cierre]
    notas_limpias = notas_raw[cierre + 1:].strip()
    return metodo, notas_limpias


def _calcular_total(carrito: list) -> float:
    return sum(_precio_linea(i["nombre"], i["cantidad"], i["precio"]) for i in carrito)


def _formato_carrito(carrito: list, costo_envio: float = 0) -> str:
    if not carrito:
        return "🛒 Tu carrito está vacío."
    lineas = ["🛒 *Tu pedido:*"]
    for i in carrito:
        subtotal = _precio_linea(i["nombre"], i["cantidad"], i["precio"])
        promo = _PROMOS_TACOS.get(i["nombre"])
        tiene_promo = promo and i["cantidad"] >= promo["paquete"]
        etiqueta_promo = " 🎉" if tiene_promo else ""
        lineas.append(f"  • {i['cantidad']}x {i['nombre']} — {_fmt_precio(subtotal)}{etiqueta_promo}")
    total = _calcular_total(carrito)
    if costo_envio > 0:
        lineas.append(f"  • Envío — {_fmt_precio(costo_envio)}")
        lineas.append(f"\n💰 *Total: {_fmt_precio(total + costo_envio)}*")
    else:
        lineas.append(f"\n💰 *Total: {_fmt_precio(total)}*")
    return "\n".join(lineas)


_EMOJI_CATEGORIA = {
    "tacos":        "🌮",
    "quesadillas":  "🫔",
    "tortas":       "🥙",
    "bebidas":      "🥤",
    "complementos": "🍟",
    "postres":      "🍮",
    "desayunos":    "🍳",
    "sopas":        "🍲",
    "mariscos":     "🦐",
    "carnes":       "🥩",
    "ensaladas":    "🥗",
    "pizzas":       "🍕",
    "hamburguesas": "🍔",
    "hot dogs":     "🌭",
    "sushi":        "🍱",
    "combos":       "🎁",
    "especiales":   "⭐",
    "antojitos":    "🫓",
}

def _emoji_cat(categoria: str) -> str:
    return _EMOJI_CATEGORIA.get(categoria.lower().strip(), "🍽️")

def _fmt_precio(precio: float) -> str:
    """Muestra precio sin decimales si es numero redondo."""
    return f"${int(precio)}" if precio == int(precio) else f"${precio:.2f}"

def _formato_menu(items: list) -> str:
    if not items:
        return "No hay productos disponibles en este momento."

    por_categoria: dict = {}
    for item in items:
        cat = item.get("categoria", "General")
        por_categoria.setdefault(cat, []).append(item)

    lineas = ["✨ *MENÚ* ✨", ""]

    total_cats = len(por_categoria)
    for idx, (cat, productos) in enumerate(por_categoria.items()):
        emoji = _emoji_cat(cat)
        lineas.append(f"{emoji} *{cat.upper()}*")
        for p in productos:
            precio_txt = _fmt_precio(float(p['precio']))
            promo = _PROMOS_TACOS.get(p['nombre'])
            if promo:
                if promo["paquete"] == 2 and promo["precio_paquete"] == p['precio']:
                    promo_txt = f" _(2x1)_"
                else:
                    promo_txt = f" _({promo['paquete']}x{_fmt_precio(promo['precio_paquete'])})_"
                lineas.append(f"  • *{p['nombre']}* — {precio_txt}{promo_txt}")
            else:
                lineas.append(f"  • *{p['nombre']}* — {precio_txt}")
        if idx < total_cats - 1:
            lineas.append("")

    lineas += [
        "",
        "━━━━━━━━━━━━━━",
        "👇 *¿Qué te gustaría ordenar?*",
        "_Puedes pedir varios productos a la vez, por ejemplo:_",
        "_\"2 tacos de pastor y una horchata\"_",
        "_¿Quieres saber qué lleva algún platillo? Solo pregunta._",
    ]
    return "\n".join(lineas)


def _normalizar_txt(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()


def _singularizar(palabra: str) -> str:
    """Quita plurales simples en espanol: volcanes->volcan, tacos->taco,
    aguas->agua. Permite que 'dame volcanes' encuentre todas las variantes
    de Volcan en el menu sin que el plural rompa la coincidencia."""
    if palabra.endswith("ces"):
        return palabra[:-3] + "z"
    if palabra.endswith("es") and len(palabra) > 4:
        return palabra[:-2]
    if palabra.endswith("s") and len(palabra) > 3:
        return palabra[:-1]
    return palabra


def _buscar_en_menu(texto: str, menu: list) -> Optional[dict]:
    """Busca un producto en el menu por nombre. Solo devuelve resultado si
    hay una unica coincidencia clara (ver _buscar_coincidencias)."""
    coincidencias = _buscar_coincidencias(texto, menu)
    if len(coincidencias) == 1:
        return coincidencias[0]
    return None


def _buscar_coincidencias(texto: str, menu: list) -> list:
    """Devuelve TODAS las coincidencias posibles de un texto contra el menu.
    Prioriza: 1) coincidencia exacta de nombre completo, 2) el texto contiene
    el nombre completo del producto (busqueda especifica), 3) el nombre
    singularizado del texto esta contenido en el nombre del producto
    (busqueda generica, ej. 'volcanes' encuentra las 4 variantes de Volcan).
    Esto evita que un termino generico devuelva solo la primera coincidencia
    cuando en realidad hay varias variantes — en ese caso deben devolverse
    todas para que la herramienta pueda detectar la ambiguedad y preguntar."""
    t = _normalizar_txt(texto.strip())
    t_sing = " ".join(_singularizar(p) for p in t.split())

    exactas = [i for i in menu if _normalizar_txt(i["nombre"]) == t]
    if exactas:
        return exactas

    contiene_nombre = [i for i in menu if _normalizar_txt(i["nombre"]) in t]
    if contiene_nombre:
        return contiene_nombre

    texto_en_nombre = [i for i in menu if t_sing in _normalizar_txt(i["nombre"])]
    if texto_en_nombre:
        return texto_en_nombre

    return [i for i in menu if t in _normalizar_txt(i["nombre"])]


# ── WEBHOOK ──────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN and params.get("hub.challenge"):
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="Token inválido")


@app.post("/webhook")
async def recibir_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        msgs    = value.get("messages", [])
        if not msgs:
            return {"status": "ok"}
        msg     = msgs[0]
        tipo    = msg.get("type", "")
        coords_ubicacion = None
        if tipo == "text":
            texto = msg["text"]["body"].strip()
        elif tipo == "location":
            loc = msg.get("location", {})
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                return {"status": "ok"}
            coords_ubicacion = (float(lat), float(lng))
            # Texto placeholder; procesar_mensaje detecta coords_ubicacion
            # y lo trata como una direccion ya geocodificada, sin volver a
            # llamar a la API de geocoding (es mas preciso y mas rapido).
            nombre_lugar = loc.get("name") or loc.get("address") or ""
            texto = f"[ubicación]{(' ' + nombre_lugar) if nombre_lugar else ''}"
        elif tipo in ("image", "audio", "video", "document", "sticker"):
            texto = f"[{tipo}]"
        else:
            return {"status": "ok"}
        telefono       = msg["from"]
        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
        if not phone_number_id or phone_number_id not in _negocios_cache:
            return {"status": "ok"}
        background_tasks.add_task(procesar_mensaje, texto, telefono, phone_number_id, coords_ubicacion)
        return {"status": "ok"}
    except Exception as e:
        print(f"!!! Error en webhook: {e}")
        return {"status": "ok"}


# ── WEBHOOK DE PAGOS (Mercado Pago) ──────────────────────────────────────────

@app.post("/webhook_pago")
async def recibir_webhook_pago(request: Request, background_tasks: BackgroundTasks):
    """Mercado Pago notifica aqui cuando el estado de un pago cambia.
    IMPORTANTE: Mercado Pago manda el tipo y el id de dos formas distintas
    segun el caso — a veces en el BODY (JSON), a veces como QUERY PARAMS en
    la URL (ej. '?id=123&topic=merchant_order'). Hay que revisar ambos.
    Tambien manda dos topics distintos: 'payment' (id es un payment_id
    directo) y 'merchant_order' (id es una orden que agrupa uno o mas
    pagos — hay que consultarla para sacar los payment_id reales)."""
    try:
        params = dict(request.query_params)
        try:
            body = await request.json()
        except Exception:
            body = {}

        tipo = params.get("topic") or params.get("type") or body.get("type") or body.get("topic")
        id_notificacion = params.get("id") or (body.get("data") or {}).get("id") or body.get("id")

        if not tipo or not id_notificacion:
            return {"status": "ok"}

        if tipo == "payment":
            background_tasks.add_task(procesar_webhook_pago, str(id_notificacion))
        elif tipo == "merchant_order":
            background_tasks.add_task(procesar_webhook_merchant_order, str(id_notificacion))

        return {"status": "ok"}
    except Exception as e:
        print(f"!!! Error en webhook_pago: {e}")
        return {"status": "ok"}


def procesar_webhook_merchant_order(order_id: str):
    """Una merchant_order agrupa los pagos asociados a una preferencia.
    La consultamos para sacar los payment_id reales y procesarlos igual
    que si hubieran llegado como notificacion tipo 'payment'."""
    try:
        info = pagos.consultar_merchant_order(order_id)
        if not info:
            print(f"!!! No se pudo consultar merchant_order {order_id}")
            return
        for payment_id in info.get("payment_ids", []):
            procesar_webhook_pago(payment_id)
    except Exception as e:
        print(f"!!! Error en procesar_webhook_merchant_order: {e}")


def procesar_webhook_pago(payment_id: str):
    """Consulta el pago en Mercado Pago, encuentra el pedido relacionado
    por su preference_id, y si el pago fue aprobado lo pasa a cocina."""
    try:
        info = pagos.consultar_pago(payment_id)
        if not info:
            print(f"!!! No se pudo consultar el pago {payment_id}")
            return

        referencia = info.get("external_reference", "")
        pedido = db.buscar_pedido_por_referencia(referencia)
        if not pedido:
            # Puede pasar si el pago no corresponde a esta taqueria, o si
            # el webhook llega antes de que guardemos la referencia — no
            # es necesariamente un error.
            print(f"!!! No se encontró pedido para referencia '{referencia}' (pago {payment_id})")
            return

        pedido_id = pedido["id"]
        estado_mp = info.get("status")

        if estado_mp == "approved":
            db.confirmar_pago_pedido(pedido_id, payment_id)
            print(f"   [Pagos] Pedido #{pedido_id} — pago APROBADO (${info.get('monto')}). Pasa a cocina.")
            # Avisamos al cliente que su pedido ya esta confirmado. Buscamos
            # el negocio (telefono + token de WhatsApp) por su negocio_id
            # entre los negocios cargados en cache.
            telefono_neg, token_neg = None, None
            for pid, neg in _negocios_cache.items():
                if neg.get("id") == pedido.get("negocio_id"):
                    token_neg = neg.get("whatsapp_token")
                    telefono_neg = pid
                    break
            if telefono_neg and token_neg:
                resp = (
                    f"✅ *¡Pago confirmado! Pedido #{pedido_id}*\n\n"
                    f"Tu pedido ya pasó a preparación. 🌮\n"
                    f"¡Gracias por tu preferencia!"
                )
                enviar_whatsapp(pedido["telefono"], resp, token_neg, telefono_neg)

        elif estado_mp == "rejected":
            db.rechazar_pago_pedido(pedido_id, payment_id)
            print(f"   [Pagos] Pedido #{pedido_id} — pago RECHAZADO.")
            for pid, neg in _negocios_cache.items():
                if neg.get("id") == pedido.get("negocio_id"):
                    resp = (
                        f"❌ Tu pago para el pedido #{pedido_id} no pudo procesarse.\n"
                        f"¿Quieres intentar de nuevo o prefieres pagar en efectivo?"
                    )
                    enviar_whatsapp(pedido["telefono"], resp, neg.get("whatsapp_token"), pid)
                    break
        else:
            print(f"   [Pagos] Pedido #{pedido_id} — pago en estado '{estado_mp}', sin acción.")

    except Exception as e:
        import traceback
        print(f"!!! Error en procesar_webhook_pago: {e}\n{traceback.format_exc()}")


# ── PROCESAMIENTO PRINCIPAL ──────────────────────────────────────────────────

def procesar_mensaje(texto: str, telefono: str, phone_number_id: str, coords_ubicacion=None):
    try:
        # Leemos la config del negocio SIEMPRE fresca de Supabase para que
        # cualquier cambio desde el panel aplique en el siguiente mensaje,
        # sin necesidad de reiniciar el servicio.
        negocio_cache = _negocios_cache.get(phone_number_id)
        if not negocio_cache:
            return
        negocio_id = negocio_cache["id"]

        # Recarga la fila del negocio directo de DB
        negocios_frescos = db.cargar_negocios()
        negocio = next((n for n in negocios_frescos if n["id"] == negocio_id), negocio_cache)
        # Actualizar cache en memoria tambien
        _negocios_cache[phone_number_id] = negocio

        nombre_neg    = negocio["nombre"]
        token         = negocio["whatsapp_token"]
        menu          = _menu_cache.get(negocio_id, [])
        email_notif   = negocio.get("email_notificaciones", "")
        tipo_servicio = negocio.get("tipo_servicio", "ambos")
        costo_envio   = float(negocio.get("costo_envio") or 0)
        pedido_min    = float(negocio.get("pedido_minimo") or 0)
        tiempo_rec    = negocio.get("tiempo_recoger_min", 20)
        tiempo_env    = negocio.get("tiempo_envio_min", 40)
        metodos_pago  = negocio.get("metodos_pago", "Efectivo")
        envio_dinamico = bool(negocio.get("costo_envio_dinamico"))
        modo_lluvia    = bool(negocio.get("modo_lluvia"))
        coords_negocio = None
        if negocio.get("lat") and negocio.get("lng"):
            coords_negocio = (float(negocio["lat"]), float(negocio["lng"]))

        llave   = f"{phone_number_id}_{telefono}"
        sesion  = db.cargar_sesion(llave)
        historial  = sesion["historial"]
        carrito    = sesion["carrito"] or []
        fase       = sesion["fase_pedido"] or ""
        texto_low  = texto.lower().strip()

        print(f"-> [Procesando] {telefono} | fase={fase or 'inicio'} | msg='{texto[:60]}'")

        # ── CANCELAR PEDIDO ───────────────────────────────────────────────
        # El cliente puede cancelar su pedido mas reciente en cualquier
        # momento, SIEMPRE que todavia no haya pasado a 'en_proceso' (la
        # cocina ya empezo a prepararlo). Esto funciona desde cualquier
        # fase y aunque el negocio este cerrado, porque cancelar es una
        # accion que no depende del horario.
        _PALABRAS_CANCELAR = {"cancelar", "cancela", "cancelar pedido", "anular", "anula"}
        if texto_low.strip(".,!¡¿? ") in _PALABRAS_CANCELAR or texto_low.startswith("cancelar"):
            pedido_cancelable = db.buscar_pedido_cancelable(negocio_id, telefono)
            if pedido_cancelable:
                db.actualizar_estado_pedido(pedido_cancelable["id"], "cancelado")
                resp = (
                    f"❌ Tu pedido #{pedido_cancelable['id']} fue cancelado. "
                    f"Si cambias de opinión, aquí estamos para ayudarte. 🌮"
                )
                db.limpiar_sesion(llave)
                print(f"   [{nombre_neg}] Pedido #{pedido_cancelable['id']} cancelado por el cliente.")
            else:
                resp = (
                    "No encontré ningún pedido tuyo que se pueda cancelar en este momento "
                    "(puede que ya esté en preparación — en ese caso contáctanos directo)."
                )
            enviar_whatsapp(telefono, resp, token, phone_number_id)
            return

        # Caducidad de sesion por inactividad: si una sesion con fase activa
        # (pedido a medias) lleva mas de 2 horas sin actividad, la
        # consideramos abandonada y la limpiamos automaticamente — SIN
        # depender de que el cliente salude primero. Esto evita que un
        # cliente real quede atorado en una fase vieja para siempre si
        # simplemente dejo de contestar a medio pedido y vuelve dias
        # despues con un mensaje cualquiera (no necesariamente un saludo).
        SESION_CADUCIDAD_HORAS = 2
        if fase and sesion.get("updated_at"):
            try:
                ts_str = sesion["updated_at"].replace("Z", "+00:00")
                ts_actualizacion = datetime.datetime.fromisoformat(ts_str)
                horas_inactivo = (datetime.datetime.now(datetime.timezone.utc) - ts_actualizacion).total_seconds() / 3600
                if horas_inactivo >= SESION_CADUCIDAD_HORAS:
                    db.limpiar_sesion(llave)
                    carrito = []
                    fase = ""
                    sesion = db.cargar_sesion(llave)
                    historial = sesion["historial"]
                    print(f"   [{nombre_neg}] Sesión caducada por inactividad ({horas_inactivo:.1f}h), limpiada automáticamente.")
            except Exception as e:
                print(f"!!! Error calculando caducidad de sesión: {e}")

        # Limpieza de carrito huerfano: si hay productos en el carrito pero
        # no hay una fase activa (el pedido quedo a medias por algun error),
        # limpiamos al detectar un saludo nuevo para empezar desde cero.
        # (Esta es una red de seguridad adicional para el caso especifico
        # de carrito sin fase; la caducidad de arriba cubre el caso general
        # de cualquier fase atorada, con o sin saludo.)
        _SALUDOS = {"hola","buenas","buen dia","buenos dias","buenas tardes",
                    "buenas noches","hey","hi","hello","ola","saludos"}
        if carrito and not fase and any(s in texto_low for s in _SALUDOS):
            db.limpiar_sesion(llave)
            carrito = []
            sesion  = db.cargar_sesion(llave)
            historial = sesion["historial"]
            print(f"   [{nombre_neg}] Carrito huerfano limpiado al detectar saludo nuevo.")

        # ── VALIDACION DE HORARIO ────────────────────────────────────────────
        # Si el negocio esta cerrado (cerrado_hoy o fuera de horario), avisamos
        # y no procesamos el pedido. Solo se permite si ya habia una sesion
        # activa en curso (carrito con productos o en fase de confirmacion),
        # para no cortar a un cliente a la mitad de su pedido.
        def _esta_abierto() -> bool:
            if negocio.get("cerrado_hoy"):
                return False
            horarios = negocio.get("horarios_json") or {}
            if isinstance(horarios, str):
                import json as _j
                try: horarios = _j.loads(horarios)
                except: return True  # si no se puede parsear, no bloqueamos
            ahora_mx = datetime.datetime.now(TZ_MX)
            dia_semana = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][ahora_mx.weekday()]
            info = horarios.get(dia_semana, {})
            if not info.get("abierto", True):
                return False
            try:
                ap_h, ap_m = map(int, info.get("apertura","00:00").split(":"))
                ci_h, ci_m = map(int, info.get("cierre","23:59").split(":"))
                apertura_min = ap_h * 60 + ap_m
                cierre_min   = ci_h * 60 + ci_m
                actual_min   = ahora_mx.hour * 60 + ahora_mx.minute
                if cierre_min < apertura_min:
                    # cruza medianoche (ej. 18:00 a 01:00)
                    return actual_min >= apertura_min or actual_min <= cierre_min
                return apertura_min <= actual_min <= cierre_min
            except Exception:
                return True  # si falla el parse, no bloqueamos

        sesion_activa = bool(carrito) or fase in ("confirmando", "nombre", "direccion", "tipo")
        if not _esta_abierto() and not sesion_activa:
            msg_cerrado = negocio.get("mensaje_cerrado") or (
                "Lo sentimos, en este momento estamos cerrados. "
                "¡Te esperamos en nuestro horario de atención! 🕐"
            )
            enviar_whatsapp(telefono, msg_cerrado, token, phone_number_id)
            print(f"   [{nombre_neg}] Fuera de horario — mensaje de cerrado enviado.")
            return

        # ── FLUJO DETERMINISTICO POR FASE ───────────────────────────────────

        # FASE: confirmando — el cliente responde SI/NO al resumen del pedido
        if fase == "confirmando":
            if any(p in texto_low for p in ["si", "sí", "confirmo", "dale", "va", "ok", "correcto", "listo"]):
                tipo_entrega = sesion["tipo_entrega"]
                nombre_cl    = sesion["nombre_cliente"]
                direccion    = sesion["direccion_entrega"]
                notas_raw    = sesion.get("notas_pedido", "")
                costo_envio_real = sesion.get("costo_envio_calc", 0) or costo_envio
                total        = _calcular_total(carrito)
                total_con_envio = total + (costo_envio_real if tipo_entrega == "envio" else 0)

                # Extraer el metodo de pago si viene en el prefijo [PAGO:..]
                # (solo aplica a envios, ver fase "pago" mas arriba).
                metodo_pago_usado, notas = _extraer_pago_de_notas(notas_raw)

                # Segunda capa de proteccion: el metodo de pago online SOLO
                # tiene sentido si el pedido es de envio. Si por algun
                # motivo quedo un [PAGO:...] arrastrado en la nota pero el
                # pedido actual es para recoger, lo ignoramos — evita
                # cobrar con un metodo de pago de un pedido anterior.
                if tipo_entrega != "envio":
                    metodo_pago_usado = ""

                requiere_pago_online = metodo_pago_usado in ("Tarjeta", "Transferencia")

                if requiere_pago_online:
                    # No confirmamos el pedido todavia — lo guardamos en
                    # estado 'pendiente_pago' (no aparece en cocina) y le
                    # mandamos al cliente un link de Mercado Pago. Solo
                    # cuando el webhook de pagos confirme el pago aprobado,
                    # el pedido pasa a 'nuevo' y aparece en la cocina.
                    referencia = f"taqueria_{telefono}_{int(datetime.datetime.now().timestamp())}"
                    pedido_id = db.guardar_pedido(
                        negocio_id=negocio_id, telefono=telefono,
                        nombre_cliente=nombre_cl, items=carrito,
                        total=total_con_envio, tipo_entrega=tipo_entrega,
                        direccion=direccion, notas=notas,
                        metodo_pago=metodo_pago_usado,
                        estado_pago="pendiente",
                        estado_inicial="pendiente_pago",
                    )
                    if not pedido_id:
                        resp = "Hubo un problema guardando tu pedido. Por favor intenta de nuevo en un momento."
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        return

                    url_webhook = "https://bot-taqueria.onrender.com/webhook_pago"
                    resultado_mp = pagos.crear_link_pago(
                        pedido_referencia=referencia,
                        nombre_negocio=nombre_neg,
                        descripcion=f"Pedido #{pedido_id}",
                        monto=total_con_envio,
                        url_notificacion=url_webhook,
                    )

                    if not resultado_mp:
                        # Fallo Mercado Pago — no dejamos al cliente colgado,
                        # ofrecemos efectivo como respaldo.
                        resp = (
                            "Tuvimos un problema generando el link de pago. 😕\n"
                            "¿Prefieres pagar en efectivo al recibir tu pedido?"
                        )
                        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago", carrito=carrito)
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        return

                    # Guardamos la referencia de Mercado Pago en el pedido
                    # para poder encontrarlo cuando llegue el webhook.
                    db.actualizar_pedido_referencia_mp(pedido_id, resultado_mp["preference_id"])

                    resp = (
                        f"📋 Tu pedido #{pedido_id} está listo, solo falta el pago.\n\n"
                        f"💰 Total a pagar: *{_fmt_precio(total_con_envio)}*\n"
                        f"💳 Método: *{metodo_pago_usado}*\n\n"
                        f"Paga aquí de forma segura:\n{resultado_mp['link_pago']}\n\n"
                        f"_En cuanto se confirme tu pago, tu pedido pasará a preparación automáticamente._ ✅"
                    )
                    db.limpiar_sesion(llave)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Pedido #{pedido_id} pendiente de pago ({metodo_pago_usado}) — link generado.")
                    return

                # Efectivo (o sin metodo de pago, ej. para recoger): se
                # confirma directo, igual que antes.
                pedido_id = db.guardar_pedido(
                    negocio_id=negocio_id, telefono=telefono,
                    nombre_cliente=nombre_cl, items=carrito,
                    total=total_con_envio, tipo_entrega=tipo_entrega,
                    direccion=direccion, notas=notas,
                    metodo_pago=metodo_pago_usado,
                )
                tiempo = tiempo_env if tipo_entrega == "envio" else tiempo_rec
                notas_linea = f"📝 {notas}\n" if notas else ""
                pago_linea = f"💳 Pago: *{metodo_pago_usado}*\n" if metodo_pago_usado else ""
                resp = (
                    f"✅ *¡Pedido #{pedido_id} confirmado!*\n\n"
                    f"👤 *{nombre_cl}*\n"
                    f"{'🛵 Envío a: ' + direccion if tipo_entrega == 'envio' else '🏪 Para recoger en el local'}\n"
                    f"{pago_linea}"
                    f"{notas_linea}"
                    f"⏱ Tiempo estimado: *{tiempo} minutos*\n"
                    f"💰 Total: *{_fmt_precio(total_con_envio)}*\n\n"
                    f"¡Gracias por tu preferencia! 🌮\n"
                    f"_Tu pedido ya está en preparación._"
                )
                if email_notif:
                    notificar_dueno(
                        email_notif,
                        f"Nuevo pedido #{pedido_id} en {nombre_neg}",
                        _html_correo_pedido(
                            nombre_neg, nombre_cl, telefono, carrito,
                            total_con_envio, tipo_entrega, direccion,
                            metodo_pago_usado or metodos_pago, notas,
                        ),
                    )
                db.limpiar_sesion(llave)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Pedido #{pedido_id} confirmado — {nombre_cl} / ${total_con_envio:.2f}")
                return

            elif any(p in texto_low for p in ["no", "cancel", "cambiar", "modificar", "error"]):
                resp = (
                    "Sin problema. ¿Qué quieres hacer?\n\n"
                    "1️⃣ *Agregar* un producto\n"
                    "2️⃣ *Quitar* un producto\n"
                    "3️⃣ *Cancelar* el pedido\n\n"
                    "Solo dime qué necesitas, por ejemplo: "
                    "_\"agrega una horchata\"_ o _\"quita el alambre\"_"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="armando", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            else:
                # El cliente dijo algo que no es SÍ ni NO (saludo, pregunta,
                # etc.) — le recordamos que tiene un pedido pendiente en vez
                # de caer al LLM que puede ignorar la fase y hacer cualquier cosa.
                costo_envio_real = sesion.get("costo_envio_calc", 0) or costo_envio
                resumen = _formato_carrito(carrito, costo_envio_real if sesion.get("tipo_entrega") == "envio" else 0)
                resp = (
                    f"Tienes un pedido pendiente de confirmar 👆\n\n"
                    f"{resumen}\n\n"
                    f"¿Lo confirmas?\n"
                    f"✅ *SÍ* para confirmar  |  ❌ *NO* para modificar"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

        # FASE: nombre — esperando el nombre del cliente
        if fase == "nombre":
            nombre_cl = texto.strip().title()
            # Frases y palabras que NO son nombres
            _NO_ES_NOMBRE_PALABRAS = {
                "no","si","sí","ok","okay","vale","cancel","cancelar",
                "modificar","cambiar","espera","otro","otra","nada","ninguno",
            }
            _NO_ES_NOMBRE_FRASES = [
                "para llevar","para recoger","para envio","para envío",
                "a domicilio","sin cilantro","sin cebolla","sin chile",
                "con todo","sin todo","para mi","es todo","ya es todo",
                "nada mas","nada más",
            ]
            texto_lower_n = texto.lower().strip(".,!¡¿? ")
            es_nombre_valido = (
                len(nombre_cl) >= 2
                and not any(c.isdigit() for c in nombre_cl)
                and texto_lower_n not in _NO_ES_NOMBRE_PALABRAS
                and not any(f in texto_lower_n for f in _NO_ES_NOMBRE_FRASES)
                and len(nombre_cl.split()) <= 4
            )
            if es_nombre_valido:
                carrito      = sesion["carrito"]
                tipo_entrega = sesion["tipo_entrega"]
                direccion    = sesion["direccion_entrega"]

                # Si es ENVÍO, antes del resumen final preguntamos el método
                # de pago (el dueño confirmó que pide el pago por adelantado
                # para pedidos a domicilio). Si es para recoger, se salta este
                # paso — el cliente paga al llegar al local.
                if tipo_entrega == "envio":
                    metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                    resp = (
                        f"Para pedidos con envío a domicilio pedimos el pago por adelantado.\n\n"
                        f"¿Cómo deseas pagar? Opciones: *{metodos_lista}*"
                    )
                    db.guardar_sesion(llave, historial, fase_pedido="pago",
                                      nombre_cliente=nombre_cl, carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago",
                                      nombre_cliente=nombre_cl, carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Nombre capturado: {nombre_cl}, pidiendo método de pago.")
                    return

                # Recoger: no se pregunta metodo de pago, va directo al resumen.
                # IMPORTANTE: aunque aqui nunca deberiamos tener un [PAGO:..]
                # legitimo (la fase "pago" no aplica a recoger), igual lo
                # limpiamos por seguridad — si quedo arrastrado de un pedido
                # anterior, NUNCA debe mostrarse crudo al cliente.
                _, notas = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                total        = _calcular_total(carrito)
                resumen = _formato_carrito(carrito)
                notas_linea = f"📝 *Notas:* {notas}\n" if notas else ""
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"🏪 *Para recoger* en el local\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                db.guardar_sesion(llave, historial, fase_pedido="confirmando",
                                  nombre_cliente=nombre_cl, carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  nombre_cliente=nombre_cl, carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Nombre capturado: {nombre_cl}, mostrando resumen.")
                return
            else:
                # El cliente escribio algo que no parece un nombre — puede
                # ser una pregunta suelta ("¿hasta que hora abren?") en
                # medio del flujo. No lo mandamos al LLM libre (eso podria
                # sacarlo de la fase sin querer); solo reconocemos el
                # mensaje y volvemos a pedir el dato pendiente, para no
                # perder el progreso del pedido.
                resp = (
                    "Veo que me escribiste algo más, ¡con gusto te ayudo después! 😊\n"
                    "Por ahora, ¿me confirmas a nombre de quién registramos tu pedido?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto no parece nombre ('{texto[:30]}'), reiterando pregunta.")
                return

        # FASE: pago — esperando metodo de pago (solo para pedidos de envio)
        if fase == "pago":
            metodos_validos = [m.strip().lower() for m in metodos_pago.split(",")]
            metodo_elegido = None
            for m in metodos_validos:
                if m in texto_low:
                    metodo_elegido = m.title()
                    break
            if not metodo_elegido:
                # Tambien aceptamos sinonimos comunes
                if any(p in texto_low for p in ["efect", "cash"]):
                    metodo_elegido = "Efectivo"
                elif any(p in texto_low for p in ["transf", "deposito", "spei"]):
                    metodo_elegido = "Transferencia"
                elif any(p in texto_low for p in ["tarjeta", "card", "credito", "débito", "debito"]):
                    metodo_elegido = "Tarjeta"

            if metodo_elegido:
                carrito      = sesion["carrito"]
                nombre_cl    = sesion["nombre_cliente"]
                direccion    = sesion["direccion_entrega"]
                # Limpiamos cualquier [PAGO:..] viejo arrastrado ANTES de
                # mostrar las notas o de envolverlas con el nuevo metodo —
                # si no se hace esto, un [PAGO:Tarjeta] viejo termina
                # mostrandose crudo, o anidado dentro de un nuevo [PAGO:..].
                _, notas = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                costo_envio_real = sesion.get("costo_envio_calc", 0) or costo_envio
                total        = _calcular_total(carrito)
                total_con_envio = total + costo_envio_real
                resumen = _formato_carrito(carrito, costo_envio_real)
                notas_linea = f"📝 *Notas:* {notas}\n" if notas else ""
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"🛵 *Envío a:* {direccion}\n"
                    f"💳 *Pago:* {metodo_elegido}\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                # Guardamos el metodo de pago dentro de notas_pedido con un
                # prefijo reconocible (no hay columna dedicada para esto en
                # la sesion). Se extrae despues al confirmar el pedido, ya
                # limpio (sin anidar con un [PAGO:..] anterior).
                notas_con_pago = f"[PAGO:{metodo_elegido}] {notas}".strip()
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  carrito=carrito, notas_pedido=notas_con_pago)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Método de pago: {metodo_elegido}, mostrando resumen.")
                return
            else:
                metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                resp = f"No reconocí ese método de pago. Por favor elige una opción: *{metodos_lista}*"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return
        if fase == "direccion":
            direccion = texto.strip()

            # Si el cliente PEGA un link de Google Maps como texto (en vez de
            # usar el boton nativo de "Compartir ubicacion" de WhatsApp),
            # extraemos las coordenadas del link con regex y lo tratamos
            # igual que una ubicacion GPS real — evita geocodificar la URL
            # cruda como si fuera una direccion de texto, lo cual no funciona.
            if not coords_ubicacion and ("maps.google" in direccion or "google.com/maps" in direccion or "goo.gl/maps" in direccion):
                import re
                match = re.search(r"[?&]q=(-?\d+\.\d+)[,%2C\s]+(-?\d+\.\d+)", direccion)
                if not match:
                    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", direccion)
                if match:
                    coords_ubicacion = (float(match.group(1)), float(match.group(2)))
                    print(f"   [{nombre_neg}] Coordenadas extraídas de link de Maps: {coords_ubicacion}")

            # Si el cliente comparte su ubicacion GPS (tipo "location" de
            # WhatsApp, o un link de Maps con coordenadas extraidas arriba),
            # usamos las coordenadas exactas directo — es mas preciso que
            # pedirle que escriba la direccion, y mas rapido (sin geocoding
            # de texto). Guardamos un texto legible para el resumen y correo.
            if coords_ubicacion:
                direccion = "📍 Ubicación compartida por WhatsApp"
                costo_calculado = costo_envio
                aviso_envio = ""

                if envio_dinamico and coords_negocio:
                    resultado = geo.calcular_envio_desde_coords(coords_ubicacion, coords_negocio, lluvia=modo_lluvia)
                    if resultado["ok"]:
                        costo_calculado = resultado["costo"]
                        km = resultado["km"]
                        aviso_envio = f"\n_Distancia: {km} km — Envío: {_fmt_precio(costo_calculado)}_"
                        # Texto legible con la distancia + link aparte, para
                        # que no se vea como una URL larga y cruda pegada al
                        # texto. El link sigue siendo clicable para el dueño
                        # y los repartidores (correo, botón de cocina, etc.).
                        lat, lng = coords_ubicacion
                        direccion = f"📍 Ubicación compartida ({km} km)\nhttps://maps.google.com/?q={lat},{lng}"
                    elif "Fuera de cobertura" in resultado["razon"]:
                        resp = (
                            f"Tu ubicación está fuera de nuestra zona de cobertura para envío "
                            f"({resultado['km']} km, máximo 20 km). "
                            f"¿Prefieres pasar a recoger tu pedido al local?"
                        )
                        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        print(f"   [{nombre_neg}] Ubicación fuera de cobertura ({resultado['km']} km).")
                        return
                    else:
                        # Fallo el calculo de distancia con la API (raro, pero
                        # puede pasar) — seguimos con la tarifa fija como
                        # respaldo en vez de trabar al cliente.
                        print(f"   [{nombre_neg}] No se pudo calcular distancia desde ubicación: {resultado['razon']}")
                        lat, lng = coords_ubicacion
                        direccion = f"📍 Ubicación compartida\nhttps://maps.google.com/?q={lat},{lng}"

                resp = f"¿A nombre de quién registramos el pedido?{aviso_envio}"
                db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Ubicación GPS capturada — envío: ${costo_calculado}")
                return

            if len(direccion) >= 5:
                costo_calculado = costo_envio  # fallback: tarifa fija configurada
                aviso_envio = ""

                if envio_dinamico and coords_negocio:
                    resultado = geo.calcular_envio_completo(direccion, coords_negocio, lluvia=modo_lluvia)
                    if resultado["ok"]:
                        costo_calculado = resultado["costo"]
                        km = resultado["km"]
                        aviso_envio = f"\n_Distancia: {km} km — Envío: {_fmt_precio(costo_calculado)}_"
                    else:
                        # No se pudo calcular (direccion no encontrada o fuera
                        # de cobertura): avisamos y pedimos que confirme o
                        # de una direccion mas precisa, sin trabar el flujo.
                        if "Fuera de cobertura" in resultado["razon"]:
                            resp = (
                                f"Tu dirección está fuera de nuestra zona de cobertura para envío "
                                f"({resultado['km']} km, máximo 20 km). "
                                f"¿Prefieres pasar a recoger tu pedido al local?"
                            )
                            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                            enviar_whatsapp(telefono, resp, token, phone_number_id)
                            print(f"   [{nombre_neg}] Dirección fuera de cobertura ({resultado['km']} km).")
                            return
                        else:
                            resp = (
                                "No logré ubicar bien esa dirección. ¿Puedes darme más detalles "
                                "(calle, número, colonia), o mejor mándame tu ubicación con el "
                                "📎 de WhatsApp para calcular el envío exacto?"
                            )
                            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                            enviar_whatsapp(telefono, resp, token, phone_number_id)
                            print(f"   [{nombre_neg}] No se pudo geocodificar: '{direccion}' — {resultado['razon']}")
                            return

                resp = f"¿A nombre de quién registramos el pedido?{aviso_envio}"
                db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Dirección capturada: {direccion[:40]} — envío: ${costo_calculado}")
                return
            else:
                # El texto es muy corto para ser una direccion real (ej.
                # "ok", o una pregunta suelta). Reiteramos la pregunta sin
                # perder la fase, en vez de dejar que caiga al LLM libre.
                resp = (
                    "No alcancé a identificar bien tu dirección. ¿Me la compartes de nuevo "
                    "(calle, número, colonia), o mándame tu ubicación con el 📎 de WhatsApp?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto muy corto para dirección ('{texto[:30]}'), reiterando pregunta.")
                return

        # FASE: tipo — esperando si es para recoger o envío
        if fase == "tipo":
            if any(p in texto_low for p in ["recoger", "local", "paso", "voy", "ahi voy"]):
                if tipo_servicio == "envio":
                    resp = "Solo manejamos envío a domicilio. ¿Me das tu dirección de entrega?"
                    db.guardar_sesion(llave, historial, fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                else:
                    resp = "¿A nombre de quién registramos el pedido?"
                    db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            elif any(p in texto_low for p in ["envio", "envío", "domicilio", "llevar", "lleva", "mandar", "delivery"]):
                if tipo_servicio == "recoger":
                    resp = "Solo manejamos recolección en el local. ¿A qué nombre registramos tu pedido?"
                    db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return

                # Validar pedido minimo ANTES de pasar a pedir direccion —
                # el minimo solo aplica a envio, asi que es aqui donde
                # corresponde checarlo, no antes (cuando aun no sabiamos
                # si era recoger o envio).
                total_actual = _calcular_total(carrito)
                if pedido_min > 0 and total_actual < pedido_min:
                    falta = pedido_min - total_actual
                    resumen = _formato_carrito(carrito)
                    resp = (
                        f"{resumen}\n\n"
                        f"_Para envío a domicilio el pedido mínimo es de {_fmt_precio(pedido_min)}. "
                        f"Te faltan {_fmt_precio(falta)}._\n\n"
                        f"¿Quieres agregar algo más, o prefieres pasar a recoger tu pedido sin mínimo?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return

                resp = "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                db.guardar_sesion(llave, historial, fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            else:
                # El cliente escribio algo que no es "recoger" ni "envio"
                # (ej. una pregunta suelta). No lo mandamos al LLM libre
                # porque eso podria sacarlo de la fase sin querer y perder
                # el carrito. Reiteramos la pregunta pendiente.
                resp = (
                    "Antes de seguir, ¿me confirmas si tu pedido es para *recoger en el local* "
                    "o *envío a domicilio*? 😊"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto no reconocido en fase 'tipo' ('{texto[:30]}'), reiterando pregunta.")
                return

        # ── LLM CON HERRAMIENTAS ─────────────────────────────────────────────
        # El modelo maneja: saludo, mostrar menu, agregar/quitar productos del
        # carrito, y disparar el cierre del pedido cuando el cliente diga "ya es
        # todo" / "eso seria todo" / etc.

        # Herramientas disponibles para el modelo
        carrito_estado = list(carrito)  # mutable dentro del turno

        @tool
        def ver_menu() -> str:
            """Muestra el menu completo con precios al cliente."""
            return _formato_menu(menu)

        @tool
        def info_producto(nombre_producto: str) -> str:
            """Usa esta herramienta cuando el cliente pregunta que lleva o que
            ingredientes tiene un producto especifico (ej. '¿qué lleva la Juana?',
            '¿qué es el Volcán?', '¿tiene queso?'). Devuelve la descripcion exacta
            del producto segun el menu — NUNCA inventes ingredientes."""
            item = _buscar_en_menu(nombre_producto, menu)
            if not item:
                return f"No encontré '{nombre_producto}' en el menú. ¿Te refieres a otro producto?"
            desc = item.get("descripcion", "").strip()
            if not desc:
                return (
                    f"*{item['nombre']}* — {_fmt_precio(float(item['precio']))}\n"
                    f"_No tenemos descripción adicional de este producto._"
                )
            return (
                f"*{item['nombre']}* lleva: {desc}.\n"
                f"Precio: {_fmt_precio(float(item['precio']))} 🌮"
            )

        @tool
        def agregar_al_carrito(nombre_producto: str, cantidad: int = 1) -> str:
            """Agrega un producto al carrito del cliente.
            nombre_producto: copia TEXTUALMENTE las palabras que el cliente usó
            para nombrar el producto. Por ejemplo, si el cliente dice "quiero 2
            que me ves", pasa nombre_producto="que me ves" — NUNCA lo cambies
            por una categoría como "tacos" ni inventes un nombre distinto.
            cantidad: cuantos quiere (default 1).
            IMPORTANTE: si el cliente menciona un producto generico que tiene
            varias variantes en el menu (ej. 'volcán' cuando hay Volcán de
            Pastor/Bistec/Sirloin/Chorizo, o 'agua' cuando hay Agua 1L y ½L),
            esta herramienta te devolverá la lista de opciones — debes
            preguntarle al cliente cuál quiere en vez de elegir tú.
            El resultado SIEMPRE incluye el carrito completo actualizado con
            precios y subtotal — usa ese texto tal cual en tu respuesta al
            cliente, no lo reescribas ni lo resumas tú mismo."""
            coincidencias = _buscar_coincidencias(nombre_producto, menu)

            if not coincidencias:
                nombres = ", ".join(i["nombre"] for i in menu)
                return f"No encontré '{nombre_producto}' en el menú. Los productos disponibles son: {nombres}."

            if len(coincidencias) > 1:
                opciones = "\n".join(
                    f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in coincidencias
                )
                return (
                    f"Tenemos varias opciones, ¿cuál te gustaría? 😊\n{opciones}"
                )

            item = coincidencias[0]
            ya_existia = False
            for c in carrito_estado:
                if c["nombre"] == item["nombre"]:
                    c["cantidad"] += max(1, cantidad)
                    ya_existia = True
                    break
            if not ya_existia:
                carrito_estado.append({
                    "nombre":   item["nombre"],
                    "precio":   float(item["precio"]),
                    "cantidad": max(1, cantidad),
                })

            carrito_txt = _formato_carrito(carrito_estado)
            subtotal = _calcular_total(carrito_estado)
            aviso_min = ""
            if pedido_min > 0 and subtotal < pedido_min:
                falta = pedido_min - subtotal
                aviso_min = f"\n\n_Te faltan {_fmt_precio(falta)} para el pedido mínimo de {_fmt_precio(pedido_min)}._"
            return f"{carrito_txt}{aviso_min}\n\n¿Algo más, o ya sería todo? 😊"

        @tool
        def quitar_del_carrito(nombre_producto: str) -> str:
            """Quita un producto del carrito del cliente."""
            item = _buscar_en_menu(nombre_producto, menu)
            nombre_buscado = item["nombre"] if item else nombre_producto
            for i, c in enumerate(carrito_estado):
                if c["nombre"].lower() == nombre_buscado.lower():
                    carrito_estado.pop(i)
                    return f"Eliminado: {nombre_buscado} del carrito."
            return f"No encontré '{nombre_producto}' en tu carrito."

        @tool
        def ver_carrito() -> str:
            """Muestra el resumen actual del carrito."""
            return _formato_carrito(carrito_estado, costo_envio)

        @tool
        def guardar_nota(nota: str) -> str:
            """Guarda una nota o instruccion especial del cliente para su pedido.
            Usar cuando el cliente pida algo especial: 'sin cilantro', 'sin cebolla',
            'extra queso', 'bien cocido', etc.
            IMPORTANTE:
            - Incluye SIEMPRE a qué producto aplica la nota. Ej: si el cliente
              pide '2 tacos de pastor sin cebolla y 1 con todo', guarda la nota
              como '2 pastor sin cebolla, 1 pastor con todo'.
            - Esta herramienta SOLO guarda la nota. NO vuelvas a llamar
              agregar_al_carrito — el producto YA está en el carrito."""
            notas_actuales = sesion.get("notas_pedido", "")
            nueva_nota = f"{notas_actuales} | {nota}".strip(" |") if notas_actuales else nota
            db.guardar_sesion(llave, historial, notas_pedido=nueva_nota)
            sesion["notas_pedido"] = nueva_nota
            return f"Nota guardada: '{nota}' ✅. El producto ya está en el carrito, no lo agregues de nuevo."

        @tool
        def cerrar_pedido() -> str:
            """Llama esta herramienta cuando el cliente diga que ya termino de
            pedir (frases como: 'eso es todo', 'ya es todo', 'eso seria todo',
            'nada mas', 'con eso', 'es todo', etc.). Inicia el flujo de cierre.
            NO llames agregar_al_carrito antes de esta herramienta en el mismo turno.
            NOTA: el pedido minimo (si aplica) se valida despues, una vez que
            el cliente indique si es para recoger o envio — aqui no se valida
            porque el minimo solo aplica a pedidos con envio a domicilio."""
            carrito_actual = db.cargar_sesion(llave).get("carrito") or carrito_estado
            if not carrito_actual:
                return "El carrito está vacío. Pide algo del menú primero."
            return "INICIAR_CIERRE"

        tools = [ver_menu, info_producto, agregar_al_carrito, quitar_del_carrito, ver_carrito, guardar_nota, cerrar_pedido]

        horarios = negocio.get("horarios_texto", "")
        base_conoc = negocio.get("base_conocimiento", "")
        bienvenida = negocio.get("mensaje_bienvenida", "¡Hola! ¿Qué te gustaría ordenar?")

        sistema = f"""Eres el asistente de pedidos de {nombre_neg}, una taquería que atiende por WhatsApp.
Tu trabajo es tomar el pedido del cliente de forma amigable y precisa.

Horarios: {horarios or 'Consultar con el negocio.'}
Tipos de servicio disponibles: {tipo_servicio}
Costo de envío: {'$' + str(costo_envio) if costo_envio > 0 else 'sin costo'}
Pedido mínimo: {'$' + str(pedido_min) if pedido_min > 0 else 'sin mínimo'}
Métodos de pago aceptados: {metodos_pago}

{('Información adicional: ' + base_conoc) if base_conoc else ''}

EJEMPLO DE COMPORTAMIENTO CORRECTO:
Cliente: "Quiero 2 que me ves"
Tú: [llamas agregar_al_carrito con nombre_producto="que me ves", cantidad=2 — INMEDIATAMENTE, sin preguntar nada antes]
(La herramienta te responde con el carrito. Si tiene el producto, perfecto. Si hay varias opciones, recién entonces preguntas cuál.)

REGLAS IMPORTANTES:
- Usa SIEMPRE la herramienta agregar_al_carrito para añadir productos. NUNCA inventes precios.
- REGLA ABSOLUTA: cuando el cliente quiera pedir CUALQUIER producto, llama agregar_al_carrito INMEDIATAMENTE con las palabras que usó el cliente. NUNCA preguntes "¿qué tipo?" ni asumas que un producto tiene variantes por tu cuenta — tú NO sabes qué productos tienen variantes, solo la herramienta lo sabe. Llama la herramienta primero y deja que ELLA te diga si hay que preguntar algo.
- Solo si agregar_al_carrito te DEVUELVE un mensaje con varias opciones, entonces muéstrale esas opciones al cliente tal cual. Si la herramienta agregó el producto sin problema, NO inventes que faltaba especificar nada.
- Algunos tacos tienen promociones por cantidad (ej. Pastor es 2x1, Bistec es 3x$60, etc. — se ven marcadas en el menú). El precio final con la promo ya aplicada se calcula automáticamente y aparece en el carrito que te devuelve la herramienta — tú NUNCA calcules el precio de tacos a mano, solo usa el texto que te da la herramienta.
- Cuando el cliente pregunte qué lleva o qué ingredientes tiene un producto, usa SIEMPRE la herramienta info_producto. NUNCA inventes ingredientes ni agregues cosas que no estén en la descripción del menú.
- Cuando el cliente pida algo especial (sin cilantro, sin cebolla, extra queso, bien cocido, etc.), usa guardar_nota para registrarlo. NUNCA ignores estas instrucciones.
- NO llames agregar_al_carrito y cerrar_pedido en el mismo mensaje. Si el cliente dice 'es todo' o 'nada más', llama SOLO cerrar_pedido.
- REGLA CRÍTICA: si el cliente responde solo "sí", "ok", "va", "dale", "claro" u otra confirmación corta SIN mencionar ningún producto nuevo, NUNCA llames agregar_al_carrito repitiendo el último producto que pidió — eso duplicaría su pedido por error y es un fallo grave. Una confirmación corta sin producto nuevo significa que está de acuerdo con algo que dijiste (el carrito, el precio, etc.), no que quiera repetir la compra. Si no tienes claro a qué se refiere, pregúntale qué más desea agregar.
- Cuando agregues uno o varios productos, el resultado de agregar_al_carrito ya trae el carrito completo con precios y subtotal formateado. Usa ESE texto en tu respuesta tal cual (puedes agregar una frase corta antes como "¡Listo! Así va tu pedido:"), NUNCA reescribas la lista de productos tú mismo ni inventes cómo agrupar las cantidades — eso causa errores graves como mostrar productos duplicados o cantidades incorrectas.
- Si el cliente pide algo que no está en el menú, indícalo claramente y ofrece alternativas.
- Cuando el cliente diga que ya terminó de pedir (eso es todo / ya es todo / nada más / con eso / etc.), llama cerrar_pedido.
- Sé breve, amigable y usa emojis con moderación.
- Si el cliente saluda sin pedir nada, preséntate brevemente y muestra el menú.
- Responde siempre en español.
"""
        if not historial:
            aviso_min_bienvenida = (
                f" Recuerda que el pedido mínimo para envío a domicilio es de {_fmt_precio(pedido_min)}."
                if pedido_min > 0 and tipo_servicio in ("envio", "ambos") else ""
            )
            sistema += f"\n\nMensaje de bienvenida sugerido: {bienvenida}{aviso_min_bienvenida}"

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=os.getenv("OPENAI_API_KEY"),
        ).bind_tools(tools)

        msgs_llm = [SystemMessage(content=sistema)] + historial[-MAX_HISTORIAL:] + [HumanMessage(content=texto)]
        resp_llm = llm.invoke(msgs_llm)

        # Procesar tool calls
        texto_respuesta = ""
        tool_results = []
        iniciar_cierre = False

        if resp_llm.tool_calls:
            # Proteccion anti-duplicado INTELIGENTE: el modelo a veces re-agrega
            # un producto que ya estaba en dos escenarios:
            # 1) El cliente da una instruccion tipo "sin cebolla" junto con
            #    guardar_nota -> el modelo repite el producto sin necesidad.
            # 2) El cliente responde una confirmacion CORTA y generica ("si",
            #    "ok", "va", "dale") SIN mencionar ningun producto, y el
            #    modelo malinterpreta eso como "repite mi ultimo pedido".
            # En ambos casos: si el producto ya esta en el carrito con la
            # misma cantidad Y el cliente no nombro el producto en su mensaje,
            # es casi seguro un re-agregado erroneo -> lo bloqueamos.
            nombres_tools = [tc["name"] for tc in resp_llm.tool_calls]
            hay_nota = "guardar_nota" in nombres_tools
            carrito_previo = {c["nombre"]: c["cantidad"] for c in carrito_estado}

            _CONFIRMACIONES_CORTAS = {
                "si", "sí", "ok", "okay", "va", "dale", "claro", "yes",
                "correcto", "asi es", "así es", "perfecto", "vale",
            }
            es_confirmacion_corta = (
                texto_low.strip(".,!¡¿? ") in _CONFIRMACIONES_CORTAS
                and len(texto.split()) <= 3
            )

            def _es_reagregado(args: dict) -> bool:
                item = _buscar_en_menu(args.get("nombre_producto", ""), menu)
                if not item:
                    return False
                nombre = item["nombre"]
                cant = max(1, args.get("cantidad", 1))
                # Ya estaba en el carrito con la misma cantidad...
                if carrito_previo.get(nombre) != cant:
                    return False
                # ...y o bien viene una nota en el mismo turno (instruccion
                # sobre el producto ya agregado), o el cliente NO menciono
                # ningun producto en su mensaje (confirmacion vaga) -> en
                # cualquiera de los dos casos, es un re-agregado erroneo.
                if hay_nota:
                    return True
                if es_confirmacion_corta:
                    return True
                return False

            for tc in resp_llm.tool_calls:
                fn_name = tc["name"]
                fn_args = tc["args"]
                print(f"   [{nombre_neg}] Tool: {fn_name} {fn_args}")

                if fn_name == "agregar_al_carrito" and _es_reagregado(fn_args):
                    print(f"   [{nombre_neg}] agregar_al_carrito saltado (re-agregado erroneo: confirmacion corta o nota sin producto nuevo).")
                    tool_results.append(
                        "No agregues ese producto de nuevo — el cliente solo confirmó algo, "
                        "no pidió repetir su pedido. Pregúntale si quiere algo más."
                    )
                    continue

                fn_map = {
                    "ver_menu":           ver_menu,
                    "info_producto":      info_producto,
                    "agregar_al_carrito": agregar_al_carrito,
                    "quitar_del_carrito": quitar_del_carrito,
                    "ver_carrito":        ver_carrito,
                    "guardar_nota":       guardar_nota,
                    "cerrar_pedido":      cerrar_pedido,
                }
                resultado = fn_map[fn_name].invoke(fn_args) if fn_name in fn_map else "Herramienta no encontrada."
                if resultado == "INICIAR_CIERRE":
                    iniciar_cierre = True
                    resultado = "Procesando cierre del pedido..."
                tool_results.append(resultado)

            # Si la UNICA tool llamada en este turno es ver_menu o
            # info_producto, usamos su resultado DIRECTO como respuesta, sin
            # pasar por una segunda llamada al modelo. Esto es necesario
            # porque el modelo tiende a "redactar de nuevo" el menu en sus
            # propias palabras y pierde el formato (emojis, precios, saltos
            # de linea) o incluso lo omite por completo — un bug real visto
            # en produccion. Instrucciones en el prompt no bastan para esto;
            # se necesita forzarlo a nivel de codigo.
            tools_llamadas = [tc["name"] for tc in resp_llm.tool_calls]
            si_respuesta_directa = (
                len(tools_llamadas) == 1
                and tools_llamadas[0] in ("ver_menu", "info_producto", "agregar_al_carrito", "ver_carrito")
                and not iniciar_cierre
            )
            if si_respuesta_directa:
                texto_respuesta = tool_results[0]
            # Segunda llamada al LLM con los resultados de las herramientas
            elif not iniciar_cierre:
                from langchain_core.messages import ToolMessage
                msgs_2 = msgs_llm + [resp_llm]
                for i, tc in enumerate(resp_llm.tool_calls):
                    msgs_2.append(ToolMessage(content=tool_results[i], tool_call_id=tc["id"]))
                resp_2 = ChatOpenAI(model="gpt-4o-mini", temperature=0,
                                    api_key=os.getenv("OPENAI_API_KEY")).invoke(msgs_2)
                texto_respuesta = resp_2.content.strip()
        else:
            texto_respuesta = resp_llm.content.strip()

        # Guardar carrito actualizado (puede haber cambiado por las tools)
        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=texto_respuesta or "")]
        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], carrito=carrito_estado, fase_pedido=fase)

        # Si la tool cerrar_pedido devolvio INICIAR_CIERRE, arrancamos el flujo
        # deterministico de cierre (tipo de entrega)
        if iniciar_cierre:
            # Si el cliente ya habia llegado a definir tipo_entrega antes
            # (ej. dijo "No" al resumen final y luego "Si" de nuevo tras
            # modificar algo), reutilizamos esos datos en vez de reiniciar
            # el flujo desde cero — evita volver a preguntar "recoger o
            # envio" cuando el cliente ya lo habia contestado.
            tipo_entrega_previo = sesion.get("tipo_entrega", "")
            direccion_previa    = sesion.get("direccion_entrega", "")
            nombre_previo       = sesion.get("nombre_cliente", "")

            if tipo_entrega_previo == "recoger" and nombre_previo:
                # Ya tenemos todo lo necesario para recoger -> resumen directo
                _, notas_limpias = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                resumen = _formato_carrito(carrito_estado)
                notas_linea = f"📝 *Notas:* {notas_limpias}\n" if notas_limpias else ""
                resp_cierre = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_previo}\n"
                    f"🏪 *Para recoger* en el local\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                print(f"   [{nombre_neg}] Cierre reutilizando datos previos (recoger) — directo a confirmación.")
                return

            elif tipo_entrega_previo == "envio" and direccion_previa and nombre_previo:
                # Ya teniamos direccion y nombre de envio -> resumen directo,
                # pero re-preguntamos metodo de pago si no lo teniamos ya
                # (es razonable, pudo cambiar el carrito).
                resp_cierre = "¿Cómo deseas pagar? (Efectivo, Transferencia o Tarjeta)"
                metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                resp_cierre = (
                    f"Para pedidos con envío a domicilio pedimos el pago por adelantado.\n\n"
                    f"¿Cómo deseas pagar? Opciones: *{metodos_lista}*"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago",
                                  carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                print(f"   [{nombre_neg}] Cierre reutilizando datos previos (envío) — directo a método de pago.")
                return

            # Caso normal: no hay datos previos de tipo de entrega, arrancar
            # el flujo desde el principio.
            if tipo_servicio == "recoger":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado)}\n\n"
                    "¿Es para recoger en el local?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  tipo_entrega="recoger", carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            elif tipo_servicio == "envio":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado)}\n"
                    "_El costo de envío se calculará según tu dirección._\n\n"
                    "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            else:
                # ambos: preguntar primero, NUNCA mostrar costo de envio aqui
                # porque todavia no sabemos si el cliente eligio envio.
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            print(f"   [{nombre_neg}] Cierre iniciado — carrito con {len(carrito_estado)} producto(s).")
            return

        if texto_respuesta:
            print(f"<- [{nombre_neg}] {texto_respuesta[:80]}")
            enviar_whatsapp(telefono, texto_respuesta, token, phone_number_id)

    except Exception as e:
        import traceback
        print(f"!!! Error en procesar_mensaje: {e}\n{traceback.format_exc()}")


# ── PANEL ADMIN ──────────────────────────────────────────────────────────────
# (Se importa como router separado para mantener main.py limpio)

from panel import router as panel_router
app.include_router(panel_router)
