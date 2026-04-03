# schemas.py
from pydantic import BaseModel, EmailStr, Field, validator
from datetime import date

class RegisterUser(BaseModel):
    # Required
    first_name: str = Field(min_length=2, max_length=50)
    last_name: str = Field(min_length=2, max_length=50)
    email: EmailStr
    mobile_no: str = Field(min_length=10, max_length=15)
    password: str = Field(min_length=6)
    date_of_birth: date
    city: str
    profession: str

    # =====================
    # NEW FIELDS (OPTIONAL — NO BREAK)
    # =====================
    gender: str | None = None
    looking_for: str | None = None

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

    # ✅ Age validation (UNCHANGED)
    @validator("date_of_birth")
    def validate_age(cls, v):
        today = date.today()
        age = today.year - v.year
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

    
class LoginUser(BaseModel):
    email: str | None = None
    mobile_no: str | None = None
    password: str

class UserResponse(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: EmailStr

    class Config:
        from_attributes = True


class UserOut(BaseModel):
    id: int
    first_name: str

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    receiver_id: int
    message: str


class MessageOut(BaseModel):
    sender_id: int
    receiver_id: int
    message: str
    created_at: str

# ADD THIS TO schemas.py
class InteractionCreate(BaseModel):
    target_id: int
    action: str  # Must be 'interest' or 'reject'


from typing import Dict

# Define the schema for the quiz answers
class MatchmakerQuizParams(BaseModel):
    answers: Dict[str, str]