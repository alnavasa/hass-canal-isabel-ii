# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[SemVer](https://semver.org/).

## [0.6.0] — 2026-04-26

### Cambiado — rediseño: el coste deja de ser una entidad y pasa a ser SOLO una estadística

Tras tres versiones consecutivas (v0.5.20 → 21 → 22 → 23) parcheando
síntomas distintos del mismo bug — barra negativa en el panel Energía
(coste), congelación de la entidad tras `clear_cost_stats`,
`RuntimeError` en handlers del dispatcher — el patrón quedó claro:
**el problema era estructural**, no implementacional. El coste vivía
como entidad (`CanalCumulativeCostSensor`) que mantenía estado mutable
en memoria (`_restored_value`) Y al mismo tiempo publicaba la
estadística externa de largo plazo. Cualquier desfase entre el estado
in-memory y la serie del recorder (cache trim, vigencia editada,
backfill manual, reinicio de HA) producía una "costura" que el panel
Energía renderizaba como barra negativa.

La observación que rompe el nudo: el portal de Canal de Isabel II
publica las lecturas horarias **con un mínimo de 12 h de retraso**.
"Coste vivo en este segundo" nunca fue real. Recalcular el stream
completo en cada tick del coordinator era trabajo desperdiciado Y la
fuente de cada bug de divergencia.

#### El rediseño v0.6.0

- **El coste se publica una vez por POST**, desde un módulo nuevo
  `cost_publisher.py`. La estadística externa
  `canal_isabel_ii:cost_<contract>` sigue siendo la misma (los
  paneles Energía existentes que la apuntaban siguen funcionando).
- **No hay entidad espejo**. La entidad acumulativa
  (`CanalCumulativeCostSensor`), la entidad de precio actual
  (`CanalCurrentPriceSensor`) y la de bloque tarifario actual
  (`CanalCurrentBlockSensor`) **se eliminan** junto al mixin
  compartido (`_CostSensorMixin`).
- **Sin estado in-memory** entre llamadas. El publisher lee el
  recorder, mergea con el stream nuevo y publica. El recorder es la
  única fuente de verdad. El bug de divergencia no puede volver
  porque no hay segundo escritor.
- **Algoritmo monotónico por construcción**: `merge_forward_and_backfill`
  reconstruye la suma desde cero sobre la serie mergeada, así que
  cualquier serie publicada es no-decreciente. El panel Energía no
  puede renderizar barras negativas porque las diferencias `sum[n] -
  sum[n-1]` son ≥ 0 siempre.

#### Migración

Al primer arranque tras actualizar:

- El módulo `__init__._purge_obsolete_cost_entities_v060` borra
  automáticamente del registro de entidades los tres sensores de
  coste por su `unique_id`. Idempotente — sobrevive a reinicios.
- La estadística externa `canal_isabel_ii:cost_<contract>` se
  **conserva intacta** en el recorder. La historia de coste anterior
  no se pierde.
- Si el panel Energía tenía elegida la entidad
  `sensor.<install>_coste_acumulado` como fuente de coste de agua,
  esa elección desaparece automáticamente (la entidad ya no existe).
  El usuario debe abrir Configuración → Paneles → Energía → Agua y
  re-seleccionar la **estadística externa** "&lt;install&gt; - Canal de
  Isabel II coste". Es un paso manual de un solo clic; no se puede
  evitar porque HA enlaza el panel a un id de entidad o a un id de
  estadística (no hay redirección automática entre ambos).
- En el primer arranque tras actualizar, el publisher se ejecuta
  inmediatamente con las lecturas en cache (sin esperar al primer
  POST), así que la estadística refleja el estado más reciente desde
  el primer minuto.

#### Eliminado

- `sensor.py`: 3 clases de coste + `_CostSensorMixin` +
  `_tariff_params_from_settings` + helpers de push de coste (~600
  líneas, ~40 % del archivo).
- `const.py`: `SIGNAL_CLEAR_COST_STATS`.
- `statistics_helpers.py`: `is_cost_stream_regression` (la
  asimetría que pretendía proteger ya no existe — sin entidad no
  hay segundo escritor del que divergir).
- `__init__.py`: `_migrate_cost_stats_v054` (la migración v0.5.4 ya
  no aplica; el flag `cost_stats_v054_migrated` se preserva en
  `entry.data` por compatibilidad de almacenamiento pero no se lee).
- `tests/test_clear_cost_stats_dispatch.py` (la señal del dispatcher
  desaparece junto a la entidad).

#### Añadido

- `custom_components/canal_isabel_ii/cost_publisher.py` (~370
  líneas, ~285 sin docstring) — publisher puro, sin estado mutable.
- `tests/test_cost_publisher.py` — 10 tests end-to-end con un fake
  HomeAssistant + recorder mockeado: deshabilitado, lecturas vacías,
  sin contrato coincidente, settings malformados, vigencia ausente
  (caso "backfill enero 2024 con sólo 2025+ modelado"), cold start, y
  replay con stats previos.
