# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/).

## [0.5.15] — 2026-04-26

### Añadido

- **Servicio `canal_isabel_ii.clear_cost_stats`.** Botón de
  recuperación manual para usuarios que ven el panel Energía con
  barras negativas o totales claramente equivocados en la entidad
  `Coste acumulado`. Borra **todas** las estadísticas de largo
  plazo de coste asociadas a la entrada (la externa
  `canal_isabel_ii:cost_<contract>` y la auto-generada
  `sensor.<…>_coste_acumulado`) y dispara un *refresh* del
  coordinator: el siguiente *push* reconstruye toda la serie desde
  cero por la ruta *spike-immune* (la misma que la migración
  one-shot v0.5.4).

  Igual que `refresh` y `show_bookmarklet`, acepta `instance:` con
  el nombre libre de la instalación o el `entry_id`; vacío
  significa *todas las entradas*. Documentado en `services.yaml`,
  por lo que aparece en *Herramientas para desarrolladores →
  Acciones* con descripción y validación de campos.

  Caso de uso: el reporte original (v0.5.7, panel Energía con barras
  −4.54 €, +14.91 €, +71.92 € o −1336.46 € según el rango) podría
  venir de:

  1. Restos de auto-stats pre-v0.5.4 que escaparon al filtro de la
     migración one-shot.
  2. Drift residual entre la serie auto-generada
     (`state_class=total`, recorder) y nuestro *push* explícito
     que sólo se solapan en el ventana visible — la corrección
     v0.5.4 + v0.5.5 los unifica de cara al futuro pero no
     reescribe lo que ya estaba mal en disco para la entrada
     concreta del usuario afectado.

  En vez de pedirles que editen `.storage/core.config_entries`
  para forzar otra migración, el servicio expone esa lógica como
  un click. Tras llamarlo, el panel Energía vuelve a llenarse a
  medida que el coordinator publica la serie reconstruida (puede
  tardar uno o dos *ticks* horarios).

- **6 tests de regresión end-to-end del *push* de coste**
  (`tests/test_continuation_stats.py::TestCostPushPipelineMonotonicity`).
  La pipeline pura `compute_hourly_cost_stream → cumulative_to_deltas
  → merge_forward_and_backfill` queda blindada contra la clase de
  bugs que producirían barras negativas:

  1. Push 2 con progresión B1→B2 manteniéndose monótona.
  2. Tras dos pushes consecutivos, ninguna hora regresa.
  3. Cruce de límite de bimestre dentro del mismo *push*.
  4. Cache que arranca a mitad del bimestre (rango parcial visible).
  5. Cache con hueco interno (gap en el medio del rango).
  6. *Trim* del cache donde las filas viejas del *push* 1
     **sobreviven intactas** en el recorder y el *push* 2 sólo
     extiende el extremo derecho.

  Los seis pasan en HEAD: confirma que el bug del usuario **no**
  proviene del cálculo del coste ni de la fusión con la serie
  existente. La causa más probable es residuo histórico en
  estadísticas, que el nuevo servicio resuelve directamente.

### Refactor

- Extraído el cuerpo de `_migrate_cost_stats_v054` a un *helper*
  reutilizable `_clear_cost_stats_for_entry`. La migración one-shot
  pasa a ser un *thin wrapper* que delega; el servicio nuevo usa
  el mismo *helper*. Sin cambio funcional para usuarios existentes
  — `CONF_COST_STATS_MIGRATED` sigue siendo el *gate* idempotente
  de la ejecución one-shot al primer arranque.

## [0.5.14] — 2026-04-25

### Arreglado

