# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/).

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