- `tests/test_no_cost_entities_v060.py` — guard AST que falla si
  alguien re-introduce alguna de las 3 clases de coste o el mixin.
  Pin defensivo: el rediseño es la solución estructural; revivir
  cualquiera de esas clases reabre el bug.
- `docs/v0.6.0-redesign.md` — documento de diseño con la motivación,
  invariantes, plan de migración y casos de borde considerados.

#### Tests

**240 tests** (eran 234 en v0.5.23, neto +6 después de eliminar 7
tests obsoletos de `is_cost_stream_regression`, eliminar 7 tests de
`test_clear_cost_stats_dispatch.py`, actualizar 2 tests de handlers
del dispatcher, y añadir 11 tests nuevos: 10 de cost_publisher + 1
guard de no-revival).

`tests/test_continuation_stats.py::TestCostPushPipelineMonotonicity`
queda intacto — ese pipeline lo sigue usando el publisher
(cumulative_to_deltas + merge_forward_and_backfill).

#### Por qué este es un major-bump (0.5.x → 0.6.0)

- **Eliminación de entidades públicas** (sensor.<install>_coste_acumulado,
  precio_actual, bloque_tarifario_actual). Cualquier automatización,
  template o card que las referenciase deja de funcionar.
- **Acción requerida del usuario** en el panel Energía (re-seleccionar
  la estadística como fuente de coste de agua).
- **Eliminación de un servicio**: `clear_cost_stats` se mantiene
  documentado pero ya no envía la señal del dispatcher (no hay
  entidad que la recoja); sólo limpia la estadística del recorder.

Quien sólo use el panel Energía con el sensor de **consumo** (m³ /
litros) — la mayoría de instalaciones — no nota nada salvo que el
log queda más limpio (sin tracebacks de `RuntimeError`, sin reintentos
de push divergentes).

## [0.5.23] — 2026-04-26

### Arreglado — `RuntimeError` en handlers del dispatcher (HA 2026.x)

Tras desplegar v0.5.22 en MF1 la recuperación funcionó end-to-end
(panel Energía → Agua → marzo recuperó 15,13 €, ya no 0 €), pero
el log de HA se llenó de tracebacks por cada invocación de
`clear_cost_stats`:

```
ERROR (SyncWorker_2) Exception in _on_clear_cost_stats:
RuntimeError: Detected that custom integration 'canal_isabel_ii'
  calls async_write_ha_state from a thread other than the event
  loop ... at custom_components/canal_isabel_ii/sensor.py, line 917
```

#### Causa raíz

Los cuatro handlers del dispatcher (`_on_meter_reset` × 3 sensores
+ `_on_clear_cost_stats` en el sensor de coste) son métodos
síncronos. Sin el decorador `@callback`, HA programa su ejecución
en el thread pool del executor (`SyncWorker_*`), y la llamada
posterior a `async_write_ha_state()` dispara el guard de
thread-safety (`report_non_thread_safe_operation`) que en HA 2026.x
**lanza `RuntimeError`** en lugar de loggear un warning.

Dos cosas evitaron que esto rompiera la recuperación en producción:

1. La mutación de estado (`self._restored_value = None`) ocurre
   *antes* de la llamada a `async_write_ha_state()`, así que el
   próximo tick del coordinator vio el guard limpio y empujó.
2. `recorder.async_clear_statistics` ya había corrido en el
   handler del servicio principal, así que la recuperación venía
   conducida por *esa* ruta aunque el handler crashease.

Pero los tracebacks son reales: una versión futura de HA puede
elevar el warning a abort duro que corra antes de la asignación, o
bloquear el dispatcher entero. Y en los tres `_on_meter_reset`
(ninguno se había disparado en el campo porque nadie había
invocado `reset_meter` en MF1) el mismo bug latente saltaría en
cuanto un usuario reemplazase un contador físico.

#### Cambiado

- **`sensor.py`**: importa `callback` desde `homeassistant.core`.
- **`sensor.py`**: añade `@callback` a los 4 handlers del
  dispatcher:
  - `CanalCumulativeConsumptionSensor._on_meter_reset`
  - `CanalMeterReadingSensor._on_meter_reset`
  - `CanalCumulativeCostSensor._on_meter_reset`
  - `CanalCumulativeCostSensor._on_clear_cost_stats`

  El primero lleva el docstring extendido que explica la razón
  (thread-safety en HA 2026.x); los otros tres apuntan a él.

#### Tests

2 tests nuevos en `tests/test_dispatcher_handler_decorators.py`
(introspección AST pura, sin runtime de HA, mismo patrón que
`test_clear_cost_stats_dispatch.py` y `test_services_yaml.py`):

- `test_callback_imported_from_homeassistant_core`
- `test_every_dispatcher_handler_is_marked_callback` — recorre el
  AST de `sensor.py`, encuentra los métodos cuyo nombre coincide
  con `_on_meter_reset` o `_on_clear_cost_stats` dentro de
  cualquier `class`, y exige que cada uno carry `@callback`. Si
  un futuro contributor añade un nuevo handler siguiendo la
  convención `_on_*` y olvida el decorador, este test falla
  *antes* de que el usuario vea tracebacks.