- **El sensor `Bloque tarifario actual` ya no se queda colgado en el
  bimestre anterior durante las primeras horas del cambio de
  bimestre.** `_bimonth_consumo_m3` (la función que decide qué
  bloque tarifario está en curso para `precio_actual` y
  `bloque_tarifario_actual`) llamaba a `r.timestamp.date()` sobre cada
  lectura sin normalizar la zona horaria. Para un timestamp UTC-aware
  (lo más común tras el round-trip por el recorder), `.date()`
  devuelve la fecha **UTC**, no la fecha civil de Madrid. En la
  primera hora civil de un nuevo bimestre — por ejemplo `2026-01-01
  00:30 Madrid local`, que en UTC es `2025-12-31 23:30` — el
  contador se asignaba al bimestre equivocado, y la entidad anunciaba
  un consumo del bimestre que ya no era el actual hasta que pasaba la
  primera hora UTC del nuevo bimestre.

  Solución: nuevo helper puro `sum_for_local_bimonth` en
  `attribute_helpers.py` que convierte cada timestamp a la zona horaria
  local antes de comparar contra los límites del bimestre. Mantiene la
  misma convención que el resto del módulo (timestamp naïve → asumido
  como local, timestamp aware → `astimezone(local_tz)`). Cubierto por
  7 nuevos tests, incluyendo el caso exacto del cambio invierno/verano
  con CEST.

  Sin cambios funcionales en el resto del integrador: las
  estadísticas de largo plazo y el sensor `Coste acumulado` ya
  normalizaban a local explícitamente.

## [0.5.13] — 2026-04-25

### Añadido

- **Tests explícitos del límite de vigencia 2025 → 2026.** `tariff.py`
  modela el corte de tarifa el `2026-01-01` mediante intervalos
  semi-abiertos en `_split_period_by_vigencia`. Esa lógica nunca tuvo
  cobertura directa: las dos *bills* reales del *fixture*
  `TestRealBillValidation` caen ambas dentro de la misma vigencia y la
  protegían sólo de forma indirecta.

  Nueva clase `TestVigenciaBoundary` con seis tests que cubren los
  casos que un cambio en la tabla `VIGENCIAS` o en la aritmética de
  intervalos podría romper en silencio:

  1. Periodo que **termina exactamente** en `2026-01-01` → un único
     segmento, vigencia 2025 (semántica `valid_until` exclusiva).
  2. Periodo que **empieza exactamente** en `2026-01-01` → un único
     segmento, vigencia 2026 (sin segmento residual de 0 días).
  3. Periodo que **cruza** el límite → dos segmentos cuya suma de
     `DP` coincide con la del periodo original (sin error de poste).
  4. `compute_period_total_cost` con un periodo que cruza el límite
     produce un total estrictamente entre los dos *single-vigencia*
     equivalentes (no colapsa a una sola tarifa).
  5. `compute_hourly_cost_stream` mantiene la monotonía de
     `cumulative_eur` a través del límite (crítico: un solo *tick*
     no-monótono lo lee el *recorder* como reset del contador).
  6. La suma del *stream* horario sobre dos bimestres consecutivos
     (Nov-Dec 2025 + Jan-Feb 2026) coincide con la suma de los dos
     `compute_period_total_cost` por bimestre — la invariante en la
     que se apoya la reconstrucción de la factura.

### Arreglado

- **`ruff format` ahora pasa en CI.** El job *Lint* de `v0.5.12` falló
  porque dos archivos tocados en aquella release (`store.py` y
  `tests/test_store_baseline.py`) requerían reformateo. Esta release
  trae el `ruff format` aplicado a esos dos archivos. No hay cambios
  funcionales adicionales en el integrador respecto a `v0.5.12`.

## [0.5.12] — 2026-04-26

### Arreglado

- **El sensor `Consumo periodo` ya no se congela cuando la caché
  alcanza su tope.** `ReadingStore` recorta las lecturas más antiguas
  cuando el número total supera `MAX_READINGS_PER_ENTRY` (8760, ~1
  año de hourly). Hasta ahora, ese recorte hacía que el `native_value`
  acumulado del sensor `Consumo periodo` cayera por debajo de su
  valor monotónico previamente restaurado, activando el *guard*
  ``computed < restored - 0.5`` que congelaba el valor hasta que las
  nuevas lecturas volvieran a superar el máximo histórico — meses, en
  el peor caso.

  Solución: `ReadingStore` mantiene un baseline acumulado por contrato
  (`baseline_liters`). Cada vez que se recorta una lectura, sus litros
  se suman al baseline del contrato correspondiente **antes** del
  borrado. El sensor calcula
  `native_value = baseline_liters[contract] + sum(lecturas en caché)`,
  preservando la monotonía a través de cada *roll* de la caché. El
  baseline persiste en disco junto al resto del *store*. Si el baseline
  es no-cero, se expone en el atributo `trimmed_baseline_l` de la
  entidad para visibilidad operativa.

  Las estadísticas de largo plazo (recorder) **no estaban afectadas**:
  `_push_statistics` se anclaba siempre al `last_sum` del recorder,
  no al sum local. El panel Energía siempre vio los valores correctos.
  Esta release solo arregla el valor mostrado en la card de la entidad.

