from datetime import date, datetime
from typing import List, Optional

from pydantic import AliasChoices, EmailStr, Field, field_validator, model_validator

from app.enums import (
    AdminAccessLevel,
    FollowUpStatus,
    Gender,
    HouseholdRole,
    HowDidYouHear,
    MaritalStatus,
    MemberType,
    MembershipStatus,
)
from app.schemas.common import (
    AddressField,
    InputModel,
    OptionalEmail,
    OptionalStr,
    RoleNames,
)


# --------------------------------------------------------------------------- #
# Nested inputs (one per Member-scoped table). Replace the old Dict[str, Any].
# --------------------------------------------------------------------------- #
class EmergencyContactInput(InputModel):
    full_name: str = Field(validation_alias=AliasChoices("fullName", "full_name", "name"))
    phone_number: str = Field(
        validation_alias=AliasChoices("phoneNumber", "phone_number", "phone")
    )
    relationship_type: str = Field(
        validation_alias=AliasChoices("relationshipType", "relationship_type", "relationship")
    )
    email: OptionalEmail = None
    address: OptionalStr = None
    notes: OptionalStr = None


class MemberDetailsInput(InputModel):
    membership_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("membershipDate", "membership_date")
    )
    baptism_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("baptismDate", "baptism_date")
    )
    marital_status: Optional[MaritalStatus] = Field(
        default=None, validation_alias=AliasChoices("maritalStatus", "marital_status")
    )
    occupation: OptionalStr = None


class VisitorDetailsInput(InputModel):
    visit_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("visitDate", "visit_date")
    )  # server defaults to "now"
    how_did_you_hear: HowDidYouHear = Field(
        default=HowDidYouHear.OTHER,
        validation_alias=AliasChoices("howDidYouHear", "how_did_you_hear"),
    )
    follow_up_status: FollowUpStatus = Field(
        default=FollowUpStatus.PENDING,
        validation_alias=AliasChoices("followUpStatus", "follow_up_status"),
    )
    follow_up_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("followUpDate", "follow_up_date")
    )
    follow_up_notes: OptionalStr = Field(
        default=None, validation_alias=AliasChoices("followUpNotes", "follow_up_notes")
    )
    interested_in_membership: bool = Field(
        default=False,
        validation_alias=AliasChoices("interestedInMembership", "interested_in_membership"),
    )
    # Member id of whoever invited them (was a free-text name).
    invited_by_member_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("invitedByMemberId", "invited_by_member_id")
    )


class PastorDetailsInput(InputModel):
    ordination_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("ordinationDate", "ordination_date")
    )
    biography: OptionalStr = None


class BishopDetailsInput(InputModel):
    appointment_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("appointmentDate", "appointment_date")
    )
    jurisdiction_area: OptionalStr = Field(
        default=None, validation_alias=AliasChoices("jurisdictionArea", "jurisdiction_area")
    )
    biography: OptionalStr = None


class StaffDetailsInput(InputModel):
    job_title: str = Field(validation_alias=AliasChoices("jobTitle", "job_title"))
    department: str
    start_date: datetime = Field(validation_alias=AliasChoices("startDate", "start_date"))
    end_date: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("endDate", "end_date")
    )


class AdminDetailsInput(InputModel):
    access_level: AdminAccessLevel = Field(
        default=AdminAccessLevel.NATIONAL,
        validation_alias=AliasChoices("accessLevel", "access_level"),
    )


class VolunteerDetailsInput(InputModel):
    hours_contributed: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("hoursContributed", "hours_contributed")
    )


class GuardianInput(InputModel):
    """Links a child to an existing guardian member."""

    guardian_member_id: str = Field(
        validation_alias=AliasChoices("guardianMemberId", "guardian_member_id")
    )
    relationship_type: str = Field(
        validation_alias=AliasChoices("relationshipType", "relationship_type")
    )
    is_primary: bool = Field(default=False, validation_alias=AliasChoices("isPrimary", "is_primary"))
    can_pickup: bool = Field(default=True, validation_alias=AliasChoices("canPickup", "can_pickup"))


