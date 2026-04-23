import json
import shutil
import os
import random
import string

from datetime import date , datetime , timedelta , timezone

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, or_, and_

from db import engine, SessionLocal
# Added Referral, Transaction, BlockedUser and Report from intern's code
from models import Base, User, Message, Interaction, Referral, Transaction, BlockedUser, Report , OTPCode
# Added intern's wallet schemas
from schemas import RegisterUser, LoginUser, UserResponse, MessageCreate, UpdateUser, InteractionCreate, MatchmakerQuizParams, TransactionOut, ReferralHistoryItem, WalletInfo, ProfileVisibilityUpdate, OTPRequest, OTPVerify,SupportTicketCreate,SupportTicketOut
from crud import (
    create_user,
    authenticate_user,
    get_user_by_email,
    get_all_users,
    save_message,
    get_messages,
    get_user_by_mobile,
    create_support_ticket,
    update_user_presence,
    mark_messages_as_seen
)
from auth import create_access_token, get_current_user, verify_password, hash_password

app = FastAPI()

# =====================
# STARTUP (ASYNC DB INIT)
# =====================
@app.on_event("startup")
async def on_startup():
    print("ENGINE URL:", engine.url)

    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT current_database();"))
        print("Connected to DB:", result.scalar())

        await conn.run_sync(Base.metadata.create_all)  # ✅ ENABLE THIS

        print("Database tables ensured!")


# =====================
# CORS
# =====================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "https://apnasaadhi.com",
        "https://www.apnasaadhi.com",
        "https://apnashaadi.in",       # Added your new domain
        "https://www.apnashaadi.in",   # Added the www version of your new domain
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================
# SERVE STATIC FILES (IMAGES)
# =====================
os.makedirs("uploads", exist_ok=True) # Ensure folder exists before mounting
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# =====================
# DB DEPENDENCY
# =====================
async def get_db():
    async with SessionLocal() as db:
        yield db


