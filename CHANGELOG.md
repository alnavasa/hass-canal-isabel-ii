# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/).

## [0.4.9] — 2026-04-25

### Arreglado

- **Página de instalación devolvía 401 Unauthorized**. El `HomeAssistantView`
  de v0.4.8 se registró con `requires_auth=True` esperando que la cookie de
  sesión de HA bastase, pero **una navegación normal del navegador desde un
  link markdown de notificación no lleva el header `Authorization: Bearer`**
  que HA exige (el access token vive en `localStorage` del frontend y solo
  viaja en peticiones que emite el JS del frontend, no en navegaciones
  directas del usuario). Resultado: cada user que clicaba el link veía
  *"401: Unauthorized"* en blanco. Ahora la vista corre con
  `requires_auth=False` y valida el token de la entry vía `?t=<token>` en el
  query string, comparado con `secrets.compare_digest`. La notificación
  reescribe el link incluyendo el token automáticamente — abrir la
  notificación → click → página carga.

### Notas

- **No expone nada nuevo**: el mismo token ya viaja embebido en el cuerpo
  del bookmarklet (es lo que el `<a href="javascript:…">` usa para POSTear
  al endpoint de ingesta). Quien tiene la URL de la página tiene también la
  URL del bookmarklet con el token dentro — exposición simétrica, no se
  añade superficie.
- **Acordaos de regenerar la notificación tras update**: la URL antigua
  `/api/canal_isabel_ii/bookmarklet/<entry_id>` (sin `?t=…`) seguirá dando
  401. *Ajustes → Herramientas para desarrolladores → Acciones →
  `canal_isabel_ii.show_bookmarklet`* para reaparecer la notificación con
  el link nuevo. El bookmarklet en sí (el favorito en tu barra) **no
  cambia** — solo cambia el link a la página de instalación dentro de la
  notificación.

## [0.4.8] — 2026-04-25

### Añadido

- **Página HTML de instalación del bookmarklet** con botón *Copiar* y
  enlace arrastrable. Endpoint nuevo:
  `GET /api/canal_isabel_ii/bookmarklet/<entry_id>` (autenticado con la
  cookie de sesión de Home Assistant — la abres pulsando el link de la
  notificación, sin tokens en la URL).

  Resuelve el dolor real de copiar ~1.5 KB de JavaScript URL-encoded
  desde un bloque de código Markdown, especialmente en iOS Safari
  (long-press, arrastrar marcadores de selección a través de cientos
  de caracteres escapados, dedos cruzados — y lo más típico es que el
  user lo deje a medias). La página ofrece dos formas de instalación:

  - **★ Canal → HA** — un enlace estilo botón que se arrastra
    directamente a la barra de favoritos en escritorio. Click suelto
    está bloqueado con `preventDefault()` + alert (ejecutarlo en HA
    no tiene sentido — no hay sesión del Canal allí).
  - **📋 Copiar bookmarklet** — botón que llama a
    `navigator.clipboard.writeText()`. Un solo toque, funciona en iOS
    Safari, Chrome móvil/escritorio y Firefox.

  Si tu HA tiene `internal_url` y `external_url` configuradas, la
  página renderiza una sección por cada variante (LAN + externo)
  con sus propios botones de copiar / arrastrar.

  Adicionalmente: `<details>` colapsables con el código fuente
  legible (sin minificar) y los datos técnicos (URL, entry id,
  token, endpoint ingest), un aviso resaltado sobre la regla
  *un bookmarklet ↔ un contrato*, y soporte automático para tema
  oscuro (`@media (prefers-color-scheme: dark)`).

### Cambiado

- **La notificación persistente de instalación es ahora corta** y
  enlaza a la página HTML nueva. Se mantiene el bookmarklet en bruto
  + el código fuente dentro de un bloque `<details>` colapsado como
  fallback por si la página no abre. Las instrucciones detalladas
  por navegador (Mac Safari / iOS Safari / Chrome / Firefox) se han
  movido a la página HTML, que tiene mejor UX para presentarlas.

### Notas

- **Acordaos de regenerar el bookmarklet tras update**: la URL
  `javascript:…` guardada en favoritos NO se actualiza sola al
  actualizar la integración. *Ajustes → Herramientas para
  desarrolladores → Acciones → `canal_isabel_ii.show_bookmarklet`*
  para reaparecer la notificación, pulsa el link de la página, y
  reemplaza la URL del favorito. (En v0.4.8 ningún cambio del JS
  del bookmarklet en sí — esto es solo si quieres aprovechar la
  página nueva para reinstalarlo más cómodamente.)

## [0.4.7] — 2026-04-24

### Arreglado

- **Bookmarklet v0.4.6 estaba roto en su forma minificada**. El
  minifier junta líneas del template JavaScript con un espacio (no
  con newline, para que Safari acepte la URL como marcador de una
  sola línea), y v0.4.6 había introducido comentarios `//` que al
  quedarse sin salto de línea se convertían en un comentario que
  devoraba el resto del script. Resultado: clicar el favorito no
  hacía nada visible y el portal seguía sirviéndote el rango por
  defecto (60 días) — exactamente el comportamiento que v0.4.6
  pretendía arreglar. Ahora:
  - El minifier detecta líneas puras de comentario `//…` y las
    descarta antes de unir, protegiendo contra regresiones futuras.
  - Los comentarios JS del template se han eliminado; la
    documentación vive en el docstring de Python donde pertenece.
  - Un test nuevo verifica que el cuerpo minificado no contiene
    `//` (salvo dentro de URL literals, de los que no tenemos).