Total: **243 tests** (era 241 en v0.5.22). Verificado: con
`git stash` de los cambios y reejecutando, los 2 tests nuevos
fallan — el guard captura el bug exacto que v0.5.23 arregla.

#### Recuperación tras actualizar

Sin pasos manuales. Tras update a v0.5.23, la próxima invocación
de `clear_cost_stats` (o `reset_meter`) corre el handler en el
event loop limpiamente — sin tracebacks en el log.

## [0.5.22] — 2026-04-26

### Arreglado — `clear_cost_stats` dejaba la entidad de coste congelada

Tras instalar v0.5.21, ejecutar `canal_isabel_ii.clear_cost_stats`
borraba las stats del recorder (correcto) pero **no reseteaba el
guard antirregresión en memoria** del sensor de coste. Resultado
visible: la columna *Coste* del panel Energía se quedaba en
**0,00 € para todos los meses indefinidamente**, con el sensor
congelado en el último valor alto pre-clear y el push siempre
saltando por regresión detectada contra ese ancla obsoleta.

Diagnóstico observado en MF1: tras update v0.5.20 → v0.5.21 +
`clear_cost_stats`, la entidad seguía retornando 150,54 €
mientras el stream calculaba 111,84 € desde la cache reducida.
Cada tick: `Cost stream regressed (111.84 < 150.54); skipping
recorder push` (correcto, el guard hace su trabajo). Pero como
el recorder estaba vacío y el push no entraba, el panel mostraba
0 € permanente. La cache solo crecería de vuelta a 150 € tras
semanas de consumo real.

#### Causa raíz

v0.5.21 introdujo el guard simétrico `is_cost_stream_regression`
en `native_value` y en `_push_cost_statistics_locked`. Diseñado
para preservar monotonicidad cuando el stream calculado cae por
debajo del valor restaurado de disco — y eso funciona. El gap:
el servicio de recovery `clear_cost_stats` solo tocaba el
recorder vía `async_clear_statistics`, sin avisar al sensor vivo
para que dropease su `_restored_value` en memoria. Sin ese
reseteo, el predicado del guard sigue evaluando contra el ancla
vieja y nunca permite republicar.

#### Cambiado

- **`const.py`**: nueva señal de dispatcher
  `SIGNAL_CLEAR_COST_STATS` con formato
  `{entry_id}_{contract_id}` (mismo patrón que
  `SIGNAL_METER_RESET`).
- **`sensor.py` — `CanalCumulativeCostSensor`**: subscribe a la
  señal en `async_added_to_hass`. Nuevo handler
  `_on_clear_cost_stats` que resetea `_restored_value = None` y
  llama a `async_write_ha_state()`. Mismo shape exacto que
  `_on_meter_reset` — la diferencia es semántica (reset por wipe
  de stats vs. reset por cambio de contador físico) y queda
  reflejada en el log para diagnóstico.
- **`__init__.py` — `_clear_cost_stats_for_entry`**: tras
  `recorder.async_clear_statistics(...)`, dispatcha la nueva señal
  para cada contract de la entry. El próximo tick del coordinator
  recompone `native_value` contra `_restored_value=None` (sin
  guard que aplicar) y `_push_cost_statistics_locked` empuja
  desde cero limpio.

Con este wiring, el flujo de recovery documentado en la FAQ
(*FAQ → "El panel Energía → Agua muestra una barra negativa"*)
funciona end-to-end: una sola llamada a `clear_cost_stats` y la
serie se rehidrata en el siguiente tick.

#### Recuperación tras actualizar (MF1 y similares atascados en 0 €)

1. Actualiza a v0.5.22 vía HACS.
2. *Dev Tools → Acciones* → `canal_isabel_ii.clear_cost_stats` →
   *Llamar al servicio*.
3. Espera un tick del coordinator (≤ 1 min). El log mostrará:
   ```
   [<contract>] Cumulative cost: clear_cost_stats signal received
     (was 150.54); clearing monotonic guard so next push rebuilds
     the recorder series from cold-start
   ```
4. Refresca el panel Energía → Agua. La columna *Coste* vuelve a
   poblar bimestre a bimestre conforme el push avanza.

#### Tests

7 tests nuevos en `tests/test_clear_cost_stats_dispatch.py`
(introspección AST + grep, sin runtime de HA — mismo patrón que
`test_services_yaml.py`):

- Constante de señal existe en `const.py` con los placeholders
  correctos (`{entry_id}` + `{contract_id}`).
- `__init__.py` importa la señal y la dispatcha vía
  `async_dispatcher_send`.
- `sensor.py` importa la señal y la conecta vía
  `async_dispatcher_connect`.
- Sender y receiver usan el mismo identificador (anti-typo).
- El dispatch ocurre dentro de `_clear_cost_stats_for_entry`
  (no en `async_setup_entry`, donde dispararía en cada boot).
