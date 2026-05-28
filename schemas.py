from pydantic import BaseModel, EmailStr, Field, field_validator
from datetime import date, datetime
from typing import Dict, List, Optional

class AadhaarInitRequest(BaseModel):
    aadhaar_number: str
    # Note: no mobile field needed — UIDAI sends OTP to the mobile registered with this Aadhaar

class AadhaarVerifyRequest(BaseModel):
    ref_id: str
    otp: str

class RegisterUser(BaseModel):
    # Required
    first_name: str = Field(min_length=2, max_length=50)
    last_name: str = Field(min_length=2, max_length=50)
    email: EmailStr
    mobile_no: str = Field(min_length=10, max_length=15)
    password: str = Field(min_length=6)
    date_of_birth: date
    city: str
    state: str | None = None
    profession: str

    # =====================
    # NEW FIELDS
    # =====================
    gender: str | None = None
    looking_for: str | None = None
    relationship_type: str | None = None  # Serious / Casual / Marriage
    account_created_by: str | None = None  # Self / Parents / Guardian
    terms_accepted: bool | None = False

    preferred_min_age: int | None = None
    preferred_max_age: int | None = None
    preferred_city: str | None = None
    preferred_religion: str | None = None

    # Optional fields (existing)
    height: str | None = None
    marital_status: str | None = None
    education: str | None = None
    annual_income: str | None = None
    
    religion: str | None = None
    caste: str | None = None
    mother_tongue: str | None = None
    family_type: str | None = None
    family_values: str | None = None
    
    diet: str | None = None
    habits: str | None = None
    hobbies: str | None = None
    bio: str | None = None

    # UPDATED: Referral code during registration
    referred_by_code: str | None = None 

    # ✅ Age validation (FIXED for precise date comparison)
    @field_validator("date_of_birth")
    @classmethod
    def validate_age(cls, v):
        today = date.today()
        # Checks if the birthday has occurred yet this year
        age = today.year - v.year - ((today.month, today.day) < (v.month, v.day))
        if age < 18:
            raise ValueError("User must be at least 18 years old")
        return v


