# models.py
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, Date
from sqlalchemy.sql import func
from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # 1. Basic & Account Info
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    mobile_no = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=False)
    city = Column(String, nullable=False)

    # =====================
    # NEW FIELDS (FIXED INDENTATION)
    # =====================
    gender = Column(String, nullable=True)
    looking_for = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)

    preferred_min_age = Column(Integer, nullable=True)
    preferred_max_age = Column(Integer, nullable=True)
    preferred_city = Column(String, nullable=True)
    preferred_religion = Column(String, nullable=True)

    # 2. Physical & Career
    height = Column(String, nullable=True)
    marital_status = Column(String, nullable=True)  # e.g., Never Married, Divorced
    education = Column(String, nullable=True)
    profession = Column(String, nullable=False)
    annual_income = Column(String, nullable=True)

    # 3. Cultural & Family
    religion = Column(String, nullable=True)
    caste = Column(String, nullable=True)
    mother_tongue = Column(String, nullable=True)
    family_type = Column(String, nullable=True)  # Nuclear, Joint
    family_values = Column(String, nullable=True)  # Traditional, Moderate, Liberal

    # 4. Lifestyle & Bio
    diet = Column(String, nullable=True)  # Veg, Non-Veg, Jain
    habits = Column(String, nullable=True)  # e.g., Non-Smoker, Social Drinker
    hobbies = Column(String, nullable=True)
    bio = Column(Text, nullable=True)

    # ✅ SAFE ADDITIONS (no logic break)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    profile_completed = Column(Integer, default=0)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # ADD THIS TO THE BOTTOM OF models.py
class Interaction(Base):
    __tablename__ = "interactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))     # Who took the action
    target_id = Column(Integer, ForeignKey("users.id"))   # Who received the action
    action = Column(String, nullable=False)               # 'interest' or 'reject'
    created_at = Column(DateTime(timezone=True), server_default=func.now())