- El connect ocurre dentro de
  `CanalCumulativeCostSensor.async_added_to_hass` (no en otro
  método donde la entidad no esté lista).
- El handler `_on_clear_cost_stats` realmente asigna
  `self._restored_value = None` (no es no-op).

Si alguno de estos invariantes se rompe, el flujo de recovery
deja de funcionar y los usuarios vuelven al freeze de v0.5.21.

#### FAQ actualizada

`docs/USE.md` señala explícitamente que en v0.5.21
`clear_cost_stats` no era suficiente y dirige a actualizar a
v0.5.22+ antes de invocar el servicio.

## [0.5.21] — 2026-04-26

### Arreglado — barras negativas recurrentes en el panel Energía

El bug de las barras negativas (~38 € de drop en MF1) que persistía
incluso después de ejecutar `clear_cost_stats` queda cerrado por
construcción. Si lo estabas viendo: actualiza, ejecuta una vez
`canal_isabel_ii.clear_cost_stats` para limpiar las barras existentes,
y no debería volver.

#### Causa raíz

`CanalCumulativeCostSensor.native_value` tenía un guard de monotonicidad
desde v0.5.x: si el stream calculado por `compute_hourly_cost_stream`
producía un cum_eur menor que el último valor guardado en disco
(p.ej. cache perdió una bimensualidad antigua, recompute con params
distintos, etc.), el entity state se quedaba en el valor anterior
(correcto). Pero `_push_cost_statistics_locked` **no tenía el mismo
guard**: empujaba la serie nueva (más baja) al recorder sin condición.

Resultado: el entity state mostraba 150,54 € (guardado), pero el
recorder recibía una serie terminando en 111,84 €. La Energy panel
calcula cada barra como `sum[n] - sum[n-1]`, así que el seam entre el
valor antiguo (150,54) y el nuevo (111,84) renderizaba como una
barra de **−38,70 €**. La asimetría entre los dos paths era el
mecanismo: el guard estaba a medias.

Disparador típico: arranque de HA. `RestoreSensor` recupera el último
valor persistido (el alto). El primer tick del coordinator recomputa
el stream y el push escribe el valor menor. La barra negativa aparece
"al cabo del rato", no al boot — exactamente el patrón que tú
describiste.

#### Cambiado

- **`statistics_helpers.py`**: nuevo helper puro `is_cost_stream_regression(latest, restored, threshold=0.01)`
  que centraliza la detección de regresión. Devuelve `True` si
  `restored is not None and latest < restored - threshold`.
- **`sensor.py` — `CanalCumulativeCostSensor.native_value`**: refactor
  para usar el helper en vez de la condición inline (DRY, mismo
  comportamiento exacto).
- **`sensor.py` — `_push_cost_statistics_locked`**: nueva guard al
  inicio que llama al mismo helper. Si el stream regresionó, **el
  push se aborta** y el recorder retiene los valores antiguos
  (correctos). El warning del log identifica el contract afectado y
  los dos valores en juego para diagnóstico operativo.

Ambos paths ahora ejecutan el mismo predicado → no pueden divergir →
la barra negativa no se puede crear de nuevo.

#### Recuperación tras actualizar

Las barras negativas que estén ya en el recorder se quedan ahí hasta
que las limpies manualmente:

1. Actualiza a v0.5.21 vía HACS.
2. *Dev Tools → Acciones* → `canal_isabel_ii.clear_cost_stats` →
   *Llamar al servicio*.
3. La siguiente actualización del coordinator empuja una serie limpia
   (vía cold-start). El panel Energía se rehidrata correctamente en
   minutos.

A partir de aquí el guard impide nuevas barras negativas — el push
queda alineado con el entity state en todo momento.

#### Tests

7 tests nuevos en `tests/test_continuation_stats.py` que pinan el
contrato del helper:

- `restored=None` → False (cold-start, push procede).
- `latest == restored` → False (idempotente).
- `latest` ligeramente por debajo (dentro de threshold) → False
  (absorbe ruido de coma flotante).
- `latest` significativamente por debajo (caso MF1: 111,84 vs 150,54) → True.
- `threshold` configurable (más estricto / más laxo).
- `latest > restored` (caso normal) → False.
- `restored=0.0` (primer bimestre fresh) → False salvo `latest < 0`
  (que `compute_hourly_cost_stream` nunca produce, pero pinamos el
  contrato del helper).

#### Por qué no quito también el push entity-side

Lo había propuesto inicialmente para eliminar la carrera entre nuestro
push y la auto-generación de stats por HA (`state_class=TOTAL`). Tras
re-pensarlo: con el guard del push, **ambos escritores son
deterministas y convergentes** — escribimos `latest_cum_eur` solo
cuando no hay regresión, y HA escribe el entity state que también está
guardado por el mismo predicado. Mismo valor por construcción, sin
divergencia. Quitar el entity-side push hubiera roto compatibilidad
con setups que apuntaban Energy panel a la entidad. No vale la pena
para v0.5.21.

## [0.5.20] — 2026-04-26

### Documentación — FAQ + ampliación de limitaciones conocidas

Solo cambios de docs en `docs/USE.md`. Cero código tocado, cero
migración, cero impacto en runtime.

#### Añadido

- **Sección `## 10. FAQ`** con 12 preguntas/respuestas agrupadas en
  cuatro bloques (cálculo de coste, sensores, troubleshooting,
  operaciones / migración). Cubre dudas que han ido apareciendo en uso
  real:

  - **Cálculo de coste**: precisión vs factura real (<1 % en condiciones
    normales), si el coste lleva IVA, por qué el primer bimestre tras
    instalar suele subestimar, cómo se comporta cuando el ciclo de
    facturación está desfasado del bimestre natural, qué pasa cuando
    Canal sube tarifa a mitad de periodo.
  - **Sensores**: por qué `Bloque tarifario actual` puede no coincidir
    con la factura (usa bimestre natural), qué hacer si te cambian el
    contador físico (servicio `reset_meter`).
  - **Troubleshooting**: barras negativas en el panel Energía → Agua
    (servicio `clear_cost_stats`), bookmarklet devuelve 401 (token
    rotado / entry recreada / favorito corrupto), invalidar el
    bookmarklet sin borrar la integración (rotación de token v0.5.18+).
  - **Operaciones**: qué se preserva al actualizar via HACS, qué se
    pierde al eliminar y reinstalar la entry y cómo evitar dejar
    estadísticas huérfanas en el recorder.

  Anchors clicables (`#faq-bloque`, `#faq-primer-bimestre`,
  `#faq-negativo`) para enlazar desde otras partes de la doc.

#### Cambiado

- **`## 9. Limitaciones conocidas` ampliada** con dos puntos nuevos
  (con link al FAQ para detalle completo):

  - El sensor `Bloque tarifario actual` agrupa por **bimestre natural
    calendario**, no por el ciclo real de facturación. Si el ciclo del
    contrato está desfasado, el bloque mostrado puede no coincidir con
    el de la factura — el total anual sí cuadra.
  - El `Coste acumulado` puede subestimar el primer bimestre tras
    instalar la integración por falta de cobertura de las lecturas
    previas a la primera pulsación del bookmarklet. Cuotas fijas se
    contabilizan vía catch-up automático; la parte variable solo cubre
    lo que el cache local tenga. A partir del segundo bimestre completo
    desaparece el desvío.

#### Por qué

Hasta ahora la única documentación visible al usuario sobre estos
casos vivía en sitios dispersos: la descripción del servicio
`clear_cost_stats` en `services.yaml` (visible solo al expandir el
servicio en *Dev Tools → Acciones*), comentarios en `tariff.py` y
`__init__.py` (no visibles desde HA), y entradas puntuales en el
CHANGELOG (que pocos usuarios leen). Centralizar en `USE.md` reduce
fricción para usuarios que se topan con el comportamiento esperado
pero no documentado y abren issues que ya tienen respuesta.

## [0.5.19] — 2026-04-26

### Añadido — Cierre de gaps en cobertura de tests

Tres ficheros de tests nuevos (33 tests) que cubren código que hasta
ahora solo se ejercitaba indirectamente vía los tests de continuación
o no se cubría en absoluto. La integración no cambia su comportamiento
en runtime — esto es puramente endurecimiento de la red de regresión
para que futuras refactorizaciones no rompan invariantes silenciosos.

#### `tests/test_store_extras.py` (16 tests)

`ReadingStore` solo tenía tests para la baseline-carry-over de
v0.5.12. El resto de su API (dedup, meter summary, reset, clear,
serialización, tolerancia a corrupción) iba sin guard. Ahora cubierto:

- **Dedup semántico**: re-ingerir el mismo `(contract, timestamp)`
  hace upsert in-place, no añade fila duplicada. El bookmarklet siempre
  re-baja la ventana visible completa, así que cada POST contiene
  solapamiento — sin dedup, el cache se duplicaría a cada click.
- **Conteo de filas NEW**: `async_replace` devuelve sólo las filas que
  no existían antes (no in-place updates) — el log de operación se
  apoya en este número para distinguir "POST trajo nueva data" de
  "POST repitió lo que ya tenía".
- **Preservación de meter_summary**: un POST sin `meter_summary` NO
  borra el anterior. La fast-path del bookmarklet (cuando solo se
  re-postea el CSV cacheado del navegador) podría perder el contador
  absoluto sin esta garantía.
- **Reemplazo de meter_summary**: cuando viene uno nuevo, sustituye
  al anterior wholesale.
- **`last_ingest_at` siempre avanza**: dos POSTs consecutivos
  reflejan el segundo timestamp. El sensor `data_age_minutes` se
  apoya en esto para mostrar frescura.
- **Propiedad `contracts`**: deduplica IDs y descarta el contract
  vacío. Usado por `clear_cost_stats` y `reset_meter` para iterar
  los contratos a operar.
- **`async_clear` lo borra todo**: readings, meter, baseline,
  last_ingest_at, y el fichero de disco. Usado por
  `async_remove_entry`. El `_StubStore` registra `removed=True`
  para verificar que tocamos disco.
