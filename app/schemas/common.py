"""Shared building blocks for the request/response schemas."""
from decimal import Decimal
from typing import Annotated, Any, List, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    PlainSerializer,
)


def blank_to_none(v: Any) -> Any:
    """Frontends often send "" for empty inputs; treat that as 'not provided'."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


def to_list(v: Any) -> Any:
    """Accept 'ADMIN' or ['ADMIN', 'STAFF'] and always return a list."""
    if v is None:
        return None
    if isinstance(v, str):
        return [v] if v.strip() else None
    return v


OptionalEmail = Annotated[Optional[EmailStr], BeforeValidator(blank_to_none)]
OptionalStr = Annotated[Optional[str], BeforeValidator(blank_to_none)]
RoleNames = Annotated[Optional[List[str]], BeforeValidator(to_list)]

# Money is a Decimal internally (matches Numeric(12, 2)) but is serialised to JSON
# as a number, so existing clients that expect `amount: 100.5` keep working.
Money = Annotated[
    Decimal,
    Field(max_digits=12, decimal_places=2),
    PlainSerializer(lambda v: float(v), return_type=float, when_used="json"),
]


class InputModel(BaseModel):
    """Base for request bodies: ignore unknown keys, allow field-name population."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AddressInput(InputModel):
    """Maps to the `address` table (owners hold address_id)."""

    street: OptionalStr = None
    city: OptionalStr = None
    state: OptionalStr = None
    zip_code: OptionalStr = Field(
        default=None, validation_alias=AliasChoices("zipCode", "zip_code", "postalCode")
    )
    country: str = "Kenya"


def _coerce_address(v: Any) -> Any:
    # Older clients send a plain string; treat it as the street line.
    if isinstance(v, str):
        return {"street": v} if v.strip() else None
    return v


AddressField = Annotated[Optional[AddressInput], BeforeValidator(_coerce_address)]