# =====================
# AUTH ROUTES
# =====================
@app.post("/register", response_model=UserResponse)
async def register(
    user: RegisterUser,
    db: AsyncSession = Depends(get_db),
):
    existing = await get_user_by_email(db, user.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    existing_mobile = await get_user_by_mobile(db, user.mobile_no)
    if existing_mobile:
        raise HTTPException(status_code=400, detail="Mobile already registered")

    return await create_user(db, user)

@app.post("/login")
async def login(
    data: LoginUser,
    db: AsyncSession = Depends(get_db),
):
    # Determine if user is logging in with email or mobile
    if data.email:
        user = await get_user_by_email(db, data.email)
    elif data.mobile_no:
        user = await get_user_by_mobile(db, data.mobile_no)
    else:
        raise HTTPException(status_code=400, detail="Provide email or mobile number")

    # Verify password
    if not user or not verify_password(data.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check if account is activated by admin
    if not getattr(user, 'is_active', False):
        raise HTTPException(
            status_code=403,
            detail="Your account is pending activation. Please wait for admin approval."
        )

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "user_id": user.id,
        "first_name": user.first_name,
    }

# =====================
# BYPASS-ACTIVE LOGIN (Registration photo upload only)
# =====================
@app.post("/login-bypass-active")
async def login_bypass_active(
    data: LoginUser,
    db: AsyncSession = Depends(get_db),
):
    """Used internally right after registration to allow profile-pic upload before admin activates account."""
    if data.email:
        user = await get_user_by_email(db, data.email)
    elif data.mobile_no:
        user = await get_user_by_mobile(db, data.mobile_no)
    else:
        raise HTTPException(status_code=400, detail="Provide email or mobile number")

    if not user or not verify_password(data.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(user.id)
    return {"access_token": token, "user_id": user.id}

# =====================
# GET MY PROFILE 🔥
# =====================
@app.get("/profile/me")
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Auto-generate profile_id if missing
    if not user.profile_id:
        user.profile_id = f"AS{str(user.id).zfill(5)}"
        await db.commit()
        
    return user


# =====================
# PROFILE UPDATE 🔥
# =====================
@app.put("/profile/update")
async def update_profile(
    data: UpdateUser,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # This loops through only the data sent and updates it
    for key, value in data.dict(exclude_unset=True).items():
        if value is not None:
            setattr(user, key, value)

    await db.commit()
    await db.refresh(user)

    # ── Intern's update: Auto-recalculate profile completion & fire referral reward ──
    from crud import calculate_profile_score
    new_score = calculate_profile_score(user)
    if user.profile_completed != new_score:
        user.profile_completed = new_score
        await db.commit()
        await db.refresh(user)

    # If user just hit 100%, try to credit their referrer
    if user.profile_completed >= 100:
        ref_result = await db.execute(
            select(Referral).where(
                Referral.referred_id == user_id,
                Referral.reward_given == False,
            )
        )
        ref_row = ref_result.scalars().first()
        if ref_row:
            coins = 10
            done_count_res = await db.execute(
                select(Referral).where(
                    Referral.referrer_id == ref_row.referrer_id,
                    Referral.reward_given == True,
                )
            )
            done_count = len(done_count_res.scalars().all())
            if done_count + 1 == 5:
                coins += 20
            elif done_count + 1 == 10:
                coins += 50
            await _credit_coins(db, ref_row.referrer_id, coins, f"Referral reward: {coins} Apna Coins")
            ref_row.reward_given = True
            await db.commit()

    return {"message": "Profile updated successfully", "profile_completed": user.profile_completed}

# =====================
# PROFILE PIC UPLOAD 🔥
# =====================
@app.post("/upload/profile-pic")
async def upload_profile_pic(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    os.makedirs("uploads", exist_ok=True)

    file_location = f"uploads/{user_id}_{file.filename}"

    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()

    user.profile_pic = file_location

    await db.commit()

    return {"profile_pic": file_location}


# =====================
# PROFILE VISIBILITY
# =====================
@app.get("/profile/visibility")
async def get_profile_visibility(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"profile_visibility": user.profile_visibility or "public"}


@app.put("/profile/visibility")
async def update_profile_visibility(
    data: ProfileVisibilityUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.profile_visibility = data.profile_visibility
    await db.commit()
    return {"message": "Visibility updated", "profile_visibility": user.profile_visibility}


# =====================
# ACCOUNT INFO
# =====================
@app.get("/account/info")
async def get_account_info(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Auto-generate profile_id if missing
    if not user.profile_id:
        user.profile_id = f"AS{str(user.id).zfill(5)}"
        await db.commit()
    return {
        "profile_id": user.profile_id,
        "plan_type": user.plan_type or "free",
        "plan_expiry": user.plan_expiry.isoformat() if user.plan_expiry else None,
        "email": user.email,
        "mobile_no": user.mobile_no,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "profile_pic": user.profile_pic,
    }


# =====================
# SECURITY
# =====================
@app.post("/security/change-password")
async def change_password(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    old_password = payload.get("old_password", "")
    new_password = payload.get("new_password", "")

    if not old_password or not new_password:
        raise HTTPException(status_code=422, detail="Both old and new passwords are required")

    if len(new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify old password using bcrypt
    if not verify_password(old_password, user.password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Prevent reusing the same password
    if verify_password(new_password, user.password):
        raise HTTPException(status_code=422, detail="New password must be different from the current password")

    # Hash & save new password with bcrypt — old password will no longer work
    user.password = hash_password(new_password)
    await db.commit()
    await db.refresh(user)   # Ensure new hash is confirmed in DB session
    return {"message": "Password changed successfully. Please log in again with your new password."}


@app.post("/security/logout-all")
async def logout_all_devices(
    user_id: int = Depends(get_current_user),
):
    """
    Instructs the client to clear its local token.
    In production this would invalidate all active session rows in a user_sessions table.
    """
    return {"message": "Logged out from all devices", "clear_token": True}


# =====================
# BLOCK & REPORT
# =====================
@app.post("/block/{target_id}")
async def block_user(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    if user_id == target_id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    # Check already blocked
    existing = await db.execute(
        select(BlockedUser).where(
            BlockedUser.user_id == user_id,
            BlockedUser.blocked_user_id == target_id
        )
    )
    if existing.scalars().first():
        return {"message": "Already blocked"}
    block = BlockedUser(user_id=user_id, blocked_user_id=target_id)
    db.add(block)
    await db.commit()
    return {"message": "User blocked successfully"}


@app.delete("/block/{target_id}")
async def unblock_user(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(
        select(BlockedUser).where(
            BlockedUser.user_id == user_id,
            BlockedUser.blocked_user_id == target_id
        )
    )
    block = result.scalars().first()
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    await db.delete(block)
    await db.commit()
    return {"message": "User unblocked successfully"}


@app.get("/block/list")
async def get_blocked_users(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(
        select(BlockedUser).where(BlockedUser.user_id == user_id)
    )
    blocked = result.scalars().all()
    if not blocked:
        return []
    blocked_ids = [b.blocked_user_id for b in blocked]
    users_res = await db.execute(select(User).where(User.id.in_(blocked_ids)))
    safe = []
    for u in users_res.scalars().all():
        safe.append({
            "id": u.id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "profile_id": u.profile_id or f"AS{str(u.id).zfill(5)}",
            "profile_pic": u.profile_pic,
        })
    return safe


@app.post("/report/{target_id}")
async def report_user(
    target_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    reason = payload.get("reason", "Fake profile")
    if user_id == target_id:
        raise HTTPException(status_code=400, detail="Cannot report yourself")
    # Check if target exists
    tgt = await db.execute(select(User).where(User.id == target_id))
    if not tgt.scalars().first():
        raise HTTPException(status_code=404, detail="User not found")
    report = Report(reporter_id=user_id, reported_user_id=target_id, reason=reason)
    db.add(report)
    await db.commit()
    return {"message": "Report submitted successfully"}





# =====================
# USERS
# =====================
async def get_all_users_route(db, user_id):
    return await get_all_users(db, user_id)


@app.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    return await get_all_users(db, user_id)


# =====================
# MATCHMAKING LOGIC
# =====================
def calculate_match_percentage(current_user, target_user):
    score = 0
    
    # 1. STRICT GENDER CHECK
    if current_user.looking_for:
        if not target_user.gender or current_user.looking_for.lower() != target_user.gender.lower():
            return 0 
        score += 40
    else:
        if current_user.gender and target_user.gender and current_user.gender.lower() != target_user.gender.lower():
            score += 40
        else:
            score += 20

    # 2. STRICT AGE CHECK
    if current_user.preferred_min_age and current_user.preferred_max_age:
        if target_user.date_of_birth:
            age = date.today().year - target_user.date_of_birth.year
            if not (current_user.preferred_min_age <= age <= current_user.preferred_max_age):
                return 0 
            score += 30
    else:
        score += 30 
        
    # 3. Location (Soft Filter)
    if current_user.preferred_city and target_user.city:
        if current_user.preferred_city.lower() in target_user.city.lower():
            score += 15
    else:
        score += 10 

    # 4. Religion (Soft Filter)
    if current_user.preferred_religion and target_user.religion:
        if current_user.preferred_religion.lower() == target_user.religion.lower():
            score += 15
    else:
        score += 10 
        
    return max(score, 10) if score > 0 else 0

# ── Intern's Matchmaking Search Route ──
@app.post("/matchmaking/search")
async def search_matches(
    filters: dict,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user_res = await db.execute(select(User).where(User.id == user_id))
    current_user = current_user_res.scalars().first()

    # Get interacted IDs to exclude
    interactions_res = await db.execute(
        select(Interaction.target_id)
        .where(
            Interaction.user_id == user_id,
            Interaction.action.in_(['interest', 'reject'])
        )
    )
    interacted_ids = interactions_res.scalars().all()

    # Build dynamic query
    query = select(User).where(User.id != user_id)
    if interacted_ids:
        query = query.where(User.id.notin_(interacted_ids))

    # Apply filters
    min_age = filters.get("min_age")
    max_age = filters.get("max_age")
    religion = filters.get("religion")
    city = filters.get("city")
    gender = filters.get("gender")
    relationship_type = filters.get("relationship_type")  # NEW FILTER

    if gender:
        query = query.where(User.gender.ilike(gender))

    # Age filter (calculated from date_of_birth)
    if min_age or max_age:
        today = date.today()
        if min_age:
            min_dob = date(today.year - int(min_age), today.month, today.day)
            query = query.where(User.date_of_birth <= min_dob)
        if max_age:
            max_dob = date(today.year - int(max_age) - 1, today.month, today.day)
            query = query.where(User.date_of_birth > max_dob)

    if religion:
        query = query.where(User.religion.ilike(religion))

    if city:
        query = query.where(User.city.ilike(f"%{city}%"))

    if relationship_type:
        query = query.where(User.relationship_type.ilike(f"%{relationship_type}%"))

    users_res = await db.execute(query)
    found_users = users_res.scalars().all()

    # Get mutual IDs for visibility filter
    i_liked_res2 = await db.execute(
        select(Interaction.target_id).where(Interaction.user_id == user_id, Interaction.action == 'interest')
    )
    i_liked_ids2 = set(i_liked_res2.scalars().all())
    they_liked_res2 = await db.execute(
        select(Interaction.user_id).where(Interaction.target_id == user_id, Interaction.action == 'interest')
    )
    they_liked_ids2 = set(they_liked_res2.scalars().all())
    search_mutual_ids = i_liked_ids2.intersection(they_liked_ids2)

    results = []
    for u in found_users:
        # ── Profile Visibility Filter ──
        visibility = u.profile_visibility or "public"
        if visibility == "matches_only" and u.id not in search_mutual_ids:
            continue
        if visibility == "premium_only":
            continue

        match_pct = calculate_match_percentage(current_user, u)
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        user_data["match_percentage"] = match_pct
        user_data["match_reason"] = "Search Result" if match_pct < 90 else "Top Match 🌟"
        results.append(user_data)

    # Sort by match percentage
    results.sort(key=lambda x: x["match_percentage"], reverse=True)
    return results

@app.get("/matchmaking/suggested")
async def get_suggested_matches(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user_res = await db.execute(select(User).where(User.id == user_id))
    current_user = current_user_res.scalars().first()

    # Get mutual match IDs (both liked each other) — needed for "matches_only" filter
    i_liked_res = await db.execute(
        select(Interaction.target_id).where(Interaction.user_id == user_id, Interaction.action == 'interest')
    )
    i_liked_ids = set(i_liked_res.scalars().all())
    they_liked_res = await db.execute(
        select(Interaction.user_id).where(Interaction.target_id == user_id, Interaction.action == 'interest')
    )
    they_liked_ids = set(they_liked_res.scalars().all())
    mutual_ids = i_liked_ids.intersection(they_liked_ids)

    # 🔥 FIXED: Only hide users if we 'interest' or 'reject' them. Ignore 'visit' actions.
    interactions_res = await db.execute(
        select(Interaction.target_id)
        .where(
            Interaction.user_id == user_id,
            Interaction.action.in_(['interest', 'reject'])
        )
    )
    interacted_ids = interactions_res.scalars().all()

    # Fetch all OTHER users we haven't interacted with yet
    query = select(User).where(
        User.id != user_id,
        User.id.notin_(interacted_ids) if interacted_ids else True
    )
    users_res = await db.execute(query)
    all_users = users_res.scalars().all()

    suggestions = []
    for u in all_users:
        # ── Profile Visibility Filter ──
        visibility = u.profile_visibility or "public"
        if visibility == "matches_only" and u.id not in mutual_ids:
            continue  # hidden from non-matches
        if visibility == "premium_only":
            continue  # hidden from everyone in free search

        match_pct = calculate_match_percentage(current_user, u)
        
        # Now we show ALL matches above 0% in the swipe feed!
        if match_pct > 0:
            user_data = u.__dict__.copy()
            user_data.pop("_sa_instance_state", None)
            user_data.pop("password", None) 
            user_data["match_percentage"] = match_pct
            
            # Send a reason to the frontend for the badge
            if match_pct >= 90:
                user_data["match_reason"] = "Perfect Match 🌟"
            else:
                user_data["match_reason"] = "Suggested"
                
            suggestions.append(user_data)

    # Sort by highest match first
    suggestions.sort(key=lambda x: x["match_percentage"], reverse=True)
    return suggestions

@app.post("/interactions/action")
async def handle_interaction(
    data: InteractionCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Block check: Cannot send interest to someone who blocked you or whom you blocked
    blocked_check = await db.execute(
        select(BlockedUser).where(
            or_(
                and_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == data.target_id),
                and_(BlockedUser.user_id == data.target_id, BlockedUser.blocked_user_id == user_id),
            )
        )
    )
    if blocked_check.scalars().first():
        raise HTTPException(status_code=403, detail="Cannot interact with this user")

    # Save the interaction (Interest or Reject)
    new_interaction = Interaction(
        user_id=user_id,
        target_id=data.target_id,
        action=data.action
    )
    db.add(new_interaction)
    await db.commit()

    # If it's an interest, check if it's a mutual match (Did they already like me?)
    is_mutual = False
    if data.action == 'interest':
        check_mutual = await db.execute(
            select(Interaction).where(
                Interaction.user_id == data.target_id,
                Interaction.target_id == user_id,
                Interaction.action == 'interest'
            )
        )
        if check_mutual.scalars().first():
            is_mutual = True

    # 🔥 NEW: Send Real-Time WebSockets Notification to the target user
    if data.action == 'interest':
        # Get the sender's name so the notification is friendly
        sender_res = await db.execute(select(User).where(User.id == user_id))
        sender = sender_res.scalars().first()
        sender_name = sender.first_name if sender else "Someone"

        if is_mutual:
            notif_payload = {
                "type": "system_notification",
                "title": "It's a Match! 🎉",
                "body": f"You and {sender_name} liked each other!"
            }
        else:
            notif_payload = {
                "type": "system_notification",
                "title": "New Request! 💖",
                "body": f"{sender_name} sent you an interest."
            }
        
        # Fire it off through the WebSocket manager
        await manager.send_personal_message(notif_payload, data.target_id)

    return {"message": f"Successfully marked as {data.action}", "is_mutual_match": is_mutual}

# =====================
# UNDO REJECT
# =====================
@app.post("/interactions/undo")
async def undo_interaction(
    data: InteractionCreate, 
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Find the specific 'reject' interaction in the database
    result = await db.execute(
        select(Interaction).where(
            Interaction.user_id == user_id,
            Interaction.target_id == data.target_id,
            Interaction.action == 'reject'
        )
    )
    interaction = result.scalars().first()
    
    # If we found it, delete it so the user can see this profile again
    if interaction:
        await db.delete(interaction)
        await db.commit()
        
    return {"message": "Profile retrieved successfully"}


# =====================
# GET REJECTED PROFILES
# =====================
@app.get("/interactions/rejected")
async def get_rejected_profiles(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Find all IDs this user has rejected
    rejected_res = await db.execute(
        select(Interaction.target_id)
        .where(Interaction.user_id == user_id, Interaction.action == 'reject')
    )
    rejected_ids = rejected_res.scalars().all()

    if not rejected_ids:
        return []

    # Fetch those users' details
    users_res = await db.execute(select(User).where(User.id.in_(rejected_ids)))
    
    # Strip passwords before sending to frontend
    safe_users = []
    for u in users_res.scalars().all():
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        safe_users.append(user_data)
        
    return safe_users

# =====================
# MUTUAL & AUTO MATCHES 🔥
# =====================
@app.get("/matches/mutual")
async def get_mutual_matches(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user_res = await db.execute(select(User).where(User.id == user_id))
    current_user = current_user_res.scalars().first()

    # 1. Who did I send an interest to?
    i_liked_res = await db.execute(select(Interaction.target_id).where(Interaction.user_id == user_id, Interaction.action == 'interest'))
    i_liked_ids = set(i_liked_res.scalars().all())

    # 2. Who liked me?
    they_liked_res = await db.execute(select(Interaction.user_id).where(Interaction.target_id == user_id, Interaction.action == 'interest'))
    they_liked_ids = set(they_liked_res.scalars().all())

    # Mutual likes intersection
    mutual_ids = i_liked_ids.intersection(they_liked_ids)

    # 3. Who did I reject? (So we never auto-match with someone we rejected)
    i_rejected_res = await db.execute(select(Interaction.target_id).where(Interaction.user_id == user_id, Interaction.action == 'reject'))
    i_rejected_ids = set(i_rejected_res.scalars().all())

    # Fetch ALL other users to check for 90% auto-matches
    all_other_res = await db.execute(select(User).where(User.id != user_id))
    all_other_users = all_other_res.scalars().all()

    safe_users = []
    for u in all_other_users:
        if u.id in i_rejected_ids:
            continue

        match_pct = calculate_match_percentage(current_user, u)
        is_mutual = u.id in mutual_ids
        is_auto_match = match_pct >= 90  # 🌟 The 90% Auto-Match Trigger!

        # If they liked each other, OR the AI determined they are a 90%+ perfect match
        if is_mutual or is_auto_match:
            user_data = u.__dict__.copy()
            user_data.pop("_sa_instance_state", None)
            user_data.pop("password", None)
            user_data["match_percentage"] = match_pct
            user_data["match_reason"] = "Mutual Interest" if is_mutual else "Auto Matched (90%+)"
            safe_users.append(user_data)
            
    # Sort highest percentage first
    safe_users.sort(key=lambda x: x.get("match_percentage", 0), reverse=True)
    return safe_users


# =====================
# CHAT
# =====================
@app.get("/chat/{other_user_id}")
async def fetch_messages(
    other_user_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    return await get_messages(db, user_id, other_user_id)

from fastapi import UploadFile, File
import aiofiles
from datetime import datetime

# =====================
# CHAT ENDPOINTS
# =====================
@app.post("/chat/upload")
async def upload_media(file: UploadFile = File(...)):
    # Basic local storage (Update path as needed or switch to S3)
    os.makedirs("uploads", exist_ok=True) 
    file_path = f"uploads/{int(datetime.now().timestamp())}_{file.filename}" 
    
    async with aiofiles.open(file_path, 'wb') as out_file:
        content = await file.read()
        await out_file.write(content)
        
    if file.content_type.startswith("audio/"):
        media_type = "audio"
    elif file.content_type.startswith("video/"):
        media_type = "video"
    else:
        media_type = "image"
        
    return {"url": f"/{file_path}", "type": media_type}
@app.post("/chat/send")
async def send_message(
    data: MessageCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    # (Keep your block check logic here...)
    return await save_message(db, user_id, data.receiver_id, data.message, data.media_url, data.media_type)

# =====================
# WEBSOCKET MANAGER
# =====================
# =====================
# WEBSOCKET MANAGER
# =====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    async def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            try:
                self.active_connections[user_id].remove(websocket)
            except ValueError:
                pass
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            dead = []
            for ws in self.active_connections[user_id]:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                await self.disconnect(user_id, ws)

    async def broadcast_presence(self, user_id: int, is_online: bool):
        message = {
            "type": "presence",
            "user_id": user_id,
            "is_online": is_online,
            "last_seen": datetime.now().isoformat()
        }
        for uid, connections in self.active_connections.items():
            if uid != user_id:  # Broadcast to others
                for ws in connections:
                    try:
                        await ws.send_json(message)
                    except Exception:
                        pass

    # 👇 THIS IS THE METHOD YOU NEED TO ADD 👇
    def is_online(self, user_id: int) -> bool:
        """Check if a specific user currently has an active WebSocket connection."""
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0


manager = ConnectionManager()

# =====================
# WEBSOCKET ROUTE
# =====================
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket, 
    user_id: int, 
    db: AsyncSession = Depends(get_db) # Ensure you inject your DB session
):
    await manager.connect(user_id, websocket)
    
    # Set DB status online & broadcast
    await update_user_presence(db, user_id, True)
    await manager.broadcast_presence(user_id, True)
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg_data = json.loads(data)
            except json.JSONDecodeError:
                continue

            receiver_id = msg_data.get("receiver_id")
            msg_type = msg_data.get("type", "message")

            if msg_type == "seen":
                # Sender of this WS event has SEEN messages from receiver_id
                await mark_messages_as_seen(db, sender_id=receiver_id, receiver_id=user_id)
                # Notify the original sender that their messages were seen
                await manager.send_personal_message({"type": "seen", "sender_id": user_id}, receiver_id)
                continue

            if receiver_id:
                # Forward to receiver
                if manager.is_online(receiver_id):
                    msg_data["status"] = "delivered"
                
                await manager.send_personal_message(msg_data, int(receiver_id))

                # Echo delivery confirmation back to sender
                if msg_type not in ("typing", "seen"):
                    confirm = {
                        "type": "delivered",
                        "message_id": msg_data.get("id"),
                        "receiver_id": int(receiver_id),
                        "sender_id": user_id,
                    }
                    await manager.send_personal_message(confirm, user_id)

    except WebSocketDisconnect:
        await manager.disconnect(user_id, websocket)
        # Set offline & broadcast
        await update_user_presence(db, user_id, False)
        await manager.broadcast_presence(user_id, False)


# =====================
# PENDING MATCH REQUESTS
# =====================
@app.get("/interactions/pending")
async def get_pending_requests(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # 1. Find who sent 'interest' to me
    they_liked_me_res = await db.execute(
        select(Interaction.user_id)
        .where(Interaction.target_id == user_id, Interaction.action == 'interest')
    )
    they_liked_me_ids = set(they_liked_me_res.scalars().all())

    # 2. Find who I have already interacted with (liked or rejected)
    # 🔥 FIXED: Ignore 'visit' so looking at a profile doesn't remove a pending request!
    i_interacted_res = await db.execute(
        select(Interaction.target_id)
        .where(
            Interaction.user_id == user_id,
            Interaction.action.in_(['interest', 'reject'])
        )
    )
    i_interacted_ids = set(i_interacted_res.scalars().all())

    # 3. Pending requests = People who liked me minus people I already swiped on
    pending_ids = they_liked_me_ids - i_interacted_ids

    if not pending_ids:
        return []

    # Fetch those users' details
    users_res = await db.execute(select(User).where(User.id.in_(pending_ids)))
    
    safe_users = []
    for u in users_res.scalars().all():
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        safe_users.append(user_data)
        
    return safe_users


# =====================
# PROFILE VISITORS
# =====================
@app.get("/interactions/visitors")
async def get_profile_visitors(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Find users who have an interaction action of 'visit' on my profile
    visitors_res = await db.execute(
        select(Interaction.user_id)
        .where(Interaction.target_id == user_id, Interaction.action == 'visit')
    )
    
    # Use a set to only count unique visitors
    visitor_ids = set(visitors_res.scalars().all())

    if not visitor_ids:
        return []

    # Fetch those users' details
    users_res = await db.execute(select(User).where(User.id.in_(visitor_ids)))
    
    safe_users = []
    for u in users_res.scalars().all():
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        safe_users.append(user_data)
        
    return safe_users


# =====================
# LOG A PROFILE VISIT
# =====================
@app.post("/interactions/visit")
async def log_profile_visit(
    data: InteractionCreate, # Expects target_id
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Don't log if the user is looking at their own profile
    if user_id == data.target_id:
        return {"message": "Self visit ignored"}

    # Check if this person already visited this profile to avoid duplicate logs
    existing_visit = await db.execute(
        select(Interaction).where(
            Interaction.user_id == user_id,
            Interaction.target_id == data.target_id,
            Interaction.action == 'visit'
        )
    )
    
    if not existing_visit.scalars().first():
        new_visit = Interaction(
            user_id=user_id,
            target_id=data.target_id,
            action='visit'
        )
        db.add(new_visit)
        await db.commit()
        
    return {"message": "Visit logged"}


# =====================
# GET PUBLIC PROFILE (OTHER USER)
# =====================
@app.get("/profile/user/{target_id}")
async def get_public_profile(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    result = await db.execute(select(User).where(User.id == target_id))
    user = result.scalars().first()
    
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")

    # ── Profile Visibility Gate ──
    visibility = user.profile_visibility or "public"
    if visibility != "public" and user_id != target_id:
        if visibility == "matches_only":
            # Check mutual match
            i_liked = await db.execute(
                select(Interaction).where(
                    Interaction.user_id == user_id,
                    Interaction.target_id == target_id,
                    Interaction.action == 'interest'
                )
            )
            they_liked = await db.execute(
                select(Interaction).where(
                    Interaction.user_id == target_id,
                    Interaction.target_id == user_id,
                    Interaction.action == 'interest'
                )
            )
            if not i_liked.scalars().first() or not they_liked.scalars().first():
                raise HTTPException(status_code=403, detail="This profile is only visible to matched users")
        elif visibility == "premium_only":
            raise HTTPException(status_code=403, detail="This profile is only visible to premium members")
        
    # Strip password before sending!
    user_data = user.__dict__.copy()
    user_data.pop("_sa_instance_state", None)
    user_data.pop("password", None)
    
    return user_data


# =====================
# FREE "AI" QUIZ SEARCH 🔥 (Intern Updated)
# =====================
@app.post("/ai-matchmaker/quiz-search")
async def ai_quiz_search(
    data: MatchmakerQuizParams,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    ans = data.answers
    base = select(User).where(User.id != user_id)

    # ── Helper: build query applying only the chosen filter fields ────
    def build_query(fields):
        q = base
        if "city" in fields and ans.get("city"):
            q = q.where(User.city.ilike(f"%{ans['city']}%"))
        if "religion" in fields and ans.get("religion"):
            q = q.where(User.religion.ilike(f"%{ans['religion']}%"))
        if "profession" in fields and ans.get("profession"):
            q = q.where(User.profession.ilike(f"%{ans['profession']}%"))
        if "caste" in fields and ans.get("caste"):
            q = q.where(User.caste.ilike(f"%{ans['caste']}%"))
        if "diet" in fields and ans.get("diet"):
            q = q.where(User.diet.ilike(f"%{ans['diet']}%"))
        if "marital_status" in fields and ans.get("marital_status"):
            q = q.where(User.marital_status.ilike(f"%{ans['marital_status']}%"))
        if "education" in fields and ans.get("education"):
            q = q.where(User.education.ilike(f"%{ans['education']}%"))
        if "mother_tongue" in fields and ans.get("mother_tongue"):
            q = q.where(User.mother_tongue.ilike(f"%{ans['mother_tongue']}%"))
        if "habits" in fields and ans.get("habits"):
            q = q.where(User.habits.ilike(f"%{ans['habits']}%"))
        if "family_type" in fields and ans.get("family_type"):
            q = q.where(User.family_type.ilike(f"%{ans['family_type']}%"))
        if "relationship_type" in fields and ans.get("relationship_type"):
            q = q.where(User.relationship_type.ilike(f"%{ans['relationship_type']}%"))
        return q

    ALL_FIELDS  = ["city", "religion", "profession", "caste", "diet",
                   "marital_status", "education", "mother_tongue", "habits",
                   "family_type", "relationship_type"]
    SOFT_FIELDS = ["city", "religion", "relationship_type"]

    # Pass 1 — strict: ALL selected filters AND together
    result = await db.execute(build_query(ALL_FIELDS))
    matches = result.scalars().all()

    # Pass 2 — relaxed: keep only city + religion
    if not matches:
        result = await db.execute(build_query(SOFT_FIELDS))
        matches = result.scalars().all()

    # Pass 3 — broadest: no preference filters, just exclude self
    if not matches:
        result = await db.execute(base)
        matches = result.scalars().all()

    safe_matches = []
    for u in matches[:10]:
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        user_data["match_percentage"] = 95
        safe_matches.append(user_data)

    return {"suggested_profiles": safe_matches}

# =====================================================================
# REFERRAL & WALLET SYSTEM (Intern Added)
# =====================================================================

async def _credit_coins(db, user_id: int, amount: int, description: str):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if user:
        user.coin_balance = (user.coin_balance or 0) + amount
        txn = Transaction(user_id=user_id, amount=amount, description=description)
        db.add(txn)


@app.get("/referral/validate/{code}")
async def validate_referral_code(code: str, db=Depends(get_db)):
    result = await db.execute(select(User).where(User.referral_code == code.upper()))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="Invalid referral code")
    return {"valid": True, "referrer_name": user.first_name, "code": user.referral_code}


@app.get("/referral/my-code")
async def get_my_referral_code(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "referral_code": user.referral_code,
        "share_link": f"https://apnasaadhi.com/register?ref={user.referral_code}",
        "local_link": f"http://localhost:5173/register?ref={user.referral_code}",
        "coin_balance": user.coin_balance or 0,
    }


@app.get("/referral/history")
async def get_referral_history(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    refs_result = await db.execute(select(Referral).where(Referral.referrer_id == user_id))
    refs = refs_result.scalars().all()

    needs_commit = False
    history = []

    for ref in refs:
        ru = (await db.execute(select(User).where(User.id == ref.referred_id))).scalars().first()
        if not ru:
            continue

        profile_pct = ru.profile_completed or 0
        profile_complete = profile_pct >= 100

        # ── Auto-grant reward if profile is 100% but reward not yet given ──
        if profile_complete and not ref.reward_given:
            # Calculate coins (base 10 + milestone bonuses)
            coins = 10
            done_count_res = await db.execute(
                select(Referral).where(
                    Referral.referrer_id == user_id,
                    Referral.reward_given == True,
                )
            )
            done_count = len(done_count_res.scalars().all())

            # Milestone bonuses
            if done_count + 1 == 5:
                coins += 20   # 5th referral bonus
            elif done_count + 1 == 10:
                coins += 50   # 10th referral bonus

            await _credit_coins(db, user_id, coins, f"Referral reward for {ru.first_name}: {coins} Apna Coins")
            ref.reward_given = True
            needs_commit = True

        status = "Completed" if profile_complete else "Pending"
        history.append({
            "referred_name": f"{ru.first_name} {ru.last_name}",
            "status": status,
            "coins_earned": 10 if ref.reward_given else 0,
            "profile_completion": profile_pct,
        })

    # Commit any auto-granted rewards
    if needs_commit:
        await db.commit()

    total_completed = sum(1 for h in history if h["status"] == "Completed")
    total_earned = sum(h["coins_earned"] for h in history)
    return {
        "history": history,
        "total_referrals": len(history),
        "successful_referrals": total_completed,
        "total_coins_earned_from_referrals": total_earned,
    }



@app.post("/referral/check-reward")
async def check_and_grant_referral_reward(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalars().first()
    if not user or (user.profile_completed or 0) < 100:
        return {"rewarded": False, "message": "Profile not yet 100%"}
    ref_result = await db.execute(
        select(Referral).where(Referral.referred_id == user_id, Referral.reward_given == False)
    )
    ref_row = ref_result.scalars().first()
    if not ref_row:
        return {"rewarded": False, "message": "No pending referral reward"}
    coins = 10
    done_count_res = await db.execute(
        select(Referral).where(Referral.referrer_id == ref_row.referrer_id, Referral.reward_given == True)
    )
    done_count = len(done_count_res.scalars().all())
    if done_count + 1 == 5:
        coins += 20
    elif done_count + 1 == 10:
        coins += 50
    await _credit_coins(db, ref_row.referrer_id, coins, f"Referral reward: {coins} Apna Coins")
    ref_row.reward_given = True
    await db.commit()
    return {"rewarded": True, "coins_awarded": coins}


@app.get("/wallet/info")
async def get_wallet_info(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    txn_result = await db.execute(
        select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc())
    )
    txns = txn_result.scalars().all()
    total_earned = sum(t.amount for t in txns if t.amount > 0)
    total_spent = abs(sum(t.amount for t in txns if t.amount < 0))
    return {
        "coin_balance": user.coin_balance or 0,
        "total_earned": total_earned,
        "total_spent": total_spent,
        "transactions": [
            {"id": t.id, "amount": t.amount, "description": t.description,
             "created_at": t.created_at.isoformat() if t.created_at else ""}
            for t in txns
        ],
    }


@app.post("/wallet/spend")
async def spend_coins(payload: dict, db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    amount = int(payload.get("amount", 0))
    description = payload.get("description", "Coins spent")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if (user.coin_balance or 0) < amount:
        raise HTTPException(status_code=400, detail="Insufficient coin balance")
    user.coin_balance -= amount
    txn = Transaction(user_id=user_id, amount=-amount, description=description)
    db.add(txn)
    await db.commit()
    return {"message": "Coins deducted", "new_balance": user.coin_balance}


@app.get("/referral/leaderboard")
async def referral_leaderboard(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    from collections import Counter
    refs_res = await db.execute(select(Referral.referrer_id).where(Referral.reward_given == True))
    counts = Counter(refs_res.scalars().all())
    board = []
    for rid, cnt in counts.most_common(10):
        u = (await db.execute(select(User).where(User.id == rid))).scalars().first()
        if u:
            board.append({
                "name": u.first_name,
                "referrals": cnt,
                "coins": u.coin_balance or 0,
                "level": "Ambassador" if cnt >= 50 else "Pro" if cnt >= 10 else "Beginner",
            })
    return {"leaderboard": board}


# =====================================================================
# OTP EMAIL VERIFICATION ROUTES
# =====================================================================

def _generate_otp() -> str:
    """Return a cryptographically random 6-digit numeric string."""
    return "".join(random.choices(string.digits, k=6))


def _smtp_is_configured() -> bool:
    """Return True only when real (non-placeholder) SMTP credentials exist in .env."""
    PLACEHOLDER_MARKERS = {"your_email", "your_gmail", "placeholder", "example.com", "your_gmail_app_password", "youremail", "yourpassword"}
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    return bool(smtp_user and smtp_pass
                and not any(m in smtp_user.lower() for m in PLACEHOLDER_MARKERS)
                and not any(m in smtp_pass.lower() for m in PLACEHOLDER_MARKERS))


async def _send_otp_email(to_email: str, otp: str) -> None:
    """
    Send the OTP to the user via Gmail SMTP (TLS / STARTTLS).
    Credentials are read from .env at call-time.
    Raises an exception if sending fails — caller decides what to do.
    """
    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    email_from = os.getenv("EMAIL_FROM", smtp_user)

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;
                background:#fdf2f8;border-radius:16px;border:1px solid #fbcfe8;">
      <h2 style="color:#be185d;text-align:center;margin-bottom:8px;">ApnaShadhi 💍</h2>
      <p style="color:#475569;text-align:center;margin-bottom:28px;font-size:14px;">
        Your Email Verification Code
      </p>
      <div style="background:#ffffff;border-radius:12px;padding:24px;text-align:center;
                  border:1px solid #fce7f3;box-shadow:0 4px 12px rgba(219,39,119,.08);">
        <p style="font-size:42px;font-weight:700;letter-spacing:12px;color:#db2777;
                  margin:0;font-family:monospace;">{otp}</p>
      </div>
      <p style="color:#64748b;font-size:13px;text-align:center;margin-top:20px;">
        This code expires in <strong>5 minutes</strong>.
        Do not share this with anyone.
      </p>
      <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:28px;">
        If you did not request this, please ignore this email.
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your ApnaShadhi Email Verification Code"
    msg["From"] = email_from
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=smtp_port,
        username=smtp_user,
        password=smtp_pass,
        start_tls=True,
    )


@app.post("/auth/send-otp")
async def send_otp(
    data: OTPRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 – Request an OTP for a given email.
    - Generates a fresh 6-digit OTP
    - Stores it in otp_codes table (expires in 5 min)
    - Sends it to the user's inbox
    - Invalidates any previously issued, unused OTPs for that email
    """
    email = data.email.lower().strip()

    # Invalidate all previous unused OTPs for this email
    prev_result = await db.execute(
        select(OTPCode).where(
            OTPCode.email == email,
            OTPCode.is_used == False,
        )
    )
    for old_otp in prev_result.scalars().all():
        old_otp.is_used = True

    # Generate new OTP
    otp = _generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    print(f"\n[DEBUG] Generated OTP for {email}: {otp}\n")
    new_otp = OTPCode(
        email=email,
        otp_code=otp,
        expires_at=expires_at,
        is_used=False,
    )
    db.add(new_otp)
    await db.commit()

    # ── Send OTP ──────────────────────────────────────────────────────
    # If real SMTP credentials exist → try to send real email.
    # On any failure (or unconfigured SMTP) → fall back to DEV MODE:
    # the OTP is printed to the Uvicorn terminal so you can still test.
    smtp_ok = _smtp_is_configured()
    email_sent = False

    if smtp_ok:
        try:
            await _send_otp_email(email, otp)
            email_sent = True
        except Exception as exc:
            # SMTP failed — log warning and fall through to console print
            print(f"[send-otp] WARNING: SMTP send failed: {exc}")
            print("[send-otp] Falling back to DEV MODE - OTP printed below.")

    if not email_sent:
        # ── DEV / CONSOLE MODE ────────────────────────────────────────
        print("\n" + "=" * 56)
        print(f"  [EMAIL] OTP for {email}")
        print(f"  [CODE]  {otp}")
        print(f"  [INFO]  Expires in 5 minutes")
        if smtp_ok:
            print("  (SMTP send failed - OTP printed as fallback)")
        else:
            print("  (SMTP not configured - OTP printed to console)")
        print("=" * 56 + "\n")
        return {
            "message": "[DEV MODE] OTP printed to server console. Check your Uvicorn terminal.",
            "dev_mode": True,
        }

    return {"message": "OTP sent! Please check your inbox.", "dev_mode": False}


@app.post("/auth/verify-otp")
async def verify_otp(
    data: OTPVerify,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 – Verify the OTP submitted by the user.
    Returns 200 with proceed flag on success, 400/410 on failure.
    After success the frontend is free to call /register.
    """
    email = data.email.lower().strip()
    now = datetime.now(timezone.utc)

    # Find the latest unused OTP for this email
    result = await db.execute(
        select(OTPCode)
        .where(
            OTPCode.email == email,
            OTPCode.is_used == False,
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    otp_record = result.scalars().first()

    if not otp_record:
        raise HTTPException(
            status_code=400,
            detail="No OTP found for this email. Please request a new one."
        )

    # Check expiry
    if otp_record.expires_at.replace(tzinfo=timezone.utc) < now:
        otp_record.is_used = True
        await db.commit()
        raise HTTPException(
            status_code=410,
            detail="OTP has expired. Please request a new one."
        )

    # Check correctness
    if otp_record.otp_code != data.otp.strip():
        raise HTTPException(
            status_code=400,
            detail="Incorrect OTP. Please try again."
        )

    # Mark as used
    otp_record.is_used = True
    await db.commit()

    return {
        "message": "Email verified! Proceed to registration.",
        "email_verified": True,
        "email": email,
    }



# =====================
# SUPPORT TICKETS
# =====================

@app.post("/support/ticket", response_model=SupportTicketOut)
async def submit_support_ticket(
    data: SupportTicketCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — no authentication required, anyone can submit a ticket.
    'description' from the frontend is stored as 'issue' in the DB.
    'priority'   from the frontend is stored as 'urgency' in the DB.
    email_verified is automatically resolved by checking the users table.
    """
    ticket = await create_support_ticket(
        db=db,
        email=str(data.email),
        subject=data.subject,
        category=data.category,
        urgency=data.urgency,
        issue=data.issue,
    )
    return ticket


@app.delete("/chat/message/{message_id}")
async def delete_chat_message(
    message_id: int, 
    type: str, # 'me' or 'everyone'
    db: AsyncSession = Depends(get_db), 
    user_id: int = Depends(get_current_user)
):
    # Fetch the message
    result = await db.execute(select(Message).where(Message.id == message_id))
    msg = result.scalars().first()

    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    # If deleting for everyone (Only the sender can do this)
    if type == "everyone":
        if msg.sender_id != user_id:
            raise HTTPException(status_code=403, detail="You can only delete your own messages for everyone")
        
        # Soft delete: update the message text and remove media
        msg.is_deleted = True
        msg.message = "🚫 This message was deleted"
        msg.media_url = None
        
        await db.commit()
        return {"status": "deleted for everyone"}
        
    # If deleting just for "me"
    elif type == "me":
        # In a real app, you'd have a 'deleted_by_sender' and 'deleted_by_receiver' boolean column.
        # For now, if we just want to hide it, we can hard delete if they own it, or just ignore.
        # Assuming you just want a hard delete for simplicity:
        await db.delete(msg)
        await db.commit()
        return {"status": "deleted for me"}

    raise HTTPException(status_code=400, detail="Invalid delete type")