- **`async_reset_baseline` (v0.5.16) en aislamiento**: borra el
  baseline DE UN solo contrato, sin tocar readings, meter_summary,
  ni los baselines de OTROS contratos. Crítico: si rompemos esto,
  resetear el contador de un contrato corrompe el otro.
- **`async_reset_baseline` con contrato desconocido**: no-op
  silencioso, no escribe a disco. Sin esto, un servicio mal
  invocado generaría writes innecesarios y podría enmascarar
  bugs reales.
- **Round-trip de meter_summary**: serialise + reload reconstruye
  todos los campos, incluyendo `reading_at` con su parsing de ISO.
- **Tolerancia de `_meter_summary_from_dict` a basura**: `None`,
  string, dict vacío, `reading_liters` no numérico → todos
  devuelven `None` sin crash.
- **`_meter_summary_from_dict` con `reading_at` corrupto**:
  fallback a `None` en vez de propagar el ValueError.
- **`_reading_from_dict` levanta en input malformado**: pin la
  superficie de excepción que el loader captura para skipear filas
  corruptas. Si silenciamos esa raise, el loader pasaría la basura
  al cache.
- **`async_load` skipea filas corruptas**: 4 filas (2 buenas, 2
  malas) → cache con 2 filas, sin crash. Modela un fichero de
  store medio-flusheado tras un reboot abrupto.
- **`async_load` tolera baseline malformado**: `baseline_liters`
  con valores no numéricos mezclados con buenos → solo los buenos
  sobreviven, sin crash.

#### `tests/test_ingest_helpers.py` (10 tests)

`ingest.py` es mayormente HA-bound (aiohttp Request, HomeAssistantView,
hass.config_entries) pero sus helpers puros se pueden ejercitar con
stubs minimales. Cubierto:

- **`_extract_bearer` con header estándar**: parse correcto del
  formato `Bearer <token>`.
- **`_extract_bearer` strippea whitespace**: bookmarklets pegados
  con newlines colaterales no rompen la auth.
- **`_extract_bearer` case-insensitive en el scheme** (RFC 6750):
  `Bearer`, `bearer`, `BEARER`, `BeArEr` → todos funcionan.
- **`_extract_bearer` rechaza otros schemes**: `Basic`, `Token`,
  raw token sin prefijo → empty string. El caller responde 401.
- **`_extract_bearer` sin header**: empty string sin crash.
- **`_extract_bearer` con `Bearer ` sin token**: empty string. Sin
  esto, el caller validaría `compare_digest("", expected)` y
  retornaría 401, lo cual es lo correcto pero menos eficiente.
- **`_json` con status custom propaga**: 200, 418, 4xx, 5xx — no
  hay coerción accidental de status code.
- **`_error` produce shape canónico**: `{ok: false, code, detail}`.
  El bookmarklet lee estas claves literales en el navegador del
  usuario; un typo aquí rompe el mensaje de error visible.
- **`_error` propaga status**: 400, 401, 404, 409, 413, 500 → el
  status del response coincide.

#### `tests/test_coordinator.py` (7 tests)

`CanalCoordinator` es deliberadamente delgado — delega todo a
`ReadingStore`. Esa delegación es la API que los sensores usan, y
si una refactor accidentalmente inlinea estado en el coordinador,
los sensores leen datos stale tras un POST de ingest (invisible
hasta que el usuario nota que la tarjeta no avanza). Cubierto:

- **`meter_summary` delega al store**: identidad por `is`, no copia.
- **`meter_summary` devuelve `None` si el store está vacío**.
- **`baseline_liters` delega al store**: contenido idéntico.
- **`baseline_liters` devuelve copia**: mutar el resultado no
  afecta lecturas posteriores (hereda del contrato del store).
- **`_async_update_data` devuelve `store.readings`**: sin
  transformación, sin I/O. Es la fuente que `DataUpdateCoordinator`
  pasa a los sensores.
- **`_async_update_data` con store fresco**: devuelve `[]`, no
  `None` ni excepción.
- **El coordinator preserva `entry` y `store`**: los handlers de
  servicios (`clear_cost_stats`, `reset_meter`) los necesitan
  accesibles vía `coord.entry` / `coord.store`.

### Verificación

- 227 tests pasan (194 → 227, **+33 tests nuevos**).
- `ruff check` y `ruff format --check`: limpios.
- Cero cambios en código de producción — solo nuevos ficheros bajo
  `tests/`. La integración funciona idéntica a v0.5.18.

### Notas técnicas

Los tres ficheros nuevos comparten el patrón de **stub de HA antes
de importar el módulo bajo test** (`sys.modules['homeassistant.*']
= ModuleType(...)` antes del `importlib.spec_from_file_location`).
El stub `_StubStore` se mantiene compatible entre los tres ficheros
(expone `saved` y `removed`) porque `sys.modules` es process-global
y el primer test que importa "gana" — un stub más pobre rompería
los tests del otro fichero según el orden de pytest.

