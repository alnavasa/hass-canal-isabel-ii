"""Pure parser for the Canal de Isabel II hourly-consumption CSV.

Fed by the bookmarklet's POST — we receive exactly the CSV file the
user would have downloaded by hitting the "Exportar CSV" button in the
portal, so the parser only has to handle whatever dialects the portal
actually produces.

The portal CSV looks like (Spanish locale, CR-LF, semicolon-or-comma
separator depending on the day; we accept both):

    Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)
    999000001,Facturado,Y20HK123456,C/ Ejemplo,Horaria,22/04/2026 03,12,5
    ...

* ``Fecha/Hora`` is naive local-Madrid wall-clock with HOUR resolution
  ("DD/MM/YYYY HH" — no minutes). We treat it as naive on purpose: the
  upstream consumer (sensor → external statistics) is the layer that
  owns timezone normalisation, because it depends on HA's configured
  zone which we don't want to import here.
* ``Consumo (litros)`` uses Spanish decimals (","). We swap "," for
  "." before ``float()``.
* Rows with unparseable timestamps OR liters are skipped silently —
  Canal occasionally injects a partial trailer row when the export
  is interrupted, and one bad row should never sink the import.

Returns a list of :class:`models.Reading`. Empty list = empty CSV; the
caller distinguishes that from "no data sent at all" via the HTTP
endpoint's status code, not via the parser.
"""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO

from .models import Reading


def parse_csv(raw: str) -> list[Reading]:
    """Parse the Canal hourly CSV into a list of :class:`Reading`.

    Idempotent and side-effect free. Safe to call multiple times on
    the same input — produces identical output.
    """
    if not raw or not raw.strip():
        return []
    readings: list[Reading] = []
    # csv.DictReader autodetects the dialect from the first line — but
    # the portal occasionally swaps "," for ";". Sniff once at top so we
    # don't burn a Sniffer per row.
    sample = raw[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel  # comma default — Canal's normal output
    reader = csv.DictReader(StringIO(raw), dialect=dialect)
    for row in reader:
        ts_raw = (row.get("Fecha/Hora") or "").strip()
        if not ts_raw:
            continue
        try:
            ts = datetime.strptime(ts_raw, "%d/%m/%Y %H")
        except ValueError:
            continue
        try:
            liters = float((row.get("Consumo (litros)") or "0").replace(",", "."))
        except ValueError:
            continue
        contract = (row.get("Contrato") or "").strip()
        readings.append(
            Reading(
                contract=contract,
                timestamp=ts,
                liters=liters,
                period=(row.get("Periodo") or "").strip(),
                meter=(row.get("Contador") or "").strip(),
                address=(row.get("Dirección") or "").strip(),
                frequency=(row.get("Frecuencia") or "").strip(),
            )
        )
    return readings


def detect_contracts(raw: str) -> set[str]:
    """Return the distinct ``Contrato`` ids that appear in the CSV.

    Used by the ingest endpoint to validate the posted payload against
    the configured contract for the entry — see
    :mod:`custom_components.canal_isabel_ii.ingest`. Cheaper than a
    full ``parse_csv`` when all we need is the set of contracts, and
    forgiving about the format (skips empty/missing column).
    """
    contracts: set[str] = set()
    for r in parse_csv(raw):
        if r.contract:
            contracts.add(r.contract)
    return contracts
