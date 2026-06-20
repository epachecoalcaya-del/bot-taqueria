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

def enviar_whatsapp(telefono: str, mensaje: str, token: str, phone_id: str) -> bool:
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
    if not r.ok:
        print(f"!!! Error enviando WhatsApp: {r.status_code} {r.text[:200]}")
    return r.ok


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

def _calcular_total(carrito: list) -> float:
    return sum(i["precio"] * i["cantidad"] for i in carrito)


def _formato_carrito(carrito: list, costo_envio: float = 0) -> str:
    if not carrito:
        return "🛒 Tu carrito está vacío."
    lineas = ["🛒 *Tu pedido:*"]
    for i in carrito:
        subtotal = i["precio"] * i["cantidad"]
        lineas.append(f"  • {i['cantidad']}x {i['nombre']} — {_fmt_precio(subtotal)}")
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
            desc = f" _{p['descripcion']}_" if p.get("descripcion") else ""
            lineas.append(f"  • *{p['nombre']}*{desc}")
            lineas.append(f"    {_fmt_precio(float(p['precio']))}")
        if idx < total_cats - 1:
            lineas.append("")

    lineas += [
        "",
        "━━━━━━━━━━━━━━",
        "👇 *¿Qué te gustaría ordenar?*",
        "_Puedes pedir varios productos a la vez,_",
        "_por ejemplo: \"2 tacos de pastor y una horchata\"_",
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

        # Limpieza de carrito huerfano: si hay productos en el carrito pero
        # no hay una fase activa (el pedido quedo a medias por algun error),
        # limpiamos al detectar un saludo nuevo para empezar desde cero.
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
                notas        = sesion.get("notas_pedido", "")
                costo_envio_real = sesion.get("costo_envio_calc", 0) or costo_envio
                total        = _calcular_total(carrito)
                total_con_envio = total + (costo_envio_real if tipo_entrega == "envio" else 0)

                pedido_id = db.guardar_pedido(
                    negocio_id=negocio_id, telefono=telefono,
                    nombre_cliente=nombre_cl, items=carrito,
                    total=total_con_envio, tipo_entrega=tipo_entrega,
                    direccion=direccion, notas=notas,
                )
                tiempo = tiempo_env if tipo_entrega == "envio" else tiempo_rec
                notas_linea = f"📝 {notas}\n" if notas else ""
                resp = (
                    f"✅ *¡Pedido #{pedido_id} confirmado!*\n\n"
                    f"👤 *{nombre_cl}*\n"
                    f"{'🛵 Envío a: ' + direccion if tipo_entrega == 'envio' else '🏪 Para recoger en el local'}\n"
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
                            total_con_envio, tipo_entrega, direccion, metodos_pago, notas,
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
                notas        = sesion.get("notas_pedido", "")
                costo_envio_real = sesion.get("costo_envio_calc", 0) or costo_envio
                total        = _calcular_total(carrito)
                total_con_envio = total + (costo_envio_real if tipo_entrega == "envio" else 0)
                resumen = _formato_carrito(carrito, costo_envio_real if tipo_entrega == "envio" else 0)
                notas_linea = f"📝 *Notas:* {notas}\n" if notas else ""
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"{'🛵 *Envío a:* ' + direccion if tipo_entrega == 'envio' else '🏪 *Para recoger* en el local'}\n"
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

        # FASE: direccion — esperando dirección de envío
        if fase == "direccion":
            direccion = texto.strip()
            # Si el cliente comparte su ubicacion GPS (tipo "location" de
            # WhatsApp), usamos las coordenadas exactas directo — es mas
            # preciso que pedirle que escriba la direccion, y mas rapido
            # (sin geocoding de texto). Guardamos un texto legible para el
            # resumen y el correo al dueno.
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
                        # Guardamos tambien un link de Google Maps en la
                        # direccion para que el dueño/repartidor pueda abrirla
                        # directo, ademas de las coordenadas.
                        lat, lng = coords_ubicacion
                        direccion = f"📍 https://maps.google.com/?q={lat},{lng}"
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
                        direccion = f"📍 https://maps.google.com/?q={lat},{lng}"

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
            nombre_producto: nombre exacto o aproximado del producto segun el menu.
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
                    f"'{nombre_producto}' tiene varias opciones en el menú, "
                    f"pregúntale al cliente cuál de estas quiere (NO elijas tú):\n{opciones}"
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
            return f"{carrito_txt}{aviso_min}"

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

REGLAS IMPORTANTES:
- Usa SIEMPRE la herramienta agregar_al_carrito para añadir productos. NUNCA inventes precios.
- Si el cliente pide un producto genérico que tiene varias variantes (ej. "volcán", "torta", "agua", "hamburguesa", "papa rellena") SIN especificar cuál, y agregar_al_carrito te devuelve una lista de opciones, DEBES preguntarle al cliente cuál variante quiere ANTES de continuar. NUNCA elijas una variante por tu cuenta — eso causa errores graves en el pedido real.
- Cuando el cliente pregunte qué lleva o qué ingredientes tiene un producto, usa SIEMPRE la herramienta info_producto. NUNCA inventes ingredientes ni agregues cosas que no estén en la descripción del menú.
- Cuando el cliente pida algo especial (sin cilantro, sin cebolla, extra queso, bien cocido, etc.), usa guardar_nota para registrarlo. NUNCA ignores estas instrucciones.
- NO llames agregar_al_carrito y cerrar_pedido en el mismo mensaje. Si el cliente dice 'es todo' o 'nada más', llama SOLO cerrar_pedido.
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
            # un producto que ya estaba cuando el cliente solo da una instruccion
            # ("la quiero sin cebolla"). Para distinguir un re-agregado de un
            # producto nuevo legitimo, comparamos contra el carrito que YA existe:
            # si el producto ya esta en el carrito con esa misma cantidad y en el
            # turno tambien viene guardar_nota, es un re-agregado -> lo saltamos.
            # Si es un producto nuevo o cantidad distinta, lo procesamos normal.
            nombres_tools = [tc["name"] for tc in resp_llm.tool_calls]
            hay_nota = "guardar_nota" in nombres_tools
            carrito_previo = {c["nombre"]: c["cantidad"] for c in carrito_estado}

            def _es_reagregado(args: dict) -> bool:
                if not hay_nota:
                    return False
                item = _buscar_en_menu(args.get("nombre_producto", ""), menu)
                if not item:
                    return False
                nombre = item["nombre"]
                cant = max(1, args.get("cantidad", 1))
                # Ya estaba en el carrito con la misma cantidad -> es re-agregado
                return carrito_previo.get(nombre) == cant

            for tc in resp_llm.tool_calls:
                fn_name = tc["name"]
                fn_args = tc["args"]
                print(f"   [{nombre_neg}] Tool: {fn_name} {fn_args}")

                if fn_name == "agregar_al_carrito" and _es_reagregado(fn_args):
                    print(f"   [{nombre_neg}] agregar_al_carrito saltado (re-agregado de producto ya en carrito).")
                    tool_results.append("Ese producto ya estaba en el carrito; solo se registró la nota.")
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
                    f"{_formato_carrito(carrito_estado, costo_envio)}\n\n"
                    "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            else:
                # ambos: preguntar
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, costo_envio)}\n\n"
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