## [0.5.18] — 2026-04-26

### Añadido

- **Rotación de token desde *Configurar*** sin tener que borrar y
  recrear la integración. Hasta ahora el único camino para invalidar
  el token de una instalación era eliminarla del listado de
  integraciones y volver a añadirla — destruyendo de paso el cache
  local de lecturas, el `baseline_liters` (litros que se cayeron en
  *trims* del cache, ver v0.5.12), las estadísticas de coste de largo
  plazo y obligando al usuario a redescargar todo desde el portal.

  El nuevo flujo:

  1. *Ajustes → Dispositivos y servicios → Canal de Isabel II →
     Configurar*.
  2. Se muestra un menú con dos opciones: **Editar parámetros de
     tarifa (€)** (formulario de v0.5.x) y **Rotar token de acceso**
     (nuevo).
  3. Al elegir "Rotar token" se enseña una pantalla descriptiva
     explicando exactamente qué va a pasar y qué tiene que hacer el
     usuario después (volver a pegar el bookmarklet en el navegador).
     El único botón es Confirmar — no hay inputs.
  4. Al confirmar, se genera un `secrets.token_hex(24)` (192 bits,
     mismo generador que usa el wizard inicial), se persiste en
     `entry.data[CONF_TOKEN]` mediante
     `hass.config_entries.async_update_entry(...)` y se vuelve a
     publicar la notificación persistente del bookmarklet con el
     `notification_id = canal_bookmarklet_<entry_id>` (que, al
     coincidir con la del install original, simplemente la reemplaza
     en sitio — no se acumulan notificaciones huérfanas).

  Invariantes clave que esto preserva:

  - El bookmarklet anterior **deja de funcionar al instante**. Tanto
    el endpoint POST (`ingest.py`, línea ≈146) como la página
    bootstrap del bookmarklet (`bookmarklet_view.py`, línea ≈131)
    leen el token desde la fuente viva (`hass.data[DOMAIN][entry_id]`
    o `config_entry.data` respectivamente) y lo comparan con
    `secrets.compare_digest`, sin cachear nada por su cuenta. La
    actualización de `entry.data` propaga atómicamente vía el
    `_async_update_listener` ya existente (línea 530 de
    `__init__.py`), que copia `entry.data["token"]` al cache.
  - El cache de lecturas (`store.py`), el `baseline_liters`, las
    estadísticas de largo plazo y la binding contract↔entry **no se
    tocan**. La rotación es puramente del secreto compartido — todo
    el estado funcional persiste.
  - No se dispara reload de la entrada (solo `async_update_entry` +
    re-publicación de la notificación). Las entidades no leen el
    token, así que no necesitan re-evaluación.
  - Se persiste en `entry.data` (no `entry.options`) porque el token
    es estado operacional, no preferencia editable. Convención HA:
    `data` = configuración inmutable durante la vida de la entrada
    (token, URL, contract id), `options` = ajustes que el usuario
    puede tocar después (parámetros de tarifa).

  El paso `rotate_token` usa el patrón "two-pass form" estándar de
  HA: primera llamada (`user_input is None`) renderiza la pantalla
  descriptiva con `vol.Schema({})` (sin inputs), segunda llamada
  (`user_input is not None`) ejecuta la rotación. El consentimiento
  es el clic en Confirmar.

### Cambiado

- **`OptionsFlow` ahora es un menú top-level** (`async_show_menu`)
  con dos ramas — `cost_params` y `rotate_token`. La lógica que
  antes vivía en `async_step_init` (formulario de tarifa) se ha
  movido sin cambios funcionales a `async_step_cost_params`. El
  comportamiento al editar parámetros de tarifa es idéntico al de
  v0.5.x (mismas validaciones, mismos defaults, mismo reload del
  entry si cambia `enable_cost`).

- **Mensaje de abort `reauth_not_supported`** ya no es la única
  ruta para "necesito un token nuevo". Sigue mostrándose si HA
  intenta una re-autenticación automática (que no existe en este
  flujo, los datos los aporta el navegador), pero el camino real
  para el usuario es ahora *Configurar → Rotar token*.

### Traducciones

- `strings.json` y `translations/{en,es}.json` actualizados:

  * Nuevo bloque `options.step.init` con `menu_options.cost_params`
    y `menu_options.rotate_token`.
  * Renombrado el antiguo `options.step.init` (formulario tarifa) a
    `options.step.cost_params`. Mismas claves de campo y
    descripciones — solo cambia el `step_id`.
  * Nuevo bloque `options.step.rotate_token` con título y
    descripción larga explicando consecuencias e implicaciones.
  * Añadidas las entradas faltantes `services.clear_cost_stats`
    (v0.5.15) y `services.reset_meter` (v0.5.16) en ambos
    ficheros de traducción — antes solo estaban en `strings.json`,
    así que los usuarios con HA en español o inglés veían los
    nombres por defecto sin localizar.

## [0.5.17] — 2026-04-26

### Añadido