### Compatibilidad

- *Stores* anteriores a v0.5.12 no tienen el campo `baseline_liters`
  en su JSON; el cargador tolera su ausencia y arranca con baseline
  vacío. Para usuarios cuya caché ya hubiera recortado bajo el código
  antiguo, el siguiente recorte rellena el baseline desde ese punto;
  los litros perdidos en aquel recorte siguen reflejados en las
  estadísticas del recorder, así que no hay pérdida de datos en el
  panel Energía — solo el `native_value` puede tardar unas horas en
  re-cruzar su máximo monotónico anterior.

### Tests

- Nuevo `tests/test_store_baseline.py` con 4 tests: acumulación durante
  el trim, separación por contrato (entries multi-contrato), *roundtrip*
  por serialización, y tolerancia a *stores* pre-v0.5.12 sin el campo.

## [0.5.11] — 2026-04-25

### Arreglado

- **POSTs concurrentes contra el mismo *entry* ya no corrompen el
  estado.** El endpoint `/api/canal_isabel_ii/ingest/<entry_id>`
  realiza un *read-modify-write* sobre estado compartido:
  `config_entry.data` (para reclamar el contrato en el primer ingest),
  el JSON del *store* en disco y la caché del *coordinator*. Si llegan
  dos POSTs separados por milisegundos contra el mismo *entry* (por
  doble click del bookmarklet, o el camino de retry de Chrome cuando
  la red parpadea), los dos pasaban por la misma sección crítica:

  - Los dos veían `expected_contract == ""` y los dos llamaban a
    `async_update_entry` para reclamar el contrato. Hoy es no-op
    (mismo id) pero en una cuenta multi-contrato sería una corrupción
    silenciosa.
  - Los dos ejecutaban `store.async_replace` y se pisaban en el `write()`
    del JSON, dejando un fichero parcialmente fusionado.
  - Los dos programaban un *reload* del *entry*, dejando el segundo
    reload en mitad del *setup* del primero.

  Solución: cada *entry* lleva su propio `asyncio.Lock` (`ingest_lock`,
  creado en `__init__.py`). El bloque crítico de `CanalIngestView.post`
  va envuelto en `async with entry_data["ingest_lock"]:`, así que dos
  POSTs al mismo *entry* se serializan estrictamente. POSTs a *entries*
  distintos siguen pudiéndose procesar en paralelo (un *lock* por
  *entry*, no global).

## [0.5.10] — 2026-04-25

### Arreglado

- **Race condition entre el push inicial de estadísticas y un POST
  concurrente.** El push de *long-term statistics* se dispara desde
  dos sitios:

  1. `async_added_to_hass` cuando la entidad se añade por primera vez
     tras un *reload* del *entry* (por ejemplo después del primer POST
     del bookmarklet).
  2. El listener de `_handle_coordinator_update` cuando llega cada
     POST posterior y el *coordinator* propaga datos nuevos.

  Si el segundo POST llegaba en los milisegundos que el primer push
  estaba dentro de `get_last_statistics` / `statistics_during_period`,
  los dos *tasks* paralelos hacían su propio ciclo de
  *read-modify-write* contra el *recorder* y entrelazaban su lectura
  con la escritura del otro. El resultado eran *deltas* fusionadas
  inconsistentes (sumas mal calculadas en el panel Energía) y, en el
  peor caso, una entrada con `start` duplicado que el *upsert* del
  *recorder* tolera pero deja la serie con un valor *intermedio*.

  Solución: cada entidad lleva su propio `asyncio.Lock` (`_push_lock`)
  que serializa **TODA** invocación a `_push_statistics` y
  `_push_cost_statistics` para esa entidad. Los pushes de entidades
  distintas siguen pudiendo ejecutarse en paralelo (un *lock* por
  contrato, no global). Patrón estándar para serializar
  *read-modify-write* en `asyncio`.

## [0.5.9] — 2026-04-25

### Arreglado

