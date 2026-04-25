# Canal de Isabel II — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alnavasa&repository=hass-canal-isabel-ii&category=integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)

Integración de Home Assistant que importa las **lecturas horarias de consumo de agua** de la Oficina Virtual de [Canal de Isabel II](https://oficinavirtual.canaldeisabelsegunda.es) (Madrid) al **panel de Energía → Agua** y a sensores nativos por contrato.

## ¿Cómo funciona?

El portal de la Oficina Virtual **no tiene API pública** y su login está protegido por reCAPTCHA Enterprise. La integración usa un truco que evita scrapear desde el servidor:

> **El navegador del usuario hace la descarga, ya autenticado, mediante un *bookmarklet* (favorito JavaScript).** Home Assistant publica un endpoint HTTP donde recibe el CSV; los sensores se actualizan al instante.

```
┌──────────────────────┐       click       ┌───────────────────────────────┐
│ Tu navegador (logado │ ───────────────►  │ Oficina Virtual               │
│ en el portal CYII)   │                   │ - genera CSV horario           │
└──────────┬───────────┘                   └───────────────────────────────┘
           │ POST CSV+HTML (cookies del usuario, mismo navegador)
           ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Home Assistant /api/canal_isabel_ii/ingest/<entry_id>                  │
│ - valida Bearer token + origen del portal                              │
│ - parsea CSV → litros/hora + scrape "Última lectura" en m³            │
│ - persiste en .storage + empuja estadísticas externas → panel Energía │
└────────────────────────────────────────────────────────────────────────┘
```

Ventajas del modelo:

- ✅ **Funciona en cualquier variante de HA** — Container, Core, OS y Supervised.
- ✅ **Sin procesos en segundo plano** — no hay nada gastando RAM mientras no pulses.
- ✅ **Sin captcha** ni detección de bots: tu navegador real ya está autenticado en el portal.
- ✅ **Sin sesiones que caducan en HA** — el bookmarklet lleva dentro un Bearer token permanente. No hay reauth.
- ✅ Una **URL de bookmarklet** que pegas en favoritos de Safari/Chrome (PC, Mac, iOS, Android) y pulsas cuando quieras refrescar. Automatizable con cualquier scheduler (atajo iOS, automatización de macOS, cron + Selenium, etc.).

## Requisitos

- Home Assistant accesible por **HTTPS** desde el **navegador** donde vayas a pulsar el bookmarklet. HTTPS es obligatorio porque la página del portal (`https://oficinavirtual.canaldeisabelsegunda.es`) es HTTPS y los navegadores bloquean el `fetch()` a HTTP por *mixed content*. **Puede ser HTTPS local o HTTPS público** — ambos funcionan, elige según tu caso:

  | Modo | URL típica | Funciona cuando | Privacidad |
  |------|------------|-----------------|-----------|
  | **HTTPS local** | `https://192.168.1.50:8123`, `https://homeassistant.local:8123` | El dispositivo desde el que pulsas el bookmarklet está en la LAN de HA (cable, WiFi doméstico, VPN al router). | Máxima — HA no sale a internet. |
  | **HTTPS público** | `https://micasa.duckdns.org`, `https://abc123.ui.nabu.casa`, `https://hass.midominio.com` | Desde cualquier sitio (4G, oficina, otro WiFi). | Menor — HA expuesto a internet. |

  Para HTTPS local puedes usar el integration *NGINX Home Assistant SSL proxy*, un reverse proxy propio con Let's Encrypt DNS-01, o un certificado auto-firmado aceptado manualmente en el navegador. Para HTTPS público, lo más cómodo es DuckDNS + Let's Encrypt, Nabu Casa Remote, o un túnel Cloudflare/Tailscale.
- Cuenta activa en la Oficina Virtual.
- Navegador con favoritos (cualquiera de los modernos sirve).
- HACS (recomendado, para auto-update).

> **Lo que NO funciona:** `http://192.168.x.x:8123`, `http://homeassistant.local:8123`, o cualquier URL HTTP sin certificado. Es cosa del navegador, no de la integración.

## Instalación

### 1. Instalar la integración (HACS)

1. HACS → menú `⋮` → **Repositorios personalizados**.
2. URL `https://github.com/alnavasa/hass-canal-isabel-ii`, categoría **Integration**.
3. Cierra el modal, busca **"Canal de Isabel II"** → **Descargar** → última versión.
4. **Reinicia Home Assistant**.

Manual: copia `custom_components/canal_isabel_ii/` a `<config>/custom_components/canal_isabel_ii/` y reinicia.

### 2. Añadir la integración

**Ajustes → Dispositivos y servicios → + Añadir integración → *Canal de Isabel II***.

El asistente pide:

| Campo                    | Qué poner                                                                                       | Ejemplo                                       |
|--------------------------|-------------------------------------------------------------------------------------------------|-----------------------------------------------|
| **Nombre instalación**   | Etiqueta libre. Aparece como nombre del dispositivo y prefijo sensor.                           | `Casa principal`                              |
| **URL de tu HA**         | URL HTTPS (local o pública). Default: `external_url` / `internal_url` configurada en HA.       | `https://192.168.1.50:8123`  o  `https://micasa.duckdns.org` |

Al pulsar **Enviar**:

1. Se crea la entry y se genera un **token único** (192 bits, `secrets.token_hex(24)`).
2. Aparece un modal corto "✅ Éxito · Configuración creada para <nombre>" → pulsa **Terminar**.
3. Inmediatamente se publica una **notificación persistente** (campana 🔔 de la barra lateral). Es **corta** y enlaza a una **página de instalación** servida por la propia integración con dos botones (arrastrar / copiar) — todo el dolor de seleccionar y copiar 1.5 KB de URL escapada desde un bloque de código markdown está resuelto ahí.
4. Los sensores **aún no existen** — se crean en el primer POST exitoso del bookmarklet. El dispositivo aparece vacío hasta entonces.

> **Si cerraste la notificación sin querer**: ejecuta el servicio
> `canal_isabel_ii.show_bookmarklet` desde **Herramientas para desarrolladores →
> Acciones** y la notificación vuelve a aparecer con el mismo contenido (mismo
> token, mismo entry_id — nada cambia).

### 3. Instalar el bookmarklet desde la página de instalación

1. Pulsa la campana 🔔 (barra lateral izquierda de HA, abajo) y abre la notificación **"Bookmarklet listo — <nombre>"**.
2. Pulsa **📥 Abrir página de instalación** dentro de la notificación. Se abre una página HTML servida por HA en `/api/canal_isabel_ii/bookmarklet/<entry_id>` con tu sesión de HA (no hay tokens en la URL).
3. La página te ofrece **dos formas** de instalar el favorito:

   | Cómo | Cuándo | Qué hacer |
   |------|--------|-----------|
   | **★ Canal → HA** (enlace estilo botón) | Escritorio (Safari Mac, Chrome, Firefox, Edge) | **Arrástralo** a la barra de favoritos. *No lo pulses* — un click suelto bloquea la ejecución (el bookmarklet no tiene sentido en HA, sin sesión del Canal). |
   | **📋 Copiar bookmarklet** (botón) | Móvil (iOS Safari, Chrome Android) y cualquier navegador | Copia al portapapeles. Crea un favorito cualquiera, edita su URL y pega. |

   Si tu HA tiene `internal_url` y `external_url` configuradas, la página renderiza una sección por cada variante (LAN + externo) con sus propios botones — instala el que vayas a usar (o ambos, para alternar).

4. Renómbralo a algo memorable: **"Canal → HA"** (o `Canal: Casa` si tienes varios contratos).
5. Abre la Oficina Virtual, loguéate y **clica el favorito**.

> **Truco iOS sin botón Copiar**: si el `navigator.clipboard` falla (HTTPS auto-firmado sin aceptar, modo privado, etc.) la página cae a `window.prompt()` con el texto preseleccionado — long-press → Copiar. Detalles + truco con iCloud Safari Sync para sincronizar el favorito desde un Mac: [docs/SETUP.md](docs/SETUP.md).

### 4. Primer click → sensores aparecen

Al pulsar el bookmarklet desde la página del portal:

1. El JS valida que estás en `oficinavirtual.canaldeisabelsegunda.es`.
2. Carga `/group/ovir/consumo`, cambia la periodicidad a **Horaria**, descarga el CSV.
3. POST a `https://<tu-ha>/api/canal_isabel_ii/ingest/<entry_id>` con Bearer token.
4. HA parsea, persiste, **bindea el contrato** (1ª vez) y **recarga la entry** para materializar los sensores.
5. Verás un `alert()` en el navegador: ✅ "168 lecturas, lectura del contador 56,735 m³".

A partir de aquí, cada click vuelca lo que haya nuevo. Las estadísticas son **upsert-seguras** — pulsar 2 veces seguidas no duplica.

> **Importar histórico (opcional)**: para meter datos antiguos (p.ej. enero entero), en **Mi consumo** del portal **filtra el rango con frecuencia Horaria** y pulsa **Ver** *antes* de pulsar el favorito. El bookmarklet lee el formulario tal cual está en pantalla, así que la fecha que veas se importa retroactivamente al panel de Energía → Agua.
>
> ⚠️ **Máximo 30 días por click** — el portal rechaza rangos mayores con error. Para meter más historia (varios meses), repite con **tramos consecutivos de ≤30 días** (p.ej. `1-30 ene`, `31 ene-1 mar`, …): los datos se **acumulan, no se sobrescriben** (las estadísticas externas son upsert por timestamp horario). El portal retiene ~7 meses; nada anterior se puede recuperar.

> **Sesión de HA**: sólo necesitas estar logado en el **portal de Canal**
> (DNI + contraseña). **No hace falta estar logado en Home Assistant** en ese
> navegador — el bookmarklet lleva su propio Bearer token embebido y el
> endpoint de ingesta no pide cookies de HA. Desde iOS, el bookmarklet se
> pulsa en **Safari** (no en la app de HA); la app no interviene. Detalle en
> [docs/SETUP.md §5](docs/SETUP.md#5-primer-click--sensores-aparecen).

## Entidades

Por contrato, **3 sensores agrupados en un dispositivo** con tu nombre de instalación:

| Sensor                  | Unidad | Clase                | Para qué                                                                |
|-------------------------|--------|----------------------|-------------------------------------------------------------------------|
| `Consumo última hora`   | L      | `total`              | Litros consumidos en la última hora publicada.                          |
| `Consumo periodo`       | L      | `total_increasing`   | Suma del cache local (no la factura). Protegido por `RestoreSensor`.    |
| `Lectura del contador`  | m³     | `total_increasing`   | **Lectura absoluta del contador físico — la que casa con la factura.**  |

Atributos comunes (los 3 sensores los exponen):

| Atributo                    | Significado                                                                 |
|-----------------------------|-----------------------------------------------------------------------------|
| `contract`                  | ID del contrato.                                                            |
| `meter`, `address`          | Nº de contador, dirección suministro.                                       |
| `period`, `frequency`       | Periodo + frecuencia del muestreo (estables).                               |
| `last_reading_at`           | Timestamp ISO local del dato horario más reciente.                          |
| `oldest_reading_at`         | Timestamp del primer dato en cache.                                         |
| `data_age_minutes`          | Minutos desde `last_reading_at` (alertas de "hace mucho que no subo CSV").  |
| `last_ingest_at`            | Timestamp del último POST recibido (≠ `last_reading_at`, que es del dato).  |
| `last_ingest_age_minutes`   | Minutos desde el último POST (alerta "olvidé pulsar el bookmarklet").        |
| `consumption_today_l`       | Litros hoy (zona horaria de HA).                                            |
| `consumption_yesterday_l`   | Litros ayer.                                                                |
| `consumption_last_7d_l`     | Rolling 7 días.                                                             |
| `consumption_last_30d_l`    | Rolling 30 días.                                                            |

`Lectura del contador` añade `meter_reading_at` y `raw_reading` ("56,735m³"). `Consumo periodo` añade `readings_count`.

Estadísticas externas: `canal_isabel_ii:consumption_<contract>` — alimenta el **panel de Energía → Agua** con histórico horario.

## Servicios

- `canal_isabel_ii.refresh` `{instance?}` — fuerza al coordinator a recomputar atributos sin esperar al tick horario. **No** descarga datos del portal (eso sólo lo hace el bookmarklet).
- `canal_isabel_ii.show_bookmarklet` `{instance?}` — vuelve a publicar la notificación con el bookmarklet (útil si la perdiste o quieres reinstalarlo en otro navegador).

`instance` acepta el nombre de la instalación (`"Casa principal"`) o el `entry_id`. Si lo omites, aplica a todas las entries.

## Multi-contrato (importante)

⚠️ **Cada entry de la integración = 1 contrato = 1 bookmarklet.**

Si tu cuenta de Canal tiene **varios contratos** (segunda residencia, garaje, etc.):

1. Añade **una entry separada** por contrato (`+ Añadir integración` repetido con nombre distinto: `Casa`, `Oficina`, etc.).
2. Cada entry te dará un bookmarklet **distinto** con su propio token + entry_id.
3. Instala los 2 (o N) bookmarklets en favoritos. Renómbralos para no confundirlos.

**El integration NO acepta CSVs con varios contratos**. Si pulsas un bookmarklet teniendo seleccionado en el portal otro contrato distinto al que esa entry tenía bindeado, recibirás **HTTP 409** y aparecerá una **notificación persistente** explicándote qué hacer (cambiar de contrato en el portal o usar otro bookmarklet).

El primer POST de una entry **bindea el contrato automáticamente** — no hay que configurar nada por anticipado, sólo pulsar el bookmarklet con el contrato deseado seleccionado en el portal la primera vez.

## Solución de problemas

| Síntoma                                              | Probable causa                                | Qué hacer                                                                                                       |
|------------------------------------------------------|-----------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Click bookmarklet → "Estás en `accounts.google.com`"| El favorito se pulsó fuera del portal CYII    | Abre antes la Oficina Virtual y vuelve a pulsar.                                                                |
| "Portal no autenticado (HTTP 302)"                  | Sesión del portal caducada                    | Vuelve a entrar en la Oficina Virtual (DNI + contraseña), y reintenta.                                          |
| "HTTP 401 — invalid_token"                          | Token rotado o entry mal                      | Lanza `canal_isabel_ii.show_bookmarklet` y reinstala el favorito (el token cambia si la entry se eliminó/recreó).|
| "HTTP 404 — unknown_entry"                          | El `entry_id` del bookmarklet ya no existe en HA (integración borrada/recreada) o se copió un bookmarklet antiguo con el placeholder `<pending>` | Añade la integración otra vez, lanza `canal_isabel_ii.show_bookmarklet`, sustituye el favorito viejo por el nuevo. |
| "HTTP 409 — contract_mismatch"                      | Otro contrato seleccionado en el portal       | Cambia al contrato correcto en el portal o pulsa el bookmarklet de la otra entry.                               |
| "HTTP 400 — multiple_contracts"                     | El CSV trae > 1 contrato                      | En el portal sólo selecciona 1 contrato antes de descargar.                                                     |
| "fetch failed" / mixed content                      | HA accesible por HTTP, no HTTPS               | Pon HA detrás de HTTPS (DuckDNS, Cloudflare Tunnel, Nabu Casa…).                                                |
| Sensor `Consumo periodo` no coincide con factura    | Por diseño                                    | Usa `Lectura del contador` (m³) para la cifra absoluta.                                                          |
| Panel de Energía vacío tras 1 click                 | Sólo hay 1 reading horaria importada          | Pulsa el bookmarklet más veces a lo largo del día (o programa un Atajo iOS / cron).                              |

## Créditos

Derivado de [miguelangel-nubla/homeassistant_canal_isabel_II](https://github.com/miguelangel-nubla/homeassistant_canal_isabel_II) (código base e idea original) con parches de la comunidad upstream.

Este fork: arquitectura **bookmarklet + endpoint HTTP** sin dependencias externas, con atributos extendidos de freshness (`last_ingest_at`, `data_age_minutes`) para alertas de "hace mucho que no subo datos".

## Licencia

MIT — ver [LICENSE](LICENSE). Copyright original de Miguel Angel Nubla preservado.