- **Test de regresión `tests/test_services_yaml.py`** que verifica
  que `custom_components/canal_isabel_ii/services.yaml` y la
  registración real de servicios en `__init__.py` están
  sincronizadas. Cubre cuatro modos de fallo silenciosos que ningún
  otro test ni `ruff` detectan:

  1. Servicio registrado en código pero ausente del YAML — el
     usuario no lo ve en *Herramientas para desarrolladores →
     Acciones* y no puede invocarlo desde la UI.
  2. Servicio en el YAML pero no registrado en código — HA emite
     `service <name> for domain canal_isabel_ii not found` cada
     arranque y el botón en la UI da `Failed to call service`.
  3. Campo declarado en el YAML pero no en el `vol.Schema(...)`
     — el handler ignora silenciosamente lo que el usuario teclea.
  4. Campo en el `vol.Schema(...)` pero no en el YAML — el
     handler lo lee pero la UI no lo ofrece como input.

  El test es **AST + `yaml.safe_load`**, sin dependencia de Home
  Assistant: parsea el AST de `__init__.py` para encontrar las
  llamadas a `hass.services.async_register(DOMAIN, SERVICE_X, …,
  schema=X_SCHEMA)`, resuelve las constantes `SERVICE_X = "x"` y
  el dict de `vol.Schema({vol.Optional(ATTR_INSTANCE): ...})` y
  compara contra los servicios + campos declarados en
  `services.yaml`.

  Si en el futuro alguien añade un servicio nuevo o renombra un
  campo, el test falla con un mensaje que apunta directo al
  fichero y al identificador a corregir. Si la forma del
  `vol.Schema` se sale del patrón conocido (por ejemplo nuevos
  validadores compuestos), el test también falla — alto y claro
  — para forzar al desarrollador a extender el parser en vez de
  silenciar la regresión.

- `requirements-test.txt` añade `pyyaml>=6.0` (dependencia del
  nuevo test). Es la misma librería YAML que usa Home Assistant
  internamente, así que no hay riesgo de divergencia entre lo que
  el test ve y lo que HA carga en runtime.

### Verificación

Los cuatro servicios actualmente registrados pasan el nuevo guard:
`refresh`, `show_bookmarklet`, `clear_cost_stats` (v0.5.15) y
`reset_meter` (v0.5.16) — todos con su entrada en YAML, descripción,
campo `instance` opcional y selector `text:` consistente.

## [0.5.16] — 2026-04-26

### Añadido

- **Servicio `canal_isabel_ii.reset_meter`.** Para usar cuando el
  instalador de Canal cambia físicamente el contador de agua. El
  contador nuevo arranca en cero (o cerca), lo que produce dos
  efectos perniciosos sin este servicio:

  1. La guarda monotónica de las entidades `Consumo acumulado`,
     `Coste acumulado` y `Lectura del contador` rechaza cualquier
     lectura inferior al último máximo restaurado, así que la
     tarjeta de la entidad **se queda congelada** durante meses
     hasta que el contador nuevo cruza por casualidad el valor
     antiguo.
  2. El `baseline_liters` del store (los litros que se cayeron al
     hacer *trim* del cache, ver v0.5.12) corresponde al contador
     viejo y, sumado a las lecturas nuevas, **infla artificialmente**
     el valor cumulativo.

  El nuevo servicio resuelve ambos: borra el `baseline_liters` del
  contrato afectado en el store y dispara
  `SIGNAL_METER_RESET.format(entry_id, contract_id)` por el
  *dispatcher*. Las tres entidades cumulativas escuchan esa señal,
  ponen su `_restored_value` a `None` y vuelven a publicar el
  estado, esta vez aceptando la lectura baja como válida.

  **Las estadísticas de largo plazo del *recorder* NO se tocan**:
  la serie de coste y la de consumo siguen anclando a su `last_sum`
  y el siguiente *push* continúa la curva sin saltos negativos. El
  panel Energía conserva todo el histórico previo al cambio de
  contador y la curva sigue creciendo de forma monótona — sólo el
  contador físico vuelve a cero, no la representación gráfica de
  cuánto agua/dinero ha pasado.

  Documentado en `services.yaml` (aparece en *Herramientas para
  desarrolladores → Acciones* con descripción y campo `instance`
  filtrable). Vacío = todas las entradas; nombre o `entry_id` =
  esa instalación concreta. Si la entrada no tiene contratos
  cacheados aún, el servicio se queda en log informativo y no
  hace daño.

### Cambiado

- `ReadingStore.async_reset_baseline(contract)` — nuevo método
  pequeño que borra una entrada del diccionario `baseline_liters`
  y persiste a disco. Usado únicamente desde el servicio nuevo;
  separado para que sea testeable de forma aislada (un test futuro
  puede instanciar el store sin Hass completo y verificar el
  contrato sin tocar la lógica del servicio).

- `const.py` añade `SIGNAL_METER_RESET` con el formato
  `canal_isabel_ii_meter_reset_{entry_id}_{contract_id}` para
  permitir reset por contrato dentro de una entrada multicontrato.

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