def _wrap_single_contact(v):
    # Old clients sent one dict (`emergencyDetails`); a member can now have several.
    return [v] if isinstance(v, dict) else v


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
class AdminMemberCreateRequest(InputModel):
    """
    Creates a Member (a church-scoped person). A login (User) is optional:
    visitors and children normally have none.
    """

    organizationId: Optional[str] = None  # church id; taken from the session if omitted
    # Exactly one branch per member.
    branchId: str

    firstName: str
    lastName: str
    memberType: MemberType = MemberType.MEMBER
    dateOfBirth: Optional[date] = None
    gender: Optional[Gender] = None  # was defaulted to "MALE"; no longer assumed
    email: OptionalEmail = None
    phoneNumber: OptionalStr = None
    position: Optional[str] = "member"
    # Role names (church Role rows). Old `role` (string or list) is accepted.
    roles: RoleNames = Field(default=None, validation_alias=AliasChoices("roles", "role"))

    householdId: Optional[str] = None
    householdRole: Optional[HouseholdRole] = None
    membershipNumber: OptionalStr = None
    joinedAt: Optional[date] = None
    address: AddressField = None

    # Login (only when the person should be able to sign in)
    createLogin: bool = False
    # No default password any more. If createLogin is true and this is empty,
    # the service should send an invitation / set-password email instead.
    password: Optional[str] = Field(default=None, min_length=8)

    emergencyContacts: Optional[List[EmergencyContactInput]] = Field(
        default=None, validation_alias=AliasChoices("emergencyContacts", "emergencyDetails")
    )
    visitorDetails: Optional[VisitorDetailsInput] = None
    guardians: Optional[List[GuardianInput]] = None  # for children

    @field_validator("emergencyContacts", mode="before")
    @classmethod
    def _contacts(cls, v):
        return _wrap_single_contact(v)

    @model_validator(mode="after")
    def _rules(self):
        if self.createLogin and not self.email:
            raise ValueError("email is required when createLogin is true")
        if self.password and not self.createLogin:
            raise ValueError("password can only be set when createLogin is true")
        if self.memberType == MemberType.CHILD and not self.dateOfBirth:
            raise ValueError("dateOfBirth is required for a child")
        return self


class AdminMemberUpdateRequest(InputModel):
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    memberType: Optional[MemberType] = None  # e.g. VISITOR -> MEMBER on conversion
    dateOfBirth: Optional[date] = None
    gender: Optional[Gender] = None
    email: OptionalEmail = None
    phoneNumber: OptionalStr = None
    position: Optional[str] = None
    # If provided, REPLACES the member's role assignments.
    roles: RoleNames = Field(default=None, validation_alias=AliasChoices("roles", "role"))
    status: Optional[MembershipStatus] = None  # was a free string
    # Transfers the member to another branch (a member has exactly one).
    branchId: Optional[str] = None
    householdId: Optional[str] = None
    householdRole: Optional[HouseholdRole] = None
    membershipNumber: OptionalStr = None
    joinedAt: Optional[date] = None
    profilePictureUrl: Optional[str] = None  # stored on Member.photo_url
    address: AddressField = None

    # If provided, REPLACES the member's emergency contacts.
    emergencyContacts: Optional[List[EmergencyContactInput]] = Field(
        default=None, validation_alias=AliasChoices("emergencyContacts", "emergencyDetails")
    )
    adminDetails: Optional[AdminDetailsInput] = None
    staffDetails: Optional[StaffDetailsInput] = None
    volunteerDetails: Optional[VolunteerDetailsInput] = None
    memberDetails: Optional[MemberDetailsInput] = None
    visitorDetails: Optional[VisitorDetailsInput] = None
    pastorDetails: Optional[PastorDetailsInput] = None
    bishopDetails: Optional[BishopDetailsInput] = None

    @field_validator("emergencyContacts", mode="before")
    @classmethod
    def _contacts(cls, v):
        return _wrap_single_contact(v)


# --------------------------------------------------------------------------- #
# Branches and branch membership
# --------------------------------------------------------------------------- #
class AdminBranchCreateRequest(InputModel):
    organizationId: Optional[str] = None
    name: Optional[str] = None
    branchName: Optional[str] = None
    pastorId: Optional[str] = None  # Member id of a pastor (creates a PastorAssignment)
    description: Optional[str] = None
    email: OptionalEmail = None
    phone: OptionalStr = None
    phoneNumber: OptionalStr = None
    capacity: Optional[int] = Field(default=500, ge=0)  # numeric strings are coerced
    establishedDate: Optional[str] = None
    address: AddressField = None

    @model_validator(mode="after")
    def _needs_name(self):
        if not (self.name or self.branchName):
            raise ValueError("name or branchName is required")
        return self


class AdminBranchUpdateRequest(InputModel):
    name: Optional[str] = None
    branchName: Optional[str] = None
    pastorId: Optional[str] = None
    description: Optional[str] = None
    email: OptionalEmail = None
    phone: OptionalStr = None
    phoneNumber: OptionalStr = None
    capacity: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None
    establishedDate: Optional[str] = None
    address: AddressField = None


class BranchMemberAssignRequest(InputModel):
    """
    Puts a member in this branch. A member belongs to exactly ONE branch, so this
    TRANSFERS them (sets Member.branch_id); there is no branch_member table any more.
    """

    memberId: Optional[str] = None
    userId: Optional[str] = None  # fallback: resolved to the member of this church
    # Branch-scoped role assignments (MemberRoleAssignment with branch_id set).
    roles: RoleNames = Field(default=None, validation_alias=AliasChoices("roles", "role"))
    position: Optional[str] = None

    @model_validator(mode="after")
    def _needs_target(self):
        if not (self.memberId or self.userId):
            raise ValueError("memberId or userId is required")
        return self


# Old name kept so existing imports keep working.
BranchMemberAddRequest = BranchMemberAssignRequest
