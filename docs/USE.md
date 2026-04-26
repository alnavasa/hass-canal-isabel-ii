# Guía de uso — sacarle partido a la integración

> Asume que ya tienes la integración instalada y los sensores creados
> ([SETUP.md](SETUP.md)). Esta guía cuenta **qué medir, cómo
> visualizarlo y qué automatizaciones tienen sentido**.

## 1. Qué hace cada sensor (de verdad)

La integración crea **tres sensores por contrato** + **una estadística
externa horaria**. Saber cuál usar evita gráficas raras y alertas
inconsistentes.

### `Consumo última hora` — `sensor.<install>_consumo_ultima_hora`

- **Estado**: litros consumidos en la **última hora publicada por el portal**.
- **`state_class: total`** → HA NO lo acumula automáticamente. Cada
  fetch sustituye el valor.
- **Cuándo usarlo**: trigger de automatizaciones que reaccionen al
  *valor instantáneo* — alertas de pico, dashboard "ahora mismo".
- **Cuándo NO usarlo**: gráficas históricas. El portal publica con
  ~1-2 h de retraso, y solo verás puntos sueltos en el log de la
  entidad.

### `Consumo periodo` — `sensor.<install>_consumo_periodo`

- **Estado**: suma de **todas las lecturas que están en el cache local
  de la integración** (hasta ~1 año horario, se trunca a
  `MAX_READINGS_PER_ENTRY = 8760`).
- **`state_class: total_increasing`** → HA lo trata como un contador
  monotónico. Reset = cache vaciado (borrar `.storage/canal_isabel_ii.<entry_id>`).
- **Trampa**: NO es la lectura real del contador ni el consumo del
  periodo de facturación. Es **lo que está en cache ahora mismo**. Si
  borras el cache, baja a 0 y al rato vuelve a subir con el siguiente
  click del bookmarklet.
- **Cuándo usarlo**: con cuidado, para ver "lo consumido en lo que
  está cacheado". La integración protege contra reset fantasma: si el
  cache se vacía, el sensor mantiene el último valor — ver
  `_restored_value` en `sensor.py`.

### `Lectura del contador` — `sensor.<install>_lectura_del_contador`

- **Estado**: lectura **absoluta** del contador físico, en m³, igual a
  la del dial girante en la calle.
- **Atributos**: `meter_reading_at` (cuándo se tomó la lectura, según
  el portal — suele ser de las 03:00 del día anterior) y `raw_reading`
  (string original "56,735m3" para auditoría).
- **`state_class: total_increasing`**, unidad **m³**.
- **Cuándo usarlo**: **conciliar con la factura**. Las facturas de
  Canal indican lectura inicial y final del periodo — este sensor te
  da la lectura actual exacta.
- **Trampa**: el portal solo refresca esta lectura **una vez al día**
  (madrugada). Verás el mismo valor durante 24 h, lo cual es normal.

### Estadística externa `canal_isabel_ii:consumption_<contract>`

- **No es una entidad** — vive en la tabla `statistics_meta` del
  *recorder*. No la verás en *Estados*.
- **La publica la integración** desde el cache local cada vez que
  llega un POST nuevo del bookmarklet. **Es horaria**. Es **upsert** —
  el `start` de cada hora es la clave, así que repetir la importación
  no duplica.
- **Etiqueta visible** en el panel de Energía: `<install> - Canal de
  Isabel II` (ej. `Casa - Canal de Isabel II`). Si la cuenta tiene
  varios contratos, se añade el contract id entre paréntesis.
- **Cuándo usarla**: panel de Energía, gráficas largas (semanas, meses,
  años). Es la única vía que permite ver historial completo (incluido
  el histórico que traiga el primer click, ~7 meses). El panel `agua`
  la consume nativamente.

## 2. Panel de Energía — configuración fina

Ya cubierto en [SETUP.md §4](SETUP.md#4-conectar-al-panel-de-energía-agua).
Notas adicionales de uso:

### Coste — desde v0.5.0 lo hace la integración

A partir de v0.5.0 la integración trae **modelo de tarifa de Canal
incluido** y publica entidades de coste opcionales. La forma
recomendada de tener coste en el panel de Energía es:

1. *Ajustes → Dispositivos y servicios → Canal de Isabel II →
   Configurar*.
2. Marca **Calcular precio (€)** y rellena los 4 parámetros (calibre
   del contador, nº viviendas, cuota suplementaria de alcantarillado
   €/m³, IVA %). Los tres primeros vienen en tu factura; IVA por
   defecto 10 %.
3. Tras guardar, la integración crea 3 sensores nuevos por contrato:
   - `sensor.<install>_coste_acumulado` — € acumulados, monotónico.
   - `sensor.<install>_precio_actual` — €/m³ del próximo m³ (bloque
     actual + IVA + suplementaria, sumando los 4 servicios).
   - `sensor.<install>_bloque_tarifario_actual` — bloque 1-4 del
     próximo m³.
4. En el panel de Energía → Agua, opción **"Usa una entidad
   rastreando el coste total"** y selecciona
   `sensor.<install>_coste_acumulado`. Coste correcto por bloques,
   con cuota fija prorrateada y vigencia 2025/2026 manejadas
   automáticamente.

> **Sigue funcionando "sin coste"**: si dejas la casilla sin marcar,
> el panel de Energía → Agua muestra solo m³ y el resto del
> comportamiento es idéntico a v0.4.x. Las opciones legacy *precio
> fijo €/m³* o *sensor de precio template* del panel de Energía
> también siguen funcionando, pero con la entidad de coste
> built-in obtienes mejor precisión sin escribir templates.

**Validación**: el modelo se ha calibrado contra dos facturas reales
con < 1 % de desvío en ambos casos (alta consumición cruzando los
cuatro bloques, y consumo bajo cruzando la frontera de vigencia
01-01-2026). Si una nueva factura tuya se desvía > 10 %, abre un
issue con la factura anonimizada.

**Limitaciones del modelo en v0.5.0**:

- Solo "Doméstico 1 vivienda". Industrial, comercial y comunidades
  grandes tienen tablas distintas que aún no están encodificadas.
- Los precios B2-B4 de la vigencia 2026 son **extrapolados** del
  delta % de B1 (la única banda observada en facturas reales que
  cruzan la frontera). Para usuarios con consumo < 20 m³ bimestral
  da igual — solo ven B1. Cuando aparezca una factura con > 20 m³
  posterior a 01-01-2026, se reemplazarán por valores reales.
- Asume **bimestres naturales** (ene-feb, mar-abr, …, nov-dic). Si
  tu ciclo de lectura está desfasado, los totales por bimestre serán
  ligeramente inexactos pero el **total anual sigue siendo exacto**.

### Histórico el día 1

El **primer click del bookmarklet**, sin filtro de pantalla,
descarga el rango por defecto del portal (últimos ~60 días horarios).
Para tirar de los **~7 meses** completos que el portal retiene, **filtra
en pantalla por tramos de ≤30 días** y pulsa el favorito una vez por
tramo:

1. En *Mi consumo*, fija frecuencia **Horaria** + rango de fechas
   ≤30 días (p.ej. `1-30 ene`).
2. Pulsa **Ver** para que el portal cargue ese tramo.
3. Pulsa el bookmarklet — POSTea las horas filtradas.
4. Cambia el rango (p.ej. `31 ene-1 mar`), **Ver**, bookmarklet otra
   vez. Repite hasta cubrir lo que quieras.

Las estadísticas externas son **upsert por timestamp horario**, así que
los tramos se **acumulan, no se sobrescriben**, sin riesgo de duplicar.

> **Por qué 30 días**: el portal rechaza rangos mayores con error en el
> formulario `consumoForm`. No es una limitación del bookmarklet —
> simplemente respetamos lo que el backend del Canal sirve.

Para mantenerlo fresco basta con pulsar el bookmarklet 1-2 veces por
semana: el POST es upsert-seguro, no duplica datos.

## 3. Lovelace — tarjetas que importan

### Estado actual

```yaml
type: entities
title: Agua — Casa
entities:
  - entity: sensor.casa_lectura_del_contador
    name: Contador (m³)
  - entity: sensor.casa_consumo_ultima_hora
    name: Última hora publicada
    secondary_info: last-updated
  - entity: sensor.casa_consumo_periodo
    name: Consumo cacheado
```

### Gráfica horaria de la última semana

```yaml
type: statistics-graph
title: Consumo horario — última semana
entities:
  - canal_isabel_ii:consumption_999000001  # statistic_id, no entidad
chart_type: bar
period: hour
days_to_show: 7
stat_types:
  - sum
```

> **Importante**: usa `canal_isabel_ii:consumption_<id>` como entity id
> aunque no aparezca en el autocompletado. La tarjeta `statistics-graph`
> acepta statistic_ids externos. Si lo dejas como entidad
> (`sensor.casa_consumo_periodo`), verás la curva monotónica del
> acumulado, no el consumo por hora.

### Comparativa día actual vs día anterior

```yaml
type: statistics-graph
title: Hoy vs ayer
entities:
  - canal_isabel_ii:consumption_999000001
period: hour
days_to_show: 2
stat_types:
  - sum
```

### Gauge — consumo de la última hora

```yaml
type: gauge
entity: sensor.casa_consumo_ultima_hora
min: 0
max: 200       # litros — ajusta a tu hogar
severity:
  green: 0
  yellow: 80
  red: 150
needle: true
```

## 4. utility_meter — bimestral, mensual, diario

El panel de Energía agrupa por día/semana/mes/año pero **no por ciclo
de facturación bimestral** (Canal factura cada dos meses). Para tener
ese contador, usa `utility_meter` apoyado en la **estadística externa**
no funciona — `utility_meter` necesita una entidad. Hay dos rutas:

### Opción A — `utility_meter` sobre `Lectura del contador` (recomendada)

```yaml
# configuration.yaml
utility_meter:
  agua_bimestral:
    source: sensor.casa_lectura_del_contador
    cycle: bimonthly
    offset:
      days: 0   # ajusta al día que arranca tu ciclo según factura
  agua_mensual:
    source: sensor.casa_lectura_del_contador
    cycle: monthly
  agua_diaria:
    source: sensor.casa_lectura_del_contador
    cycle: daily
```

Esto te da `sensor.agua_bimestral`, `sensor.agua_mensual`,
`sensor.agua_diaria` — cada uno se resetea automáticamente al final
del ciclo y empieza a contar desde 0.

> El sensor fuente está en m³ y es `total_increasing`, así que
> `utility_meter` lo usa correctamente. No necesitas ningún `template:`
> intermedio.

### Opción B — `utility_meter` sobre `Consumo periodo`

Funciona pero es **menos fiable**: si vacías el cache local de la
integración durante el ciclo, el sensor fuente se aplana y
`utility_meter` puede contabilizar un reset falso. Úsalo solo si por
alguna razón A no te funciona (ej. tu contador no responde y el portal
no actualiza la lectura absoluta).

## 5. Cálculo de coste por tramos (Canal)

> **Desde v0.5.0 esto lo hace la integración por ti**. Marca
> *Calcular precio (€)* en la configuración de la integración (o vía
> *Configurar* después) y obtienes los sensores
> `sensor.<install>_coste_acumulado`,
> `sensor.<install>_precio_actual` y
> `sensor.<install>_bloque_tarifario_actual` con tarifa por **4
> bloques**, **cuota fija** prorrateada al periodo, **cuota
> suplementaria de alcantarillado** y **IVA**, partidos por vigencia
> 2025/2026 cuando toca. Validado contra facturas reales con desvío
> < 1 %.

Si necesitas más control que el que ofrecen los parámetros de la
integración (p.ej. quieres modelar un escalón de coste distinto, o
hacer un sensor de "lo que llevo gastado este bimestre" sin esperar
al cierre del periodo), puedes seguir usando el approach manual:

| Bloque | Rango (m³ bimestral) | Precio €/m³ aprox. (sin IVA, suma 4 servicios) |
|--------|----------------------|------------------------------------------------|
| B1     | 0–20                 | ~0,87                                          |
| B2     | 20–40                | ~1,53                                          |
| B3     | 40–60                | ~3,78                                          |
| B4     | 60+                  | ~4,35                                          |

(Valores 2025 — consulta tu factura para el año en vigor; los
exactos están en `tariff.py`.)

Plantilla manual de respaldo:

```yaml
template:
  - sensor:
      - name: "Coste agua bimestre actual (manual)"
        unit_of_measurement: "EUR"
        device_class: monetary
        state: >-
          {% set m = states('sensor.agua_bimestral') | float(0) %}
          {% set t1 = [m, 20] | min %}
          {% set t2 = [[m - 20, 0] | max, 20] | min %}
          {% set t3 = [[m - 40, 0] | max, 20] | min %}
          {% set t4 = [m - 60, 0] | max %}
          {{ ((t1 * 0.87 + t2 * 1.53 + t3 * 3.78 + t4 * 4.35) * 1.10) | round(2) }}
```

Recordatorio: la entidad built-in **`coste_acumulado`** ya incluye
cuota fija + suplementaria + IVA y se publica como estadística
externa (`canal_isabel_ii:cost_<contract>`) lista para el panel de
Energía. La plantilla manual es solo para casos avanzados.

## 6. Automatizaciones útiles

### Alerta de fuga (consumo nocturno anómalo)

Si entre 02:00 y 05:00 el consumo es > 30 L/h tres horas seguidas,
algo gotea (cisterna, jardín automático mal cerrado, fuga real).

```yaml
alias: Alerta posible fuga agua
trigger:
  - platform: time_pattern
    hours: "/1"
condition:
  - condition: time
    after: "02:00:00"
    before: "05:00:00"
  - condition: numeric_state
    entity_id: sensor.casa_consumo_ultima_hora
    above: 30
action:
  - service: notify.mobile_app_iphone
    data:
      title: "💧 Posible fuga"
      message: >-
        Consumo nocturno {{ states('sensor.casa_consumo_ultima_hora') }} L/h —
        revisa cisternas y riego.
```

### Resumen diario por la mañana

```yaml
alias: Resumen agua diario
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: notify.mobile_app_iphone
    data:
      title: "Consumo agua ayer"
      message: >-
        {{ states('sensor.agua_diaria') }} m³ ({{
        (states('sensor.agua_diaria') | float * 1000) | round(0) }} L).
        Bimestre: {{ states('sensor.agua_bimestral') }} m³.
```

### Aviso "queda poco para superar tramo"

```yaml
alias: Aviso tramo agua
trigger:
  - platform: numeric_state
    entity_id: sensor.agua_bimestral
    above: 60   # 6 m³ antes del cambio a tramo 3
action:
  - service: persistent_notification.create
    data:
      title: "Atención tarifa agua"
      message: >-
        Llevas {{ states('sensor.agua_bimestral') }} m³ este bimestre.
        A partir de 66 m³ entra el tramo más caro.
```

### Sincronización con la factura (manual, mensual)

Cuando llega la factura, anota el cierre del periodo y guarda:

```yaml
alias: Snapshot lectura factura
trigger:
  - platform: state
    entity_id: input_boolean.factura_recibida
    to: "on"
action:
  - service: input_text.set_value
    target:
      entity_id: input_text.lectura_ultima_factura
    data:
      value: "{{ states('sensor.casa_lectura_del_contador') }}"
```

Útil para detectar discrepancias entre lo que tú mides y lo que te
facturan.

## 7. Multi-contrato — qué cambia

Cuando tu cuenta tiene varios contratos, **cada uno es un dispositivo
separado**, con sus tres sensores y su propia estadística externa.

- En Lovelace, agrúpalos con tarjetas `entities` separadas o un
  `vertical-stack` con un título por instalación.
- En el panel de Energía, **añade cada estadística externa por
  separado** como fuente de agua. El panel suma automáticamente.
- `utility_meter` por contrato — si tienes dos casas, te interesará
  un `agua_bimestral_principal` y un `agua_bimestral_segunda`.

## 8. Debugging — saber por qué un sensor no actualiza

```yaml
# Herramientas para desarrolladores → Acciones
action: logger.set_level
data:
  custom_components.canal_isabel_ii: debug
```

Tras esto, en `Ajustes → Sistema → Logs` filtra por `canal_isabel_ii`.
Verás cada tick del coordinator (pasa-through, sin I/O), cada POST
entrante al endpoint `CanalIngestView` (con el `contract` detectado
y los `imported / new` tras dedupe) y cada `_push_statistics` al
recorder.

Para verificar la base de datos directamente:

```bash
# Vía SSH al HA OS:
docker exec homeassistant python3 -c "
import sqlite3
c = sqlite3.connect('/config/home-assistant_v2.db')
for row in c.execute(\"SELECT statistic_id, name, unit_of_measurement FROM statistics_meta WHERE statistic_id LIKE 'canal%'\"):
    print(row)
"
```

Deberías ver al menos un `('canal_isabel_ii:consumption_<id>', 'Casa - Canal de Isabel II', 'L')`.

## 9. Limitaciones conocidas

- **Granularidad horaria, no cuart-horaria**: el portal solo expone
  consumo por hora. Para ver picos de 15 min necesitarías un contador
  IoT propio.
- **Latencia del portal**: las lecturas aparecen en el portal con
  **~1-2 h de retraso** sobre el consumo real. La gráfica del panel de
  Energía siempre va una o dos horas detrás del momento actual.
- **Retención ~7 meses**: el portal no expone nada anterior a ese
  rango, ni con backfill. Para histórico largo, tendrás que hacer
  *snapshot* del recorder periódicamente (es lo que hace `recorder` por
  defecto, pero conviene revisar `purge_keep_days`).
- **Rango máx. por click: 30 días**: el formulario `consumoForm` del
  portal rechaza rangos de fechas mayores de 30 días naturales. El
  bookmarklet POSTea exactamente lo que tengas filtrado en pantalla —
  si pones 31 días, el portal te devuelve error y el bookmarklet
  no recibe CSV. Para meter más historia, parte el rango en tramos
  consecutivos de ≤30 días y pulsa el favorito una vez por tramo
  (las estadísticas externas son upsert por timestamp horario, así
  que se acumulan sin duplicar).
- **Lectura absoluta diaria**: el sensor `Lectura del contador` se
  refresca **una vez al día** porque así lo expone el portal. No
  esperes verlo subir minuto a minuto.
- **`Bloque tarifario actual` usa bimestres naturales calendario**
  (ene-feb, mar-abr…), no tu ciclo real de facturación. Si Canal te
  factura desfasado (típico: del día 6 al 14 del mes equivalente dos
  meses después), el bloque mostrado puede no coincidir exactamente
  con el de la factura. El total anual sigue siendo correcto. Detalle
  en [FAQ → ¿Por qué el bloque tarifario actual no coincide con mi
  factura?](#faq-bloque).
- **`Coste acumulado` puede subestimar el primer bimestre tras
  instalar la integración**, porque le faltan las lecturas de las
  horas previas a la primera pulsación del bookmarklet. Cuotas fijas
  sí se contabilizan (catch-up automático), variable no. A partir
  del segundo bimestre completo la diferencia desaparece. Detalle en
  [FAQ → ¿Por qué el coste de mi primer bimestre es menor que la
  factura?](#faq-primer-bimestre).
- **Tarifa real**: la integración no incluye tarifa por defecto porque
  Canal cambia precios anualmente y la fórmula bimestral por bloques
  no es expresable en una sola constante. Modela tú la tarifa con
  `template:` (ver §5).

## 10. FAQ

Preguntas que aparecen recurrentemente. Si no encuentras la tuya, abre
un issue.

### Sobre el cálculo de coste

#### ¿Cuán preciso es el `Coste acumulado` respecto a mi factura real?

El motor está calibrado para quedar **por debajo del 10 % de
desviación** vs factura real. En la práctica, con cobertura completa
de lecturas en el bimestre (es decir, primera pulsación del
bookmarklet anterior al inicio del periodo de facturación), la
desviación medida en facturas reales se mantiene **bajo el 1 %**.

Ese ±1 % residual lo explican fundamentalmente dos efectos: (a) el
modelo prorratea el cambio de vigencia (p.ej. 2025→2026) por días
mientras que la factura suele aplicar una sola vigencia, y (b) la
factura puede traer conceptos extras del Ayuntamiento (basuras, tasas
locales) que el modelo no incorpora.

#### ¿Lleva IVA incluido?

Sí. Por defecto se aplica **10 %** sobre la base imponible (consumo +
cuota fija + suplementaria de alcantarillado). El porcentaje es
configurable desde *Configurar → Editar parámetros de coste* — útil
si en algún momento cambia el tipo de IVA del agua.

<a id="faq-primer-bimestre"></a>

#### ¿Por qué el coste de mi primer bimestre es menor que la factura?

Cuando instalas la integración a mitad de bimestre, los m³ consumidos
**antes** de tu primera pulsación del bookmarklet no entran en el
modelo: el portal de Canal solo te devuelve histórico hasta donde
tenga datos disponibles en ese momento. La cuota fija de servicio
**sí** se contabiliza para todo el bimestre (catch-up automático),
pero la parte variable (€/m³ por bloque) solo cubre lo que el cache
local tenga.

A partir del **segundo bimestre completo** capturado desde el día 1,
la diferencia desaparece y verás <1 % de desviación.

Truco para acortarlo: en tu primera pulsación, filtra en pantalla
desde el inicio del bimestre actual y captura por tramos de ≤30 días
(ver §2 *Histórico el día 1*).

#### ¿Funciona si mi ciclo de facturación está desfasado del bimestre natural?

Sí. El motor reparte el consumo por bimestre natural (ene-feb,
mar-abr…) y aplica la vigencia de tarifa que corresponda a cada
día. Para un periodo de factura desfasado (típico: 6/11 a 14/1, p.ej.)
los m³ totales y el coste anual cuadran; lo que puede no cuadrar al
céntimo son los totales bimestre-a-bimestre cuando un periodo cruza
una frontera de vigencia (cambio de año).

#### ¿Qué pasa cuando Canal sube la tarifa a mitad de periodo?

El código separa vigencias (`_TARIFA_2025`, `_TARIFA_2026`…) en
`tariff.py` y hace **split por días**: cada día factura a la vigencia
en vigor. Cuando Canal publica una nueva vigencia, se añade al
módulo y la versión nueva de la integración la incorpora — espera
una release ese año.

### Sobre los sensores

<a id="faq-bloque"></a>

#### ¿Por qué el `Bloque tarifario actual` a veces no coincide con mi factura?

El sensor agrupa el consumo por **bimestre natural calendario** (1
de mes impar al 1 del mes impar siguiente), no por tu ciclo real
de facturación. Si Canal te factura desfasado, el bloque que ves en
HA puede no coincidir exactamente con el que figura en la factura.
El **total anual** sigue siendo correcto, porque al final del año
acabas consumiendo los mismos m³ independientemente de dónde caigan
los cortes bimestrales.

Si tu factura va del 6/11 al 14/1, por ejemplo, el sensor reparte
ese consumo entre bimestre nov-dic y bimestre ene-feb naturales, y
el bloque actual lo computa contra el bimestre natural que esté en
curso ahora.

#### ¿Qué hago si me cambian el contador físico?

Llama al servicio **`canal_isabel_ii.reset_meter`** (disponible
desde v0.5.16) desde *Dev Tools → Acciones*. Resetea el baseline
del contador para que la integración aprenda la nueva lectura
absoluta sin que el sensor `Consumo periodo` interprete la bajada
de m³ como un consumo negativo.

### Troubleshooting

<a id="faq-negativo"></a>

#### El panel Energía → Agua muestra una barra negativa en `Coste agua`

A partir de **v0.5.21** este caso queda cerrado por construcción:
la integración aplica el mismo guard antirregresión en el estado
de la entidad y en el push al recorder, así que ambos caminos
saltan en lockstep cuando el `cum_eur` recién calculado cae por
debajo del último valor estable. Resultado: nunca más se escribe
una serie con `sum[n] < sum[n-1]`, que es lo que el panel pinta
como barra negativa.

Si vienes de v0.5.20 o anterior y ya tienes barras negativas
**heredadas** en el recorder (corruption persistente, sobrevive a
reinicios), ejecuta **una vez** el servicio
**`canal_isabel_ii.clear_cost_stats`** desde *Dev Tools →
Acciones*. Borra las stats antiguas y la integración republica la
serie monótona desde cero en el siguiente tick del coordinator
(≤ 1 min). Refresca el navegador y la barra negativa desaparece.

A partir de ese momento ya no necesitas volver a llamarlo: con
v0.5.21 instalada, los reinicios de HA, los trims de cache, los
cambios de parámetros tarifarios y las recomputaciones por
fronteras de vigencia dejan de producir barras negativas sin
intervención manual.

Caveat del clear: el histórico de coste anterior al clear se
pierde (las nuevas stats arrancan en 0 €). Las nuevas barras a
partir de ese momento serán correctas.

#### El bookmarklet devuelve 401 al pulsarlo

Tres causas posibles, por orden:

1. **Token rotado**. Si rotaste el token desde *Configurar →
   Rotar token* (v0.5.18+), el bookmarklet viejo deja de funcionar
   inmediatamente. Re-arrastra el bookmarklet nuevo desde la
   notificación persistente (o llama al servicio
   `canal_isabel_ii.show_bookmarklet` para regenerarla).
2. **Entry recreada**. Si borraste y volviste a añadir la
   integración, el `entry_id` es nuevo y el bookmarklet apunta a un
   endpoint inexistente. Mismo fix: re-arrastra el bookmarklet
   actual.
3. **El bookmarklet en sí está corrupto**. Algunos navegadores
   recortan URLs muy largas al arrastrarlas. Vuelve a copiar el
   `javascript:…` de la notificación a mano y pégalo en
   "Editar" sobre el favorito.

#### Quiero invalidar el bookmarklet sin borrar la integración

*Configurar → Rotar token* (disponible desde v0.5.18). Genera un
token nuevo de 192 bits, invalida el anterior atómicamente, y
republica la notificación con el bookmarklet actualizado. La caché
de lecturas, el baseline y las estadísticas externas se preservan.

### Operaciones / migración

#### ¿Pierdo datos si actualizo la integración via HACS?

No. El cache de lecturas, el baseline del contador y las
estadísticas externas (`canal_isabel_ii:consumption_<id>`,
`canal_isabel_ii:cost_<id>`) sobreviven. Cuando hay migración de
schema (one-shot tras un cambio de formato), se anuncia
explícitamente en el CHANGELOG.

#### ¿Pierdo datos si elimino y vuelvo a instalar la integración?

Sí, parcialmente. Al eliminar la entry se borran:

- Cache local de lecturas (`.storage/canal_isabel_ii.<entry_id>`).
- Baseline del contador.
- Token del bookmarklet (el siguiente tendrá un valor distinto).

Lo que **sobrevive** son las estadísticas externas en el recorder
(`canal_isabel_ii:consumption_<contract>` y
`canal_isabel_ii:cost_<contract>`), que quedan **huérfanas** sin
nadie que las refresque. Si vas a reinstalar, conviene ejecutar
**`clear_cost_stats`** *antes* de eliminar la entry para no dejar
basura en el recorder, y luego reconstruirlas desde cero tras
reinstalar.
