import hashlib
import json
import logging
import random
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("ais_mock")

router = APIRouter()

# Public bank list, captured from GET /api/v2/institutions.
_INSTITUTIONS: list[dict] = json.loads(
    (Path(__file__).parent / "ais_fixtures" / "institutions.json").read_text(encoding="utf-8")
)

# Everything below is made up; transactions only mimic the shapes real banks send.
_COMPANY = "Mock Tvrtka d.o.o."
_OWNER_NAME = "MOCK TVRTKA D.O.O."
# Card transactions (ATM/POS) carry the full registered name as debtorName.
_OWNER_LEGAL_NAME = "MOCK TVRTKA D.O.O. ZA USLUGE I TRGOVINU"
_REQUISITION_PRICE = 1.50
_OPENING_BALANCE = 25000.00
_HISTORY_DAYS = 120

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")

_requisitions: dict[str, dict] = {}
_accounts: dict[str, dict] = {}
_wallet = {"saldo_net": 8.50}


def _now() -> str:
    # Unlike the f1-web mock, the real AIS API sends UTC timestamps with a 'Z' suffix
    # (e.g. '2026-09-24T10:19:55.252551Z'), so this mock does the same.
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _error(status_code: int, summary: str, detail: str, error_type: str | None = None) -> JSONResponse:
    body = {"summary": summary, "detail": detail, "status_code": status_code}
    if error_type:
        body["type"] = error_type
    return JSONResponse(status_code=status_code, content=body)


def _oib(rng: random.Random) -> str:
    # ISO 7064 MOD 11,10 check digit, so the OIB passes validation on the caller's side.
    digits = [rng.randint(0, 9) for _ in range(10)]
    acc = 10
    for d in digits:
        acc = (acc + d) % 10 or 10
        acc = (acc * 2) % 11
    return "".join(map(str, digits)) + str((11 - acc) % 10)


def _iban(rng: random.Random, bank_code: str | None = None) -> str:
    # Valid HR IBAN (MOD 97 checksum): 7-digit bank code + 10-digit account number.
    bban = (bank_code or rng.choice(["2340009", "2360000", "2402006", "2484008", "2500009", "2390001"])) + "".join(
        str(rng.randint(0, 9)) for _ in range(10)
    )
    check = 98 - int(bban + "172700") % 97  # "HR00" → H=17, R=27
    return f"HR{check:02d}{bban}"


_OIB = _oib(random.Random("mock-company"))


def _random_iban() -> str:
    return _iban(random.Random())


# ── Transaction generator ───────────────────────────────────────────────────────

_SUPPLIERS = [
    ("KOMUNALNO PODUZEĆE d.o.o.", "Potrošak vode"),
    ("ENERGIJA PLUS d.o.o.", "Račun za električnu energiju"),
    ("PAPIRNICA SLOVO d.o.o.", "Uredski materijal"),
    ("IT RJEŠENJA d.o.o.", "Održavanje informatičke opreme"),
    ("OSIGURANJE SIGURNO d.d.", "Polica osiguranja"),
    ("TELEKOMUNIKACIJE NET d.o.o.", "Telefon i internet"),
    ("RAČUNOVODSTVO BILANCA j.d.o.o.", "Knjigovodstvene usluge"),
    ("AUTO SERVIS KOVAČ obrt", "Servis vozila"),
    ("NAJAM PROSTORA d.o.o.", "Najam poslovnog prostora"),
]
_CUSTOMERS = [
    "GRAD PRIMJEROVO",
    "OPĆINA NOVO SELO",
    "GRADNJA HORVAT d.o.o.",
    "PROJEKT INŽENJERING d.o.o.",
    "DIZAJN STUDIO j.d.o.o.",
    "TVRTKA D.O.O.",
]
_TAXES = [
    # (creditor, remittance, model-prefix of HR68 reference)
    ("DRŽAVNI PRORAČUN REPUBLIKE HRVATSKE", "PDV", "1201"),
    ("DRŽAVNI PRORAČUN REPUBLIKE HRVATSKE", "Doprinos za MO", "2003"),
    ("DRŽAVNI PRORAČUN REPUBLIKE HRVATSKE", "Doprinos za zdravstveno osiguranje", "8486"),
    ("Porez na dohodak - ZAGREB", "Porez i prirez na dohodak", "1880"),
]
_ATMS = [
    "ILICA 1, 10000 ZAGREB",
    "TRG BANA JELAČIĆA 3, 10000 ZAGREB",
    "RIVA 12, 21000 SPLIT",
    "KORZO 5, 51000 RIJEKA",
    "ULICA KRALJA TOMISLAVA 8, 47000 KARLOVAC",
    "ŠETALIŠTE 2, 53291 NOVALJA",
]
_POS_MERCHANTS = [
    ("BENZINSKA POSTAJA 112", "ZAGREB"),
    ("SUPERMARKET 104", "ZAGREB"),
    ("PEKARNICA KLAS", "SPLIT"),
    ("LJEKARNA CENTAR", "RIJEKA"),
    ("RESTORAN DALMACIJA", "ZADAR"),
    ("TRGOVINA ALATI", "KARLOVAC"),
    ("PARKING SERVIS", "ZAGREB"),
]


