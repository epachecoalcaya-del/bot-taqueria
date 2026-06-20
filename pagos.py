"""
pagos.py — Agendify Pedidos
Integracion con Mercado Pago (Checkout Pro) para generar links de pago
y procesar las notificaciones de pago confirmado via webhook.
"""
import os
import requests
from typing import Optional

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_API_BASE = "https://api.mercadopago.com"


def crear_link_pago(
    pedido_referencia: str,
    nombre_negocio: str,
    descripcion: str,
    monto: float,
    url_notificacion: str,
) -> Optional[dict]:
    """Crea una preferencia de pago en Mercado Pago (Checkout Pro) y
    devuelve el link de pago listo para mandar al cliente.

    pedido_referencia: identificador unico nuestro (ej. 'taqueria_5214427189947_1718900000')
                        para poder relacionar el webhook con el pedido despues.
    url_notificacion: URL publica de nuestro webhook para recibir el aviso de pago.

    Devuelve dict con 'link_pago' e 'preference_id', o None si falla.
    """
    if not MP_ACCESS_TOKEN:
        print("!!! MP_ACCESS_TOKEN no configurado")
        return None
    try:
        body = {
            "items": [{
                "title": f"{descripcion} — {nombre_negocio}",
                "quantity": 1,
                "unit_price": round(float(monto), 2),
                "currency_id": "MXN",
            }],
            "external_reference": pedido_referencia,
            "notification_url": url_notificacion,
            "back_urls": {
                "success": "https://bot-taqueria.onrender.com/pago_exitoso",
                "failure": "https://bot-taqueria.onrender.com/pago_fallido",
                "pending": "https://bot-taqueria.onrender.com/pago_pendiente",
            },
            "auto_return": "approved",
        }
        r = requests.post(
            f"{MP_API_BASE}/checkout/preferences",
            json=body,
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10,
        )
        data = r.json()
        if r.status_code not in (200, 201):
            print(f"!!! Error creando preferencia MP: {data}")
            return None
        return {
            "link_pago": data.get("init_point"),
            "preference_id": data.get("id"),
        }
    except Exception as e:
        print(f"!!! Error creando link de pago: {e}")
        return None


def consultar_pago(payment_id: str) -> Optional[dict]:
    """Consulta el estado de un pago especifico por su ID.
    Devuelve dict con 'status' ('approved', 'pending', 'rejected', etc.),
    'external_reference' (nuestra referencia del pedido), y 'monto'."""
    if not MP_ACCESS_TOKEN:
        return None
    try:
        r = requests.get(
            f"{MP_API_BASE}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=10,
        )
        data = r.json()
        if r.status_code != 200:
            print(f"!!! Error consultando pago {payment_id}: {data}")
            return None
        return {
            "status": data.get("status"),
            "external_reference": data.get("external_reference"),
            "monto": data.get("transaction_amount"),
            "metodo_detalle": data.get("payment_method_id"),
        }
    except Exception as e:
        print(f"!!! Error consultando pago: {e}")
        return None
