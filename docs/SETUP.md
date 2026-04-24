# Guía de instalación — bookmarklet + integración

## Resumen visual del flujo

```
1. HACS instala la integración           ──►  Reinicia HA
2. Añadir integración (nombre + URL HA)  ──►  Notificación → página de instalación
3. Página: arrastrar (escritorio) o      ──►  Favorito creado
   📋 copiar (móvil)
4. Login en oficinavirtual.canal…        ──►  Click favorito → POST automático
5. Sensores aparecen                      ──►  ✅ Listo. Repite paso 4 cuando quieras refrescar
```

---

## 1. Pre-requisito: HA accesible por HTTPS (local O externo)

El bookmarklet corre en el **navegador** (no en HA). Cuando lo pulsas desde la
web del portal (`https://oficinavirtual.canaldeisabelsegunda.es`) tu navegador
hace un `fetch()` hacia tu Home Assistant. Las reglas del navegador imponen
una sola restricción dura:

- **HTTPS** sí o sí. La página del portal es HTTPS, y Safari/Chrome/Firefox
  bloquean cualquier `fetch` HTTP desde una página HTTPS (*mixed content*).
- **Certificado aceptado** por el navegador desde el que pulsas el bookmarklet.
  Let's Encrypt / Nabu Casa valen sin tocar nada; un certificado auto-firmado
  vale si previamente lo has aceptado en ese navegador.

Con eso en mente, **cualquiera** de estas dos formas funciona:

### Opción A — HA accesible solo en tu red local (privacidad máxima)

URL ejemplo: `https://192.168.1.50:8123` o `https://homeassistant.local:8123`.

- Funciona **mientras el dispositivo desde el que pulsas el bookmarklet esté
  en esa red** (portátil en casa, móvil en el WiFi doméstico, etc.).
- Si te conectas fuera de casa y pulsas el bookmarklet → *fetch failed*.
  Es el trade-off de no exponer HA a internet.
- Necesitas HTTPS local. Las rutas más cortas:
  - Add-on *NGINX Home Assistant SSL proxy* + Let's Encrypt con DNS-01.
  - Reverse proxy propio (Nginx/Caddy/Traefik) con cert Let's Encrypt o
    auto-firmado aceptado en el navegador.
  - Cert auto-firmado generado por `openssl` + aceptado manualmente una vez.

### Opción B — HA accesible desde fuera (usar el bookmarklet en cualquier sitio)

URL ejemplo: `https://micasa.duckdns.org` · `https://abc123.ui.nabu.casa` ·
`https://hass.midominio.com`.

| Setup                              | URL ejemplo                                | Notas                                        |
|------------------------------------|--------------------------------------------|----------------------------------------------|
| DuckDNS + Let's Encrypt (HA OS)    | `https://miinstancia.duckdns.org`          | Add-on DuckDNS de la tienda. Gratis.         |
| Nabu Casa Remote                   | `https://abc123.ui.nabu.casa`              | El de pago oficial — out-of-box.             |
| Cloudflare Tunnel                  | `https://hass.tudominio.com`               | Gratis, sin abrir puertos en el router.      |
| Tailscale Funnel                   | `https://hass.tailnet-xxx.ts.net`          | HTTPS gratis con Tailscale.                  |

**Recomendación**: si vas a pulsar el bookmarklet siempre desde casa o desde una
VPN a casa, usa la **Opción A** (más privado, un paso menos). Si quieres poder
pulsarlo desde cualquier sitio, usa la **Opción B**.

### No funciona

- `http://192.168.x.x:8123` o cualquier URL HTTP — bloqueado por mixed content.
- `http://homeassistant.local:8123` (salvo que tengas mDNS **y** HTTPS).
- IPv6 link-local (`fe80::…`) — Safari lo rechaza.

### ¿VPN site-to-site al router de casa?

Funciona en **Opción A** si el navegador desde el que pulsas el bookmarklet
está dentro del túnel VPN cuando lo pulsas (puede resolver y alcanzar
`192.168.x.x`). Si el navegador no está en la VPN, no alcanza la IP local →
no va.

---

## 2. Instalar la integración

### Vía HACS (recomendado, auto-update)

1. **HACS → menú `⋮` → Repositorios personalizados.**
2. Pega `https://github.com/alnavasa/hass-canal-isabel-ii`.
3. Categoría: **Integration → Añadir**.
4. Cierra el modal, busca **"Canal de Isabel II"** → **Descargar** → última versión.
5. **Reinicia Home Assistant** (Ajustes → Sistema → menú `⋮` → Reiniciar).

