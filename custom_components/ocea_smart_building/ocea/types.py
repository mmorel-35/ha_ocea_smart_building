"""Typed contracts and boundary validation for the Ocea resident API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict, TypeGuard

from .exceptions import OceaApiError


class OceaResidentInfo(TypedDict, total=False):
    """Known resident fields."""

    id: str
    civilite: str | None
    nom: str | None
    prenom: str
    email: str | None
    telephone: str | None
    dateDebutOccupation: str | None
    dateLastConnection: str | None
    dateAccountDisabled: str | None
    isWelcomeModaleDisplayed: bool


class OceaOccupation(TypedDict, total=False):
    """Occupation of a dwelling, including its independent date range."""

    logementId: str
    residentId: str | None
    codeSite: str | None
    typeOccupation: str | None
    dateDebut: str | None
    dateFin: str | None


class OceaResidentResponse(TypedDict, total=False):
    """Resident and occupation response."""

    resident: OceaResidentInfo
    occupations: list[OceaOccupation]
    codeClient: str | None
    nomClient: str | None


class OceaDashboardConsumption(TypedDict, total=False):
    """One dwelling-level consumption."""

    fluide: str
    valeur: str
    unite: str | None


class OceaPdsResponse(TypedDict, total=False):
    """Point of measurement fields."""

    id: str
    fluide: str
    code: str | None
    emplacement: str | None
    typeReleve: str | None
    fce: int | float | None


class OceaDeviceResponse(TypedDict, total=False):
    """Installed device fields."""

    id: str | None
    pdsId: str | None
    numeroSerie: str | None
    datePose: str | None
    fc: int | float | None


class OceaConsumptionRequest(TypedDict):
    """Date range and granularity requested for a PDS."""

    debut: str
    fin: str
    granularity: str


class OceaDevicesRequest(TypedDict):
    """PDS identifiers requested from the device endpoint."""

    pdsIds: list[str]


class OceaConsumptionItem(TypedDict, total=False):
    """One dated consumption and its optional metadata."""

    date: str
    valeur: int | float | str
    fuiteEstimee: int | float | str | None
    consentement: bool | None
    type: str | None
    isEstimated: bool


class OceaConsumptionResponse(TypedDict, total=False):
    """Consumption response for one PDS."""

    unite: str | None
    consommations: list[OceaConsumptionItem]


_OPTIONAL_STRING = (str, type(None))
_OPTIONAL_NUMBER = (int, float, type(None))
_VALUE = (int, float, str)
_OPTIONAL_VALUE = (int, float, str, type(None))


def _validate_fields(
    data: Mapping[str, object], fields: Mapping[str, tuple[type, ...]]
) -> None:
    """Validate known fields without discarding unknown response fields."""
    for key, allowed in fields.items():
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, allowed) or (
            isinstance(value, bool) and bool not in allowed
        ):
            raise OceaApiError(f"Invalid API field: {key}")


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise OceaApiError("Invalid API object response")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise OceaApiError("Invalid API list response")
    return value


def is_resident_response(data: dict[str, object]) -> TypeGuard[OceaResidentResponse]:
    """Validate known fields in a resident response."""
    _validate_fields(data, {"codeClient": _OPTIONAL_STRING, "nomClient": _OPTIONAL_STRING})
    if "resident" in data:
        _validate_fields(
            _object(data["resident"]),
            {
                "id": (str,),
                "civilite": _OPTIONAL_STRING,
                "nom": _OPTIONAL_STRING,
                "prenom": (str,),
                "email": _OPTIONAL_STRING,
                "telephone": _OPTIONAL_STRING,
                "dateDebutOccupation": _OPTIONAL_STRING,
                "dateLastConnection": _OPTIONAL_STRING,
                "dateAccountDisabled": _OPTIONAL_STRING,
                "isWelcomeModaleDisplayed": (bool,),
            },
        )
    if "occupations" in data:
        for occupation in _list(data["occupations"]):
            _validate_fields(
                _object(occupation),
                {
                    "logementId": (str,),
                    "residentId": _OPTIONAL_STRING,
                    "codeSite": _OPTIONAL_STRING,
                    "typeOccupation": _OPTIONAL_STRING,
                    "dateDebut": _OPTIONAL_STRING,
                    "dateFin": _OPTIONAL_STRING,
                },
            )
    return True


def is_dashboard_consumption(data: dict[str, object]) -> TypeGuard[OceaDashboardConsumption]:
    """Validate dwelling consumption fields."""
    _validate_fields(data, {"fluide": (str,), "valeur": (str,), "unite": _OPTIONAL_STRING})
    return True


def is_pds_response(data: dict[str, object]) -> TypeGuard[OceaPdsResponse]:
    """Validate point of measurement fields."""
    _validate_fields(
        data,
        {
            "id": (str,),
            "fluide": (str,),
            "code": _OPTIONAL_STRING,
            "emplacement": _OPTIONAL_STRING,
            "typeReleve": _OPTIONAL_STRING,
            "fce": _OPTIONAL_NUMBER,
        },
    )
    return True


def is_device_response(data: dict[str, object]) -> TypeGuard[OceaDeviceResponse]:
    """Validate installed device fields."""
    _validate_fields(
        data,
        {
            "id": _OPTIONAL_STRING,
            "pdsId": _OPTIONAL_STRING,
            "numeroSerie": _OPTIONAL_STRING,
            "datePose": _OPTIONAL_STRING,
            "fc": _OPTIONAL_NUMBER,
        },
    )
    return True


def is_consumption_response(data: dict[str, object]) -> TypeGuard[OceaConsumptionResponse]:
    """Validate the PDS consumption list and its known fields."""
    _validate_fields(data, {"unite": _OPTIONAL_STRING})
    if "consommations" in data:
        for item in _list(data["consommations"]):
            _validate_fields(
                _object(item),
                {
                    "date": (str,),
                    "valeur": _VALUE,
                    "fuiteEstimee": _OPTIONAL_VALUE,
                    "consentement": (bool, type(None)),
                    "type": _OPTIONAL_STRING,
                    "isEstimated": (bool,),
                },
            )
    return True