def _amount(value: float) -> str:
    # Banks send whole amounts without decimals ("4500", "-975") and the rest with two ("-63.14").
    value = round(value, 2)
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def _transactions_for_day(account: dict, day: date) -> list[dict]:
    # Seeded per account and day, so repeated calls and overlapping date ranges return the same data.
    rng = random.Random(f"{account['id']}:{day.isoformat()}")
    own = {"iban": account["iban"], "currency": "EUR"}
    doy = day.timetuple().tm_yday
    yy = f"{day.year % 1000:03d}"
    txs = []

    def tx(prefix: str, amount: float, **fields) -> dict:
        return {
            "transactionId": f"{prefix}{yy}{doy:03d}{rng.randint(0, 9999999):07d}",
            "endToEndId": fields.pop("endToEndId", "HR99"),
            "bookingDate": day.isoformat(),
            "valueDate": day.isoformat(),
            "transactionAmount": {"amount": _amount(amount), "currency": "EUR"},
            **fields,
            "internalTransactionId": hashlib.md5(rng.randbytes(16)).hexdigest(),
        }

    if day.weekday() >= 5:
        # Weekends: card payments only.
        if rng.random() < 0.5:
            name, city = rng.choice(_POS_MERCHANTS)
            txs.append(tx(
                "B18", -rng.uniform(8, 180),
                endToEndId=f"HR00{rng.randint(100000, 999999)}-{rng.randint(1000, 9999)}",
                creditorAccount={"currency": "EUR"},
                debtorName=_OWNER_LEGAL_NAME, debtorAccount=own,
                remittanceInformationUnstructured=f"POS KUPOVINA {f'{name} {city} {city}':<38}HR",
                remittanceInformationStructured="HR99",
            ))
        return txs

    for _ in range(rng.choice([0, 1, 1])):
        customer = rng.choice(_CUSTOMERS)
        invoice = f"{rng.randint(1, 400)}-1-1"
        txs.append(tx(
            "N02", rng.choice([rng.uniform(150, 1500), rng.randint(3, 15) * 100]),
            endToEndId=f"HR00{_oib(rng)}-{rng.randint(100000000, 999999999)}",
            creditorName=_OWNER_NAME, creditorAccount=own,
            debtorName=customer, debtorAccount={"iban": _iban(rng), "currency": "EUR"},
            remittanceInformationUnstructured=f"Račun br. {invoice}",
            remittanceInformationStructured=f"HR00 {invoice}",
        ))

    for _ in range(rng.choice([0, 1, 1, 2])):
        supplier, purpose = rng.choice(_SUPPLIERS)
        fields = dict(
            creditorName=supplier, creditorAccount={"iban": _iban(rng), "currency": "EUR"},
            debtorName=_OWNER_NAME, debtorAccount=own,
            remittanceInformationUnstructured=f"{purpose} {day.month:02d}/{day.year}",
            remittanceInformationStructured=f"HR01 {rng.randint(10000, 99999)}-{rng.randint(1000000000, 9999999999)}",
        )
        if rng.random() < 0.2:
            fields["purposeCode"] = "OTHR"
        txs.append(tx(rng.choice(["L18", "I18"]), -rng.uniform(15, 600), **fields))

    if day.day == 15:
        for creditor, purpose, model in _TAXES:
            txs.append(tx(
                "I18", -rng.uniform(80, 2500),
                creditorName=creditor,
                creditorAccount={"iban": _iban(rng, "1001005"), "currency": "EUR"},
                debtorName=_OWNER_NAME, debtorAccount=own,
                remittanceInformationUnstructured=purpose,
                remittanceInformationStructured=f"HR68 {model}-{_OIB}",
            ))

    if rng.random() < 0.15:
        txs.append(tx(
            "B18", -rng.choice([50, 100, 200, 300, 400, 500]),
            endToEndId=f"HR00{rng.randint(100000, 999999)}-{rng.randint(1000, 9999)}",
            creditorAccount={"currency": "EUR"},
            debtorName=_OWNER_LEGAL_NAME, debtorAccount=own,
            remittanceInformationUnstructured=f"ISPLATA BANKOMAT {rng.choice(_ATMS)}",
            remittanceInformationStructured="HR99",
        ))

    for _ in range(rng.choice([0, 0, 1, 2])):
        name, city = rng.choice(_POS_MERCHANTS)
        txs.append(tx(
            "B18", -rng.uniform(5, 250),
            endToEndId=f"HR00{rng.randint(100000, 999999)}-{rng.randint(1000, 9999)}",
            creditorAccount={"currency": "EUR"},
            debtorName=_OWNER_LEGAL_NAME, debtorAccount=own,
            remittanceInformationUnstructured=f"POS KUPOVINA {f'{name} {city} {city}':<38}HR",
            remittanceInformationStructured="HR99",
        ))

    if day.day == 1:
        txs.append(tx(
            "H18", -rng.choice([9.95, 12.50, 15.00]),
            creditorName="NAKNADE BANKE", creditorAccount={"currency": "EUR"},
            debtorName=_OWNER_NAME, debtorAccount=own,
            remittanceInformationUnstructured="Naknada za vođenje računa",
            remittanceInformationStructured="HR99",
        ))

    return txs