### Manual

```bash
cd <config>/custom_components
git clone https://github.com/alnavasa/hass-canal-isabel-ii canal_isabel_ii
# o copia sólo la carpeta custom_components/canal_isabel_ii del repo
```

Reinicia HA.

---

## 3. Añadir la integración

1. **Ajustes → Dispositivos y servicios → + Añadir integración**.
2. Busca **Canal de Isabel II** → click.
3. El asistente pide 2 cosas:

   | Campo                    | Qué poner                                                                  | Ejemplo                                       |
   |--------------------------|----------------------------------------------------------------------------|-----------------------------------------------|
   | **Nombre instalación**   | Etiqueta libre (será nombre del dispositivo + prefijo de los sensores).    | `Casa principal`, `Casa`, `Piso oficina`        |
   | **URL pública de tu HA** | URL HTTPS desde fuera. Default: la `external_url` configurada en HA.       | `https://micasa.duckdns.org`        |

4. **Enviar**.

Lo que pasa por dentro:

- Se crea la **entry** con un `entry_id` único.
- Se genera un **token de 192 bits** (`secrets.token_hex(24)`).
- Aparece una **notificación persistente corta** que enlaza a la página de
  instalación del bookmarklet (servida por la propia integración en
  `/api/canal_isabel_ii/bookmarklet/<entry_id>`).
- Los sensores **todavía no existen** — se materializan en el primer POST.

> Verás primero un modal "✅ Éxito · Configuración creada para <nombre>" con
> un botón **Terminar**. Pulsa Terminar y pasa al siguiente paso: la
> notificación con el bookmarklet ya está esperándote.

---

## 3.5 Página de instalación del bookmarklet

La notificación es **corta** y enlaza a una página HTML que la integración
sirve en `/api/canal_isabel_ii/bookmarklet/<entry_id>`. La página
sustituye al antiguo "copia este bloque de código markdown a mano" y es
la forma recomendada de instalar el favorito.

### Cómo llegar a la página

1. Pulsa la **campana** 🔔 (barra lateral izquierda de HA, abajo del todo).
2. Abre la notificación **"Bookmarklet listo — <nombre instalación>"**.
3. Pulsa **📥 Abrir página de instalación**. Se abre en una pestaña nueva.

> La página usa la cookie de sesión de HA para autenticarte; **no hay tokens
> en la URL**. Si no estás logado en HA, te llevará primero al login y
> después a la página.

### Qué encuentras en la página

Una sección por cada URL de HA (si tienes `internal_url` y `external_url`,
verás 2 secciones — LAN + externo, etiquetadas), y dentro de cada sección
**dos formas** de instalar el favorito:

| Forma                              | Cuándo conviene                                                  | Qué hace                                                                                                |
|------------------------------------|------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| **★ Canal → HA** (botón arrastrable) | Escritorio (Safari Mac, Chrome, Firefox, Edge)                  | Lo arrastras directamente a la **barra de favoritos**. Click suelto está bloqueado (preventDefault + alert) — clicarlo dentro de HA no haría nada útil porque ahí no hay sesión del Canal. |
| **📋 Copiar bookmarklet** (botón)    | Móvil (iOS Safari, Chrome Android) y cualquier navegador         | Copia al portapapeles via `navigator.clipboard.writeText()`. Crea un favorito cualquiera, edita su URL y pega. |

Además la página incluye, plegados:

- **Cómo usarlo (paso a paso)** — 6 puntos con el flujo de uso.
- **Datos técnicos** — URL HA, entry id, token, endpoint de ingest. Útil
  para auditoría / debugging.
- **Código JavaScript legible (sin minificar)** — la fuente del bookmarklet
  multi-línea, por si quieres revisar qué hace antes de ejecutarlo.

### Si el botón Copiar falla

Algunos contextos bloquean el `navigator.clipboard` (HTTPS auto-firmado sin
aceptar, modo privado, IFrame raro). En ese caso la página cae a un
`window.prompt()` con el texto preseleccionado — **long-press → Copiar** en
móvil, o **Cmd+C / Ctrl+C** en escritorio. Es un fallback automático, no
tienes que hacer nada.

### ¿Se ha cerrado la notificación sin querer?

No pasa nada. Abre **Herramientas para desarrolladores → Acciones**
(o *Ajustes → Herramientas para desarrolladores → Acciones* según
versión), busca la acción `canal_isabel_ii.show_bookmarklet` y pulsa
**Realizar acción**. La notificación vuelve a aparecer con el mismo enlace
a la misma página. Si tienes varias integraciones y quieres sólo una,
añade el campo opcional `instance: Casa principal`.

