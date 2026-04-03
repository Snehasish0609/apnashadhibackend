# crud.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import User, Message
from auth import hash_password, verify_password

# =====================
# USERS
# =====================

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(
        select(User).where(User.email == email)
    )
    return result.scalars().first()


# ✅ NEW: check mobile duplicate
async def get_user_by_mobile(db: AsyncSession, mobile: str):
    result = await db.execute(
        select(User).where(User.mobile_no == mobile)
    )
    return result.scalars().first()


# ✅ NEW: profile completion calculator
def calculate_profile_score(user):
    fields = [
        user.height,
        user.marital_status,
        user.education,
        user.annual_income,
        user.religion,
        user.caste,
        user.mother_tongue,
        user.family_type,
        user.family_values,
        user.diet,
        user.habits,
        user.hobbies,
        user.bio,
        user.gender,
        user.looking_for,
        user.preferred_min_age,
        user.preferred_max_age,
        user.preferred_city,
        user.preferred_religion,
    ]
    filled = sum(1 for f in fields if f is not None and f != "")
    return int((filled / len(fields)) * 100)


async def create_user(db: AsyncSession, user):
    db_user = User(
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        mobile_no=user.mobile_no,
        city=user.city,
        profession=user.profession,
        date_of_birth=user.date_of_birth,
        password=hash_password(user.password),

        # ✅ EXISTING OPTIONAL FIELDS
        height=user.height,
        marital_status=user.marital_status,
        education=user.education,
        annual_income=user.annual_income,
        religion=user.religion,
        caste=user.caste,
        mother_tongue=user.mother_tongue,
        family_type=user.family_type,
        family_values=user.family_values,
        diet=user.diet,
        habits=user.habits,
        hobbies=user.hobbies,
        bio=user.bio,

        # =====================
        # NEW FIELDS (NO BREAK)
        # =====================
        gender=user.gender,
        looking_for=user.looking_for,
        profile_pic=None,

        preferred_min_age=user.preferred_min_age,
        preferred_max_age=user.preferred_max_age,
        preferred_city=user.preferred_city,
        preferred_religion=user.preferred_religion,
    )

    # ✅ NEW: calculate profile %
    db_user.profile_completed = calculate_profile_score(db_user)

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user


async def authenticate_user(db: AsyncSession, email: str, password: str):
    user = await get_user_by_email(db, email)
    if not user:
        return None
    if not verify_password(password, user.password):
        return None
    return user


async def get_all_users(db: AsyncSession, current_user_id: int):
    result = await db.execute(
        select(User).where(User.id != current_user_id)
    )
    return result.scalars().all()


# =====================
# CHAT
# =====================

async def save_message(
    db: AsyncSession,
    sender_id: int,
    receiver_id: int,
    message: str,
):
    msg = Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_messages(db: AsyncSession, user1: int, user2: int):
    result = await db.execute(
        select(Message)
        .where(
            ((Message.sender_id == user1) & (Message.receiver_id == user2)) |
            ((Message.sender_id == user2) & (Message.receiver_id == user1))
        )
        .order_by(Message.created_at)
    )
    return result.scalars().all()