def _booked_transactions(account: dict) -> list[dict]:
    # Newest first, like the bank. Today's transactions are still pending, so booked ends yesterday.
    today = datetime.now(timezone.utc).date()
    days = (today - timedelta(days=n) for n in range(1, _HISTORY_DAYS + 1))
    return [t for day in days for t in _transactions_for_day(account, day)]


def _pending_transactions(account: dict) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    rng = random.Random(f"{account['id']}:pending:{today.isoformat()}")
    pending = []
    for _ in range(rng.choice([1, 2])):
        name, city = rng.choice(_POS_MERCHANTS)
        pending.append({
            "valueDate": today.isoformat(),
            "transactionAmount": {"amount": _amount(-rng.uniform(3, 120)), "currency": "EUR"},
            "remittanceInformationUnstructured": f"POS KUPOVINA {f'{name} {city} {city}':<38}HR",
        })
    return pending


def _find_institution(institution_id: str) -> dict | None:
    return next((i for i in _INSTITUTIONS if i["id"] == institution_id), None)


def _new_requisition(institution_id: str, link: str, requisition_id: str | None = None) -> dict:
    requisition_id = requisition_id or str(uuid.uuid4())
    return {
        "id": requisition_id,
        "created": _now(),
        "redirect": "https://ais.eposlovanje.hr/ais/callback",
        "status": "CR",
        "institution_id": institution_id,
        "agreement": str(uuid.uuid4()),
        "reference": str(uuid.uuid4()),
        "accounts": [],
        "user_language": "HR",
        "link": link,
        "ssn": None,
        "account_selection": False,
        "redirect_immediate": False,
    }