---

## 4. Si prefieres pegar el bookmarklet manualmente (fallback)

La página HTML de §3.5 es la ruta recomendada. Esta sección sólo es útil
si la página no se abre por algún motivo (HA caído, cookie expirada en una
sesión rara) y necesitas pegar el bookmarklet en bruto desde el bloque
`<details>` colapsado de la notificación.

> El bloque `<details>` de la notificación trae siempre el `javascript:…`
> en bruto y los datos técnicos como fallback. Es exactamente lo que ofrece
> la página de instalación, sin la UX de los botones.

### Safari macOS

1. Marcadores → **Añadir marcador** (cualquier página, da igual cuál).
2. Marcadores → **Editar marcadores** (`Cmd+Alt+B`).
3. Selecciona el favorito recién creado, **edita la URL** (doble click sobre el campo URL): pega el bloque que empieza por `javascript:`.
4. Renombra a algo memorable: **`Canal → HA`**.
5. Arrástralo a la **barra de favoritos** si quieres tenerlo a 1 click.

### iOS Safari — truco con iCloud Safari Sync

iOS no deja editar URLs de favoritos directamente desde el iPhone, pero sí
los sincroniza desde Mac:

1. Crea el favorito en **Safari Mac** como en la sección de arriba.
2. Asegúrate de tener iCloud → Safari activado en ambos dispositivos.
3. En segundos aparece en iOS Safari: Marcadores → Mobile Bookmarks → tu carpeta.
4. **Púlsalo** mientras estás en el portal de Canal.

> Sin Mac: el botón **📋 Copiar bookmarklet** de la página de §3.5 funciona
> perfectamente en iOS Safari. Crear el favorito y pegar la URL editada se
> hace desde la propia app de Safari iOS — sólo lleva un par más de toques
> que el flujo Mac+Sync, pero no necesita ningún Mac.

### Chrome / Edge (PC, Mac, Android escritorio)

1. **Ctrl+D** (Cmd+D en Mac) para añadir favorito en cualquier página.
2. Click derecho sobre la barra de favoritos → **"Editar"** (o ve a `chrome://bookmarks`).
3. Pega el `javascript:…` en el campo URL.
4. Renombra a **`Canal → HA`**.

### Firefox

1. **Ctrl+D** → guarda el favorito en cualquier carpeta.
2. **Ctrl+Shift+B** abre la barra de favoritos.
3. Click derecho sobre el favorito → **Propiedades**.
4. Pega el `javascript:…` en URL → **Guardar**.

### Chrome Android / Firefox Android

Móviles Android no permiten editar URLs de favoritos en la UI estándar. Lo
más cómodo es:

- Usar el botón **📋 Copiar bookmarklet** de la página de §3.5, crear un
  favorito en Chrome PC, sincronizarlo a Android via cuenta Google → Sync.
- O instalarlo desde Chrome PC y dejar que el Chrome Sync lo propague.

---

## 5. Primer click — sensores aparecen

