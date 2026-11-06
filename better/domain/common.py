from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[AwareDatetime, AfterValidator(utc)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
DecimalOdds = Annotated[float, Field(gt=1, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, allow_inf_nan=False)
