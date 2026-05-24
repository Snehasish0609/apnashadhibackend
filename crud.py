import httpx
import random
import string
import os
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, or_
from models import User, Message, Referral, Transaction, SupportTicket, BlockedUser, DeactivatedAccount, DeletedAccount, OTPCode
from auth import hash_password, verify_password
from models import Admin, Report
PG_SECRET = os.getenv("PG_SECRET_KEY", "Apnashaadi.in123")

# =====================
# SECURE DATA HELPERS
# =====================

def sanitize_user_dict(u):
    """Safely strips out encrypted LargeBinary objects so they don't break JSON serialization in FastAPI."""
    data = u.__dict__.copy()
    data.pop("_sa_instance_state", None)
    data.pop("password", None)
    data.pop("email_encrypted", None)
    data.pop("mobile_encrypted", None)
    # Re-attach the plaintext string if it was decrypted during the query
    if hasattr(u, 'email'): data['email'] = u.email
    if hasattr(u, 'mobile_no'): data['mobile_no'] = u.mobile_no
    return data

async def get_user_by_id(db: AsyncSession, user_id: int):
    """Fetches user and decrypts email and mobile on the fly."""
    result = await db.execute(
        select(
            User,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_no")
        ).where(User.id == user_id)
    )
    row = result.first()
    if row:
        u, e, m = row
        u.email = e
        u.mobile_no = m
        return u
    return None

async def get_user_by_email(db: AsyncSession, email: str):
    """Searches DB by decrypting the column and comparing it to the input."""
    result = await db.execute(
        select(
            User,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_no")
        ).where(func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET) == email.lower().strip())
    )
    row = result.first()
    if row:
        u, e, m = row
        u.email = e
        u.mobile_no = m
        return u
    return None

async def get_user_by_mobile(db: AsyncSession, mobile: str):
    result = await db.execute(
        select(
            User,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_no")
        ).where(func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET) == mobile.strip())
    )
    row = result.first()
    if row:
        u, e, m = row
        u.email = e
        u.mobile_no = m
        return u
    return None

def calculate_profile_score(user):
    fields = [
        user.height, user.marital_status, user.education, user.annual_income,
        user.religion, user.caste, user.mother_tongue, user.family_type,
        user.family_values, user.diet, user.habits, user.hobbies, user.bio,
        user.gender, user.looking_for, user.preferred_min_age,
        user.preferred_max_age, user.preferred_city, user.preferred_religion,
    ]
    filled = sum(1 for f in fields if f is not None and f != "")
    return int((filled / len(fields)) * 100)

async def generate_unique_referral_code(db: AsyncSession, first_name: str) -> str:
    base = first_name.upper()[:5]
    for _ in range(10):  
        suffix = ''.join(random.choices(string.digits + string.ascii_uppercase, k=3))
        code = f"{base}{suffix}"
        existing = await db.execute(select(User).where(User.referral_code == code))
        if not existing.scalars().first():
            return code
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


async def get_coordinates_from_city(city: str, state: str = None) -> tuple[float, float]:
    """
    Takes a city and state, calls OpenStreetMap Nominatim, and returns (latitude, longitude).
    Fixed: uses httpx params dict for proper URL encoding (spaces/commas in city names
    were previously breaking the request).
    """
    try:
        # Build the search query
        query = f"{city}, {state}, India" if state else f"{city}, India"
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"User-Agent": "ApnaShaadi/1.0 (care@apnasaadhi.com)"}
            # 🔥 KEY FIX: use params= dict so httpx URL-encodes the query automatically.
            # Previously we put the raw string directly in the URL which broke on spaces/commas.
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "format": "json",
                    "q": query,
                    "limit": "1",
                    "countrycodes": "in",
                },
                headers=headers,
            )
            response.raise_for_status()
            
            data = response.json()
            if data and len(data) > 0:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                # Sanity check: valid India bounding box (lat 6–38, lon 66–98)
                # Rejects Null Island (0,0) and overseas mismatches
                if 6.0 <= lat <= 38.0 and 66.0 <= lon <= 98.0:
                    print(f"[Geocode] ✅ '{city}' → ({lat}, {lon})")
                    return lat, lon
                else:
                    print(f"[Geocode] ⚠️ Result for '{city}' outside India: ({lat}, {lon}) — skipped")
                
    except httpx.ConnectTimeout:
        print(f"[Geocode] Nominatim timed out for city: '{city}'")
    except httpx.HTTPStatusError as e:
        print(f"[Geocode] HTTP error for '{city}': {e.response.status_code}")
    except Exception as e:
        print(f"[Geocode] Failed for '{city}': {e}")
        
    return None, None