1. Abre [oficinavirtual.canaldeisabelsegunda.es](https://oficinavirtual.canaldeisabelsegunda.es) y **loguéate** (DNI/CIF + contraseña, captcha si lo pide).
2. Si tienes varios contratos, **selecciona el que quieres importar** en el desplegable. Cada bookmarklet se atará a 1 contrato la primera vez.
3. **Click en el favorito Canal → HA**.

### ¿Hace falta estar logado en Home Assistant?

**No.** Sólo hace falta estar logado en **un** sitio: la **Oficina Virtual de Canal**, en el **mismo navegador** donde pulsas el bookmarklet. La sesión de Home Assistant en ese navegador da igual — puedes no haber entrado nunca a HA desde ese dispositivo y el bookmarklet funciona igual.

| ¿Login en…                                                     | es obligatorio? | por qué                                                                                                                                                                                         |
|----------------------------------------------------------------|-----------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Portal Canal** (`oficinavirtual.canaldeisabelsegunda.es`)    | ✅ Sí            | El bookmarklet reusa tu **cookie de sesión** del portal para descargar el CSV. Sin login el portal responde `302 → /login` y verás `Portal no autenticado (HTTP 302)`.                          |
| **Home Assistant** (`https://<tu-ha>:8123`)                    | ❌ No            | El bookmarklet ya lleva **dentro un Bearer token** propio (192 bits) generado al crear la entry. Es **lo único** que HA comprueba para aceptar el POST.                                          |

**Cómo lo valida HA, exactamente**:

1. El endpoint `/api/canal_isabel_ii/ingest/<entry_id>` tiene `requires_auth = False` dentro de HA — no mira cookies ni sesión de usuario de HA.
2. En su lugar compara con `secrets.compare_digest` el token pegado en el header `Authorization: Bearer …` contra el que se guardó en la entry.
3. Además comprueba que el `Origin` del request es `https://oficinavirtual.canaldeisabelsegunda.es` (para que el bookmarklet no se pueda disparar desde cualquier web que te engañen a visitar).

Resultado: el bookmarklet se autentica **solo**. Da igual el navegador, el dispositivo, la sesión de HA.

**Caso iOS — app de HA vs Safari**:

La app de Home Assistant para iOS es un WKWebView propio y **no comparte cookies ni favoritos con Safari**. Eso daría problema *si* necesitásemos cookies de HA — pero no las usamos. El bookmarklet:

- **Se pulsa desde Safari** (donde tienes la Oficina Virtual abierta con tu DNI + contraseña).
- **Manda su POST directamente a tu HA por HTTPS** con el Bearer token embebido.
- **La app de HA no participa** en ningún momento. Da igual si está instalada, si está logada, o si no la has abierto nunca.

**Requisito único desde iOS**: que Safari (no la app) pueda alcanzar la URL HTTPS de tu HA — ver [sección 1](#1-requisitos-previos) (opción A local con certificado instalado en el iPhone, u opción B pública).

**Caso Android / Chrome escritorio**: idéntico. Chrome donde tengas la Oficina Virtual abierta hace el POST. La app de HA Android, si la tienes, no interviene.

**Chequeo rápido**: abre la URL HTTPS de tu HA en el mismo navegador donde pulsas el bookmarklet y comprueba que **carga sin advertencia de certificado** y sin `mixed content`. No hace falta loguear — basta con que responda. Si eso funciona, el POST del bookmarklet funcionará.

Lo que verás:

```
✅ Canal → HA (Casa principal)

Contrato: 999000001
Lecturas importadas: 168
Nuevas: 168
Lectura del contador: 56,735 m³
```

Si aparece error en `alert()`:

| Mensaje                                         | Causa                                          | Qué hacer                                                          |
|-------------------------------------------------|------------------------------------------------|--------------------------------------------------------------------|
| `Estás en accounts.google.com`                  | Pulsaste el favorito fuera del portal          | Abre la Oficina Virtual primero.                                   |
| `Portal no autenticado (HTTP 302)`              | Sesión caducada                                | Vuelve a loguear con DNI + contraseña.                             |
| `Descarga del CSV falló (HTTP 4xx)`             | Periodicidad no disponible o cuenta sin datos | Comprueba que la lectura horaria está activada en tu suministro.   |
| `HTTP 401 — invalid_token`                      | Token mal pegado al crear el favorito          | Lanza `canal_isabel_ii.show_bookmarklet` y vuelve a copiar.        |
| `HTTP 404 — unknown_entry`                      | El `entry_id` del bookmarklet ya no existe en HA (integración borrada/recreada) o se copió un favorito antiguo con `<pending>` | Vuelve a añadir la integración, lanza `canal_isabel_ii.show_bookmarklet`, sustituye el favorito viejo por el nuevo. |
| `HTTP 409 — contract_mismatch`                  | Otro contrato seleccionado en el portal        | Cambia al contrato correcto o usa el bookmarklet de otra entry.    |
| `HTTP 400 — multiple_contracts`                 | El CSV trajo > 1 contrato                      | En el portal selecciona 1 contrato antes de descargar.             |

---

## 6. Verificar las entidades creadas

**Ajustes → Dispositivos y servicios → Canal de Isabel II → tu instalación**.

Por contrato, deberías ver **un dispositivo** con **3 sensores**:

| Sensor                  | Unidad | Clase                | Para qué                                                  |
|-------------------------|--------|----------------------|-----------------------------------------------------------|
| `Consumo última hora`   | L      | `total`              | Litros de la última hora publicada.                       |
| `Consumo periodo`       | L      | `total_increasing`   | Suma del cache local. **No** es la cifra de la factura.   |
| `Lectura del contador`  | m³     | `total_increasing`   | **Lectura absoluta — la que casa con la factura.**        |

Y una **estadística externa** registrada como `canal_isabel_ii:consumption_<contract>`,
que alimenta el panel de Energía.

---

## 7. Conectar al panel de Energía (Agua)

1. **Ajustes → Tableros → Energía → Configurar el consumo de agua**.
2. **Añadir consumo de agua → Sensor**.
3. En el selector busca tu nombre de instalación (`Casa principal`, p.ej.):
   - `Casa principal - Canal de Isabel II` — la **estadística externa** horaria *(recomendada para gráficas históricas)*.
   - `Lectura del contador Casa principal` — el sensor absoluto en m³.
   - `Consumo periodo Casa principal` — la suma del cache.
4. **Selecciona la estadística externa** (`Casa principal - Canal de Isabel II`). Es la que tiene mejor pinta histórica. **Guardar**.

> El panel de Energía sólo muestra agua si el sensor tiene unidades de
> volumen (L o m³) y `state_class: total` o `total_increasing`.

---

## 8. Multi-contrato (importante)

⚠️ **Cada entry de la integración = 1 contrato = 1 bookmarklet.**

Si tu cuenta de Canal tiene **varios contratos** (segunda residencia, garaje):

1. **Repite los pasos 3 y 4** con un nombre distinto: `Casa`, `Oficina`, `Trastero`.
2. Cada entry te dará un bookmarklet **distinto** con su propio token + entry_id.
3. Renombra los favoritos para distinguirlos: `Canal → HA (Casa)`, `Canal → HA (Oficina)`.
4. Para el primer POST de cada entry: **selecciona el contrato correspondiente en el portal** y pulsa el bookmarklet de esa entry. La integración bindea el contrato al `entry_id` automáticamente y ya nunca aceptará un POST de otro contrato (HTTP 409).

> ¿Por qué esta separación? Cada estadística externa va por contrato. Mezclarlos
> contaminaría el panel de Energía. La separación física por entry te garantiza
> aislamiento de datos.

---

## 9. Programar el refresco automático

El click manual funciona, pero se olvida. Opciones para automatizarlo:

### A. Atajo de iOS (Atajos.app)

> Próximamente — guía en [docs/IOS_SHORTCUT.md](IOS_SHORTCUT.md).

Permite ejecutar el bookmarklet automáticamente cada hora desde un iPhone que
pase tiempo logado en el portal. Útil si tienes el iPhone como hub doméstico.

### B. macOS Automator + cron

`launchctl` con un script que abre Safari, navega al portal y simula el click.
Requiere mantener una sesión Safari logada.

### C. Manual

Pulsa el favorito **una vez al día** o cada vez que quieras ver gráfica fresca. El
portal mantiene retroactivo (~7 meses), así que un click semanal es suficiente
para no perder datos — sólo retrasarás un poco la actualización del panel.

---

## Si algo va mal

| Síntoma                                                  | Probable causa                              | Qué hacer                                                                                                       |
|----------------------------------------------------------|---------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Notificación con bookmarklet no apareció tras añadir     | Race en el primer setup                     | Lanza `canal_isabel_ii.show_bookmarklet` desde *Herramientas para desarrolladores → Acciones*.                  |
| Click bookmarklet → "Estás en `dominio.com`"            | Favorito pulsado fuera del portal           | Abre la Oficina Virtual antes y reintenta.                                                                       |
| `fetch failed` / mixed content en consola del navegador  | HA por HTTP, no HTTPS                       | Pon HA detrás de HTTPS público (DuckDNS, Nabu Casa, Cloudflare Tunnel).                                         |
| `CORS preflight failed`                                  | Origin del bookmarklet bloqueado            | Mira logs de HA: el `CanalIngestView` registra qué origin rechazó. Verifica que clicas desde el portal real.    |
| Sensor `Consumo periodo` cae a 0 tras reinicio HA        | Cache vacío                                  | Está protegido por `RestoreSensor` — debería mantener el último valor. Si cae, abre issue.                       |
| Panel de Energía vacío tras 1 click                      | Sólo hay 1 reading horaria                  | Pulsa el bookmarklet más veces (idealmente 1× al día) o automatiza con Atajo iOS / cron.                         |
| `consumption_today_l` siempre 0                          | Diferencia de tu hora local con HA          | Verifica `homeassistant.config.time_zone` en *Ajustes → Sistema → General*.                                     |

Para cualquier issue no listada, abre un report en
[GitHub Issues](https://github.com/alnavasa/hass-canal-isabel-ii/issues)
con los logs de HA filtrados por `canal_isabel_ii` y la consola del navegador
del momento del click.