- **Acordaos de regenerar el bookmarklet**: la URL `javascript:…`
  guardada en tus favoritos es el minificado del momento de la
  instalación. Actualizar la integración NO reescribe tu favorito.
  Tras update: *Ajustes → Desarrollador → Servicios → Canal de Isabel II
  Mostrar bookmarklet* para que reaparezca la notificación con el
  código actualizado, luego edita la URL de tu favorito y pega el
  bloque nuevo.

## [0.4.6] — 2026-04-24

### Arreglado

- **Bookmarklet respeta el filtro de pantalla**. Si estás en *Mi
  consumo* con un mes concreto filtrado (p.ej. enero), el favorito
  ahora lee directamente el DOM actual en vez de ignorarlo haciendo
  una recarga limpia. Antes, filtrabas enero en pantalla pero el
  bookmarklet descargaba el rango por defecto (últimos 60 días) y
  nunca entraban los datos del mes antiguo — el alert decía
  *"Lecturas importadas: 1439, Nuevas: 0"* aunque hubieses filtrado
  enero. La recarga automática solo se usa como *fallback* cuando
  pulsas el favorito desde otra página del portal sin formulario
  cargado.
- **Import histórico retroactivo**. El algoritmo de estadísticas
  detecta cuando un push incluye horas anteriores a la última
  estadística almacenada y, en ese caso, lee la serie completa
  existente, fusiona con las horas nuevas (en colisión de marca de
  tiempo gana la nueva), y reescribe la serie recalculando la suma
  corriente desde cero. El panel Energía → Agua renderiza cada
  barra como `sum[n] - sum[n-1]`, así que reescribir la serie no
  cambia ninguna barra ya pintada; solo inserta las nuevas en su
  posición cronológica. En la práctica: filtras un mes del pasado
  en el portal, pulsas el favorito, y los datos aparecen
  retroactivamente en el dashboard. Antes, esas horas se descartaban
  silenciosamente porque el filtro anti-spike las confundía con
  duplicados de una caché vieja.
- Captura de `<select>` en el POST al portal: además de los
  `<input>`, el bookmarklet ahora recoge también las selects del
  formulario, para que tus selecciones de mes/año en dropdowns se
  incluyan en el switch a frecuencia horaria.

### Notas

- Upgrade seguro desde 0.4.5. Las estadísticas ya almacenadas no se
  alteran hasta que el user pulsa el favorito con un rango
  histórico filtrado — el flujo rolling-forward cotidiano sigue
  idéntico (mismo algoritmo spike-immune que siempre).

## [0.4.5] — 2026-04-24

Primera release pública.

### Funcionalidad

- **Setup en dos campos**: nombre de instalación + URL HTTPS de Home
  Assistant (local o pública). Integración pura de Home Assistant:
  custom_component de ~50 KB, sin procesos externos ni dependencias
  adicionales.
- **Bookmarklet generado por la integración**. El asistente publica una
  notificación persistente con la URL `javascript:…` lista para arrastrar
  a la barra de favoritos. Si HA tiene `internal_url` y `external_url`
  configuradas, se generan **dos bookmarklets** (LAN + externa) en
  bloques separados y etiquetados.
- **Click → datos en HA**. Con sesión abierta en el portal de Canal de
  Isabel II, un click en el favorito descarga el CSV horario, lo POSTea
  al endpoint `api/canal_isabel_ii/ingest/<entry_id>` con `Authorization:
  Bearer <token>`, y la integración lo persiste como sensores +
  estadísticas externas horarias.
- **Sensores creados**: lectura absoluta del contador (m³), consumo de la
  última hora (L), consumo del periodo cargado (L), timestamp de la
  última lectura. Todos con `device_class` y `state_class` correctas para
  alimentar el panel **Energía → Agua** de HA.
- **Estadísticas spike-immune**. El algoritmo que empuja al recorder
  preserva continuidad cuando el cache local se vacía: pushar los mismos
  datos dos veces es no-op, una caché reducida no sobrescribe sumas
  almacenadas, y el panel Energía nunca dibuja barras negativas
  artificiales en la unión.
- **Servicio `canal_isabel_ii.show_bookmarklet`**: re-publica la
  notificación si la cierras sin querer. Acepta `instance` opcional para
  filtrar a una entry concreta.

### Seguridad

- Endpoint de ingest **independiente del login de HA** (`requires_auth =
  False`) y autenticado con token de 192 bits por entry. No requiere
  estar logado en HA al pulsar el bookmarklet — práctico para iOS Safari
  donde la app de HA y el navegador no comparten cookies.
- Token validado con `secrets.compare_digest` (constant-time, sin leaks
  de timing).
- Validación cruzada **contrato vs entry**: si dos entries tienen
  bookmarklets de contratos distintos, no se puede mezclar — el endpoint
  responde 409 si el CSV recibido no corresponde al contrato bindeado a
  esa entry.

### Compatibilidad

- HA Core ≥ 2025.10.
- Python 3.13.
- HACS (instalación como repositorio personalizado).