- **Lecturas fuera de cualquier vigencia conocida ya no rompen el
  sensor de coste.** Si en el caché aparece aunque sea una sola lectura
  cuya fecha cae fuera de toda *vigencia* modelada en `tariff.py` (por
  ejemplo, un *backfill* histórico anterior a la vigencia 2025, o una
  fecha futura para la que aún no se ha publicado la próxima vigencia),
  `compute_hourly_cost_stream` lanzaba `ValueError` desde dentro de
  `_split_period_by_vigencia`. Esa excepción se propagaba al *property*
  `native_value` del sensor de coste acumulado y al `_push_cost_statistics`
  que se ejecuta en cada *tick* del *coordinator*, llenando el log de
  *tracebacks* y dejando el sensor sin valor. Ahora `_cost_stream`
  captura el `ValueError`, registra un *warning* claro identificando
  el contrato afectado y devuelve `[]`, lo que hace que el sensor
  conserve el último valor restaurado (`RestoreSensor`) en lugar de
  caer. Cuando se publique una nueva versión que incluya la vigencia
  faltante, el cálculo se reanuda solo. Se añade test de regresión que
  documenta el contrato (`compute_hourly_cost_stream` propaga
  `ValueError`; los *callers* lo capturan).

## [0.5.8] — 2026-04-25

### Cambiado

- **Default sensato para `Cuota suplementaria de alcantarillado`**:
  pasa de `0.0` a `0.1002 €/m³`. El valor `0.0` solo es correcto en
  los pocos municipios que no cobran esta cuota; para la mayoría de
  los que sirve Canal de Isabel II el rango típico está entre
  `0.05` y `0.15 €/m³` (vigencia 2026). Con un default razonable, los
  usuarios que activen el cálculo de coste sin haber consultado aún
  su factura ven un importe estimado mucho más cercano al real desde
  el primer momento, en lugar de ver la cuota suplementaria a cero.
  Quien quiera el comportamiento anterior puede ponerlo a `0` en el
  Options flow tras la instalación.

### Interno

- `.gitignore` añade exclusiones para sidecars de macOS
  (`._*`, `.AppleDouble/`, `.Spotlight-V100`, `.Trashes`) que algunos
  workflows de desarrollo (copia vía Samba/SMB hacia el host de Home
  Assistant) generan automáticamente. Defensa en profundidad — la
  historia del repo ya estaba limpia, esto previene futuras coladas
  por error.

## [0.5.7] — 2026-04-25

### Cambiado

- **Bump de versión sin cambios funcionales.** Idéntica a `0.5.6` en
  código. Publicada únicamente para que HACS detecte una versión nueva
  tras un *clean install* (eliminación de la integración + borrado de
  estadísticas residuales) y se distinga claramente de cualquier copia
  cacheada de versiones anteriores. Si vienes de `0.5.6` no necesitas
  hacer nada: la actualización es no-op.

## [0.5.6] — 2026-04-25

### Cambiado

- **Entidades ahora se buscan tecleando "canal"** en *Herramientas de
  Desarrollo → Estadísticas* y en cualquier otro filtro. Hasta ahora
  el `friendly_name` se construía como `<install> + <entidad>` y
  ninguna de las dos partes contenía la palabra "canal", así que al
  filtrar por "canal" solo aparecían las *external statistics*
  (`canal_isabel_ii:cost_*`, `canal_isabel_ii:consumption_*`),
  dejando 6 entidades por contrato escondidas a menos que el usuario
  recordara los sufijos exactos (`coste`, `precio`, `bloque`,
  `lectura`, `consumo`). Ahora cada entidad lleva el prefijo "Canal"
  en el nombre traducido:

  | Antes | Ahora |
  |-------|-------|
  | `Casa principal Consumo última hora` | `Casa principal Canal Consumo última hora` |
  | `Casa principal Consumo periodo` | `Casa principal Canal Consumo periodo` |
  | `Casa principal Lectura del contador` | `Casa principal Canal Lectura del contador` |
  | `Casa principal Coste acumulado` | `Casa principal Canal Coste acumulado` |
  | `Casa principal Precio actual` | `Casa principal Canal Precio actual` |
  | `Casa principal Bloque tarifario actual` | `Casa principal Canal Bloque tarifario actual` |

  El `unique_id` y el `entity_id` de las **entidades existentes no
  cambian** — solo cambia el `friendly_name` que se ve en la UI.
  Instalaciones nuevas tendrán además `canal` en el slug del
  `entity_id` (ej. `sensor.casa_principal_canal_coste_acumulado`),
  con lo que también son buscables tecleando "canal" en cualquier
  selector.

