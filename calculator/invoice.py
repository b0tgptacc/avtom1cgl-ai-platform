"""Auditable Decimal invoice calculation."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


MONEY_QUANTUM = Decimal("0.01")


class InvoiceValidationError(ValueError):
    """Base class for invalid invoice inputs."""


class InvalidDiscountPercentError(InvoiceValidationError):
    """Raised when the discount percentage is outside 0 through 100."""


class InvalidQuantityError(InvoiceValidationError):
    """Raised when quantity is not a positive finite Decimal."""


class InvalidUnitPriceError(InvoiceValidationError):
    """Raised when unit_price is not a non-negative finite Decimal."""


class InvalidVatPercentError(InvoiceValidationError):
    """Raised when vat_percent is not a non-negative finite Decimal."""


class InvalidCurrencyError(InvoiceValidationError):
    """Raised when currency is not a non-empty label."""


@dataclass(frozen=True)
class InvoiceTotals:
    """The named monetary values that form an invoice calculation."""

    gross_amount: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    currency: str


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _is_finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def calculate_invoice(
    *,
    quantity: Decimal,
    unit_price: Decimal,
    discount_percent: Decimal,
    vat_percent: Decimal,
    currency: str,
) -> InvoiceTotals:
    """Calculate rounded invoice amounts from Decimal inputs."""
    if not _is_finite_decimal(quantity) or quantity <= Decimal("0"):
        raise InvalidQuantityError("quantity must be a positive finite Decimal")
    if not _is_finite_decimal(unit_price) or unit_price < Decimal("0"):
        raise InvalidUnitPriceError(
            "unit_price must be a non-negative finite Decimal"
        )
    if (
        not _is_finite_decimal(discount_percent)
        or not Decimal("0") <= discount_percent <= Decimal("100")
    ):
        raise InvalidDiscountPercentError(
            "discount_percent must be a finite Decimal between 0 and 100 inclusive"
        )
    if not _is_finite_decimal(vat_percent) or vat_percent < Decimal("0"):
        raise InvalidVatPercentError(
            "vat_percent must be a non-negative finite Decimal"
        )
    if not isinstance(currency, str) or not currency.strip():
        raise InvalidCurrencyError("currency must be a non-empty label")

    gross_amount = _money(quantity * unit_price)
    discount_amount = _money(gross_amount * discount_percent / Decimal("100"))
    net_amount = gross_amount - discount_amount
    vat_amount = _money(net_amount * vat_percent / Decimal("100"))
    total_amount = net_amount + vat_amount
    return InvoiceTotals(
        gross_amount=gross_amount,
        discount_amount=discount_amount,
        net_amount=net_amount,
        vat_amount=vat_amount,
        total_amount=total_amount,
        currency=currency,
    )
