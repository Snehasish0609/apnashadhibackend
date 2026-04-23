from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, Date, Boolean, UniqueConstraint ,LargeBinary
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from db import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    mobile_no = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=False)
    city = Column(String, nullable=False)
    state = Column(String, nullable=True)          # NEW: Indian State
    gender = Column(String, nullable=True)
    looking_for = Column(String, nullable=True)
    relationship_type = Column(String, nullable=True)  # NEW: Serious/Casual/Marriage
    profile_pic = Column(String, nullable=True)

    # Account metadata
    account_created_by = Column(String, nullable=True)  # NEW: Self / Parents
    terms_accepted = Column(Boolean, default=False, nullable=False, server_default="false")  # NEW
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")  # Auto-activate by default

    # Preferences
    preferred_min_age = Column(Integer, nullable=True)
    preferred_max_age = Column(Integer, nullable=True)
    preferred_city = Column(String, nullable=True)
    preferred_religion = Column(String, nullable=True)

    # Career & Physical
    height = Column(String, nullable=True)
    marital_status = Column(String, nullable=True)
    education = Column(String, nullable=True)
    profession = Column(String, nullable=False)
    annual_income = Column(String, nullable=True)

    # Cultural
    religion = Column(String, nullable=True)
    caste = Column(String, nullable=True)
    mother_tongue = Column(String, nullable=True)
    family_type = Column(String, nullable=True)
    family_values = Column(String, nullable=True)

    # Lifestyle
    diet = Column(String, nullable=True)
    habits = Column(String, nullable=True)
    hobbies = Column(String, nullable=True)
    bio = Column(Text, nullable=True)

    # System Fields
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    profile_completed = Column(Integer, default=0)

    # Privacy
    profile_visibility = Column(String, default="public", nullable=False, server_default="public")

    # Profile identity & plan
    profile_id = Column(String, unique=True, nullable=True, index=True)
    plan_type = Column(String, default="free", nullable=False, server_default="free")
    plan_expiry = Column(DateTime(timezone=True), nullable=True)

    # Referral & Wallet
    referral_code = Column(String, unique=True, nullable=True, index=True)
    referred_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    coin_balance = Column(Integer, default=0)
    is_online = Column(Boolean, default=False, server_default="false")
    last_seen = Column(DateTime(timezone=True), nullable=True)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    
    # Encrypted message stored as binary
    message = Column(LargeBinary, nullable=True) 
    
    # Media support
    media_url = Column(String, nullable=True)
    media_type = Column(String, nullable=True) # 'image' or 'video'
    
    # Ticks: 'sent', 'delivered', 'seen'
    status = Column(String, default="sent") 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Interaction(Base):
    __tablename__ = "interactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    target_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String, nullable=False) # 'interest', 'reject', 'visit'
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True, index=True)
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    referred_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reward_given = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class BlockedUser(Base):
    __tablename__ = "blocked_users"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    blocked_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "blocked_user_id"),)


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reported_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Integer, nullable=False) # +ve credit, -ve debit
    description = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    
class OTPCode(Base):
    """
    Stores a 6-digit OTP keyed to an email address.
    Expires after 5 minutes. Multiple rows per email are allowed;
    verification always checks the latest unexpired record.
    """
    __tablename__ = "otp_codes"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    otp_code = Column(String(6), nullable=False)  # plain 6-digit string
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SupportTicket(Base):
    """
    Stores support/help-desk tickets submitted by users from the Support page.
    Fields map directly to what Support.jsx sends.
    """
    __tablename__ = "support_tickets"

    id             = Column(Integer, primary_key=True, index=True)
    email          = Column(String(255), nullable=False, index=True)   # user-supplied email
    subject        = Column(String(500), nullable=False)
    category       = Column(String(100), nullable=False)               # e.g. "Account Help"
    urgency        = Column(String(50),  nullable=False, default="medium")  # low / medium / high
    issue          = Column(Text,        nullable=False)               # description text
    email_verified = Column(Boolean,     nullable=False, default=False) # True if email found in users table
    created_at     = Column(DateTime(timezone=True), server_default=func.now())