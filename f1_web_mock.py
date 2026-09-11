import itertools
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger("f1_web_mock")

router = APIRouter()

_receipts: dict[int, dict] = {}
_id_seq = itertools.count(1)

_PLACEHOLDER_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tax_rate_display(rate: float) -> str:
    return f"PDV {rate:g}%"


def _payment_method_display(method: str) -> str:
    return {"Cash": "Gotovina", "Card": "Kartica", "Other": "Ostalo", "BankTransfer": "Transakcijski račun"}.get(method, method)


def _fiscal_status_display(status: str) -> str:
    return {
        "Pending": "Na čekanju",
        "Success": "Uspješno",
        "Failed": "Neuspješno",
        "RetryPending": "Ponovni pokušaj na čekanju",
        "LateDelivery": "Zakašnjela dostava",
        "NotRequired": "Nije potrebno",
    }.get(status, status)


def _build_items(raw_items: list[dict]) -> tuple[list[dict], list[dict], float, float]:
    items = []
    tax_totals: dict[float, dict] = {}
    total_amount = 0.0
    tax_amount = 0.0

    for idx, raw in enumerate(raw_items or [], start=1):
        quantity = raw.get("quantity") or 0.0
        unit_price = raw.get("unitPrice") or 0.0
        discount_amount = raw.get("discountAmount") or 0.0
        discount_percent = raw.get("discountPercent") or 0.0
        rate = raw.get("taxRate")
        rate = float(rate) if rate is not None else 0.0

        gross = quantity * unit_price
        line_total = round(gross - discount_amount - gross * (discount_percent / 100.0), 2)
        line_tax = round(line_total * (rate / 100.0), 2)

        items.append({
            "id": idx,
            "name": raw.get("name"),
            "description": raw.get("description"),
            "quantity": quantity,
            "unitPrice": unit_price,
            "taxRate": rate,
            "unitOfMeasure": raw.get("unitOfMeasure"),
            "discountAmount": discount_amount,
            "discountPercent": discount_percent,
            "totalPrice": line_total,
            "taxAmount": line_tax,
            "totalPriceWithTax": round(line_total + line_tax, 2),
        })

        bucket = tax_totals.setdefault(rate, {"baseAmount": 0.0, "taxAmount": 0.0})
        bucket["baseAmount"] = round(bucket["baseAmount"] + line_total, 2)
        bucket["taxAmount"] = round(bucket["taxAmount"] + line_tax, 2)

        total_amount = round(total_amount + line_total, 2)
        tax_amount = round(tax_amount + line_tax, 2)

    tax_breakdown = [
        {
            "taxRate": rate,
            "taxRateDisplay": _tax_rate_display(rate),
            "baseAmount": totals["baseAmount"],
            "taxAmount": totals["taxAmount"],
            "totalAmount": round(totals["baseAmount"] + totals["taxAmount"], 2),
        }
        for rate, totals in sorted(tax_totals.items())
    ]

    return items, tax_breakdown, total_amount, tax_amount


def _receipt_summary(r: dict) -> dict:
    return {
        "id": r["id"],
        "receiptNumber": r["receiptNumber"],
        "formattedReceiptNumber": r["formattedReceiptNumber"],
        "issueDateTime": r["issueDateTime"],
        "grandTotal": r["grandTotal"],
        "paymentMethod": r["paymentMethod"],
        "paymentMethodDisplay": r["paymentMethodDisplay"],
        "fiscalStatus": r["fiscalStatus"],
        "fiscalStatusDisplay": r["fiscalStatusDisplay"],
        "isFiscalized": r["isFiscalized"],
        "itemCount": len(r["items"]),
        "receiptType": r["receiptType"],
        "isCreditNote": r["isCreditNote"],
        "hasCreditNote": r["hasCreditNote"],
        "referencedReceiptFormattedNumber": r["referencedReceiptFormattedNumber"],
        "canDeleteCreditNote": r["canDeleteCreditNote"],
        "canFiscalize": r["canFiscalize"],
        "lastError": r["lastError"],
        "hasRestrictiveError": r["hasRestrictiveError"],
        "emailSentCount": r["emailSentCount"],
        "hasZki": r["zki"] is not None,
    }


