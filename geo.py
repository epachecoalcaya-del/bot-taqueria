"""
geo.py — Agendify Pedidos
Geocodificacion de direcciones y calculo de tarifa de envio por distancia,
usando Google Maps Geocoding API y Distance Matrix API.
"""
import os
import requests
from typing import Optional, Tuple

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Tabla de tarifas "Yo Lo Llevo" 2026 — (limite_superior_km, normal, lluvia)
# Se evalua en orden: la primera fila cuyo km_destino <= limite_superior aplica.
TABLA_TARIFAS = [
    (5.0,  50,  65),
    (6.0,  55,  70),
    (7.0,  60,  75),
    (8.0,  65,  80),
    (9.0,  75,  90),
    (10.0, 85,  100),
    (11.0, 100, 120),
    (12.0, 110, 130),
    (13.0, 120, 140),
    (14.0, 130, 150),
    (15.0, 140, 160),
    (16.0, 150, 170),
    (17.0, 160, 180),
    (18.0, 170, 190),
    (19.0, 180, 200),
    (20.0, 200, 220),
]


def geocodificar(direccion: str, ciudad_default: str = "Querétaro, México") -> Optional[Tuple[float, float]]:
    """Convierte una direccion en texto a coordenadas (lat, lng).
    Devuelve None si no se pudo geocodificar o no hay API key configurada."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        query = direccion.strip()
        if ciudad_default.lower() not in query.lower():
            query = f"{query}, {ciudad_default}"
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": GOOGLE_MAPS_API_KEY},
            timeout=8,
        )
        data = r.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        loc = data["results"][0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    except Exception as e:
        print(f"!!! Error geocodificando '{direccion}': {e}")
        return None


def calcular_distancia_km(origen: Tuple[float, float], destino: Tuple[float, float]) -> Optional[float]:
    """Calcula la distancia de manejo en km entre dos puntos usando Distance
    Matrix API. Devuelve None si falla."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": f"{origen[0]},{origen[1]}",
                "destinations": f"{destino[0]},{destino[1]}",
                "units": "metric",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=8,
        )
        data = r.json()
        elemento = data["rows"][0]["elements"][0]
        if elemento.get("status") != "OK":
            return None
        metros = elemento["distance"]["value"]
        return round(metros / 1000, 2)
    except Exception as e:
        print(f"!!! Error calculando distancia: {e}")
        return None


def calcular_tarifa(km: float, lluvia: bool = False) -> Optional[int]:
    """Devuelve el costo de envio segun la tabla de tarifas, para una
    distancia dada en km. Si la distancia excede el maximo de la tabla
    (20km), devuelve None — ese caso debe manejarse aparte (fuera de zona)."""
    for limite, normal, lluvia_tarifa in TABLA_TARIFAS:
        if km <= limite:
            return lluvia_tarifa if lluvia else normal
    return None  # fuera de cobertura


def calcular_envio_completo(
    direccion_cliente: str,
    coords_negocio: Tuple[float, float],
    lluvia: bool = False,
) -> dict:
    """Funcion principal: geocodifica la direccion del cliente, calcula
    distancia desde el negocio, y devuelve la tarifa correspondiente.

    Devuelve un dict con:
      - ok: True/False
      - costo: int o None
      - km: float o None
      - razon: string explicando el resultado (para logs/debug)
    """
    if not GOOGLE_MAPS_API_KEY:
        return {"ok": False, "costo": None, "km": None, "razon": "Sin API key configurada"}

    coords_cliente = geocodificar(direccion_cliente)
    if not coords_cliente:
        return {"ok": False, "costo": None, "km": None, "razon": "No se pudo geocodificar la dirección"}

    km = calcular_distancia_km(coords_negocio, coords_cliente)
    if km is None:
        return {"ok": False, "costo": None, "km": None, "razon": "No se pudo calcular la distancia"}

    costo = calcular_tarifa(km, lluvia)
    if costo is None:
        return {"ok": False, "costo": None, "km": km, "razon": f"Fuera de cobertura ({km} km, máximo 20 km)"}

    return {"ok": True, "costo": costo, "km": km, "razon": "OK"}
