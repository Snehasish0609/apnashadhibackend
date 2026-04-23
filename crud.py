import random
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, or_
from models import User, Message, Referral, Transaction,SupportTicket
from auth import hash_password, verify_password
import os

# =====================
# USERS
# =====================

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(
        select(User).where(User.email == email)
    )
    return result.scalars().first()


async def get_user_by_mobile(db: AsyncSession, mobile: str):
    result = await db.execute(
        select(User).where(User.mobile_no == mobile)
    )
    return result.scalars().first()


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


# ✅ NEW: Referral Code Generator
async def generate_unique_referral_code(db: AsyncSession, first_name: str) -> str:
    """Create a short, unique referral code like RAHUL5X."""
    base = first_name.upper()[:5]
    for _ in range(10):  # Try 10 times to find a unique suffix
        suffix = ''.join(random.choices(string.digits + string.ascii_uppercase, k=3))
        code = f"{base}{suffix}"
        existing = await db.execute(select(User).where(User.referral_code == code))
        if not existing.scalars().first():
            return code
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


async def create_user(db: AsyncSession, user):
    # 1. Resolve referrer if a code was provided
    referrer = None
    if getattr(user, 'referred_by_code', None):
        ref_result = await db.execute(
            select(User).where(User.referral_code == user.referred_by_code.strip().upper())
        )
        referrer = ref_result.scalars().first()

    # 2. Generate new user's code
    new_code = await generate_unique_referral_code(db, user.first_name)

    db_user = User(
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        mobile_no=user.mobile_no,
        city=user.city,
        state=getattr(user, 'state', None),
        profession=user.profession,
        date_of_birth=user.date_of_birth,
        password=hash_password(user.password),

        # Optional Fields
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
        gender=user.gender,
        looking_for=user.looking_for,
        relationship_type=getattr(user, 'relationship_type', None),
        profile_pic=None,
        preferred_min_age=user.preferred_min_age,
        preferred_max_age=user.preferred_max_age,
        preferred_city=user.preferred_city,
        preferred_religion=user.preferred_religion,

        # Registration metadata
        account_created_by=getattr(user, 'account_created_by', None),
        terms_accepted=getattr(user, 'terms_accepted', False) or False,
        is_active=True,  # Admin must activate the account

        # Referral & Wallet Initialization
        referral_code=new_code,
        referred_by=referrer.id if referrer else None,
        coin_balance=0,
    )

    db_user.profile_completed = calculate_profile_score(db_user)

    db.add(db_user)
    await db.flush()  # To get db_user.id for the Referral table

    # 3. Log the referral relationship
    if referrer:
        new_referral = Referral(referrer_id=referrer.id, referred_id=db_user.id)
        db.add(new_referral)

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

# Define your secret key for encryption (store this in .env)
PG_SECRET = os.getenv("PG_SECRET_KEY", "Apnashaadi.in123")

async def save_message(
    db: AsyncSession, 
    sender_id: int, 
    receiver_id: int, 
    message: str = None, 
    media_url: str = None, 
    media_type: str = None
):
    # Encrypt the text message before saving
    encrypted_msg = func.pgp_sym_encrypt(message, PG_SECRET) if message else None

    msg = Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=encrypted_msg,
        media_url=media_url,
        media_type=media_type,
        status="sent" # Default status
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    
    # Fetch it back to return the decrypted string to the immediate sender
    return {
        "id": msg.id,
        "sender_id": msg.sender_id,
        "receiver_id": msg.receiver_id,
        "message": message,
        "media_url": msg.media_url,
        "media_type": msg.media_type,
        "status": msg.status,
        "created_at": msg.created_at
    }

async def get_messages(db: AsyncSession, user1: int, user2: int):
    # Decrypt on read
    result = await db.execute(
        select(
            Message.id,
            Message.sender_id,
            Message.receiver_id,
            func.pgp_sym_decrypt(Message.message, PG_SECRET).label("message"),
            Message.media_url,
            Message.media_type,
            Message.status,
            Message.created_at
        )
        .where(
            ((Message.sender_id == user1) & (Message.receiver_id == user2)) |
            ((Message.sender_id == user2) & (Message.receiver_id == user1))
        )
        .order_by(Message.created_at)
    )
    return [dict(r._mapping) for r in result.all()]

async def mark_messages_as_seen(db: AsyncSession, sender_id: int, receiver_id: int):
    # Marks messages sent by 'sender_id' and received by 'receiver_id' as seen
    await db.execute(
        update(Message)
        .where(
            and_(
                Message.sender_id == sender_id, 
                Message.receiver_id == receiver_id, 
                Message.status != "seen"
            )
        )
        .values(status="seen")
    )
    await db.commit()

async def update_user_presence(db: AsyncSession, user_id: int, is_online: bool):
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(is_online=is_online, last_seen=func.now())
    )
    await db.commit()
# =====================
# WALLET HELPERS
# =====================

async def credit_coins(db: AsyncSession, user_id: int, amount: int, description: str):
    """Helper to add coins and log the transaction."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if user:
        user.coin_balance = (user.coin_balance or 0) + amount
        txn = Transaction(user_id=user_id, amount=amount, description=description)
        db.add(txn)
        return True
    return False


# =====================
# SUPPORT TICKETS
# =====================

async def create_support_ticket(
    db: AsyncSession,
    email: str,
    subject: str,
    category: str,
    urgency: str,
    issue: str,
) -> SupportTicket:
    """
    Persists a new support ticket.
    Automatically checks whether the submitted email belongs to a
    registered (= verified) user and sets email_verified accordingly.
    """
    # Check if the email is linked to a registered user
    user_result = await db.execute(select(User).where(User.email == email.lower().strip()))
    email_verified = user_result.scalars().first() is not None

    ticket = SupportTicket(
        email=email.lower().strip(),
        subject=subject,
        category=category,
        urgency=urgency,
        issue=issue,
        email_verified=email_verified,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket
