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

### Coste

El panel de Energía permite asociar un **precio** a cada consumo de
agua. Tres opciones:

1. **Precio fijo €/m³** — ojo: Canal usa **tarifa por bloques** (tramo 1
   barato hasta cierto consumo bimestral, tramo 2 medio, tramo 3
   penalización). Un único precio fijo te dará una estimación pesimista
   o demasiado optimista según tu volumen.
2. **Sensor de precio** (`input_number` o template) — útil si quieres
   modelar la tarifa por bloques (ver §5 abajo, "Cálculo de coste por
   tramos").
3. **Sin coste** — sólo agua bruta. Simplísimo, gráfica clara, y dejas
   el coste para el repaso bimestral cuando llega la factura.

> Recomendación: empieza sin coste. Cuando lleves dos facturas, calibra
> el modelo de bloques (ver más abajo).

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
  - entity: sensor.mf1_lectura_del_contador
    name: Contador (m³)
  - entity: sensor.mf1_consumo_ultima_hora
    name: Última hora publicada
    secondary_info: last-updated
  - entity: sensor.mf1_consumo_periodo
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
> (`sensor.mf1_consumo_periodo`), verás la curva monotónica del
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
entity: sensor.mf1_consumo_ultima_hora
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
    source: sensor.mf1_lectura_del_contador
    cycle: bimonthly
    offset:
      days: 0   # ajusta al día que arranca tu ciclo según factura
  agua_mensual:
    source: sensor.mf1_lectura_del_contador
    cycle: monthly
  agua_diaria:
    source: sensor.mf1_lectura_del_contador
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

Tarifa **bimestral 2026** (consulta tu factura — varía por municipio
y bloques tarifarios). Estructura tipo (no es la real, es plantilla):

| Bloque | Rango (m³ bimestral) | Precio €/m³ |
|--------|----------------------|-------------|
| 1      | 0–22                 | 0,55        |
| 2      | 22–66                | 1,10        |
| 3      | 66+                  | 1,80        |

Plantilla:

```yaml
template:
  - sensor:
      - name: "Coste agua bimestre actual"
        unit_of_measurement: "EUR"
        device_class: monetary
        state: >-
          {% set m = states('sensor.agua_bimestral') | float(0) %}
          {% set t1 = [m, 22] | min %}
          {% set t2 = [[m - 22, 0] | max, 44] | min %}
          {% set t3 = [m - 66, 0] | max %}
          {{ (t1 * 0.55 + t2 * 1.10 + t3 * 1.80) | round(2) }}
```

Pruébalo en *Developer Tools → Templates* primero. Cuando coincide
±5% con tu última factura, el modelo está bien calibrado.

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
    entity_id: sensor.mf1_consumo_ultima_hora
    above: 30
action:
  - service: notify.mobile_app_iphone
    data:
      title: "💧 Posible fuga"
      message: >-
        Consumo nocturno {{ states('sensor.mf1_consumo_ultima_hora') }} L/h —
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
      value: "{{ states('sensor.mf1_lectura_del_contador') }}"
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
- **Tarifa real**: la integración no incluye tarifa por defecto porque
  Canal cambia precios anualmente y la fórmula bimestral por bloques
  no es expresable en una sola constante. Modela tú la tarifa con
  `template:` (ver §5).