async def create_user(db: AsyncSession, user):
    referrer = None
    if getattr(user, 'referred_by_code', None):
        ref_result = await db.execute(
            select(User).where(User.referral_code == user.referred_by_code.strip().upper())
        )
        referrer = ref_result.scalars().first()

    new_code = await generate_unique_referral_code(db, user.first_name)

    # 🔥 Fetch GPS coordinates based on the City and State they typed in the registration form
    lat, lon = await get_coordinates_from_city(user.city, getattr(user, 'state', None))


    # 🎁 New referred user gets 5 welcome coins if they were referred by someone
    welcome_coins = 5 if referrer else 0

    db_user = User(
        first_name=user.first_name, last_name=user.last_name, 
        
        # 🔥 Encrypting PII at creation
        email_encrypted=func.pgp_sym_encrypt(user.email.lower().strip(), PG_SECRET),
        mobile_encrypted=func.pgp_sym_encrypt(user.mobile_no.strip(), PG_SECRET),
        
        city=user.city, state=getattr(user, 'state', None),
        profession=user.profession, date_of_birth=user.date_of_birth,
        password=hash_password(user.password), height=user.height,
        marital_status=user.marital_status, education=user.education,
        annual_income=user.annual_income, religion=user.religion, caste=user.caste,
        mother_tongue=user.mother_tongue, family_type=user.family_type,
        family_values=user.family_values, diet=user.diet, habits=user.habits,
        hobbies=user.hobbies, bio=user.bio, gender=user.gender,
        looking_for=user.looking_for, relationship_type=getattr(user, 'relationship_type', None),
        preferred_min_age=user.preferred_min_age, preferred_max_age=user.preferred_max_age,
        preferred_city=user.preferred_city, preferred_religion=user.preferred_religion,
        account_created_by=getattr(user, 'account_created_by', None),
        terms_accepted=getattr(user, 'terms_accepted', False) or False,
        is_active=True, referral_code=new_code,
        referred_by=referrer.id if referrer else None,
        coin_balance=welcome_coins,  # 🎁 5 coins credited immediately if referred
	
	# 🔥 Automatically save the generated coordinates at registration!
        latitude=lat,
        longitude=lon,
    )

    db_user.profile_completed = int(calculate_profile_score(db_user))  # type: ignore[assignment]

    db.add(db_user)
    await db.flush()

    if referrer:
        # ── Count how many successful referrals the referrer already has ──
        done_count_res = await db.execute(
            select(Referral).where(
                Referral.referrer_id == referrer.id,
                Referral.reward_given == True,
            )
        )
        done_count = len(done_count_res.scalars().all()) + 1  # +1 for this new referral

        # ── Referrer gets 10 coins immediately on registration ──
        referrer_coins = 10
        description_parts = [f"Referral reward: {db_user.first_name} joined with your code"]

        # ── Milestone bonuses ──
        if done_count == 5:
            referrer_coins += 20
            description_parts.append("+20 milestone bonus (5 referrals! 🔥)")
        elif done_count == 10:
            referrer_coins += 50
            description_parts.append("+50 milestone bonus (10 referrals! ⚡)")

        # Credit coins to referrer
        ref_user_res = await db.execute(select(User).where(User.id == referrer.id))
        ref_user = ref_user_res.scalars().first()
        if ref_user:
            ref_user.coin_balance = (ref_user.coin_balance or 0) + referrer_coins
            referrer_txn = Transaction(
                user_id=referrer.id,
                amount=referrer_coins,
                description=" | ".join(description_parts)
            )
            db.add(referrer_txn)

        # Create the referral record — mark reward_given=True since we credited immediately
        new_referral = Referral(
            referrer_id=referrer.id,
            referred_id=db_user.id,
            reward_given=True,  # ✅ Already credited above
        )
        db.add(new_referral)

        # 🎁 Record the 5-coin welcome transaction for the referred user
        welcome_txn = Transaction(
            user_id=db_user.id,
            amount=welcome_coins,
            description=f"Welcome bonus: Joined via referral code {referrer.referral_code}"
        )
        db.add(welcome_txn)

    await db.commit()
    await db.refresh(db_user)
    
    # Attach plain text so Pydantic Response Model can read it
    db_user.email = user.email.lower().strip()
    db_user.mobile_no = user.mobile_no.strip()
    
    return db_user