- **`Cuota suplementaria de alcantarillado` admite ahora hasta
  `5,0 €/m³`** (antes el máximo era `1,0`). Las cuotas reales de los
  municipios están todas muy por debajo de 1 €/m³, pero el límite de
  1 era demasiado estricto: si el usuario tecleaba un dígito de más
  por error (ej. `1,1234` en vez de `0,1234`), HA respondía con
  *"Value too large"* en lugar de aceptarlo y dejar que el usuario
  detectara el typo cotejando con la factura real. Cambio puramente de
  permisividad — los valores correctos siguen funcionando idénticamente.

### Arreglado

- (Interno, sin impacto funcional para el usuario) Limpieza de
  comentarios y casos de test que contenían valores literales de
  facturas reales del autor. Sustituidos por valores sintéticos para
  cumplir la regla del proyecto de cero datos personales en el repo
  público.

## [0.5.5] — 2026-04-25

### Arreglado

- **HA escupía warnings al log** sobre dos sensores de coste con
  combinaciones `device_class` + `state_class` que HA endureció en
  versiones recientes:
  - `Coste acumulado`: era `monetary` + `total_increasing`. HA exige
    para `monetary` que el state_class sea `total` (o `None`) — no
    `total_increasing`, porque el dinero puede decrecer (devoluciones,
    correcciones) y `total_increasing` lleva detección de reset
    automática que no encaja con valores monetarios. Cambiado a
    `total`. **No** publicamos `last_reset` así que HA trata la serie
    como un acumulador puro, igual que antes.
  - `Precio actual`: era `monetary` + `measurement`. La combinación es
    inválida por la misma razón. Pero además semánticamente `monetary`
    es para *cantidades de dinero*, y este sensor reporta una *tasa*
    (€/m³). Eliminado el `device_class`, manteniendo `measurement`
    (que es lo correcto para una medida instantánea). El icono
    `mdi:cash-clock` y la unidad `EUR/m³` siguen exactamente igual.

  Estos warnings aparecían en *Ajustes → Sistema → Registros* a la hora
  de arrancar HA con la integración actualizada. No afectaban
  funcionalmente al panel de Energía (que sigue funcionando con la
  estadística externa `canal_isabel_ii:cost_<contract>` que no
  depende del state_class del sensor), pero ensuciaban el log y
  podrían terminar siendo un error duro en versiones futuras de HA.

## [0.5.4] — 2026-04-25

### Arreglado

- **Panel de Energía mostraba costes incoherentes** con dos síntomas
  habituales tras llevar varias versiones instalado:
  - Sólo aparecía coste del **último periodo**, con todos los meses
    anteriores a 0 € (aunque sí hubiera consumo).
  - Aparecían **barras negativas gigantes** (típicamente en el último
    mes), en algunas instalaciones del orden de varios cientos de €.

  Ambos eran la misma causa raíz: el sensor `Coste acumulado` está
  marcado `state_class = total_increasing`, así que el *recorder* de HA
  auto-generaba estadísticas long-term para él **a partir del momento
  en que se activó la feature**, usando el `state` de cada hora. Cuando
  v0.5.2 añadió el push explícito vía `async_import_statistics` al
  mismo `statistic_id`, ambos caminos competían: el push solo
  sobrescribía las horas que cubría su stream, y las que no, conservaban
  el `sum` anterior — produciendo series **no monótonas** que el panel
  renderiza como `bar = sum[n] - sum[n-1] = NEGATIVO`.

  La v0.5.4 ataca el problema por dos lados:

  - **Migración one-shot al primer arranque**: limpia las
    estadísticas existentes (entidad + externa) para cada contrato del
    entry. Idempotente vía un flag persistente `cost_stats_v054_migrated`
    en `entry.data` — corre exactamente una vez por instalación. Al
    siguiente tick del coordinator, el push reconstruye desde cero.
  - **Push de coste con réplica completa "spike-immune"**: igual que
    el push de consumo desde la incidencia de octubre 2025, ahora el
    push de coste convierte la serie acumulativa a *deltas*, lee la
    serie existente, las funde (gana lo nuevo en colisión) y reescribe
    la serie completa replayed-from-zero. El panel solo lee
    `sum[n] - sum[n-1]`, así que el offset absoluto es invisible y
    cada barra queda correcta. Cualquier divergencia futura entre
    fuentes auto-rellena en lugar de propagarse.

