import re
from typing import List, Optional, Union

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import MemberType
from app.models.auth import MemberRoleAssignment, Role
from app.models.church import Church
from app.models.member import Member


def generate_church_code(
    church_name: str, slug: Optional[str] = None, metadata: Optional[dict] = None
) -> str:
    """
    Generate a clean uppercase church code (e.g. ACEM for Apostolic Church Evangelistic Ministry).
    Prioritizes metadata_json.get('churchCode') or metadata_json.get('code') or metadata_json.get('abbreviation').
    Otherwise extracts initials from significant words in the church name.
    """
    if metadata and isinstance(metadata, dict):
        custom = (
            metadata.get("churchCode")
            or metadata.get("code")
            or metadata.get("abbreviation")
        )
        if custom and isinstance(custom, str) and custom.strip():
            cleaned = re.sub(r"[^A-Za-z0-9]", "", custom).upper()
            if cleaned:
                return cleaned[:8]

    if not church_name:
        return "CH"

    # Remove non-alphanumeric punctuation
    clean_name = re.sub(r"[^\w\s]", "", church_name).strip()
    words = [w for w in clean_name.split() if w]

    stop_words = {"OF", "AND", "THE", "FOR", "IN", "AT", "TO", "A", "AN"}
    filtered_words = [w for w in words if w.upper() not in stop_words] or words

    if len(filtered_words) >= 2:
        code = "".join(w[0].upper() for w in filtered_words)
    elif filtered_words:
        code = filtered_words[0][:4].upper()
    else:
        code = "CH"

    code = re.sub(r"[^A-Za-z0-9]", "", code)
    if len(code) < 2:
        code = (re.sub(r"[^A-Za-z0-9]", "", church_name)[:4] or "CH").upper()

    return code[:8]


def get_role_type_code(
    roles: Optional[Union[List[str], str]] = None,
    position: Optional[str] = None,
    member_type: Optional[MemberType] = None,
) -> str:
    """
    Determine role type code for member identifier:
    - VIS: Visitor / Guest            (member_type VISITOR)
    - CHD: Child                      (member_type CHILD)
    - ADMIN: Church Administrator / Owner / Admin
    - PST: Pastor / Bishop / Reverend
    - STF: Employed / Ministry Staff
    - VOL: Ministry Volunteer
    - MEM: Church Member
    `roles` are Role names (from MemberRoleAssignment), not the removed Member.roles column.
    """
    # member_type wins: visitors and children are identified by what they are.
    if member_type == MemberType.VISITOR:
        return "VIS"
    if member_type == MemberType.CHILD:
        return "CHD"

    if isinstance(roles, str):
        roles_list = [roles]
    elif roles:
        roles_list = list(roles)
    else:
        roles_list = []

    upper_roles = [r.upper() for r in roles_list]
    pos = (position or "").upper()

    if (
        any("ADMIN" in r for r in upper_roles)
        or "ADMIN" in pos
        or any("OWNER" in r for r in upper_roles)
    ):
        return "ADMIN"
    if any(p in pos for p in ["PASTOR", "BISHOP", "REVEREND"]) or any(
        p in r for r in upper_roles for p in ["PASTOR", "BISHOP"]
    ):
        return "PST"
    if any("STAFF" in r for r in upper_roles) or "STAFF" in pos:
        return "STF"
    if any("VOLUNTEER" in r for r in upper_roles) or "VOLUNTEER" in pos:
        return "VOL"
    if any("VISITOR" in r for r in upper_roles) or "VISITOR" in pos:
        return "VIS"
    return "MEM"


async def get_member_role_names(session: AsyncSession, member_id: str) -> List[str]:
    """Role names held by a member (replaces the old Member.roles array)."""
    stmt = (
        select(Role.name)
        .join(MemberRoleAssignment, MemberRoleAssignment.role_id == Role.id)
        .where(MemberRoleAssignment.member_id == member_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def generate_unique_membership_number(
    session: AsyncSession,
    church_id: str,
    church: Optional[Church] = None,
    roles: Optional[Union[List[str], str]] = None,
    position: Optional[str] = None,
    member_type: Optional[MemberType] = None,
) -> str:
    """
    Generates a unique membership number formatted as:
    {CHURCH_CODE}-{ROLE_TYPE}-{SEQUENCE_NUMBER}
    Example: ACEM-ADMIN-001, ACEM-MEM-001, ACEM-VOL-001

    Concurrency: member has a UNIQUE (church_id, membership_number) constraint, so two
    requests picking the same "next" number would make one of them fail. A Postgres
    advisory lock serialises number generation per church. It is held until the current
    TRANSACTION ends, so call this in the same session/transaction that inserts the
    member and commit afterwards.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"membership_number:{church_id}"},
    )

    if not church:
        church_stmt = select(Church).where(Church.id == church_id)
        church = (await session.execute(church_stmt)).scalar_one_or_none()

    church_name = church.name if church else "Church"
    slug = church.slug if church else None
    metadata = church.metadata_json if church else None

    church_code = generate_church_code(church_name, slug=slug, metadata=metadata)
    type_code = get_role_type_code(
        roles=roles, position=position, member_type=member_type
    )
    prefix = f"{church_code}-{type_code}-"

    # Query all existing membership numbers in this church matching the prefix.
    # Soft-deleted members are deliberately INCLUDED: their numbers stay reserved
    # (the unique constraint still applies to them), so numbers are never reused.
    stmt = select(Member.membership_number).where(
        Member.church_id == church_id, Member.membership_number.like(f"{prefix}%")
    )
    existing_numbers = (await session.execute(stmt)).scalars().all()

    max_num = 0
    for num_str in existing_numbers:
        if num_str and num_str.startswith(prefix):
            suffix = num_str[len(prefix) :]
            try:
                val = int(suffix)
                if val > max_num:
                    max_num = val
            except ValueError:
                pass

    next_num = max_num + 1
    return f"{prefix}{next_num:03d}"


async def ensure_member_membership_number(
    session: AsyncSession, member: Member, church: Optional[Church] = None
) -> str:
    """
    Ensures a member has a membership_number. If missing, generates one,
    persists it, and returns it.
    """
    if member.membership_number:
        return member.membership_number

    # Roles now live in MemberRoleAssignment -> Role (Member.roles no longer exists).
    role_names = await get_member_role_names(session, member.id)

    number = await generate_unique_membership_number(
        session=session,
        church_id=member.church_id,
        church=church,
        roles=role_names,
        position=member.position,
        member_type=member.member_type,
    )
    member.membership_number = number
    session.add(member)
    await session.commit()  # also releases the advisory lock
    await session.refresh(member)
    return number