def _get_receipt_or_404(receipt_id: int) -> dict:
    receipt = _receipts.get(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"Receipt {receipt_id} not found")
    return receipt


def _create_receipt(body: dict) -> dict:
    receipt_id = next(_id_seq)
    items, tax_breakdown, total_amount, tax_amount = _build_items(body.get("items") or [])
    payment_method = body.get("paymentMethod") or "Cash"
    receipt_type = body.get("receiptType") or "Standard"

    receipt = {
        "id": receipt_id,
        "businessId": body.get("businessId"),
        "businessName": "Mock Business d.o.o.",
        "receiptNumber": receipt_id,
        "formattedReceiptNumber": f"{receipt_id:06d}",
        "issueDateTime": body.get("issueDateTime") or _now(),
        "paymentMethod": payment_method,
        "paymentMethodDisplay": _payment_method_display(payment_method),
        "receiptType": receipt_type,
        "totalAmount": total_amount,
        "taxAmount": tax_amount,
        "grandTotal": round(total_amount + tax_amount, 2),
        "zki": None,
        "jir": None,
        "fiscalStatus": "Pending",
        "fiscalStatusDisplay": _fiscal_status_display("Pending"),
        "isFiscalized": False,
        "canRetry": False,
        "canFiscalize": True,
        "isLateDelivery": False,
        "retryCount": 0,
        "lastRetryAt": None,
        "lastError": None,
        "hasRestrictiveError": False,
        "fiscalizedAt": None,
        "operatorOib": body.get("operatorOib"),
        "operatorLabel": body.get("operatorOib"),
        "businessUnitLabel": "Mock Business Unit",
        "cashRegisterLabel": str(body.get("cashRegisterId") or ""),
        "businessOib": None,
        "notes": body.get("notes"),
        "paymentDueDate": body.get("paymentDueDate"),
        "buyerName": body.get("buyerName"),
        "buyerOib": body.get("buyerOib"),
        "buyerAddress": body.get("buyerAddress"),
        "buyerCity": body.get("buyerCity"),
        "buyerPostalCode": body.get("buyerPostalCode"),
        "buyerEmail": body.get("buyerEmail"),
        "isB2BInvoice": bool(body.get("buyerOib")),
        "referencedReceiptId": None,
        "referencedReceiptJir": None,
        "referencedReceiptFormattedNumber": None,
        "isCreditNote": False,
        "hasCreditNote": False,
        "canDeleteCreditNote": False,
        "items": items,
        "taxBreakdown": tax_breakdown,
        "marginAmount": body.get("marginAmount"),
        "vatExemptAmount": body.get("vatExemptAmount"),
        "nonTaxableAmount": body.get("nonTaxableAmount"),
        "consumptionTaxes": body.get("consumptionTaxes") or [],
        "otherTaxes": body.get("otherTaxes") or [],
        "surcharges": body.get("surcharges") or [],
        "paragonReceiptNumber": body.get("paragonReceiptNumber"),
        "specialPurpose": body.get("specialPurpose"),
        "emailSentCount": 0,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    _receipts[receipt_id] = receipt
    return receipt


def _fiscalize_receipt(receipt: dict) -> dict:
    receipt["fiscalStatus"] = "Success"
    receipt["fiscalStatusDisplay"] = _fiscal_status_display("Success")
    receipt["isFiscalized"] = True
    receipt["canFiscalize"] = False
    receipt["canRetry"] = False
    receipt["jir"] = str(uuid.uuid4())
    receipt["zki"] = uuid.uuid4().hex
    receipt["fiscalizedAt"] = _now()
    receipt["updatedAt"] = _now()
    return receipt


def _fiscalization_result(receipt: dict) -> dict:
    return {
        "isSuccess": True,
        "jir": receipt["jir"],
        "zki": receipt["zki"],
        "errorMessage": None,
        "errorCode": None,
        "canRetry": False,
        "receipt": receipt,
    }


# ── Fixed single-segment routes (must be registered before /{id}) ──────────────

@router.get("/api/Receipts/statistics")
async def get_statistics():
    values = list(_receipts.values())
    fiscalized = [r for r in values if r["isFiscalized"]]
    pending = [r for r in values if r["fiscalStatus"] in ("Pending", "RetryPending")]
    failed = [r for r in values if r["fiscalStatus"] == "Failed"]
    today = _now()[:10]
    today_receipts = [r for r in values if r["issueDateTime"][:10] == today]

    return {
        "totalCount": len(values),
        "fiscalizedCount": len(fiscalized),
        "pendingCount": len(pending),
        "failedCount": len(failed),
        "totalRevenue": round(sum(r["grandTotal"] for r in values), 2),
        "totalTax": round(sum(r["taxAmount"] for r in values), 2),
        "todayRevenue": round(sum(r["grandTotal"] for r in today_receipts), 2),
        "todayCount": len(today_receipts),
        "averageReceiptAmount": round(sum(r["grandTotal"] for r in values) / len(values), 2) if values else 0.0,
    }


@router.get("/api/Receipts/pending-retry")
async def get_pending_retry(maxRetryCount: int | None = None):
    values = [r for r in _receipts.values() if r["fiscalStatus"] in ("Failed", "RetryPending")]
    if maxRetryCount is not None:
        values = [r for r in values if r["retryCount"] <= maxRetryCount]
    return [_receipt_summary(r) for r in values]


@router.get("/api/Receipts/by-date-range")
async def get_by_date_range(startDate: str | None = None, endDate: str | None = None):
    values = list(_receipts.values())
    if startDate:
        values = [r for r in values if r["issueDateTime"] >= startDate]
    if endDate:
        values = [r for r in values if r["issueDateTime"] <= endDate]
    return [_receipt_summary(r) for r in values]


@router.get("/api/Receipts/summary-report")
async def get_summary_report(startDate: str | None = None, endDate: str | None = None, reportTitle: str | None = None):
    return Response(content=_PLACEHOLDER_PDF, media_type="application/pdf")


@router.post("/api/Receipts/validate")
async def validate_receipt(request: Request):
    body = await request.json()
    errors = []
    if not body.get("items"):
        errors.append({"propertyName": "items", "errorMessage": "At least one item is required"})
    if not body.get("businessId"):
        errors.append({"propertyName": "businessId", "errorMessage": "businessId is required"})
    return {"isValid": len(errors) == 0, "errors": errors}


@router.post("/api/Receipts/create-and-fiscalize")
async def create_and_fiscalize(request: Request):
    body = await request.json()
    receipt = _create_receipt(body)
    if body.get("autoFiscalize", True):
        _fiscalize_receipt(receipt)
    return _fiscalization_result(receipt)


@router.get("/api/Receipts/by-status/{status}")
async def get_by_status(status: str):
    return [_receipt_summary(r) for r in _receipts.values() if r["fiscalStatus"] == status]


# ── Collection routes ───────────────────────────────────────────────────────────

@router.get("/api/Receipts")
async def list_receipts(request: Request):
    q = request.query_params
    values = list(_receipts.values())

    fiscal_status = q.get("fiscalStatus")
    if fiscal_status:
        values = [r for r in values if r["fiscalStatus"] == fiscal_status]

    payment_method = q.get("paymentMethod")
    if payment_method:
        values = [r for r in values if r["paymentMethod"] == payment_method]

    search_term = q.get("searchTerm")
    if search_term:
        needle = search_term.lower()
        values = [
            r for r in values
            if needle in (r["buyerName"] or "").lower() or needle in r["formattedReceiptNumber"].lower()
        ]

    sort_by = q.get("sortBy") or "id"
    descending = (q.get("sortDescending") or "false").lower() == "true"
    values.sort(key=lambda r: r.get(sort_by, r["id"]), reverse=descending)

    page = int(q.get("page") or 1)
    page_size = int(q.get("pageSize") or 20)
    total_count = len(values)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    start = (page - 1) * page_size
    page_items = values[start:start + page_size]

    return {
        "items": [_receipt_summary(r) for r in page_items],
        "totalCount": total_count,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "hasPreviousPage": page > 1,
        "hasNextPage": page < total_pages,
    }


@router.post("/api/Receipts")
async def create_receipt(request: Request):
    body = await request.json()
    receipt = _create_receipt(body)
    if body.get("autoFiscalize"):
        _fiscalize_receipt(receipt)
    return receipt


# ── Single-receipt routes ───────────────────────────────────────────────────────

@router.get("/api/Receipts/{receipt_id}")
async def get_receipt(receipt_id: int):
    return _get_receipt_or_404(receipt_id)


@router.put("/api/Receipts/{receipt_id}")
async def update_receipt(receipt_id: int, request: Request):
    receipt = _get_receipt_or_404(receipt_id)
    body = await request.json()

    items, tax_breakdown, total_amount, tax_amount = _build_items(body.get("items") or receipt["items"])
    receipt.update({
        "issueDateTime": body.get("issueDateTime", receipt["issueDateTime"]),
        "paymentMethod": body.get("paymentMethod", receipt["paymentMethod"]),
        "paymentMethodDisplay": _payment_method_display(body.get("paymentMethod", receipt["paymentMethod"])),
        "receiptType": body.get("receiptType", receipt["receiptType"]),
        "notes": body.get("notes", receipt["notes"]),
        "paymentDueDate": body.get("paymentDueDate", receipt["paymentDueDate"]),
        "buyerName": body.get("buyerName", receipt["buyerName"]),
        "buyerOib": body.get("buyerOib", receipt["buyerOib"]),
        "buyerAddress": body.get("buyerAddress", receipt["buyerAddress"]),
        "buyerCity": body.get("buyerCity", receipt["buyerCity"]),
        "buyerPostalCode": body.get("buyerPostalCode", receipt["buyerPostalCode"]),
        "buyerEmail": body.get("buyerEmail", receipt["buyerEmail"]),
        "items": items,
        "taxBreakdown": tax_breakdown,
        "totalAmount": total_amount,
        "taxAmount": tax_amount,
        "grandTotal": round(total_amount + tax_amount, 2),
        "marginAmount": body.get("marginAmount", receipt["marginAmount"]),
        "vatExemptAmount": body.get("vatExemptAmount", receipt["vatExemptAmount"]),
        "nonTaxableAmount": body.get("nonTaxableAmount", receipt["nonTaxableAmount"]),
        "paragonReceiptNumber": body.get("paragonReceiptNumber", receipt["paragonReceiptNumber"]),
        "specialPurpose": body.get("specialPurpose", receipt["specialPurpose"]),
        "consumptionTaxes": body.get("consumptionTaxes", receipt["consumptionTaxes"]),
        "otherTaxes": body.get("otherTaxes", receipt["otherTaxes"]),
        "surcharges": body.get("surcharges", receipt["surcharges"]),
        "updatedAt": _now(),
    })
    return receipt


@router.post("/api/Receipts/{receipt_id}/fiscalize")
async def fiscalize(receipt_id: int):
    receipt = _get_receipt_or_404(receipt_id)
    if receipt["isFiscalized"]:
        return {
            "isSuccess": False,
            "jir": None,
            "zki": None,
            "errorMessage": "Receipt is already fiscalized",
            "errorCode": "ALREADY_FISCALIZED",
            "canRetry": False,
            "receipt": receipt,
        }
    _fiscalize_receipt(receipt)
    return _fiscalization_result(receipt)


@router.post("/api/Receipts/{receipt_id}/retry-fiscalization")
async def retry_fiscalization(receipt_id: int):
    receipt = _get_receipt_or_404(receipt_id)
    receipt["retryCount"] += 1
    receipt["lastRetryAt"] = _now()
    _fiscalize_receipt(receipt)
    return _fiscalization_result(receipt)


@router.post("/api/Receipts/{receipt_id}/change-payment-method")
async def change_payment_method(receipt_id: int, request: Request):
    receipt = _get_receipt_or_404(receipt_id)
    body = await request.json()
    new_method = body.get("newPaymentMethod")
    if new_method:
        receipt["paymentMethod"] = new_method
        receipt["paymentMethodDisplay"] = _payment_method_display(new_method)
        receipt["updatedAt"] = _now()
    return {
        "isSuccess": True,
        "jir": receipt["jir"],
        "zki": receipt["zki"],
        "errorMessage": None,
        "errorCode": None,
        "canRetry": False,
        "receipt": receipt,
    }


@router.get("/api/Receipts/{receipt_id}/fiscalization-readiness")
async def fiscalization_readiness(receipt_id: int):
    receipt = _get_receipt_or_404(receipt_id)
    issues = [] if receipt["items"] else ["Receipt has no items"]
    return {
        "isReady": not receipt["isFiscalized"] and bool(receipt["items"]),
        "issues": issues,
        "warnings": [],
        "isBusinessConfigured": True,
        "isCertificateValid": True,
        "hasItems": bool(receipt["items"]),
        "isAlreadyFiscalized": receipt["isFiscalized"],
    }


@router.post("/api/Receipts/{receipt_id}/storno")
async def storno(receipt_id: int):
    original = _get_receipt_or_404(receipt_id)
    if not original["isFiscalized"]:
        return {
            "isSuccess": False,
            "jir": None,
            "zki": None,
            "errorMessage": "Only fiscalized receipts can be storno-ed",
            "errorCode": "NOT_FISCALIZED",
            "canRetry": False,
            "receipt": original,
        }

    credit_note_id = next(_id_seq)
    credit_note = dict(original)
    credit_note.update({
        "id": credit_note_id,
        "receiptNumber": credit_note_id,
        "formattedReceiptNumber": f"{credit_note_id:06d}",
        "receiptType": "CreditNote",
        "issueDateTime": _now(),
        "totalAmount": -original["totalAmount"],
        "taxAmount": -original["taxAmount"],
        "grandTotal": -original["grandTotal"],
        "zki": None,
        "jir": None,
        "fiscalStatus": "Pending",
        "fiscalStatusDisplay": _fiscal_status_display("Pending"),
        "isFiscalized": False,
        "canFiscalize": True,
        "referencedReceiptId": original["id"],
        "referencedReceiptJir": original["jir"],
        "referencedReceiptFormattedNumber": original["formattedReceiptNumber"],
        "isCreditNote": True,
        "hasCreditNote": False,
        "canDeleteCreditNote": False,
        "createdAt": _now(),
        "updatedAt": _now(),
    })
    _fiscalize_receipt(credit_note)
    _receipts[credit_note_id] = credit_note

    original["hasCreditNote"] = True
    original["canDeleteCreditNote"] = True
    original["updatedAt"] = _now()

    return _fiscalization_result(credit_note)


@router.get("/api/Receipts/{receipt_id}/credit-note-readiness")
async def credit_note_readiness(receipt_id: int):
    receipt = _get_receipt_or_404(receipt_id)
    if not receipt["isFiscalized"]:
        return {"canCreate": False, "reason": "Receipt is not fiscalized", "isFiscalized": False, "hasExistingCreditNote": receipt["hasCreditNote"]}
    if receipt["hasCreditNote"]:
        return {"canCreate": False, "reason": "Receipt already has a credit note", "isFiscalized": True, "hasExistingCreditNote": True}
    return {"canCreate": True, "reason": None, "isFiscalized": True, "hasExistingCreditNote": False}


@router.get("/api/Receipts/{receipt_id}/pdf")
async def get_receipt_pdf(receipt_id: int, format: str | None = None):
    _get_receipt_or_404(receipt_id)
    return Response(content=_PLACEHOLDER_PDF, media_type="application/pdf")


@router.get("/api/Receipts/{receipt_id}/pdf/a4")
async def get_receipt_pdf_a4(receipt_id: int):
    _get_receipt_or_404(receipt_id)
    return Response(content=_PLACEHOLDER_PDF, media_type="application/pdf")


@router.get("/api/Receipts/{receipt_id}/pdf-copy")
async def get_receipt_pdf_copy(receipt_id: int, copyNumber: int | None = None):
    _get_receipt_or_404(receipt_id)
    return Response(content=_PLACEHOLDER_PDF, media_type="application/pdf")