### Añadido

- `cumulative_to_deltas()` en `statistics_helpers.py` — invierte una
  serie monótona acumulada en *deltas* por hora, defensivo contra
  regresiones del input (clampa negativos a 0). Cubierto por
  `tests/test_continuation_stats.py::TestCumulativeToDeltas`.

### Notas

- **No hay que tocar nada manualmente.** La migración corre sola al
  primer arranque tras actualizar; las stats limpias se rellenan al
  siguiente tick del coordinator (en cuanto pulses el bookmarklet o
  pase la próxima hora). El panel de Energía debería mostrar el
  histórico completo y monótono inmediatamente.

## [0.5.3] — 2026-04-25

### Arreglado

- **Wizard de "Añadir integración" mostraba "Unknown error occurred"**
  en cuanto se marcaba la casilla *Calcular precio (€)*, dejando
  imposible crear nuevas instalaciones con coste habilitado (regresión
  introducida en v0.5.1 al cambiar los campos de tarifa de slider a
  caja tecleable). El campo *Cuota suplementaria de alcantarillado*
  llevaba `step=0.0001`, pero el `NumberSelector` de HA exige
  `step >= 1e-3` (o el literal `"any"`), así que la construcción del
  schema lanzaba `MultipleInvalid` y el wizard caía con error genérico.
  Ahora se usa `step="any"`, que deja al usuario teclear los 4
  decimales típicos de la cuota suplementaria (`0,1234 €/m³`) sin
  forzar ninguna rejilla.

### Añadido

- Test de regresión `tests/test_config_flow_schema.py` que comprueba
  por AST que **todos** los `NumberSelectorConfig(step=...)` del config
  flow respetan la restricción de HA (`"any"` o `>= 1e-3`). Sin
  necesidad de instalar `homeassistant` en CI — pesa ~50 MB y para
  cazar este bug basta con leer el AST.

## [0.5.2] — 2026-04-25

### Arreglado

- **Panel de Energía → Costes mostraba 0 €** cuando se seleccionaba
  el sensor `Coste acumulado` como entidad de coste, porque las
  estadísticas auto-generadas del sensor solo arrancaban en el
  momento en que se activó la casilla *Calcular precio (€)* — para
  cualquier periodo anterior, el panel calculaba un *delta* nulo. Ahora
  la integración **siembra el statistic_id del propio sensor** con el
  histórico horario completo (vía `async_import_statistics`),
  paralelo al push de la estadística externa `canal_isabel_ii:cost_<contract>`
  que ya existía. Cualquiera de las dos opciones que escoja el user
  en el asistente del panel de Energía funciona ya con histórico real.

### Notas

- Si vienes de v0.5.0/v0.5.1 con el panel ya configurado y mostrando
  0 €, **bórralo y vuelve a añadirlo** desde *Ajustes → Paneles →
  Energía → editar* (el panel cachea las cost-stats y solo recalcula
  al cambiar la fuente). Tras esa re-creación, los nuevos seteos
  recuperan el histórico completo desde el primer ingest.
- README ampliado con sección **"Conectarlo al panel de Energía"**:
  pasos numerados, qué seleccionar en cada dropdown, y por qué el
  warning *"Estadísticas no definidas"* es benigno.

## [0.5.1] — 2026-04-25

### Cambiado

- Los cuatro campos del paso *"Parámetros de tarifa"* (asistente y
  *Configurar*) ahora se renderizan como **inputs numéricos
  tecleables** con flechas arriba/abajo, no como sliders. Antes, el
  *Calibre del contador* salía como slider porque Home Assistant
  auto-elegía el widget en función del rango entero — ahora es
  consistente con los otros tres campos.
- Rangos válidos ajustados a uso doméstico realista: calibre 10-50 mm
  (era 10-200), número de viviendas 1-200 (era 1-999), cuota
  suplementaria 0-1,0 €/m³ (era 0-10), IVA 0-25 % (era 0-100). Los
  límites superiores anteriores eran absurdos para una factura
  doméstica.

### Notas

- Cambio puramente de UX en el formulario — los valores ya guardados
  no se ven afectados, no hay migración. Si abres *Configurar* después
  de actualizar verás los mismos cuatro campos pero en cajas
  numéricas.

## [0.5.0] — 2026-04-25