def _new_account(requisition: dict, iban: str, account_id: str | None = None) -> dict:
    account = {
        "id": account_id or str(uuid.uuid4()),
        "created": _now(),
        "last_accessed": None,
        "iban": iban,
        "institution_id": requisition["institution_id"],
        "status": "READY",
        "owner_name": _OWNER_NAME,
        "bban": None,
        "name": "",
        # Internal: not part of the API response.
        "_requisition_id": requisition["id"],
    }
    _accounts[account["id"]] = account
    requisition["accounts"].append(account["id"])
    return account


def _link_requisition(requisition: dict) -> None:
    if requisition["status"] == "LN":
        return
    requisition["status"] = "LN"
    _wallet["saldo_net"] = round(_wallet["saldo_net"] - _REQUISITION_PRICE, 2)
    _new_account(requisition, _random_iban())


def _public_requisition(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _public_account(a: dict) -> dict:
    return {k: v for k, v in a.items() if not k.startswith("_")}


def _active_requisitions() -> list[dict]:
    return [r for r in _requisitions.values() if not r.get("_deleted")]


def _get_account(account_id: str) -> tuple[dict | None, JSONResponse | None]:
    account = _accounts.get(account_id)
    if account is None:
        return None, _error(404, "Not found.", f"Account '{account_id}' not found.")
    requisition = _requisitions.get(account["_requisition_id"])
    if requisition is None or requisition.get("_deleted") or requisition["status"] != "LN":
        return None, _error(
            403, "Requisition not active",
            "Rekvizicija ovog računa nije aktivna — pristup podacima banke je zaustavljen.",
        )
    account["last_accessed"] = _now()
    return account, None


def _seed() -> None:
    # One already-linked requisition with one account, using the example ids from the API docs,
    # so account routes can be exercised without going through the consent flow first.
    requisition = _new_requisition(
        "UNICREDIT_CORPORATE_ZABAHR2X",
        "https://ob.example.com/start/11111111-1111-4111-8111-111111111111/UNICREDIT_CORPORATE_ZABAHR2X",
        requisition_id="11111111-1111-4111-8111-111111111111",
    )
    requisition.update({
        "status": "LN",
        "agreement": "33333333-3333-4333-8333-333333333333",
        "reference": "44444444-4444-4444-8444-444444444444",
    })
    _requisitions[requisition["id"]] = requisition
    _new_account(
        requisition, _iban(random.Random("mock-account"), "2360000"),
        account_id="22222222-2222-4222-8222-222222222222",
    )


_seed()


# ── API key check ───────────────────────────────────────────────────────────────

def _unauthorized(request: Request) -> Response | None:
    # The real API answers 401 with no body when the key is missing/invalid. The mock accepts any value.
    if not request.headers.get("X-Api-Key"):
        return Response(status_code=401)
    return None


# ── Me ──────────────────────────────────────────────────────────────────────────

@router.get("/api/v2/me")
async def get_me(request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    active = [r for r in _active_requisitions() if r["status"] == "LN"]
    pending = [r for r in _active_requisitions() if r["status"] not in ("LN", "RJ", "EX", "SU", "ER")]
    reserved = round(len(pending) * _REQUISITION_PRICE, 2)
    return {
        "company": _COMPANY,
        "oib": _OIB,
        "saldo_net": _wallet["saldo_net"],
        "reserved_net": reserved,
        "available_net": round(_wallet["saldo_net"] - reserved, 2),
        "active_requisitions": len(active),
        "pending_requisitions": len(pending),
    }


# ── Institutions ────────────────────────────────────────────────────────────────

@router.get("/api/v2/institutions")
async def list_institutions(request: Request, country: str | None = None):
    if (denied := _unauthorized(request)) is not None:
        return denied
    country = (country or "hr").upper()
    if not _COUNTRY_RE.match(country):
        return _error(400, "Invalid country choice.", f"{country} is not a valid choice.")
    return [i for i in _INSTITUTIONS if country in i.get("countries", [])]


@router.get("/api/v2/institutions/{institution_id}")
async def get_institution(institution_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    institution = _find_institution(institution_id)
    if institution is None:
        return _error(404, "Not found.", f"Institution '{institution_id}' not found.")
    return institution


# ── Requisitions ────────────────────────────────────────────────────────────────

@router.post("/api/v2/requisitions")
async def create_requisition(request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error(400, "Invalid request", "Tijelo zahtjeva nije ispravan JSON.")

    institution_id = body.get("institution_id")
    if not institution_id:
        return _error(400, "Invalid request", "Polje institution_id je obavezno.")

    max_days = body.get("max_historical_days")
    if max_days is not None and (not isinstance(max_days, int) or isinstance(max_days, bool) or max_days <= 0):
        return _error(400, "Invalid request", "Polje max_historical_days mora biti pozitivan cijeli broj.")

    institution = _find_institution(institution_id)
    if institution is None:
        # Bank-side validation error: passed through with the error under the field name.
        return JSONResponse(status_code=400, content={
            "institution_id": {
                "summary": f"Unknown Institution ID {institution_id}",
                "detail": "Get Institution IDs from /institutions/?country={$COUNTRY_CODE}",
            },
            "status_code": 400,
        })
    if max_days is not None and max_days > int(institution.get("transaction_total_days") or 90):
        return JSONResponse(status_code=400, content={
            "max_historical_days": {
                "summary": "Incorrect max_historical_days",
                "detail": f"max_historical_days must be > 0 and <= {institution['transaction_total_days']} for {institution_id}",
            },
            "status_code": 400,
        })

    pending = [r for r in _active_requisitions() if r["status"] == "CR"]
    available = _wallet["saldo_net"] - len(pending) * _REQUISITION_PRICE
    if available < _REQUISITION_PRICE:
        return _error(
            402, "Insufficient balance",
            "Nedovoljan raspoloživi saldo za kreiranje nove rekvizicije — nadoplatite saldo pa pokušajte ponovno.",
        )

    requisition_id = str(uuid.uuid4())
    link = str(request.url_for("ais_mock_bank_consent", requisition_id=requisition_id))
    requisition = _new_requisition(institution_id, link, requisition_id=requisition_id)
    _requisitions[requisition_id] = requisition
    return JSONResponse(status_code=201, content=_public_requisition(requisition))


@router.get("/api/v2/requisitions")
async def list_requisitions(request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    results = [_public_requisition(r) for r in _active_requisitions()]
    return {"count": len(results), "next": None, "previous": None, "results": results}


@router.get("/api/v2/requisitions/{requisition_id}")
async def get_requisition(requisition_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    requisition = _requisitions.get(requisition_id)
    if requisition is None or requisition.get("_deleted"):
        return _error(404, "Not found.", f"Requisition '{requisition_id}' not found.")
    return _public_requisition(requisition)


@router.delete("/api/v2/requisitions/{requisition_id}")
async def delete_requisition(requisition_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    requisition = _requisitions.get(requisition_id)
    if requisition is None:
        return _error(404, "Not found.", f"Requisition '{requisition_id}' not found.")
    # 204 also when it was already deleted, as the real API does.
    requisition["_deleted"] = True
    return Response(status_code=204)


# ── Bank consent page (stands in for the bank's `link`; not part of the real API) ──

@router.get("/mock/consent/{requisition_id}", name="ais_mock_bank_consent", include_in_schema=False)
async def bank_consent(requisition_id: str, action: str | None = None):
    requisition = _requisitions.get(requisition_id)
    if requisition is None or requisition.get("_deleted"):
        return HTMLResponse(status_code=404, content="<h1>Rekvizicija nije pronađena.</h1>")

    if action == "approve":
        _link_requisition(requisition)
    elif action == "reject" and requisition["status"] != "LN":
        requisition["status"] = "RJ"

    if requisition["status"] == "LN":
        return HTMLResponse("<h1>Banka je povezana.</h1><p>Možete zatvoriti ovaj prozor.</p>")
    if requisition["status"] == "RJ":
        return HTMLResponse("<h1>Pristup je odbijen.</h1><p>Možete zatvoriti ovaj prozor.</p>")

    institution = _find_institution(requisition["institution_id"]) or {"name": requisition["institution_id"]}
    return HTMLResponse(
        f"<h1>{institution['name']} (mock)</h1>"
        f"<p>Odobriti pristup računima za rekviziciju {requisition_id}?</p>"
        f'<p><a href="?action=approve">Odobri</a> &nbsp; <a href="?action=reject">Odbij</a></p>'
    )


# ── Accounts ────────────────────────────────────────────────────────────────────

@router.get("/api/v2/accounts/{account_id}")
async def get_account(account_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    account, error = _get_account(account_id)
    if error is not None:
        return error
    return _public_account(account)


@router.get("/api/v2/accounts/{account_id}/details")
async def get_account_details(account_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    account, error = _get_account(account_id)
    if error is not None:
        return error
    return {
        "account": {
            "resourceId": str(uuid.UUID(account["id"]).int)[:19],
            "iban": account["iban"],
            "currency": "xxx",
            "ownerName": account["owner_name"],
            "name": "regular account type 11",
            "product": "Multicurrency account",
            "cashAccountType": "TRAN",
            "status": "enabled",
        }
    }


@router.get("/api/v2/accounts/{account_id}/balances")
async def get_account_balances(account_id: str, request: Request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    account, error = _get_account(account_id)
    if error is not None:
        return error
    # Consistent with the transactions: opening balance plus everything booked/pending in the window.
    booked = _OPENING_BALANCE + sum(float(t["transactionAmount"]["amount"]) for t in _booked_transactions(account))
    expected = booked + sum(float(t["transactionAmount"]["amount"]) for t in _pending_transactions(account))
    return {
        "balances": [
            {"balanceAmount": {"amount": f"{booked:.2f}", "currency": "EUR"}, "balanceType": "interimAvailable", "creditLimitIncluded": False},
            {"balanceAmount": {"amount": f"{expected:.2f}", "currency": "EUR"}, "balanceType": "expected", "creditLimitIncluded": True},
        ]
    }


@router.get("/api/v2/accounts/{account_id}/transactions")
async def get_account_transactions(account_id: str, request: Request, date_from: str | None = None, date_to: str | None = None):
    if (denied := _unauthorized(request)) is not None:
        return denied

    for field, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                if not _DATE_RE.match(value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                return _error(400, "Invalid request", f"{field} mora biti u formatu yyyy-MM-dd.")

    account, error = _get_account(account_id)
    if error is not None:
        return error

    if date_from and date_to and date_from > date_to:
        return _error(
            400, "Incorrect date range",
            f"Starting date '{date_from}' is greater than end date '{date_to}'. "
            "When specifying date range, starting date must precede the end date",
        )

    def in_range(tx: dict) -> bool:
        day = tx.get("bookingDate") or tx.get("valueDate") or ""
        return (not date_from or day >= date_from) and (not date_to or day <= date_to)

    return {
        "transactions": {
            "booked": [tx for tx in _booked_transactions(account) if in_range(tx)],
            "pending": [tx for tx in _pending_transactions(account) if in_range(tx)],
        },
        "last_updated": _now(),
    }