async def authenticate_user(db: AsyncSession, email: str, password: str):
    user = await get_user_by_email(db, email)
    if not user:
        return None
    if not verify_password(password, user.password):
        return None
    return user

async def get_all_users(db: AsyncSession, current_user_id: int):
    blocks_res = await db.execute(
        select(BlockedUser).where(
            or_(BlockedUser.user_id == current_user_id, BlockedUser.blocked_user_id == current_user_id)
        )
    )
    blocked_ids = {b.blocked_user_id if b.user_id == current_user_id else b.user_id for b in blocks_res.scalars().all()}

    result = await db.execute(
        select(
            User,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email_dec"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_dec")
        ).where(User.id != current_user_id)
    )
    rows = result.all()
    
    safe_users = []
    for row in rows:
        u, e, m = row
        u.email = e
        u.mobile_no = m
        user_data = sanitize_user_dict(u)
        
        if u.id in blocked_ids:
            user_data["is_blocked"] = True
            user_data["is_online"] = False
            user_data["last_seen"] = None
        else:
            user_data["is_blocked"] = False
            
        safe_users.append(user_data)
        
    return safe_users


async def save_message(
    db: AsyncSession, sender_id: int, receiver_id: int,
    message: str | None = None, media_url: str | None = None, media_type: str | None = None
):
    encrypted_msg = func.pgp_sym_encrypt(message, PG_SECRET) if message else None

    msg = Message(
        sender_id=sender_id, receiver_id=receiver_id, message=encrypted_msg,
        media_url=media_url, media_type=media_type, status="sent"
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    
    return {
        "id": msg.id, "sender_id": msg.sender_id, "receiver_id": msg.receiver_id,
        "message": message, "media_url": msg.media_url, "media_type": msg.media_type,
        "status": msg.status, "created_at": msg.created_at
    }

async def get_messages(db: AsyncSession, user1: int, user2: int):
    result = await db.execute(
        select(
            Message.id, Message.sender_id, Message.receiver_id,
            func.pgp_sym_decrypt(Message.message, PG_SECRET).label("message"),
            Message.media_url, Message.media_type, Message.status,
            Message.created_at, Message.is_deleted
        )
        .where(
            or_(
                and_(Message.sender_id == user1, Message.receiver_id == user2, Message.deleted_by_sender == False),
                and_(Message.sender_id == user2, Message.receiver_id == user1, Message.deleted_by_receiver == False)
            )
        )
        .order_by(Message.created_at)
    )
    return [dict(r._mapping) for r in result.all()]

async def mark_messages_as_seen(db: AsyncSession, sender_id: int, receiver_id: int):
    await db.execute(
        update(Message)
        .where(
            and_(
                Message.sender_id == sender_id, Message.receiver_id == receiver_id, Message.status != "seen"
            )
        )
        .values(status="seen")
    )
    await db.commit()

async def update_user_presence(db: AsyncSession, user_id: int, is_online: bool):
    await db.execute(
        update(User).where(User.id == user_id).values(is_online=is_online, last_seen=func.now())
    )
    await db.commit()


# Alias used throughout main.py referral reward logic
async def _credit_coins(db: AsyncSession, user_id: int, amount: int, description: str):
    """Atomic coin credit — updates balance + inserts a Transaction row.
    Does NOT call db.commit() — caller must commit after calling this."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if user:
        user.coin_balance = (user.coin_balance or 0) + amount
        txn = Transaction(user_id=user_id, amount=amount, description=description)
        db.add(txn)
        return True
    return False

# Private alias used by main.py imports
_credit_coins = _credit_coins

async def create_support_ticket(db: AsyncSession, email: str, subject: str, category: str, urgency: str, issue: str) -> SupportTicket:
    user = await get_user_by_email(db, email)
    email_verified = user is not None

    ticket = SupportTicket(
        email_encrypted=func.pgp_sym_encrypt(email.lower().strip(), PG_SECRET),
        subject=subject, category=category,
        urgency=urgency, issue=issue, email_verified=email_verified,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    
    ticket.email = email.lower().strip()
    return ticket

# =====================
# ACCOUNT DEACTIVATION & DELETION
# =====================

async def deactivate_account(db: AsyncSession, user_id: int, reason: str | None = None):
    result = await db.execute(
        select(User, func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email_dec"))
        .where(User.id == user_id)
    )
    row = result.first()
    
    if not row:
        return None
        
    user, email_dec = row
    user.email = email_dec
    
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=30)
    
    user.is_deactivated = True
    user.is_active = False
    user.deactivation_date = now
    user.reactivation_deadline = deadline
    
    deactivated = DeactivatedAccount(
        user_id=user.id,
        email_encrypted=func.pgp_sym_encrypt(user.email, PG_SECRET),
        first_name=user.first_name,
        last_name=user.last_name,
        deactivation_date=now,
        reactivation_deadline=deadline,
        reason=reason
    )
    db.add(deactivated)
    
    await db.commit()
    await db.refresh(user)
    
    return {
        "user_id": user.id,
        "email": user.email,
        "deactivation_date": now,
        "reactivation_deadline": deadline,
        "message": "Account deactivated successfully"
    }

async def reactivate_account(db: AsyncSession, user_id: int):
    result = await db.execute(
        select(User, func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email_dec"))
        .where(User.id == user_id)
    )
    row = result.first()
    if not row:
        return None
        
    user, email_dec = row
    user.email = email_dec
    
    if not user.is_deactivated:
        return {"message": "Account is already active", "status": "already_active"}
    
    now = datetime.now(timezone.utc)
    if user.reactivation_deadline and now > user.reactivation_deadline:
        return {"message": "Reactivation deadline has passed. Account cannot be reactivated.", "status": "deadline_passed"}
    
    user.is_deactivated = False
    user.is_active = True
    user.deactivation_date = None
    user.reactivation_deadline = None
    
    deactivated = await db.execute(
        select(DeactivatedAccount).where(DeactivatedAccount.user_id == user_id)
    )
    deactivated_record = deactivated.scalars().first()
    if deactivated_record:
        await db.delete(deactivated_record)
    
    await db.commit()
    await db.refresh(user)
    
    return {
        "user_id": user.id,
        "email": user.email,
        "status": "reactivated",
        "message": "Account reactivated successfully"
    }

async def delete_account_permanently(db: AsyncSession, user_id: int, reason: str | None = None):
    result = await db.execute(
        select(
            User,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email_dec"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_dec")
        ).where(User.id == user_id)
    )
    row = result.first()
    if not row:
        return None
        
    user, email_dec, mobile_dec = row
    user.email = email_dec
    user.mobile_no = mobile_dec
    
    now = datetime.now(timezone.utc)
    
    archived_data = {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "mobile_no": user.mobile_no,
        "gender": user.gender,
        "city": user.city,
        "profession": user.profession,
        "profile_id": user.profile_id,
        "plan_type": user.plan_type,
        "created_at": str(user.created_at),
    }
    
    deleted = DeletedAccount(
        user_id=user.id,
        email_encrypted=func.pgp_sym_encrypt(user.email, PG_SECRET),
        mobile_encrypted=func.pgp_sym_encrypt(user.mobile_no, PG_SECRET) if user.mobile_no else None,
        first_name=user.first_name,
        last_name=user.last_name,
        profile_pic=user.profile_pic,
        bio=user.bio,
        deletion_date=now,
        reason=reason,
        archived_data_encrypted=func.pgp_sym_encrypt(json.dumps(archived_data), PG_SECRET)
    )
    db.add(deleted)
    
    user.password = hash_password(os.urandom(32).hex())
    user.is_active = False
    user.is_deactivated = False
    
    await db.commit()
    
    return {
        "user_id": user.id,
        "email": user.email,
        "deletion_date": now,
        "status": "deleted",
        "message": "Account permanently deleted"
    }

async def check_deactivation_deadline(db: AsyncSession):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(DeactivatedAccount).where(DeactivatedAccount.reactivation_deadline <= now)
    )
    expired_accounts = result.scalars().all()
    
    for account in expired_accounts:
        await delete_account_permanently(db, int(account.user_id), reason="Deactivation deadline expired")  # type: ignore[arg-type]
    
    return len(expired_accounts) if expired_accounts else 0

# -------------------------------------------Admin Starts Here ----------------------------------------------------- 
async def create_admin_user(db: AsyncSession, admin_data):
    db_admin = Admin(
        username=admin_data.username.lower().strip(),
        email=admin_data.email.lower().strip(),
        password=hash_password(admin_data.password),
        is_superadmin=admin_data.is_superadmin
    )
    db.add(db_admin)
    await db.commit()
    await db.refresh(db_admin)
    return db_admin

async def authenticate_admin(db: AsyncSession, username: str, password: str):
    result = await db.execute(select(Admin).where(Admin.username == username.lower().strip()))
    admin = result.scalars().first()
    if not admin or not verify_password(password, str(admin.password)):  # type: ignore[arg-type]
        return None
    return admin

# --- ADMIN DASHBOARD DATA ---
async def get_admin_dashboard_stats(db: AsyncSession):
    # Total Users
    total_users_res = await db.execute(select(func.count(User.id)))
    total_users = total_users_res.scalar() or 0

    # Active Subscriptions (assuming 'free' is no sub)
    active_subs_res = await db.execute(select(func.count(User.id)).where(User.plan_type != 'free'))
    active_subs = active_subs_res.scalar() or 0

    # Banned Users
    banned_res = await db.execute(select(func.count(User.id)).where(User.is_active == False))
    banned_users = banned_res.scalar() or 0

    # Total Reports
    reports_res = await db.execute(select(func.count(Report.id)))
    total_reports = reports_res.scalar() or 0

    return {
        "total_users": total_users,
        "active_subscriptions": active_subs,
        "banned_users": banned_users,
        "total_reports": total_reports
    }

async def get_all_users_for_admin(db: AsyncSession):
    """Fetches all users with decrypted PII for the admin table"""
    result = await db.execute(
        select(
            User.id,
            User.first_name,
            User.last_name,
            func.pgp_sym_decrypt(User.email_encrypted, PG_SECRET).label("email"),
            func.pgp_sym_decrypt(User.mobile_encrypted, PG_SECRET).label("mobile_no"),
            User.plan_type,
            User.is_active,
            User.created_at
        ).order_by(User.created_at.desc())
    )
    return [dict(r._mapping) for r in result.all()]

async def toggle_user_ban_status(db: AsyncSession, user_id: int):
    user = await get_user_by_id(db, user_id)
    if not user:
        return None
    
    # Toggle the active status
    user.is_active = not user.is_active
    await db.commit()
    await db.refresh(user)
    return user

def calculate_severity_score(reason: str) -> int:
    reason_map = {
        "Scam/Fraud": 5,
        "Harassment": 5,
        "Inappropriate Content": 4,
        "Fake Profile": 3,
        "Religious Misrepresentation": 3,
        "Spam": 2,
        "Other": 1
    }
    return reason_map.get(reason, 1)

async def check_duplicate_report(db: AsyncSession, reporter_id: int, target_user_id: int) -> bool:
    res = await db.execute(
        select(Report).where(
            Report.reporter_id == reporter_id,
            Report.reported_user_id == target_user_id,
            Report.status == "pending"
        )
    )
    return res.scalars().first() is not None

async def get_all_reports_for_admin(db: AsyncSession):
    result = await db.execute(
        select(Report).order_by(Report.created_at.desc())
    )
    reports = result.scalars().all()
    
    detailed_reports = []
    for r in reports:
        reporter = await get_user_by_id(db, int(r.reporter_id))  # type: ignore[arg-type]
        reported = await get_user_by_id(db, int(r.reported_user_id))  # type: ignore[arg-type]
        detailed_reports.append({
            "id": r.id,
            "reporter_id": r.reporter_id,
            "reporter_name": f"{reporter.first_name} {reporter.last_name}" if reporter else f"User #{r.reporter_id}",
            "reported_user_id": r.reported_user_id,
            "reported_user_name": f"{reported.first_name} {reported.last_name}" if reported else f"User #{r.reported_user_id}",
            "reason": r.reason,
            "description": r.description,
            "source": r.source,
            "status": r.status,
            "severity_score": r.severity_score,
            "admin_notes": r.admin_notes,
            "resolved_at": r.resolved_at,
            "created_at": r.created_at
        })
    return detailed_reports