### Añadido

- **Entidades de coste opt-in (€)**, alimentadas por un modelo de
  tarifa de Canal de Isabel II que reproduce facturas reales con un
  desvío inferior al 1 % en los dos casos de prueba (alta consumición
  cruzando los cuatro bloques, y consumo bajo cruzando la frontera de
  vigencia 01-01-2026). Se activan marcando *"Calcular precio (€)
  además del consumo (m³)"* en el asistente de configuración o, post
  install, en *Configurar* de la integración (Options Flow). Si no
  marcas el check, la integración se comporta exactamente como la
  v0.4.x — solo entidades de m³, coste cero en runtime.
  - **`sensor.<install>_coste_acumulado`** (€, `device_class:
    monetary`, `state_class: total_increasing`). Coste total
    acumulado desde el inicio de la serie. Se publica también como
    estadística externa horaria `canal_isabel_ii:cost_<contract>`,
    para enchufarlo al **Panel de Energía → Costes** sin templates.
  - **`sensor.<install>_precio_actual`** (€/m³). Precio que pagaría
    el siguiente m³ a fecha de hoy: bloque tarifario actual sumando
    los cuatro servicios (aducción + distribución + alcantarillado +
    depuración) + cuota suplementaria de alcantarillado + IVA.
  - **`sensor.<install>_bloque_tarifario_actual`** (entero 1-4). El
    bloque del próximo m³, prorrateado al periodo bimestral en curso
    (B1 ≤ 20 m³, B2 20-40, B3 40-60, B4 > 60). Atributos con los
    umbrales y los m³ ya consumidos por bloque.
- **Cuatro parámetros editables** en el flujo de configuración cuando
  marcas la casilla, también editables después vía *Configurar*:
  - *Calibre del contador* (mm) — campo `Calibre` de la factura.
    Doméstico unifamiliar es típicamente 13 o 15 mm.
  - *Número de viviendas* — vivienda unifamiliar = 1; comunidad con
    contador único = nº de pisos.
  - *Cuota suplementaria de alcantarillado* (€/m³) — varía por
    municipio. Está en la factura como *"Cuota suplementaria de
    alcantarillado"*.
  - *IVA* (%) — por defecto 10 % (régimen general del agua en
    España). Expuesto solo por si cambia el régimen fiscal en el
    futuro.
- **Tablas de tarifa hardcoded** para las dos vigencias actuales:
  2025 (BOCM 129 de 31-05-2025) y 2026 (vigente desde 01-01-2026,
  observada en facturas reales que cruzan la frontera). Cuando BOCM
  publique la siguiente actualización, basta con añadir un
  `TariffSet` nuevo a `tariff.VIGENCIAS` y la lógica de pro-rateo
  por vigencia se aplica automáticamente a los periodos que crucen.
- **Algoritmo de coste por hora**
  (`tariff.compute_hourly_cost_stream`): agrupa lecturas por
  bimestre natural, calcula el total exacto del periodo (variable
  por bloques + cuota fija + suplementaria + IVA, partido por
  vigencia si toca), y reparte ese total por hora del bimestre
  proporcional al m³ horario (variable + suplementaria) más una
  fracción uniforme de la cuota fija. La suma exacta del periodo
  coincide con la factura; la distribución intra-día es una
  aproximación razonable que el panel de Energía no nota.
- **24 tests unitarios nuevos** en `tests/test_tariff.py`,
  incluyendo dos tests de validación contra factura real
  (anonimizadas — solo coinciden los números agregados, ningún dato
  identificativo). Stack total: 143 tests, sigue verde.

### Cambiado

- **`config_flow.py` ahora tiene un segundo paso opcional** (sólo si
  marcas *"Calcular precio (€)"* en el primer paso). El flujo
  existente sin ese check sigue siendo idéntico — un único paso con
  los dos campos de siempre.
- **Nuevo `OptionsFlow`** para editar parámetros de coste y
  desactivar / reactivar la entidad de coste sin eliminar y volver a
  añadir la integración. Cambios disparan un reload completo del
  entry para que las entidades aparezcan / desaparezcan al instante.

### Notas

- **Migración desde v0.4.x sin sorpresas**: el campo `enable_cost`
  por defecto es `False`, así que los entries existentes se
  comportan exactamente igual que antes. Para activar el coste:
  *Ajustes → Dispositivos y servicios → Canal de Isabel II →
  Configurar*.
