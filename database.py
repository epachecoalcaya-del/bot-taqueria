"""
database.py — Agendify Pedidos
Manejo de conexion a Supabase, sesiones de conversacion, menu y pedidos.
"""
import os
import json
import datetime
from datetime import timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

_client = None

def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        _client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _client


# ── NEGOCIOS ────────────────────────────────────────────────────────────────

def cargar_negocios() -> list:
    """Carga todos los negocios activos al arrancar. Se cachean en memoria."""
    try:
        r = _get_client().table("pedidos_negocios").select("*").execute()
        return r.data or []
    except Exception as e:
        print(f"!!! DB error en cargar_negocios: {e}")
        return []


# ── MENU ────────────────────────────────────────────────────────────────────

def cargar_menu(negocio_id: int) -> list:
    """Devuelve los items del menu de un negocio, solo los disponibles."""
    try:
        r = (
            _get_client()
            .table("pedidos_menu")
            .select("*")
            .eq("negocio_id", negocio_id)
            .eq("disponible", True)
            .order("categoria")
            .order("nombre")
            .execute()
        )
        return r.data or []
    except Exception as e:
        print(f"!!! DB error en cargar_menu ({negocio_id}): {e}")
        return []


def agregar_item_menu(negocio_id: int, nombre: str, precio: float,
                      descripcion: str = "", categoria: str = "General") -> bool:
    try:
        _get_client().table("pedidos_menu").insert({
            "negocio_id":  negocio_id,
            "nombre":      nombre.strip(),
            "descripcion": descripcion.strip(),
            "precio":      precio,
            "categoria":   categoria.strip() or "General",
            "disponible":  True,
        }).execute()
        return True
    except Exception as e:
        print(f"!!! DB error en agregar_item_menu: {e}")
        return False


def eliminar_item_menu(item_id: int) -> bool:
    try:
        _get_client().table("pedidos_menu").delete().eq("id", item_id).execute()
        return True
    except Exception as e:
        print(f"!!! DB error en eliminar_item_menu: {e}")
        return False


def actualizar_negocio(phone_number_id: str, datos: dict) -> bool:
    try:
        datos["updated_at"] = datetime.datetime.now(timezone.utc).isoformat()
        _get_client().table("pedidos_negocios").update(datos).eq("phone_number_id", phone_number_id).execute()
        return True
    except Exception as e:
        print(f"!!! DB error en actualizar_negocio: {e}")
        return False


# ── SESIONES ────────────────────────────────────────────────────────────────

def cargar_sesion(llave: str) -> dict:
    """Carga la sesion activa de un cliente. Devuelve defaults si no existe."""
    try:
        r = (
            _get_client()
            .table("pedidos_sesiones")
            .select("*")
            .eq("llave_memoria", llave)
            .execute()
        )
        if r.data:
            row = r.data[0]
            return {
                "historial":              _deserializar(row.get("historial") or []),
                "esperando_confirmacion": row.get("esperando_confirmacion") or False,
                "carrito":                row.get("carrito") or [],
                "fase_pedido":            row.get("fase_pedido") or "",
                "tipo_entrega":           row.get("tipo_entrega") or "",
                "direccion_entrega":      row.get("direccion_entrega") or "",
                "nombre_cliente":         row.get("nombre_cliente") or "",
                "notas_pedido":           row.get("notas_pedido") or "",
                "costo_envio_calc":       float(row.get("costo_envio_calc") or 0),
            }
    except Exception as e:
        print(f"!!! DB error en cargar_sesion ({llave}): {e}")
    return {
        "historial": [], "esperando_confirmacion": False,
        "carrito": [], "fase_pedido": "", "tipo_entrega": "",
        "direccion_entrega": "", "nombre_cliente": "", "notas_pedido": "",
        "costo_envio_calc": 0,
    }