class UpdateUser(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    mobile_no: str | None = None
    city: str | None = None
    profession: str | None = None
    gender: str | None = None
    looking_for: str | None = None
    
    preferred_min_age: int | None = None
    preferred_max_age: int | None = None
    preferred_city: str | None = None
    preferred_religion: str | None = None

    height: str | None = None
    marital_status: str | None = None
    education: str | None = None
    annual_income: str | None = None
    religion: str | None = None
    caste: str | None = None
    mother_tongue: str | None = None
    family_type: str | None = None
    family_values: str | None = None
    diet: str | None = None
    habits: str | None = None
    hobbies: str | None = None
    bio: str | None = None
    # New fields from enhanced registration
    state: str | None = None
    relationship_type: str | None = None
    account_created_by: str | None = None
    # TWO LINES for Near Me functionality:
    latitude: Optional[float] = None
    longitude: Optional[float] = None



class LoginUser(BaseModel):
    email: str | None = None
    mobile_no: str | None = None
    password: str

class UserResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: EmailStr
    profile_completed: int | None = 0 # Added to track progress in response

    class Config:
        from_attributes = True

class UserOut(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    profile_pic: str | None = None
    city: str | None = None
    profession: str | None = None
    
    # Added for Chat Presence
    is_online: bool = False
    last_seen: datetime | None = None

    class Config:
        from_attributes = True

class MessageCreate(BaseModel):
    receiver_id: int
    message: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None

class MessageOut(BaseModel):
    id: int
    sender_id: int
    receiver_id: int
    message: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True
# ADDED: Full support for profile visits, interests, and rejects
class InteractionCreate(BaseModel):
    target_id: int
    action: str  # 'interest', 'reject', or 'visit'


class MatchmakerQuizParams(BaseModel):
    answers: Dict[str, str]


# =====================================================================
# NEW: REFERRAL & WALLET SCHEMAS (From Intern Code)
# =====================================================================

class TransactionOut(BaseModel):
    id: int
    amount: int
    description: str
    created_at: datetime

    class Config:
        from_attributes = True

class ReferralHistoryItem(BaseModel):
    referred_name: str
    status: str       # 'Pending' or 'Completed'
    coins_earned: int
    profile_completion: int

class WalletInfo(BaseModel):
    coin_balance: int
    total_earned: int
    total_spent: int
    transactions: List[TransactionOut]


# =====================================================================
# PROFILE VISIBILITY SCHEMA
# =====================================================================
class ProfileVisibilityUpdate(BaseModel):
    profile_visibility: str  # "public" | "matches_only" | "premium_only"

    @field_validator("profile_visibility")
    @classmethod
    def validate_visibility(cls, v):
        allowed = {"public", "matches_only", "premium_only"}
        if v not in allowed:
            raise ValueError(f"profile_visibility must be one of {allowed}")
        return v

# =====================================================================
# OTP EMAIL VERIFICATION SCHEMAS
# =====================================================================

class OTPRequest(BaseModel):
    """Body sent by frontend when requesting a new OTP."""
    email: EmailStr


class OTPVerify(BaseModel):
    """Body sent by frontend to verify the OTP entered by the user."""
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6)



# =====================================================================
# SUPPORT TICKET SCHEMAS
# =====================================================================

class SupportTicketCreate(BaseModel):
    """
    Matches the exact field names sent by Support.jsx NewTicketTab:
      user        -> email
      priority    -> urgency
      description -> issue
    """
    email:    EmailStr = Field(alias="user")         # JSX sends 'user' for the email field
    subject:  str      = Field(min_length=1, max_length=500)
    category: str      = Field(min_length=1, max_length=100)
    urgency:  str      = Field(default="medium", max_length=50, alias="priority")  # JSX sends 'priority'
    issue:    str      = Field(min_length=1, alias="description")  # JSX sends 'description'

    class Config:
        populate_by_name = True   # allow both alias and field name


class SupportTicketOut(BaseModel):
    id:             int
    email:          str
    subject:        str
    category:       str
    urgency:        str
    issue:          str
    email_verified: bool
    created_at:     datetime

    class Config:
        from_attributes = True

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordConfirm(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=6) # Matches your registration min_length

# =====================================================================
# ACCOUNT DEACTIVATION & DELETION SCHEMAS
# =====================================================================

class DeactivateAccountRequest(BaseModel):
    """Request to deactivate account (no password needed for deactivation)"""
    reason: str | None = None  # Optional reason for deactivation

class DeleteAccountRequest(BaseModel):
    """Request to permanently delete account (password required for verification)"""
    password: str = Field(min_length=1)  # Password for verification
    reason: str | None = None  # Optional reason for deletion

class DeactivatedAccountOut(BaseModel):
    id: int
    user_id: int
    email: str
    first_name: str
    last_name: str
    deactivation_date: datetime
    reactivation_deadline: datetime
    reason: str | None
    
    class Config:
        from_attributes = True

class DeletedAccountOut(BaseModel):
    id: int
    user_id: int
    email: str
    first_name: str
    last_name: str
    deletion_date: datetime
    reason: str | None
    
    class Config:
        from_attributes = True

class ReactivateAccountResponse(BaseModel):
    """Response when account is reactivated"""
    message: str
    user_id: int
    email: str
    status: str  # "reactivated"

# -------------------------------------------Admin Starts Here ----------------------------------------------------- 
class AdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(min_length=6)
    is_superadmin: bool = False

class AdminLogin(BaseModel):
    username: str
    password: str

class AdminOut(BaseModel):
    id: int
    username: str
    email: str
    is_superadmin: bool

    class Config:
        from_attributes = True

class AdminDashboardStats(BaseModel):
    total_users: int
    active_subscriptions: int
    banned_users: int
    total_reports: int

class AdminUserList(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    mobile_no: str
    plan_type: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

class ReportDetailsOut(BaseModel):
    id: int
    reporter_id: int
    reporter_name: str
    reported_user_id: int
    reported_user_name: str
    reason: str
    created_at: datetime
