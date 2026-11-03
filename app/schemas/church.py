from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, EmailStr, Field, model_validator

from app.enums import AccountStatus, PlatformRole, SubscriptionStatus
from app.schemas.common import AddressField, InputModel, OptionalEmail, OptionalStr


class ChurchSubscriptionInput(InputModel):
    plan: OptionalStr = None  # Plan.name, e.g. "BASIC"
    planId: OptionalStr = None  # or the Plan id
    status: Optional[SubscriptionStatus] = None
    isPaid: Optional[bool] = None
    isActive: Optional[bool] = None
    startDate: Optional[datetime] = None
    endDate: Optional[datetime] = None
    features: Optional[List[str]] = None


class ChurchCreateRequest(BaseModel):
    churchName: str
    denomination: str
    description: Optional[str] = ""
    churchLogoUrl: Optional[str] = ""
    establishedDate: Optional[str] = ""
    email: EmailStr
    phoneNumber: str
    website: Optional[str] = ""
    address: AddressField = None
    subscriptionPlan: Optional[str] = "BASIC"  # name of a Plan row (seeded from ChurchPlan)
    numberOfBranches: Optional[Union[str, int]] = "1"
    churchSize: Optional[str] = "100-500"


class ChurchUpdateRequest(BaseModel):
    organizationId: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    logo: Optional[str] = None
    denomination: Optional[str] = None
    description: Optional[str] = None
    email: Optional[str] = None
    phoneNumber: Optional[str] = None
    website: Optional[str] = None
    status: Optional[str] = None
    churchSize: Optional[str] = None
    numberOfBranches: Optional[Union[str, int]] = None
    address: AddressField = None
    subscription: Optional[ChurchSubscriptionInput] = None
    metadata: Optional[Dict[str, Any]] = None


class BranchCreateRequest(BaseModel):
    name: str
    churchId: str
    # Member id of the pastor (was a free-text name). Creates a PastorAssignment.
    pastorId: Optional[str] = None
    address: AddressField = None  # a plain string is still accepted (street line)
    phone: Optional[str] = ""
    email: Optional[str] = ""
    capacity: Optional[int] = 500
    services: Optional[List[str]] = None


class BranchUpdateRequest(BaseModel):
    name: Optional[str] = None
    churchId: Optional[str] = None
    pastorId: Optional[str] = None
    address: AddressField = None
    phone: Optional[str] = None
    email: Optional[str] = None
    capacity: Optional[int] = None
    status: Optional[str] = None
    services: Optional[List[str]] = None


class SuperAdminUserCreateRequest(InputModel):
    """
    Creates a login (User). Unless the user is a platform super-admin, a Member
    profile in the given church/branch is created too, holding the church role.
    """

    firstName: str
    lastName: str
    email: EmailStr
    phoneNumber: OptionalStr = None
    # SUPER_ADMIN = platform-wide admin (no church). Everyone else is NONE.
    platformRole: PlatformRole = PlatformRole.NONE
    # Church role name (a Role row), e.g. "ADMIN". Ignored for platform super-admins.
    role: Optional[str] = "ADMIN"
    churchId: Optional[str] = None
    branchId: Optional[str] = None
    # No default password any more; if omitted the service should send an invite.
    password: Optional[str] = Field(default=None, min_length=8)

    @model_validator(mode="after")
    def _scope(self):
        if self.platformRole == PlatformRole.SUPER_ADMIN:
            if self.churchId or self.branchId:
                raise ValueError("a platform super-admin must not have churchId/branchId")
        elif not (self.churchId and self.branchId):
            raise ValueError("churchId and branchId are required for church users")
        return self


class SuperAdminUserUpdateRequest(InputModel):
    firstName: Optional[str] = None  # updates the Member profile
    lastName: Optional[str] = None
    email: Optional[EmailStr] = None  # updates the User login
    phoneNumber: OptionalStr = None
    platformRole: Optional[PlatformRole] = None
    role: Optional[str] = None  # church role name
    status: Optional[AccountStatus] = None  # was a free string
    # churchId SELECTS which membership to update (a user can be in several churches);
    # it no longer moves the user. branchId transfers them within that church.
    churchId: Optional[str] = None
    branchId: Optional[str] = None


class GlobalSettingsUpdateRequest(BaseModel):
    siteName: Optional[str] = None
    supportEmail: Optional[str] = None
    defaultCurrency: Optional[str] = None
    trialPeriodDays: Optional[int] = None
    maintenanceMode: Optional[bool] = None
    allowNewRegistrations: Optional[bool] = None
    sessionTimeoutMinutes: Optional[int] = None
    emailSettings: Optional[Dict[str, Any]] = None
    securitySettings: Optional[Dict[str, Any]] = None
