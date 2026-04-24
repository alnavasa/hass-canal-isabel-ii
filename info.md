# Canal de Isabel II

Integración de Home Assistant para lecturas horarias de consumo de agua de **Canal de Isabel II** (Madrid).

Funciona con un **bookmarklet** (favorito JavaScript) que pegas en Safari/Chrome/Firefox: tu navegador hace la descarga del CSV ya autenticado y POSTea a HA.

- Sensor con la última lectura horaria (litros) por contrato.
- Sensor acumulado por contrato con importación a **estadísticas externas** para el panel de Energía.
- Sensor de **lectura absoluta del contador** (m³) — la cifra que casa con la factura.

## Cómo empezar

1. Instala vía HACS y reinicia HA.
2. **Ajustes → Dispositivos y servicios → + Añadir integración → Canal de Isabel II**.
3. Indica un nombre y la URL HTTPS de tu HA (local o pública — ambas valen si son HTTPS).
4. Aparece una notificación con el bookmarklet — pégalo en favoritos del navegador.
5. Loguéate en la Oficina Virtual y pulsa el favorito. Sensores aparecen al instante.

Guía completa: [docs/SETUP.md](https://github.com/alnavasa/hass-canal-isabel-ii/blob/main/docs/SETUP.md).

## Requisitos

- Home Assistant accesible por **HTTPS** desde el navegador que va a pulsar el bookmarklet. Puede ser HTTPS **local** (p.ej. `https://192.168.1.50:8123` con NGINX SSL, o `https://homeassistant.local:8123` con certificado propio) o HTTPS **público** (DuckDNS + Let's Encrypt, Nabu Casa, Cloudflare Tunnel, Tailscale Funnel…). Sin HTTPS el navegador bloquea el `fetch()`.
- Cuenta activa en la Oficina Virtual.

## Créditos

Basado en [miguelangel-nubla/homeassistant_canal_isabel_II](https://github.com/miguelangel-nubla/homeassistant_canal_isabel_II) con parches de la comunidad upstream.