def guardar_sesion(
    llave: str,
    historial: list,
    esperando_confirmacion: bool = False,
    carrito: Optional[list] = None,
    fase_pedido: Optional[str] = None,
    tipo_entrega: Optional[str] = None,
    direccion_entrega: Optional[str] = None,
    nombre_cliente: Optional[str] = None,
    notas_pedido: Optional[str] = None,
    costo_envio_calc: Optional[float] = None,
):
    """Guarda o actualiza la sesion. Los campos opcionales solo se
    actualizan si se pasan explicitamente (no None)."""
    try:
        datos = {
            "llave_memoria":          llave,
            "historial":              _serializar(historial),
            "esperando_confirmacion": esperando_confirmacion,
            "updated_at":             datetime.datetime.now(timezone.utc).isoformat(),
        }
        if carrito is not None:
            datos["carrito"] = carrito
        if fase_pedido is not None:
            datos["fase_pedido"] = fase_pedido
        if tipo_entrega is not None:
            datos["tipo_entrega"] = tipo_entrega
        if direccion_entrega is not None:
            datos["direccion_entrega"] = direccion_entrega
        if nombre_cliente is not None:
            datos["nombre_cliente"] = nombre_cliente
        if notas_pedido is not None:
            datos["notas_pedido"] = notas_pedido
        if costo_envio_calc is not None:
            datos["costo_envio_calc"] = costo_envio_calc
        _get_client().table("pedidos_sesiones").upsert(datos).execute()
    except Exception as e:
        print(f"!!! DB error en guardar_sesion ({llave}): {e}")


def limpiar_sesion(llave: str):
    """Limpia el carrito y estado del pedido al terminar un pedido,
    pero conserva el historial para que el bot recuerde contexto previo."""
    guardar_sesion(
        llave, [], False,
        carrito=[], fase_pedido="", tipo_entrega="",
        direccion_entrega="", nombre_cliente="", costo_envio_calc=0,
    )


# ── PEDIDOS ─────────────────────────────────────────────────────────────────

def guardar_pedido(
    negocio_id: int,
    telefono: str,
    nombre_cliente: str,
    items: list,
    total: float,
    tipo_entrega: str,
    direccion: str = "",
    metodo_pago: str = "",
    notas: str = "",
) -> Optional[int]:
    """Guarda un pedido confirmado. Devuelve el ID del pedido o None si falla."""
    try:
        r = _get_client().table("pedidos_ordenes").insert({
            "negocio_id":    negocio_id,
            "telefono":      telefono,
            "nombre_cliente": nombre_cliente,
            "items":         items,
            "total":         total,
            "tipo_entrega":  tipo_entrega,
            "direccion":     direccion,
            "metodo_pago":   metodo_pago,
            "notas":         notas,
            "estado":        "nuevo",
        }).execute()
        return r.data[0]["id"] if r.data else None
    except Exception as e:
        print(f"!!! DB error en guardar_pedido: {e}")
        return None


def actualizar_estado_pedido(pedido_id: int, estado: str) -> bool:
    """Actualiza el estado de un pedido (nuevo/en_proceso/listo/entregado/cancelado)."""
    try:
        _get_client().table("pedidos_ordenes").update({
            "estado":     estado,
            "updated_at": datetime.datetime.now(timezone.utc).isoformat(),
        }).eq("id", pedido_id).execute()
        return True
    except Exception as e:
        print(f"!!! DB error en actualizar_estado_pedido: {e}")
        return False


def obtener_pedidos_recientes(negocio_id: int, limite: int = 50) -> list:
    """Devuelve los pedidos mas recientes de un negocio para el panel."""
    try:
        r = (
            _get_client()
            .table("pedidos_ordenes")
            .select("*")
            .eq("negocio_id", negocio_id)
            .order("created_at", desc=True)
            .limit(limite)
            .execute()
        )
        return r.data or []
    except Exception as e:
        print(f"!!! DB error en obtener_pedidos_recientes: {e}")
        return []


# ── HELPERS DE SERIALIZACION ─────────────────────────────────────────────────

def _serializar(historial: list) -> list:
    """Convierte mensajes de LangChain a formato JSON para Supabase."""
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    resultado = []
    for m in historial:
        if isinstance(m, HumanMessage):
            resultado.append({"type": "human", "content": m.content})
        elif isinstance(m, AIMessage):
            resultado.append({"type": "ai", "content": m.content})
        elif isinstance(m, SystemMessage):
            resultado.append({"type": "system", "content": m.content})
    return resultado


def _deserializar(data: list) -> list:
    """Convierte formato JSON de Supabase a mensajes de LangChain."""
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    resultado = []
    for m in data:
        t = m.get("type", "")
        c = m.get("content", "")
        if t == "human":
            resultado.append(HumanMessage(content=c))
        elif t == "ai":
            resultado.append(AIMessage(content=c))
        elif t == "system":
            resultado.append(SystemMessage(content=c))
    return resultado
