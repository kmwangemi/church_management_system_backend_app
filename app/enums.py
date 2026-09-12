import enum


class Gender(str, enum.Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"


class MaritalStatus(str, enum.Enum):
    SINGLE = "SINGLE"
    MARRIED = "MARRIED"
    DIVORCED = "DIVORCED"
    WIDOWED = "WIDOWED"


class AccountStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"


class GlobalRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    CHURCH_ADMIN = "CHURCH_ADMIN"
    MEMBER = "MEMBER"


class ChurchRole(str, enum.Enum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"
    STAFF = "STAFF"
    VOLUNTEER = "VOLUNTEER"
    ADMIN = "ADMIN"
    VISITOR = "VISITOR"


class MembershipStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    TRANSFERRED = "TRANSFERRED"
    DECEASED = "DECEASED"


class PastorAssignmentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    TRANSFERRED = "TRANSFERRED"


class EmploymentType(str, enum.Enum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CASUAL = "CASUAL"
    CONTRACT = "CONTRACT"


class VolunteerStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ON_HOLD = "ON_HOLD"
    SUSPENDED = "SUSPENDED"


class ClearanceLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AdminAccessLevel(str, enum.Enum):
    GLOBAL = "GLOBAL"
    NATIONAL = "NATIONAL"
    REGIONAL = "REGIONAL"
    BRANCH = "BRANCH"


class HowDidYouHear(str, enum.Enum):
    FRIEND = "FRIEND"
    WEBSITE = "WEBSITE"
    ADVERTISEMENT = "ADVERTISEMENT"
    FAMILY = "FAMILY"
    ONLINE = "ONLINE"
    FLYER = "FLYER"
    OTHER = "OTHER"


class FollowUpStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONTACTED = "CONTACTED"
    CONVERTED = "CONVERTED"
    DECLINED = "DECLINED"


class DepartmentCategory(str, enum.Enum):
    MINISTRY = "MINISTRY"
    ADMINISTRATION = "ADMINISTRATION"
    OPERATIONS = "OPERATIONS"
    EDUCATION = "EDUCATION"
    OUTREACH = "OUTREACH"
    SUPPORT = "SUPPORT"
    FINANCE = "FINANCE"
    FACILITIES = "FACILITIES"
    TECHNOLOGY = "TECHNOLOGY"
    COMMUNICATIONS = "COMMUNICATIONS"
    PASTORAL_CARE = "PASTORAL_CARE"
    MISSIONS = "MISSIONS"
    YOUTH = "YOUTH"
    CHILDREN = "CHILDREN"
    WORSHIP = "WORSHIP"
    DISCIPLESHIP = "DISCIPLESHIP"
    COMMUNITY = "COMMUNITY"
    EVENTS = "EVENTS"
    SECURITY = "SECURITY"
    VOLUNTEER_COORDINATION = "VOLUNTEER_COORDINATION"


class ExpenseCategory(str, enum.Enum):
    SALARIES = "SALARIES"
    RENT = "RENT"
    MAINTENANCE = "MAINTENANCE"
    MISSIONS = "MISSIONS"
    OTHER = "OTHER"
    EQUIPMENT = "EQUIPMENT"
    MATERIALS = "MATERIALS"
    TRAINING = "TRAINING"
    EVENTS = "EVENTS"
    UTILITIES = "UTILITIES"
    TRANSPORTATION = "TRANSPORTATION"
    REFRESHMENTS = "REFRESHMENTS"
    MISCELLANEOUS = "MISCELLANEOUS"


class ActivityType(str, enum.Enum):
    MEETING = "MEETING"
    EVENT = "EVENT"
    TRAINING = "TRAINING"
    OUTREACH = "OUTREACH"
    OTHER = "OTHER"
    BIBLE_STUDY = "BIBLE_STUDY"
    PRAYER_MEETING = "PRAYER_MEETING"
    SOCIAL_EVENT = "SOCIAL_EVENT"
    DISCUSSION = "DISCUSSION"
    RETREAT = "RETREAT"
    WORSHIP = "WORSHIP"
    FELLOWSHIP = "FELLOWSHIP"
    SERVICE = "SERVICE"


class GoalStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class MemberRole(str, enum.Enum):
    LEADER = "LEADER"
    ASSISTANT_LEADER = "ASSISTANT_LEADER"
    COORDINATOR = "COORDINATOR"
    MEMBER = "MEMBER"
    VOLUNTEER = "VOLUNTEER"


class GroupCategory(str, enum.Enum):
    SMALL_GROUP = "SMALL_GROUP"
    MINISTRY = "MINISTRY"
    BIBLE_STUDY = "BIBLE_STUDY"
    SUPPORT = "SUPPORT"
    YOUTH = "YOUTH"
    PRAYER = "PRAYER"
    FELLOWSHIP = "FELLOWSHIP"
    CHILDREN = "CHILDREN"
    MARRIAGE = "MARRIAGE"
    WORSHIP = "WORSHIP"
    CONTRIBUTION = "CONTRIBUTION"
    OTHERS = "OTHERS"


class GroupActivityType(str, enum.Enum):
    FELLOWSHIP = "FELLOWSHIP"
    STUDY = "STUDY"
    SERVICE = "SERVICE"
    OUTREACH = "OUTREACH"
    BIBLE_STUDY = "BIBLE_STUDY"
    PRAYER_MEETING = "PRAYER_MEETING"
    SOCIAL_EVENT = "SOCIAL_EVENT"
    DISCUSSION = "DISCUSSION"
    RETREAT = "RETREAT"
    MEETING = "MEETING"
    TRAINING = "TRAINING"
    EVENT = "EVENT"
    WORSHIP = "WORSHIP"
    OTHER = "OTHER"


class GroupGoalStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EventStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    POSTPONED = "POSTPONED"


class OfferingType(str, enum.Enum):
    TITHE = "TITHE"
    OFFERING = "OFFERING"
    BUILDING_FUND = "BUILDING_FUND"
    MISSIONS = "MISSIONS"
    SPECIAL_GIVING = "SPECIAL_GIVING"
    SPECIAL_OFFERING = "SPECIAL_OFFERING"
    THANKSGIVING = "THANKSGIVING"
    PARTNERSHIP = "PARTNERSHIP"
    DONATION = "DONATION"
    MISSION = "MISSION"
    OTHER = "OTHER"


class PaymentMethod(str, enum.Enum):
    CASH = "CASH"
    M_PESA = "M_PESA"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"
    CARD = "CARD"
    ONLINE = "ONLINE"


class PledgePurpose(str, enum.Enum):
    BUILDING = "BUILDING"
    MISSIONS = "MISSIONS"
    GENERAL = "GENERAL"
    OTHER = "OTHER"
    YOUTH_PROGRAM = "YOUTH_PROGRAM"
    EQUIPMENT = "EQUIPMENT"
    OUTREACH = "OUTREACH"
    EDUCATION = "EDUCATION"


class PaymentSchedule(str, enum.Enum):
    ONE_TIME = "ONE_TIME"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class PledgeStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"


class PrayerCategory(str, enum.Enum):
    PERSONAL = "PERSONAL"
    CHURCH = "CHURCH"
    COMMUNITY = "COMMUNITY"
    GLOBAL = "GLOBAL"
    THANKSGIVING = "THANKSGIVING"
    HEALTH = "HEALTH"
    FAMILY = "FAMILY"
    CAREER = "CAREER"
    FINANCIAL = "FINANCIAL"
    SPIRITUAL = "SPIRITUAL"
    GUIDANCE = "GUIDANCE"
    OTHER = "OTHER"


class Priority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PrayerStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"


class DayOfWeek(str, enum.Enum):
    SUNDAY = "SUNDAY"
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"


class ServiceType(str, enum.Enum):
    WORSHIP = "WORSHIP"
    PRAYER = "PRAYER"
    STUDY = "STUDY"
    FELLOWSHIP = "FELLOWSHIP"
    BIBLE_STUDY = "BIBLE_STUDY"
    YOUTH = "YOUTH"
    CHILDREN = "CHILDREN"
    SPECIAL = "SPECIAL"


class AssetStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    MAINTENANCE = "MAINTENANCE"
    DISPOSED = "DISPOSED"
    SOLD = "SOLD"
    DONATED = "DONATED"
    LOST = "LOST"
    STOLEN = "STOLEN"


class AnnouncementTarget(str, enum.Enum):
    ALL = "ALL"
    BRANCH = "BRANCH"
    DEPARTMENT = "DEPARTMENT"
    GROUP = "GROUP"


class AttendanceStatus(str, enum.Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LATE = "LATE"
    EXCUSED = "EXCUSED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class ReportType(str, enum.Enum):
    FINANCE = "FINANCE"
    MEMBERSHIP = "MEMBERSHIP"
    ATTENDANCE = "ATTENDANCE"
    EVENTS = "EVENTS"
    GOALS = "GOALS"
    FINANCIAL = "FINANCIAL"
    GIVING = "GIVING"
    ACTIVITIES = "ACTIVITIES"


class MessageStatus(str, enum.Enum):
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    READ = "READ"
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    CANCELLED = "CANCELLED"


class ContentCategory(str, enum.Enum):
    SPIRITUAL = "SPIRITUAL"
    EDUCATIONAL = "EDUCATIONAL"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    WORSHIP = "WORSHIP"
    YOUTH = "YOUTH"
    CHILDREN = "CHILDREN"
    MISSIONS = "MISSIONS"
    FELLOWSHIP = "FELLOWSHIP"
    OUTREACH = "OUTREACH"
    DISCIPLESHIP = "DISCIPLESHIP"


class ContentStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"
    PRIVATE = "PRIVATE"


class ContentType(str, enum.Enum):
    SERMON = "SERMON"
    BIBLE_STUDY = "BIBLE_STUDY"
    PRAYER = "PRAYER"
    WORSHIP = "WORSHIP"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    EVENT = "EVENT"
    DEVOTIONAL = "DEVOTIONAL"
    TESTIMONY = "TESTIMONY"
    MUSIC = "MUSIC"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"


class DiscipleLevel(str, enum.Enum):
    NEW_CONVERT = "NEW_CONVERT"
    GROWING = "GROWING"
    MATURE = "MATURE"
    LEADER = "LEADER"


class DiscipleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    DISCONTINUED = "DISCONTINUED"


class ProgressStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MessageType(str, enum.Enum):
    SMS = "SMS"
    EMAIL = "EMAIL"


class TemplateCategory(str, enum.Enum):
    SERVICE_REMINDER = "SERVICE_REMINDER"
    EVENT_REGISTRATION = "EVENT_REGISTRATION"
    WELCOME_MEMBER = "WELCOME_MEMBER"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    CUSTOM = "CUSTOM"


class ScheduleType(str, enum.Enum):
    NOW = "NOW"
    SCHEDULED = "SCHEDULED"
    DRAFT = "DRAFT"


class ChurchPlan(str, enum.Enum):
    BASIC = "BASIC"
    MINISTRY = "MINISTRY"
    CATHEDRAL = "CATHEDRAL"
    CUSTOM = "CUSTOM"


class MaintenanceSchedule(str, enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUALLY = "ANNUALLY"


class AssetCondition(str, enum.Enum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"


class AssetType(str, enum.Enum):
    VEHICLE = "VEHICLE"
    PROPERTY = "PROPERTY"
    EQUIPMENT = "EQUIPMENT"
    FURNITURE = "FURNITURE"
    TECHNOLOGY = "TECHNOLOGY"
    MUSICAL = "MUSICAL"


class ActivityTypeEnum(str, enum.Enum):
    SERVICE = "SERVICE"
    MEETING = "MEETING"
    EVENT = "EVENT"
    PROGRAM = "PROGRAM"
    MINISTRY = "MINISTRY"
    SOCIAL = "SOCIAL"
    OUTREACH = "OUTREACH"


class ActivityStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    ONGOING = "ONGOING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    POSTPONED = "POSTPONED"


class UserPlan(str, enum.Enum):
    CONNECT = "CONNECT"
    ENGAGE = "ENGAGE"
    SERVE = "SERVE"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"
    TRIAL = "TRIAL"


class PaymentMethodType(str, enum.Enum):
    CREDIT_CARD = "CREDIT_CARD"
    PAYPAL = "PAYPAL"
    STRIPE = "STRIPE"
    M_PESA = "M_PESA"


class DateRange(str, enum.Enum):
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    LAST_3_MONTHS = "LAST_3_MONTHS"
    LAST_6_MONTHS = "LAST_6_MONTHS"
    LAST_YEAR = "LAST_YEAR"
    CUSTOM = "CUSTOM"


class ReportFormat(str, enum.Enum):
    PDF = "PDF"
    EXCEL = "EXCEL"
    CSV = "CSV"


class ReportStatus(str, enum.Enum):
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RecipientType(str, enum.Enum):
    ALL_USERS = "ALL_USERS"
    ACTIVE_USERS = "ACTIVE_USERS"
    DEPARTMENT = "DEPARTMENT"
    GROUP = "GROUP"
    CUSTOM = "CUSTOM"


class TargetModel(str, enum.Enum):
    USER = "USER"
    DEPARTMENT = "DEPARTMENT"
    GROUP = "GROUP"


class AnnouncementCategory(str, enum.Enum):
    GENERAL = "GENERAL"
    SERVICE = "SERVICE"
    EVENT = "EVENT"
    PRAYER = "PRAYER"
    MINISTRY = "MINISTRY"
    YOUTH = "YOUTH"
    CHILDREN = "CHILDREN"
    FINANCE = "FINANCE"
    VOLUNTEER = "VOLUNTEER"
    EMERGENCY = "EMERGENCY"


class AnnouncementStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    SCHEDULED = "SCHEDULED"
    EXPIRED = "EXPIRED"


class LogLevel(str, enum.Enum):
    ERROR = "ERROR"
    WARN = "WARN"
    INFO = "INFO"
    DEBUG = "DEBUG"


class LogSource(str, enum.Enum):
    API = "API"
    CLIENT = "CLIENT"
    SERVER = "SERVER"
    DATABASE = "DATABASE"
    AUTH = "AUTH"
    PAYMENT = "PAYMENT"
    EMAIL = "EMAIL"


class Environment(str, enum.Enum):
    DEVELOPMENT = "DEVELOPMENT"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    TEST = "TEST"


class MilestoneCategory(str, enum.Enum):
    SPIRITUAL_GROWTH = "SPIRITUAL_GROWTH"
    BIBLE_STUDY = "BIBLE_STUDY"
    PRAYER = "PRAYER"
    SERVICE = "SERVICE"
    LEADERSHIP = "LEADERSHIP"
    EVANGELISM = "EVANGELISM"
    FELLOWSHIP = "FELLOWSHIP"
    WORSHIP = "WORSHIP"
    DISCIPLESHIP = "DISCIPLESHIP"