- **Sólo soporta uso "Doméstico 1 vivienda" en v0.5.0**. La
  estructura de bloques y precios para industrial / comercial /
  comunidades grandes es distinta y necesitaríamos facturas de
  muestra para modelarlas. Si vas a usar el coste en otro régimen,
  espera a una versión futura o abre un issue con una factura
  anonimizada.
- **B2-B4 de 2026 son extrapolados** (con el delta % observado en
  B1) hasta que llegue una factura real con > 20 m³ post-2026 que
  sirva para fijarlos. Para usuarios doméstico 1-vivienda con
  consumos < 20 m³ bimestrales (la mayoría) la extrapolación es
  irrelevante porque solo ven B1.

## [0.4.11] — 2026-04-25

### Documentación

- **Aviso del límite de 30 días por click para import histórico**, en
  los cuatro sitios donde se explica el flujo *filtrar en pantalla →
  pulsar favorito*: la notificación persistente, la página HTML de
  instalación, el README (§4) y `docs/USE.md` (sección «Histórico el
  día 1» + Limitaciones conocidas). El formulario `consumoForm` del
  portal rechaza rangos de fechas mayores de 30 días — si pones 31, el
  portal devuelve error y el bookmarklet se queda sin CSV. Para meter
  más historia (varios meses) se parte el rango en tramos consecutivos
  de ≤30 días y se pulsa el favorito una vez por tramo. Las
  estadísticas externas son upsert por timestamp horario, así que los
  tramos se acumulan sin duplicar.
- **Corregido detalle previo en USE.md**: la sección «Histórico el día
  1» decía que el primer click descarga «~7 meses» retroactivos. Eso
  no es exacto — sin filtro en pantalla, el portal sirve su rango por
  defecto (~60 días). Para los ~7 meses completos hay que iterar con
  tramos ≤30d como arriba.

### Notas

- **Sin cambios de código** en los sensores, el bookmarklet JS, ni el
  endpoint de ingesta. Solo cambia el texto de la notificación, de la
  página de instalación y de la documentación.
- Tras update no es necesario regenerar el bookmarklet (no cambia
  nada en el favorito). Si quieres ver la notificación con el aviso
  nuevo: *Ajustes → Herramientas para desarrolladores → Acciones →
  `canal_isabel_ii.show_bookmarklet`*.

## [0.4.10] — 2026-04-25

### Arreglado

- **El enlace de la notificación abría el Lovelace en lugar de la página de
  instalación**. v0.4.9 dejaba el link como markdown puro
  `[texto](url)`, que renderiza a `<a href="url">texto</a>` sin
  `target`. El frontend de Home Assistant es una SPA: su router
  intercepta TODO click en `<a>` del mismo origen y lo enruta él mismo.
  Como `/api/canal_isabel_ii/bookmarklet/<id>?t=…` no es una ruta del
  Lovelace, el router caía al dashboard por defecto y el user nunca
  llegaba a la página. Hack manual que funcionaba: click derecho →
  *Abrir en pestaña nueva* (eso esquiva el router porque el navegador
  maneja la nueva pestaña sin pasar por el SPA). Fix definitivo: el
  link ahora se emite como HTML crudo
  `<a href="…" target="_blank" rel="noopener">…</a>`. `target="_blank"`
  hace que el navegador abra la URL nativamente en una pestaña nueva,
  saltándose el router del frontend; `rel="noopener"` es la dureza
  estándar para enlaces externos. `<ha-markdown>` (el renderer markdown
  de HA) preserva ambos atributos a través de DOMPurify.
- **Test de regresión** verifica que `target="_blank"` y
  `rel="noopener"` están presentes en el cuerpo de la notificación,
  pegados al `href` de la página — para que un futuro refactor no
  pueda regresar silenciosamente a un link markdown puro.

### Notas

- **Acordaos de regenerar la notificación tras update**. La
  notificación de v0.4.9 que tengáis aún viva sigue trayendo el link
  markdown roto. *Ajustes → Herramientas para desarrolladores →
  Acciones → `canal_isabel_ii.show_bookmarklet`* para reaparecer la
  notificación con el link nuevo. El bookmarklet en sí (el favorito
  en la barra) **no cambia** — solo cambia el link a la página de
  instalación dentro de la notificación.

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
