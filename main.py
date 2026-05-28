import math
import json
import shutil
import os
import random
import string
import httpx 

from pydantic import BaseModel
from schemas import AadhaarInitRequest, AadhaarVerifyRequest

from contextlib import asynccontextmanager

from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, or_, and_, update, func

from db import engine, SessionLocal
# Added Referral, Transaction, BlockedUser and Report from intern's code
from models import Base, User, Message, Interaction, Referral, Transaction, BlockedUser, Report , OTPCode
# Added intern's wallet schemas & new deactivation schemas
from schemas import (
    RegisterUser, LoginUser, UserResponse, MessageCreate, UpdateUser, InteractionCreate, MatchmakerQuizParams, 
    TransactionOut, ReferralHistoryItem, WalletInfo, ProfileVisibilityUpdate, OTPRequest, OTPVerify, 
    SupportTicketCreate, SupportTicketOut, ForgotPasswordRequest, ResetPasswordConfirm,
    DeactivateAccountRequest, DeleteAccountRequest, ReportCreate, AdminReportAction
)
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
    mark_messages_as_seen,
    deactivate_account,
    delete_account_permanently,
    get_user_by_id,          # 🔥 ADDED
    sanitize_user_dict,      # 🔥 ADDED
    PG_SECRET,
    get_admin_settings_data

)
from auth import create_access_token, get_current_user, verify_password, hash_password
# Add to schemas import:
from schemas import AdminCreate, AdminLogin, AdminOut

# Add to crud import:
from crud import (
    create_admin_user, authenticate_admin, get_admin_dashboard_stats,
    get_all_users_for_admin, toggle_user_ban_status, get_all_reports_for_admin
)

# Add to auth import:
from auth import get_current_admin

from dotenv import load_dotenv
load_dotenv()


# =====================
# IN-MEMORY GEOCODE CACHE
# Avoids hitting Nominatim repeatedly for the same city during a single search.
# Format: { "city|state": (lat, lon) }
# =====================
from crud import get_coordinates_from_city as _geocode_fn
import asyncio
_geocode_cache: dict = {}

async def get_cached_geocode(city: str, state: str = None):
    """Returns (lat, lon) for a city, using an in-memory cache to avoid
    repeated Nominatim calls for the same city during a search loop."""
    key = f"{(city or '').lower().strip()}|{(state or '').lower().strip()}"
    if key in _geocode_cache:
        return _geocode_cache[key]
    lat, lon = await _geocode_fn(city, state)
    if lat is not None and lon is not None:
        _geocode_cache[key] = (lat, lon)
        return lat, lon
    _geocode_cache[key] = (None, None)  # Cache failures too, to avoid repeat calls
    return None, None
# =====================
# EMAIL UTILITY STUBS (Prevent NameError)
# =====================
def send_deactivation_email(email, first_name, reactivation_deadline):
    print(f"[MAIL] Deactivation email sent to {email}. Deadline: {reactivation_deadline}")

def send_deletion_email(email, first_name):
    print(f"[MAIL] Permanent deletion email sent to {email}.")

# =====================
# STARTUP (ASYNC DB INIT) — using lifespan (replaces deprecated @app.on_event)
# =====================
@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    print("ENGINE URL:", engine.url)
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT current_database();"))
        print("Connected to DB:", result.scalar())
        await conn.run_sync(Base.metadata.create_all)
        print("Database tables ensured!")

        # ── Safe column migrations (ALTER TABLE ... ADD COLUMN IF NOT EXISTS) ──
        # These run on every startup but are no-ops if the column already exists.
        migrations = [
            # Aadhaar KYC verification
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_aadhaar_verified BOOLEAN NOT NULL DEFAULT false;",
            # Selfie / face verification
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_selfie_verified BOOLEAN NOT NULL DEFAULT false;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS face_embedding TEXT;",
        ]
        for sql in migrations:
            await conn.execute(text(sql))
        print("Column migrations applied!")

    yield  # App runs here


app=FastAPI(lifespan=lifespan)

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

# =====================================================================
# AADHAAR VERIFICATION — Real UIDAI OTP via Setu KYC API
# =====================================================================
# How it works:
#   1. User enters their 12-digit Aadhaar number
#   2. We call Setu's API → Setu calls UIDAI → UIDAI sends OTP to the
#      mobile number that the user registered with their Aadhaar
#   3. We store Setu's refId in DB
#   4. User enters OTP → we call Setu verify with refId → Setu confirms with UIDAI
#   5. On success → mark user as is_aadhaar_verified = True
#
# Setup (takes ~5 minutes):
#   1. Sign up free at https://bridge.setu.co
#   2. Go to Products → Aadhaar Validation → Aadhaar OTP Validation → Enable Sandbox
#   3. Copy CLIENT_ID, CLIENT_SECRET, PRODUCT_INSTANCE_ID into .env.local
#   4. To go live: complete KYC with Setu and switch to production credentials
# =====================================================================

SETU_CLIENT_ID           = os.getenv("SETU_CLIENT_ID", "")
SETU_CLIENT_SECRET       = os.getenv("SETU_CLIENT_SECRET", "")
SETU_PRODUCT_INSTANCE_ID = os.getenv("SETU_PRODUCT_INSTANCE_ID", "")
# Sandbox: https://dg.setu.co  |  Production: https://dg.setu.co (same URL, different credentials)
SETU_BASE_URL            = os.getenv("SETU_BASE_URL", "https://dg.setu.co")


def _setu_headers() -> dict:
    return {
        "x-client-id": SETU_CLIENT_ID,
        "x-client-secret": SETU_CLIENT_SECRET,
        "x-product-instance-id": SETU_PRODUCT_INSTANCE_ID,
        "Content-Type": "application/json",
    }


def _setu_configured() -> bool:
    return bool(SETU_CLIENT_ID and SETU_CLIENT_SECRET and SETU_PRODUCT_INSTANCE_ID)


@app.post("/verification/aadhaar/send-otp")
async def init_aadhaar_verification(
    data: AadhaarInitRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # 1. Validate Aadhaar format
    aadhaar = data.aadhaar_number.strip()
    if len(aadhaar) != 12 or not aadhaar.isdigit():
        raise HTTPException(status_code=400, detail="Invalid Aadhaar number. Must be exactly 12 digits.")

    # 2. Check Setu is configured
    if not _setu_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Aadhaar verification service is not configured. "
                "Please set SETU_CLIENT_ID, SETU_CLIENT_SECRET and SETU_PRODUCT_INSTANCE_ID "
                "in your .env.local. Get free sandbox credentials at https://bridge.setu.co"
            )
        )

    # 3. Get user
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_aadhaar_verified:
        raise HTTPException(status_code=400, detail="Your Aadhaar is already verified.")

    # 4. Call Setu → Setu calls UIDAI → OTP sent to Aadhaar-registered mobile
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SETU_BASE_URL}/api/v2/aadhaar-validation/aadhaar-otp/generate",
                headers=_setu_headers(),
                json={"aadhaar": aadhaar},
            )
    except httpx.RequestError as e:
        print(f"[Setu] Network error: {e}")
        raise HTTPException(status_code=503, detail="Could not reach verification service. Please try again.")

    if resp.status_code not in (200, 201):
        err = resp.json()
        detail = err.get("message") or err.get("error") or "Failed to initiate Aadhaar verification."
        print(f"[Setu] send-otp error {resp.status_code}: {err}")
        raise HTTPException(status_code=400, detail=detail)

    setu_data = resp.json()
    ref_id = setu_data.get("refId") or setu_data.get("data", {}).get("refId", "")
    if not ref_id:
        raise HTTPException(status_code=502, detail="Verification service returned an unexpected response.")

    print(f"[Setu] ✅ OTP triggered for Aadhaar ...{aadhaar[-4:]} — refId: {ref_id}")

    # 5. Store refId in otp_codes table (keyed to user email, expires in 10 min)
    #    The otp_code field stores Setu's refId so we can retrieve it in the verify step.
    await db.execute(
        text(
            "UPDATE otp_codes SET is_used = true "
            "WHERE pgp_sym_decrypt(email_encrypted, :secret) = :email AND is_used = false"
        ),
        {"secret": PG_SECRET, "email": user.email}
    )
    new_otp = OTPCode(
        email_encrypted=func.pgp_sym_encrypt(user.email, PG_SECRET),
        otp_code=ref_id,                                          # ← stores Setu refId
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        is_used=False,
    )
    db.add(new_otp)
    await db.commit()

    return {
        "message": "OTP sent to the mobile number registered with your Aadhaar by UIDAI. Check your phone.",
        "ref_id": "stored",   # ref_id is stored server-side; frontend doesn't need it
    }


@app.post("/verification/aadhaar/verify")
async def verify_aadhaar_otp(
    data: AadhaarVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    if not _setu_configured():
        raise HTTPException(status_code=503, detail="Aadhaar verification service is not configured.")

    # 1. Get user
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_aadhaar_verified:
        raise HTTPException(status_code=400, detail="Aadhaar is already verified.")

    # 2. Retrieve the stored Setu refId from DB
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OTPCode).where(
            func.pgp_sym_decrypt(OTPCode.email_encrypted, PG_SECRET) == user.email,
            OTPCode.is_used == False,
            OTPCode.expires_at > now,
        ).order_by(OTPCode.created_at.desc()).limit(1)
    )
    otp_record = result.scalars().first()

    if not otp_record:
        raise HTTPException(
            status_code=400,
            detail="Verification session expired. Please click 'Get OTP' again."
        )

    ref_id = otp_record.otp_code   # The Setu refId stored in the generate step

    # 3. Call Setu → Setu verifies OTP with UIDAI
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SETU_BASE_URL}/api/v2/aadhaar-validation/aadhaar-otp/verify",
                headers=_setu_headers(),
                json={"otp": data.otp.strip(), "refId": ref_id},
            )
    except httpx.RequestError as e:
        print(f"[Setu] Network error during verify: {e}")
        raise HTTPException(status_code=503, detail="Could not reach verification service. Please try again.")

    if resp.status_code not in (200, 201):
        err = resp.json()
        detail = err.get("message") or err.get("error") or "Invalid OTP. Please check and try again."
        print(f"[Setu] verify error {resp.status_code}: {err}")
        raise HTTPException(status_code=400, detail=detail)

    # 4. Success — mark OTP as used and user as verified
    otp_record.is_used = True
    user.is_aadhaar_verified = True
    await db.commit()

    print(f"[Setu] ✅ User {user_id} ({user.first_name}) Aadhaar verified successfully via UIDAI.")

    return {
        "message": "Aadhaar verified successfully! Your profile now shows a verified badge. 🛡️",
        "is_aadhaar_verified": True
    }



# =====================================================================
# SELFIE / FACE VERIFICATION
# =====================================================================

class SelfieVerifyRequest(BaseModel):
    descriptor: list[float]   # 128-element face descriptor from face-api.js

@app.post("/verification/selfie")
async def verify_selfie(
    data: SelfieVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    import numpy as np, json

    if len(data.descriptor) != 128:
        raise HTTPException(status_code=400, detail="Invalid face descriptor. Expected 128 values.")

    incoming = np.array(data.descriptor, dtype=np.float64)
    norm = np.linalg.norm(incoming)
    if norm == 0:
        raise HTTPException(status_code=400, detail="Face descriptor is empty. Please retake your selfie.")
    incoming = incoming / norm

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_selfie_verified:
        return {"message": "Your selfie is already verified! ✅", "is_selfie_verified": True}

    # Compare against stored reference if one exists
    if user.face_embedding:
        stored = np.array(json.loads(user.face_embedding), dtype=np.float64)
        stored_norm = np.linalg.norm(stored)
        if stored_norm > 0:
            stored = stored / stored_norm
        distance = float(np.linalg.norm(incoming - stored))
        print(f"[Selfie] User {user_id} face distance: {distance:.4f}")
        if distance > 0.5:
            raise HTTPException(
                status_code=400,
                detail=f"Face did not match (score={distance:.2f}). Please ensure good lighting and try again."
            )
        user.is_selfie_verified = True
        await db.commit()
        return {"message": "Selfie verified! Your profile now shows a verified badge. 📸", "is_selfie_verified": True}

    # First capture — store as reference AND mark verified
    user.face_embedding = json.dumps(data.descriptor)
    user.is_selfie_verified = True
    await db.commit()
    print(f"[Selfie] User {user_id} selfie stored and verified.")
    return {"message": "Selfie verified successfully! Your profile now shows a verified badge. 📸", "is_selfie_verified": True}


# =====================
# GET MY PROFILE 🔥
# =====================
@app.get("/profile/me")
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    user = await get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Auto-generate profile_id if missing
    if not user.profile_id:
        user.profile_id = f"AS{str(user.id).zfill(5)}"
        await db.commit()

    # 🔥 Auto-geocode from city if coordinates are missing or zero (Null Island)
    needs_geocode = (
        not user.latitude or not user.longitude
        or user.latitude == 0.0 or user.longitude == 0.0
    )
    if needs_geocode and user.city:
        from crud import get_coordinates_from_city
        lat, lon = await get_coordinates_from_city(user.city, user.state)
        if lat is not None and lon is not None:
            user.latitude = lat
            user.longitude = lon
            await db.commit()
            print(f"[AutoGeocode] User {user_id} '{user.city}' → ({lat}, {lon})")
        
    return sanitize_user_dict(user)


# =====================
# PROFILE UPDATE 🔥 (FIXED REFERRAL LOGIC)
# =====================
@app.put("/profile/update")
async def update_profile(
    data: UpdateUser,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 🔥 We will track if their address changed during this update
    address_changed = False

    # This loops through only the data sent and updates it
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            # ⚠️ GUARD: Never let raw GPS lat/lon from the browser overwrite the profile's
            # authoritative city-geocoded coordinates. Coordinates are only ever set by
            # the city-geocoding step below (when city/state changes). Raw GPS from the
            # browser is transient and is passed directly as query-params to the search API.
            if key in ("latitude", "longitude"):
                continue
            # Encrypt PII updates immediately
            if key == "email":
                user.email_encrypted = func.pgp_sym_encrypt(value.lower().strip(), PG_SECRET)
            elif key == "mobile_no":
                user.mobile_encrypted = func.pgp_sym_encrypt(value.strip(), PG_SECRET)
            else:
                setattr(user, key, value)
                # 🔥 If they updated their city or state, flag it!
                if key in ["city", "state"]:
                    address_changed = True

    # 🔥 If the address changed (or coords are still missing), re-geocode from city
    needs_geocode = (
        address_changed
        or not user.latitude or not user.longitude
        or user.latitude == 0.0 or user.longitude == 0.0
    )
    if needs_geocode and user.city:
        from crud import get_coordinates_from_city
        lat, lon = await get_coordinates_from_city(user.city, user.state)
        if lat is not None and lon is not None:
            user.latitude = lat
            user.longitude = lon

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
            ref_row.reward_given = True 
            await db.flush()
            
            done_count_res = await db.execute(
                select(Referral).where(
                    Referral.referrer_id == ref_row.referrer_id,
                    Referral.reward_given == True,
                )
            )
            done_count = len(done_count_res.scalars().all())
            
            coins = 10
            if done_count == 5:
                coins += 20
            elif done_count == 10:
                coins += 50
                
            from crud import _credit_coins
            await _credit_coins(db, int(ref_row.referrer_id), coins, f"Referral reward: {coins} Apna Coins")  
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

    user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.profile_pic = file_location  # type: ignore[assignment]

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
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"profile_visibility": user.profile_visibility or "public"}


@app.put("/profile/visibility")
async def update_profile_visibility(
    data: ProfileVisibilityUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    user = await get_user_by_id(db, user_id)
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
    user = await get_user_by_id(db, user_id)
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
        "email": user.email, # Dynamically attached in get_user_by_id
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

    user = await get_user_by_id(db, user_id)
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


# =====================
# AI SECURITY & MODERATION SERVICES
# =====================
def run_ai_moderation_scans(reason: str, description: str | None, source: str) -> dict:
    """
    Placeholder architecture for future AI moderation engine:
    - Toxicity detection
    - Scam detection
    - Fake profile detection
    """
    scans = {
        "toxicity_score": 0.0,
        "scam_likelihood": "low",
        "fake_profile_match_rate": 0.0,
        "flagged_keywords": []
    }
    
    desc_lower = (description or "").lower()
    
    # 1. Toxicity check (Harassment, abusive words)
    toxic_words = ["abuse", "kill", "idiot", "hate", "ugly", "fool", "harass", "stupid"]
    flagged_toxic = [w for w in toxic_words if w in desc_lower]
    if flagged_toxic:
        scans["toxicity_score"] = min(0.1 + (len(flagged_toxic) * 0.2), 0.95)
        scans["flagged_keywords"].extend(flagged_toxic)
        
    # 2. Scam/Fraud detection (money, crypto, bank, wire, transfer, pay)
    scam_words = ["money", "crypto", "pay", "bank", "transfer", "western union", "card", "rupees", "dollar"]
    flagged_scam = [w for w in scam_words if w in desc_lower]
    if flagged_scam:
        scans["scam_likelihood"] = "medium" if len(flagged_scam) < 2 else "high"
        scans["flagged_keywords"].extend(flagged_scam)
        
    # 3. Fake profile detection
    fake_words = ["fake", "stolen", "impersonator", "bot", "photo", "not real"]
    flagged_fake = [w for w in fake_words if w in desc_lower]
    if flagged_fake:
        scans["fake_profile_match_rate"] = min(0.2 + (len(flagged_fake) * 0.25), 0.9)
        scans["flagged_keywords"].extend(flagged_fake)
        
    return scans

def send_admin_report_email(report_id: int, reporter_id: int, reporter_profile_id: str, reporter_name: str, reported_id: int, reported_profile_id: str, reported_name: str, reason: str, description: str | None, source: str, severity: int):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = os.getenv("SMTP_HOST", "smtp.hostinger.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER", "care@apnasaadhi.com")
    smtp_pass = os.getenv("SMTP_PASS", "Apna@2026")
    email_from = os.getenv("EMAIL_FROM", "ApnaShaadi <care@apnasaadhi.com>")
    admin_email = "care@apnasaadhi.com"
    
    msg = MIMEMultipart()
    msg['From'] = email_from
    msg['To'] = admin_email
    msg['Subject'] = f"[URGENT] New User Report Submitted - Severity {severity}/5"
    
    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #333333; background-color: #f9f9f9; }}
            .container {{ max-width: 600px; margin: 20px auto; padding: 30px; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); border-top: 6px solid #e63946; }}
            .header {{ font-size: 20px; font-weight: bold; color: #e63946; margin-bottom: 20px; text-transform: uppercase; letter-spacing: 1px; }}
            .details-table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
            .details-table th, .details-table td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eeeeee; }}
            .details-table th {{ background-color: #f8f9fa; color: #495057; font-weight: 600; width: 35%; }}
            .severity-badge {{ padding: 4px 10px; border-radius: 20px; font-weight: bold; font-size: 12px; }}
            .severity-5 {{ background-color: #ffe3e3; color: #e63946; }}
            .severity-4 {{ background-color: #fff0e3; color: #f4a261; }}
            .severity-3 {{ background-color: #fef9e7; color: #e9c46a; }}
            .severity-2 {{ background-color: #eafaf1; color: #2a9d8f; }}
            .severity-1 {{ background-color: #e8f4fd; color: #264653; }}
            .description-box {{ background-color: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 4px solid #cccccc; margin-top: 15px; font-style: italic; }}
            .footer {{ font-size: 12px; color: #888888; text-align: center; margin-top: 30px; border-top: 1px solid #eeeeee; padding-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">🚨 New Abuse/Safety Report</div>
            <p>Hello Admin Team,</p>
            <p>A new user report has been submitted on the ApnaShaadi platform. Please review the details below immediately:</p>
            
            <table class="details-table">
                <tr>
                    <th>Report ID</th>
                    <td>#{report_id}</td>
                </tr>
                <tr>
                    <th>Severity Score</th>
                    <td><span class="severity-badge severity-{severity}">{severity} / 5</span></td>
                </tr>
                <tr>
                    <th>Reason</th>
                    <td style="font-weight: bold; color: #264653;">{reason}</td>
                </tr>
                <tr>
                    <th>Source (Where)</th>
                    <td>{source.upper()}</td>
                </tr>
                <tr>
                    <th>Reporter User</th>
                    <td>{reporter_name} (ID: {reporter_id} / Profile ID: {reporter_profile_id})</td>
                </tr>
                <tr>
                    <th>Reported User</th>
                    <td style="font-weight: bold; color: #e63946;">{reported_name} (ID: {reported_id} / Profile ID: {reported_profile_id})</td>
                </tr>
                <tr>
                    <th>Submitted At</th>
                    <td>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} IST</td>
                </tr>
            </table>
            
            <div class="header" style="font-size: 15px; margin-top: 25px; margin-bottom: 5px; color: #495057;">User Description:</div>
            <div class="description-box">
                {description if description else "No additional description provided by the reporter."}
            </div>
            
            <p style="margin-top: 25px;">Please log into the <a href="https://apnasaadhi.com/admin" target="_blank" style="color: #e63946; font-weight: bold; text-decoration: none;">Admin Moderation Dashboard</a> to take action (Warn, Ban, or Delete Account).</p>
            
            <div class="footer">
                This is an automated notification from the ApnaShaadi Security & Moderation System.<br/>
                &copy; 2026 ApnaShaadi. All rights reserved.
            </div>
        </div>
    </body>
    </html>
    """
    
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, admin_email, msg.as_string())
        server.quit()
        print(f"[MAIL] Admin report email successfully sent to {admin_email}")
    except Exception as e:
        print(f"[MAIL] Error sending admin notification email: {str(e)}")

@app.post("/report")
async def create_user_report(
    payload: ReportCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    from schemas import ReportCreate
    from crud import check_duplicate_report, calculate_severity_score
    
    reported_user_id = payload.reported_user_id
    
    if user_id == reported_user_id:
        raise HTTPException(status_code=400, detail="You cannot report yourself.")
        
    # Check if reported user exists
    target_user = await get_user_by_id(db, reported_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="The reported user does not exist.")
        
    # Prevent duplicate spam reports
    is_duplicate = await check_duplicate_report(db, user_id, reported_user_id)
    if is_duplicate:
        raise HTTPException(
            status_code=400, 
            detail="You have already submitted a pending report for this profile. Our team is reviewing it."
        )
        
    # Calculate Severity Score
    severity = calculate_severity_score(payload.reason)
    
    # AI Moderation Engine Scans (Placeholder / Analysis)
    scans = run_ai_moderation_scans(payload.reason, payload.description, payload.source)
    if scans["toxicity_score"] > 0.8 or scans["scam_likelihood"] == "high":
        # Boost severity score by 1 if AI triggers high flags
        severity = min(severity + 1, 5)
        
    # Save Report
    report = Report(
        reporter_id=user_id,
        reported_user_id=reported_user_id,
        reason=payload.reason,
        description=payload.description,
        source=payload.source,
        status="pending",
        severity_score=severity
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    
    # Get Reporter details for Email
    reporter_user = await get_user_by_id(db, user_id)
    reporter_profile_id = (reporter_user.profile_id if reporter_user else None) or f"AS{str(user_id).zfill(5)}"
    reported_profile_id = target_user.profile_id or f"AS{str(reported_user_id).zfill(5)}"

    reporter_name = f"{reporter_user.first_name} {reporter_user.last_name}" if reporter_user else f"User #{user_id}"
    reported_name = f"{target_user.first_name} {target_user.last_name}"

    # Send email notification immediately
    send_admin_report_email(
        report_id=int(report.id),  # type: ignore[arg-type]
        reporter_id=user_id,
        reporter_profile_id=reporter_profile_id,
        reporter_name=reporter_name,
        reported_id=reported_user_id,
        reported_profile_id=reported_profile_id,
        reported_name=reported_name,
        reason=payload.reason,
        description=payload.description,
        source=payload.source,
        severity=severity
    )
    
    return {
        "status": "success",
        "message": "Your report has been submitted successfully.",
        "report_id": report.id,
        "severity_score": severity,
        "ai_scan_results": scans
    }


# =====================
# USERS
# =====================
@app.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    return await get_all_users(db, user_id)

# 🔥 Distance Calculation Helper
def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculates distance in kilometers using the Haversine formula."""
    if None in (lat1, lon1, lat2, lon2):
        return float('inf')  # No coordinates at all
    # 🔥 FIX: Treat (0.0, 0.0) as "no location" — that's Null Island off the West African
    # coast, not a valid Indian city. This was causing 0km distances for all un-geocoded profiles.
    if lat1 == 0.0 and lon1 == 0.0:
        return float('inf')  # Searcher has no real GPS
    if lat2 == 0.0 and lon2 == 0.0:
        return float('inf')  # Profile was never geocoded
    R = 6371  # Earth radius in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat/2) * math.sin(dLat/2) + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
        math.sin(dLon/2) * math.sin(dLon/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


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

# ── Matchmaking Search Route ──
@app.post("/matchmaking/search")
async def search_matches(
    filters: dict,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user = await get_user_by_id(db, user_id)

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
    relationship_type = filters.get("relationship_type")

    # ✅ Gender filter: use explicit filter OR auto-apply from current user's looking_for preference
    gender = filters.get("gender")
    if not gender and current_user.looking_for:
        gender = current_user.looking_for

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

    # 📍 Extract GPS params ONCE, outside the loop
    req_lat = filters.get("latitude")
    req_lng = filters.get("longitude")
    req_radius = filters.get("radius")  # in km; None means no radius limit

    results = []
    for u in found_users:
        # ── Profile Visibility Filter ──
        visibility = u.profile_visibility or "public"
        if visibility == "matches_only" and u.id not in search_mutual_ids:
            continue
        if visibility == "premium_only":
            continue

        # 📍 Calculate distance and enforce radius
        distance_km = None
        if req_lat is not None and req_lng is not None:
            dist = calculate_distance(req_lat, req_lng, u.latitude, u.longitude)

            if dist == float('inf'):
                # Profile has no stored GPS (NULL or 0,0).
                # Use city-based geocoding for THIS request only — do NOT overwrite
                # the profile's stored coords so each profile keeps one authoritative lat/lon.
                if u.city:
                    p_lat, p_lon = await get_cached_geocode(u.city, u.state)
                    if p_lat is not None and p_lon is not None:
                        dist = calculate_distance(req_lat, req_lng, p_lat, p_lon)
                        if dist != float('inf'):
                            distance_km = round(dist, 1)

                # Still no distance after geocode attempt — handle gracefully
                if distance_km is None:
                    if req_radius is not None:
                        continue  # Can't verify radius → skip
                    # No radius limit → include without distance (city shown in frontend)
            else:
                distance_km = round(dist, 1)

            # ✅ RADIUS ENFORCEMENT
            if req_radius is not None and distance_km is not None:
                try:
                    if distance_km > float(req_radius):
                        continue
                except (TypeError, ValueError):
                    pass

        match_pct = calculate_match_percentage(current_user, u)

        user_data = sanitize_user_dict(u)
        user_data["match_percentage"] = match_pct
        user_data["match_reason"] = "Search Result" if match_pct < 90 else "Top Match 🌟"
        user_data["distance_km"] = distance_km

        results.append(user_data)

    # 📍 Sort by distance if GPS search, otherwise sort by match percentage
    if req_lat is not None and req_lng is not None:
        results.sort(key=lambda x: (x.get("distance_km") or 9999, -x["match_percentage"]))
    else:
        results.sort(key=lambda x: x["match_percentage"], reverse=True)
        
    return results

@app.post("/users/sync-locations")
async def sync_existing_user_locations(db: AsyncSession = Depends(get_db)):
    """
    Geocodes all users who have no GPS coordinates saved.
    Respects Nominatim rate limit: 1 request/second.
    """
    from crud import get_coordinates_from_city
    
    query = select(User).where(
        or_(
            User.latitude == None,
            User.latitude == 0.0,
            User.longitude == None,
            User.longitude == 0.0
        )
    )
    result = await db.execute(query)
    users_to_update = result.scalars().all()

    updated_count = 0
    failed_count = 0

    for u in users_to_update:
        if u.city:
            lat, lon = await get_coordinates_from_city(u.city, u.state)
            if lat is not None and lon is not None:
                u.latitude = lat
                u.longitude = lon
                updated_count += 1
                print(f"[Sync] ✅ {u.first_name} ({u.city}) → ({lat}, {lon})")
            else:
                failed_count += 1
                print(f"[Sync] ❌ Failed: {u.first_name} ({u.city})")
            # 🔥 Nominatim rate limit: max 1 request/second
            await asyncio.sleep(1.1)

    await db.commit()
    _geocode_cache.clear()  # Flush cache after bulk update

    return {
        "message": "Sync complete",
        "profiles_updated": updated_count,
        "profiles_failed": failed_count,
        "total_processed": len(users_to_update)
    }


@app.get("/test/geocode")
async def test_geocode_endpoint(city: str, state: str = None):
    """Debug endpoint: test if Nominatim geocoding is working for a given city."""
    from crud import get_coordinates_from_city
    lat, lon = await get_coordinates_from_city(city, state)
    if lat is None:
        return {"success": False, "city": city, "state": state,
                "message": "Nominatim returned no result. Check city spelling."}
    return {"success": True, "city": city, "state": state,
            "latitude": lat, "longitude": lon}

# =====================
# GEOCODE MY PROFILE
# =====================
@app.post("/profile/geocode")
async def geocode_my_profile(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    Geocode the current user's profile city/state via Nominatim and save the result.
    Called automatically by the frontend when a user's profile has no coordinates.
    """
    from crud import get_coordinates_from_city

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.city:
        raise HTTPException(status_code=400, detail="No city set on profile — cannot geocode")

    lat, lon = await get_coordinates_from_city(user.city, user.state)

    if lat is None or lon is None:
        raise HTTPException(
            status_code=422,
            detail=f"Could not geocode city '{user.city}'. Check spelling or try again later."
        )

    user.latitude = lat
    user.longitude = lon
    await db.commit()

    print(f"[Geocode] ✅ User {user_id} '{user.city}' → ({lat}, {lon})")
    return {"latitude": lat, "longitude": lon, "city": user.city, "message": "Coordinates updated successfully"}

@app.get("/matchmaking/suggested")
async def get_suggested_matches(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
    lat: float = None,
    lng: float = None,
):
    current_user = await get_user_by_id(db, user_id)

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

    # 🔥 Get blocked list
    blocks_res = await db.execute(
        select(BlockedUser).where(
            or_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == user_id)
        )
    )
    blocked_ids = {b.blocked_user_id if b.user_id == user_id else b.user_id for b in blocks_res.scalars().all()}

    # 🔥 FIXED: Only hide users if we 'interest' or 'reject' them. Ignore 'visit' actions.
    interactions_res = await db.execute(
        select(Interaction.target_id)
        .where(
            Interaction.user_id == user_id,
            Interaction.action.in_(['interest', 'reject'])
        )
    )
    interacted_ids = set(interactions_res.scalars().all())

    # 🔥 Merge interacted and blocked IDs
    exclude_ids = list(interacted_ids | blocked_ids)

    # Fetch all OTHER users we haven't interacted with yet
    query = select(User).where(User.id != user_id)
    if exclude_ids:
        query = query.where(User.id.notin_(exclude_ids))
    users_res = await db.execute(query)
    all_users = users_res.scalars().all()

    # Treat (0.0, 0.0) as "no GPS" — Null Island is not a valid location
    req_lat = lat if (lat is not None and lat != 0.0) else None
    req_lng = lng if (lng is not None and lng != 0.0) else None

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
            user_data = sanitize_user_dict(u)
            user_data["match_percentage"] = match_pct

            # Send a reason to the frontend for the badge
            if match_pct >= 90:
                user_data["match_reason"] = "Perfect Match 🌟"
            else:
                user_data["match_reason"] = "Suggested"

            # 📍 Calculate distance if the caller supplied their GPS coords
            distance_km = None
            if req_lat is not None and req_lng is not None:
                dist = calculate_distance(req_lat, req_lng, u.latitude, u.longitude)
                if dist == float('inf'):
                    # Profile has no GPS — use city geocoding for THIS request only.
                    # Do NOT save to DB so each profile keeps one authoritative lat/lon.
                    if u.city:
                        p_lat, p_lon = await get_cached_geocode(u.city, u.state)
                        if p_lat is not None and p_lon is not None:
                            dist = calculate_distance(req_lat, req_lng, p_lat, p_lon)
                            if dist != float('inf'):
                                distance_km = round(dist, 1)
                else:
                    distance_km = round(dist, 1)

            user_data["distance_km"] = distance_km
            suggestions.append(user_data)

    # Sort: by distance first (if GPS provided), then by match percentage
    if req_lat is not None and req_lng is not None:
        suggestions.sort(key=lambda x: (x.get("distance_km") or 9999, -x["match_percentage"]))
    else:
        suggestions.sort(key=lambda x: x["match_percentage"], reverse=True)
    return suggestions

# (Duplicate /matchmaking/suggested route removed — see the correct one above)

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

    # 🔥 Send Real-Time WebSocket Notifications
    if data.action == 'interest':
        # Fetch both users for personalised notification text
        sender = await get_user_by_id(db, user_id)
        receiver = await get_user_by_id(db, data.target_id)
        sender_name   = sender.first_name   if sender   else "Someone"
        receiver_name = receiver.first_name if receiver else "Someone"

        if is_mutual:
            # ── MUTUAL MATCH ────────────────────────────────────────────────
            # Notify the ORIGINAL SENDER (data.target_id) — their interest was now accepted
            await manager.send_personal_message({
                "type": "system_notification",
                "sub_type": "interest_accepted",
                "title": "Interest Accepted! 🎉",
                "body": f"{sender_name} accepted your interest! You are now matched and can chat!"
            }, data.target_id)
            print(f"[WS] interest_accepted → original sender (user {data.target_id})")

            # Notify the ACCEPTOR (user_id) — confirm they're now matched
            await manager.send_personal_message({
                "type": "system_notification",
                "sub_type": "interest_accepted",
                "title": "It's a Match! 🎉",
                "body": f"You and {receiver_name} are now matched! Start chatting."
            }, user_id)
            print(f"[WS] interest_accepted → acceptor (user {user_id})")

        else:
            # ── NON-MUTUAL INTEREST ─────────────────────────────────────────
            # 1. Notify the RECEIVER that they got an interest request
            await manager.send_personal_message({
                "type": "system_notification",
                "sub_type": "interest_received",
                "title": "New Interest Request 💖",
                "body": f"{sender_name} has sent you an interest request!"
            }, data.target_id)
            print(f"[WS] interest_received → receiver (user {data.target_id})")

            # 2. Notify the SENDER with a confirmation that their interest was sent
            await manager.send_personal_message({
                "type": "system_notification",
                "sub_type": "interest_sent",
                "title": "Interest Sent! 💖",
                "body": f"Your interest has been sent to {receiver_name}. We'll notify you when they respond!"
            }, user_id)
            print(f"[WS] interest_sent → sender (user {user_id})")

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
        safe_users.append(sanitize_user_dict(u))
        
    return safe_users

# =====================
# MUTUAL & AUTO MATCHES 🔥
# =====================
@app.get("/matches/mutual")
async def get_mutual_matches(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user = await get_user_by_id(db, user_id)

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

    # 🔥 UPDATED: Fetch block list to mask online status/hide if blocked
    blocks_res = await db.execute(select(BlockedUser).where(or_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == user_id)))
    blocked_ids = {b.blocked_user_id if b.user_id == user_id else b.user_id for b in blocks_res.scalars().all()}

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
            user_data = sanitize_user_dict(u)
            user_data["match_percentage"] = match_pct
            user_data["match_reason"] = "Mutual Interest" if is_mutual else "Auto Matched (90%+)"
            
            # 🔥 Hide presence if blocked
            if u.id in blocked_ids:
                user_data["is_blocked"] = True
                user_data["is_online"] = False
                user_data["last_seen"] = None
            else:
                user_data["is_blocked"] = False
                
            safe_users.append(user_data)
            
    # Sort highest percentage first
    safe_users.sort(key=lambda x: x.get("match_percentage", 0), reverse=True)
    return safe_users

# =====================
# UNREAD MESSAGES
# =====================
@app.get("/chat/unread")
async def get_unread_messages(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    Must be placed ABOVE /chat/{other_user_id} so FastAPI doesn't 
    confuse 'unread' for an integer ID.
    """
    # Count unread messages grouped by sender
    result = await db.execute(
        select(Message.sender_id, func.count(Message.id).label("unread_count"))
        .where(
            Message.receiver_id == user_id,
            Message.status != "seen",
            Message.is_deleted == False
        )
        .group_by(Message.sender_id)
    )
    
    # Format the data
    unread_data = [{"sender_id": row.sender_id, "count": row.unread_count} for row in result.all()]
    total_unread = sum(item["count"] for item in unread_data)

    return {
        "total_unread": total_unread, 
        "details": unread_data
    }
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
import aiofiles  # type: ignore[import-untyped]
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
        
    content_type = file.content_type or ""
    if content_type.startswith("audio/"):
        media_type = "audio"
    elif content_type.startswith("video/"):
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
    # 🔥 UPDATED: Block Sending Check added!
    blocked_check = await db.execute(
        select(BlockedUser).where(
            or_(
                and_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == data.receiver_id),
                and_(BlockedUser.user_id == data.receiver_id, BlockedUser.blocked_user_id == user_id)
            )
        )
    )
    if blocked_check.scalars().first():
        raise HTTPException(status_code=403, detail="Cannot send message. User is blocked.")

    return await save_message(db, user_id, data.receiver_id, data.message, data.media_url, data.media_type)

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
                # 🔒 Block Check: Reject WS message delivery if either party has blocked the other
                ws_block_check = await db.execute(
                    select(BlockedUser).where(
                        or_(
                            and_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == int(receiver_id)),
                            and_(BlockedUser.user_id == int(receiver_id), BlockedUser.blocked_user_id == user_id)
                        )
                    )
                )
                if ws_block_check.scalars().first():
                    # Silently reject — send error only to the sender
                    await manager.send_personal_message({
                        "type": "error",
                        "message": "Cannot send message. User is blocked."
                    }, user_id)
                    continue

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
        safe_users.append(sanitize_user_dict(u))
        
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
        safe_users.append(sanitize_user_dict(u))
        
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
    # 🔥 UPDATED: Block Profile Viewing Check
    blocked_check = await db.execute(select(BlockedUser).where(or_(
        and_(BlockedUser.user_id == user_id, BlockedUser.blocked_user_id == target_id),
        and_(BlockedUser.user_id == target_id, BlockedUser.blocked_user_id == user_id)
    )))
    if blocked_check.scalars().first(): 
        raise HTTPException(status_code=403, detail="Profile unavailable")

    user = await get_user_by_id(db, target_id)
    
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
        
    return sanitize_user_dict(user)

# =====================
# FREE "AI" QUIZ SEARCH 🔥 (Intern Updated)
# =====================
CITIES_BY_STATE = {
    "Andaman & Nicobar Islands": ["Port Blair", "Diglipur", "Mayabunder"],
    "Andhra Pradesh": ["Vijayawada", "Visakhapatnam", "Tirupati", "Guntur", "Nellore", "Kurnool", "Rajahmundry", "Kadapa", "Kakinada", "Eluru", "Ongole", "Anantapur", "Chittoor", "Vizianagaram", "Bhimavaram"],
    "Arunachal Pradesh": ["Itanagar", "Naharlagun", "Pasighat", "Tawang", "Ziro"],
    "Assam": ["Guwahati", "Silchar", "Dibrugarh", "Jorhat", "Nagaon", "Tezpur", "Tinsukia", "Bongaigaon", "Dhubri", "Karimganj"],
    "Bihar": ["Patna", "Gaya", "Bhagalpur", "Muzaffarpur", "Bihar Sharif", "Purnia", "Darbhanga", "Arrah", "Begusarai", "Katihar", "Munger", "Hajipur", "Sasaram"],
    "Chandigarh": ["Chandigarh"],
    "Chhattisgarh": ["Raipur", "Bhilai", "Bilaspur", "Korba", "Durg", "Rajnandgaon", "Jagdalpur", "Ambikapur"],
    "Dadra & Nagar Haveli and Daman & Diu": ["Daman", "Diu", "Silvassa"],
    "Delhi (NCT)": ["New Delhi", "Delhi NCR", "Dwarka", "Rohini", "Janakpuri", "Vasant Kunj", "Saket"],
    "Goa": ["Panaji", "Margao", "Vasco da Gama", "Mapusa", "Ponda"],
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot", "Bhavnagar", "Jamnagar", "Gandhinagar", "Junagadh", "Anand", "Vapi", "Ankleshwar", "Surendranagar", "Bhuj", "Bharuch", "Navsari"],
    "Haryana": ["Faridabad", "Gurgaon", "Panipat", "Ambala", "Hisar", "Rohtak", "Karnal", "Yamunanagar", "Sonipat", "Bhiwani", "Panchkula", "Sirsa"],
    "Himachal Pradesh": ["Shimla", "Dharamshala", "Solan", "Mandi", "Palampur", "Baddi", "Nahan", "Hamirpur", "Manali", "Kullu"],
    "Jammu & Kashmir": ["Jammu", "Srinagar", "Anantnag", "Baramulla"],
    "Jharkhand": ["Ranchi", "Jamshedpur", "Dhanbad", "Bokaro", "Deoghar", "Hazaribagh", "Giridih", "Ramgarh"],
    "Karnataka": ["Bangalore", "Mysore", "Hubli", "Mangalore", "Belagavi", "Kalaburagi", "Davangere", "Ballari", "Tumkur", "Udupi", "Shimoga", "Bagalkot", "Bidar", "Kolar", "Hassan"],
    "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode", "Thrissur", "Kollam", "Palakkad", "Alappuzha", "Kannur", "Kottayam", "Malappuram", "Thalassery"],
    "Ladakh": ["Leh", "Kargil"],
    "Lakshadweep": ["Kavaratti"],
    "Madhya Pradesh": ["Bhopal", "Indore", "Jabalpur", "Gwalior", "Ujjain", "Sagar", "Rewa", "Satna", "Dewas", "Chhindwara", "Khandwa", "Burhanpur"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik", "Thane", "Aurangabad", "Solapur", "Kolhapur", "Navi Mumbai", "Palghar", "Amravati", "Latur", "Nanded", "Sangli", "Satara", "Jalgaon", "Akola", "Baramati", "Kalyan-Dombivli", "Vasai-Virar"],
    "Manipur": ["Imphal", "Thoubal", "Bishnupur", "Churachandpur"],
    "Meghalaya": ["Shillong", "Tura", "Jowai", "Nongstoin"],
    "Mizoram": ["Aizawl", "Lunglei", "Saiha", "Champhai"],
    "Nagaland": ["Kohima", "Dimapur", "Mokokchung", "Tuensang"],
    "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela", "Puri", "Berhampur", "Sambalpur", "Khordha", "Balasore", "Bhadrak", "Baripada"],
    "Puducherry": ["Pondicherry", "Auroville", "Karaikal", "Mahe", "Yanam"],
    "Punjab": ["Ludhiana", "Amritsar", "Jalandhar", "Patiala", "Bathinda", "Mohali", "Pathankot", "Faridkot", "Firozpur", "Moga", "Hoshiarpur"],
    "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur", "Kota", "Bikaner", "Ajmer", "Alwar", "Bharatpur", "Sikar", "Bhilwara", "Pali", "Sri Ganganagar"],
    "Sikkim": ["Gangtok", "Namchi", "Geyzing", "Mangan"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Salem", "Tirunelveli", "Tiruppur", "Vellore", "Erode", "Thoothukudi", "Dindigul", "Thanjavur", "Kanchipuram", "Ooty"],
    "Telangana": ["Hyderabad", "Warangal", "Nizamabad", "Karimnagar", "Khammam", "Ramagundam", "Mahbubnagar", "Nalgonda", "Adilabad"],
    "Tripura": ["Agartala", "Udaipur", "Dharmanagar", "Kailasahar"],
    "Uttar Pradesh": ["Lucknow", "Kanpur", "Agra", "Varanasi", "Meerut", "Allahabad (Prayagraj)", "Ghaziabad", "Bareilly", "Aligarh", "Moradabad", "Noida", "Greater Noida", "Mathura", "Bulandshahr", "Gorakhpur", "Jhansi", "Firozabad", "Farrukhabad", "Mirzapur", "Hathras", "Hapur", "Saharanpur"],
    "Uttarakhand": ["Dehradun", "Haridwar", "Roorkee", "Haldwani", "Kashipur", "Rudrapur", "Nainital", "Rishikesh"],
    "West Bengal": ["Kolkata", "Howrah", "Durgapur", "Siliguri", "Asansol", "Kharagpur", "Kalyani", "Darjeeling", "Bankura", "Krishnanagar", "Konnagar", "Bardhaman", "Malda", "Baharampur", "Serampore", "Haldia", "Jalpaiguri", "Hooghly","Singur", "Bishnupur","Midnapore", "Barasat", "Dum Dum"]
}

@app.post("/ai-matchmaker/quiz-search")
async def ai_quiz_search(
    data: MatchmakerQuizParams,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    ans = data.answers
    
    # 🌍 Location Handling
    selected_state = ans.get("state")
    selected_city = ans.get("city")
    
    # Standardize 'any' / 'no preference' selections to None to bypass specific filtering
    if selected_state and selected_state.lower() in ["any", "no preference", "any state", "any / no preference", "open to all", "open to all states"]:
        selected_state = None
    if selected_city and selected_city.lower() in ["any", "no preference", "any city", "any / no preference", "open to all", "open to all cities"]:
        selected_city = None

    # Normalize input for validation against mapping
    state_key = next((k for k in CITIES_BY_STATE if k.lower() == (selected_state or "").lower()), None)
    
    if selected_state:
        if not state_key:
            # Validation 2: If state does not exist in mapping
            return {"status": "empty", "message": "No profiles found"}
            
        if selected_city:
            # Validation 2: If city is selected but not in the selected state's list
            city_exists = any(c.lower() == selected_city.lower() for c in CITIES_BY_STATE[state_key])
            if not city_exists:
                return {"status": "empty", "message": "No profiles found"}

    # 🎯 Base Query
    query = select(User).where(User.id != user_id)

    # 🎯 Location Filter (Rule 1)
    if selected_state:
        query = query.where(func.lower(User.state) == selected_state.lower())
    if selected_city:
        query = query.where(func.lower(User.city) == selected_city.lower())

    # 🎯 Religion (Preserving previous turn's strictness)
    ALLOWED_RELIGIONS = [
        "Hindu", "Muslim", "Christian", "Sikh", "Buddhist", "Jain",
        "Zoroastrian (Parsi)", "Jewish", "Bahá'í", "Tribal / Indigenous Faith",
        "No Religion / Atheist", "Spiritual but not Religious", "Other"
    ]
    allowed_rel_lower = [r.lower() for r in ALLOWED_RELIGIONS]
    
    # Profile data validation: religion must be in allowed list
    query = query.where(func.lower(User.religion).in_(allowed_rel_lower))
    
    rel_pref = ans.get("religion")
    if rel_pref and rel_pref.lower() not in ["any", "no preference", "any / no preference"]:
        query = query.where(func.lower(User.religion) == rel_pref.lower().strip())

    # 🎯 Gender (Hard Filter)
    gender_pref = ans.get("gender")
    if gender_pref and gender_pref.lower() not in ["any", "no preference"]:
        query = query.where(func.lower(User.gender) == gender_pref.lower())

    # 🎯 Age Range Filter (Hard Filter)
    age_range = ans.get("age_range")
    if age_range and age_range.lower() not in ["any", "no preference"]:
        try:
            parts = age_range.replace(" to ", "-").split("-")
            if len(parts) == 2:
                min_age = int(parts[0].strip())
                max_age = int(parts[1].strip())
                today = date.today()
                
                max_dob = date(today.year - min_age, today.month, today.day)
                min_dob = date(today.year - max_age - 1, today.month, today.day)
                
                query = query.where(User.date_of_birth >= min_dob)
                query = query.where(User.date_of_birth <= max_dob)
        except:
            pass # Ignore invalid age formats

    # 🎯 Execute Query for Core Matches
    result = await db.execute(query)
    all_potential = result.scalars().all()
    
    valid_matches = []

    for u in all_potential:
        # Rule 4: profile.state exists in CITIES_BY_STATE and profile.city exists under that state
        u_state_key = next((k for k in CITIES_BY_STATE if k.lower() == (u.state or "").lower()), None)
        if u_state_key:
            city_valid = any(c.lower() == (u.city or "").lower() for c in CITIES_BY_STATE[u_state_key])
            if city_valid:
                
                # 🚨 Rule 6: PROFILE PICTURE RULE
                # Check if picture exists and isn't the default, but DO NOT modify the URL string
                pic = u.profile_pic
                if not pic or pic.strip() == "" or "default.png" in pic.lower():
                    continue
                
                valid_matches.append(u)

    # 🎯 No Match Case (Rule 7)
    if not valid_matches:
        return {"status": "empty", "message": "No profiles found"}

    # Helper function to perform soft matching
    def soft_match(pref_val: str | None, profile_val: str | None) -> bool:
        if not pref_val:
            return True  # No preference chosen -> match
        pref = pref_val.lower().strip()
        if pref in ["any", "no preference", "any caste", "any / no preference", "any state", "any city", "any language", "any degree", "any age"]:
            return True
        if not profile_val:
            return False
        prof = profile_val.lower().strip()
        return pref in prof or prof in pref

    # 🎯 Calculate Dynamic Match Percentage for each profile
    profiles_out = []
    for u in valid_matches:
        # Check all soft filters
        soft_filters = [
            ("profession", u.profession),
            ("caste", u.caste),
            ("diet", u.diet),
            ("marital_status", u.marital_status),
            ("education", u.education),
            ("mother_tongue", u.mother_tongue),
            ("relationship_type", u.relationship_type),
        ]
        
        total_checked = 0
        total_matched = 0
        
        for field_name, profile_value in soft_filters:
            pref_value = ans.get(field_name)
            # Only count as "checked" if user explicitly requested a specific filter (not 'any' or 'no preference')
            if pref_value and pref_value.lower() not in ["any", "no preference", "any caste", "any / no preference", "any state", "any city"]:
                total_checked += 1
                if soft_match(pref_value, profile_value):
                    total_matched += 1

        if total_checked > 0:
            match_score = 75 + int((total_matched / total_checked) * 25)
        else:
            match_score = 95

        profiles_out.append({
            "id": str(u.id),
            "name": f"{u.first_name} {u.last_name}",
            "state": u.state,
            "city": u.city,
            "religion": u.religion,
            "profile_pic": u.profile_pic,
            "first_name": u.first_name,
            "profession": u.profession,
            "date_of_birth": u.date_of_birth.isoformat() if u.date_of_birth else None,
            "match_score": match_score
        })

    # Sort profiles so the highest match score is first
    profiles_out.sort(key=lambda x: x["match_score"], reverse=True)

    # 🎯 Shuffling within same scores to keep things dynamic
    # (Optional, but sorting is prioritized by match_score)

    return {
        "status": "success",
        "count": len(profiles_out),
        "profiles": profiles_out
    }

# =====================================================================
# REFERRAL & WALLET SYSTEM 🔥 (ATOMIC UPDATES APPLIED)
# =====================================================================

@app.get("/referral/validate/{code}")
async def validate_referral_code(code: str, db=Depends(get_db)):
    result = await db.execute(select(User).where(User.referral_code == code.upper()))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="Invalid referral code")
    return {"valid": True, "referrer_name": user.first_name, "code": user.referral_code}


@app.get("/referral/my-code")
async def get_my_referral_code(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "referral_code": user.referral_code,
        "share_link": f"https://apnasaadhi.com/register?ref={user.referral_code}",
        "local_link": f"http://localhost:5173/register?ref={user.referral_code}",
        "coin_balance": user.coin_balance or 0,
    }


# ── New: Referral Coin Stats ──
@app.get("/referral/stats")
async def get_referral_stats(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    """
    Returns detailed breakdown of:
    - How many referrals done (total & successful)
    - Coins earned from referrals
    - Next milestone progress
    - Coins the logged-in user received as a referred person
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Referrals this user made
    refs_result = await db.execute(select(Referral).where(Referral.referrer_id == user_id))
    refs = refs_result.scalars().all()
    total_referrals    = len(refs)
    successful_refs    = sum(1 for r in refs if r.reward_given)

    # Coins earned from referrals (transactions)
    txn_result = await db.execute(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.description.like("Referral reward%")
        )
    )
    referral_txns      = txn_result.scalars().all()
    coins_from_refs    = sum(t.amount for t in referral_txns)

    # Milestone bonus coins earned
    bonus_txns = [t for t in referral_txns if "milestone bonus" in (t.description or "")]
    milestone_bonus_coins = sum(t.amount - 10 for t in bonus_txns)  # subtract base 10

    # Coins this user received as a referred person (welcome bonus)
    welcome_txn_result = await db.execute(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.description.like("Welcome bonus%")
        )
    )
    welcome_coins = sum(t.amount for t in welcome_txn_result.scalars().all())

    # Next milestone progress
    MILESTONES = [
        {"count": 5,  "bonus": 20,  "label": "Going Viral"},
        {"count": 10, "bonus": 50,  "label": "Super Sharer"},
        {"count": 20, "bonus": 0,   "label": "Silver Unlock"},
        {"count": 50, "bonus": 100, "label": "Ambassador"},
    ]
    next_ms = next((m for m in MILESTONES if successful_refs < m["count"]), None)

    return {
        "total_referrals": total_referrals,
        "successful_referrals": successful_refs,
        "pending_referrals": total_referrals - successful_refs,
        "coins_from_referrals": coins_from_refs,
        "milestone_bonus_coins": milestone_bonus_coins,
        "welcome_coins_received": welcome_coins,
        "total_coin_balance": user.coin_balance or 0,
        "next_milestone": next_ms,
        # Coin rules summary
        "reward_rules": {
            "referrer_base_coins": 10,
            "referred_welcome_coins": 5,
            "milestone_5_bonus": 20,
            "milestone_10_bonus": 50,
        }
    }



app.get("/referral/history")
async def get_referral_history(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    refs_result = await db.execute(select(Referral).where(Referral.referrer_id == user_id))
    refs = refs_result.scalars().all()

    history = []

    for ref in refs:
        ru = await get_user_by_id(db, ref.referred_id)
        if not ru:
            continue

        profile_pct = ru.profile_completed or 0
        # reward_given=True means coins were already credited (at registration time)
        status = "Completed" if ref.reward_given else "Pending"

        # Look up actual coins earned from the transaction record
        txn_res = await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.description.like(f"Referral reward: {ru.first_name}%")
            ).order_by(Transaction.created_at.desc()).limit(1)
        )
        txn = txn_res.scalars().first()
        coins_earned = txn.amount if txn else (10 if ref.reward_given else 0)

        history.append({
            "referred_name": f"{ru.first_name} {ru.last_name}",
            "status": status,
            "coins_earned": coins_earned,
            "profile_completion": profile_pct,
        })

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
    user = await get_user_by_id(db, user_id)
    
    if not user or (user.profile_completed or 0) < 100:
        return {"rewarded": False, "message": "Profile not yet 100%"}
        
    ref_result = await db.execute(
        select(Referral).where(Referral.referred_id == user_id, Referral.reward_given == False)
    )
    ref_row = ref_result.scalars().first()
    
    if not ref_row:
        return {"rewarded": False, "message": "No pending referral reward"}
        
    # Prevent concurrent triggers by immediately marking as true and flushing
    ref_row.reward_given = True
    await db.flush()
    
    # Count total successful referrals by this referrer (including this one)
    done_count_res = await db.execute(
        select(Referral).where(Referral.referrer_id == ref_row.referrer_id, Referral.reward_given == True)
    )
    done_count = len(done_count_res.scalars().all())

    # ── Referrer gets 10 coins + milestone bonuses ──
    referrer_coins = 10
    description_parts = ["Referral reward: +10 Apna Coins"]

    if done_count == 5:
        referrer_coins += 20
        description_parts.append("+20 milestone bonus (5 referrals!)")
    elif done_count == 10:
        referrer_coins += 50
        description_parts.append("+50 milestone bonus (10 referrals!)")

    from crud import _credit_coins
    await _credit_coins(db, ref_row.referrer_id, referrer_coins, " | ".join(description_parts))
    await db.commit()
    
    return {"rewarded": True, "coins_awarded": referrer_coins, "milestone_bonus": referrer_coins - 10}
@app.get("/wallet/info")
async def get_wallet_info(db: AsyncSession = Depends(get_db), user_id: int = Depends(get_current_user)):
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    txn_result = await db.execute(
        select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.desc())
    )
    txns = txn_result.scalars().all()
    total_earned: int = sum(t.amount for t in txns if t.amount > 0)  # type: ignore[assignment, arg-type]
    total_spent: int = abs(sum(t.amount for t in txns if t.amount < 0))  # type: ignore[assignment, arg-type]
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
    user = await get_user_by_id(db, user_id)
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
        u = await get_user_by_id(db, rid)
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
    """
    email = data.email.lower().strip()

    # Invalidate all previous unused OTPs for this email using dynamic pgp_sym_decrypt
    prev_result = await db.execute(
        select(OTPCode).where(
            func.pgp_sym_decrypt(OTPCode.email_encrypted, PG_SECRET) == email,
            OTPCode.is_used == False,
        )
    )
    for old_otp in prev_result.scalars().all():
        old_otp.is_used = True  # type: ignore[assignment]

    # Generate new OTP
    otp = _generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    print(f"\n[DEBUG] Generated OTP for {email}: {otp}\n")
    
    new_otp = OTPCode(
        email_encrypted=func.pgp_sym_encrypt(email, PG_SECRET),  # 🔥 ENCRYPT ON INSERT
        otp_code=otp,
        expires_at=expires_at,
        is_used=False,
    )
    db.add(new_otp)
    await db.commit()

    # ── Send OTP ──────────────────────────────────────────────────────
    smtp_ok = _smtp_is_configured()
    email_sent = False

    if smtp_ok:
        try:
            await _send_otp_email(email, otp)
            email_sent = True
        except Exception as exc:
            print(f"[send-otp] WARNING: SMTP send failed: {exc}")
            print("[send-otp] Falling back to DEV MODE - OTP printed below.")

    if not email_sent:
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
    """
    email = data.email.lower().strip()
    now = datetime.now(timezone.utc)

    # Find the latest unused OTP for this email
    result = await db.execute(
        select(OTPCode)
        .where(
            func.pgp_sym_decrypt(OTPCode.email_encrypted, PG_SECRET) == email,
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
        otp_record.is_used = True  # type: ignore[assignment]
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
    otp_record.is_used = True  # type: ignore[assignment]
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
    """
    ticket = await create_support_ticket(
        db=db,
        email=data.email,
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

    # 🔥 UPDATED: Soft Deleting Instead of Hard Deleting
    if type == "everyone":
        if msg.sender_id != user_id:
            raise HTTPException(status_code=403, detail="You can only delete your own messages for everyone")

        # Soft delete: update the message text and remove media
        msg.is_deleted = True  # type: ignore[assignment]
        msg.message = func.pgp_sym_encrypt("🚫 This message was deleted", PG_SECRET)  # type: ignore[assignment]
        msg.media_url = None  # type: ignore[assignment]

        await db.commit()
        return {"status": "deleted for everyone"}

    elif type == "me":
        # Mark as deleted only for the person requesting it
        if msg.sender_id == user_id:
            msg.deleted_by_sender = True  # type: ignore[assignment]
        elif msg.receiver_id == user_id:
            msg.deleted_by_receiver = True  # type: ignore[assignment]
        else:
            raise HTTPException(status_code=403, detail="Not authorized to delete this message")

        await db.commit()
        return {"status": "deleted for me"}

    raise HTTPException(status_code=400, detail="Invalid delete type")


@app.post("/auth/forgot-password/send-otp")
async def forgot_password_send_otp(
    data: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Checks if user exists, then sends a password reset OTP."""
    email = data.email.lower().strip()

    # 1. Verify the user actually exists before sending an OTP
    user = await get_user_by_email(db, email)
    
    if not user:
        raise HTTPException(status_code=404, detail="No account found with this email.")

    # 2. Invalidate previous unused OTPs for this email
    prev_result = await db.execute(
        select(OTPCode).where(func.pgp_sym_decrypt(OTPCode.email_encrypted, PG_SECRET) == email, OTPCode.is_used == False)
    )
    for old_otp in prev_result.scalars().all():
        old_otp.is_used = True  # type: ignore[assignment]

    # 3. Generate new OTP and save to DB
    otp = _generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    
    # 🔥 ADDED: Print OTP to the terminal immediately for easy testing
    print(f"\n[DEBUG] Forgot Password OTP for {email}: {otp}\n")
    
    new_otp = OTPCode(
        email_encrypted=func.pgp_sym_encrypt(email, PG_SECRET),
        otp_code=otp,
        expires_at=expires_at,
        is_used=False,
    )
    db.add(new_otp)
    await db.commit()

    # 4. Send the Email (Reusing your existing SMTP logic)
    smtp_ok = _smtp_is_configured()
    email_sent = False

    if smtp_ok:
        try:
            await _send_otp_email(email, otp)
            email_sent = True
        except Exception as exc:
            print(f"[forgot-pwd] WARNING: SMTP send failed: {exc}")

    if not email_sent:
        # DEV MODE fallback print
        print("\n" + "=" * 56)
        print(f"  [FORGOT PASSWORD] OTP for {email}")
        print(f"  [CODE]  {otp}")
        print("=" * 56 + "\n")
        return {
            "message": "[DEV MODE] OTP printed to server console.", 
            "dev_mode": True
        }

    return {"message": "Password reset OTP sent to your email.", "dev_mode": False}


@app.post("/auth/forgot-password/reset")
async def reset_password(
    data: ResetPasswordConfirm,
    db: AsyncSession = Depends(get_db),
):
    """Verifies the OTP and updates the user's password."""
    email = data.email.lower().strip()
    now = datetime.now(timezone.utc)

    # 1. Fetch the latest unused OTP
    result = await db.execute(
        select(OTPCode)
        .where(func.pgp_sym_decrypt(OTPCode.email_encrypted, PG_SECRET) == email, OTPCode.is_used == False)
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    otp_record = result.scalars().first()

    # 2. Validate OTP existence, expiry, and match
    if not otp_record:
        raise HTTPException(status_code=400, detail="No valid OTP found. Please request a new one.")

    if otp_record.expires_at.replace(tzinfo=timezone.utc) < now:
        otp_record.is_used = True  # type: ignore[assignment]
        await db.commit()
        raise HTTPException(status_code=410, detail="OTP has expired. Please request a new one.")

    if otp_record.otp_code != data.otp.strip():
        raise HTTPException(status_code=400, detail="Incorrect OTP. Please try again.")

    # 3. OTP is valid -> Mark it as used
    otp_record.is_used = True  # type: ignore[assignment]

    # 4. Fetch User and update password
    user = await get_user_by_email(db, email)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Reusing your existing hash_password function from auth.py
    user.password = hash_password(data.new_password)
    
    await db.commit()

    return {"message": "Password reset successfully. You can now log in."}


# =====================
# SEARCH USERS BY NAME / ID
# =====================
@app.get("/users/search")
async def search_users_by_name(
    q: str,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    if not q or len(q.strip()) < 2:
        return []

    # Get blocked IDs so we don't show them in search results
    blocked_check = await db.execute(
        select(BlockedUser).where(
            or_(
                BlockedUser.user_id == user_id,
                BlockedUser.blocked_user_id == user_id
            )
        )
    )
    blocked_ids = {b.blocked_user_id if b.user_id == user_id else b.user_id for b in blocked_check.scalars().all()}

    search_term = f"%{q.strip()}%"

    # Search by concatenated first/last name or profile ID
    query = select(User).where(
        and_(
            User.id != user_id,
            or_(
                func.concat(User.first_name, ' ', User.last_name).ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.profile_id.ilike(search_term)
            )
        )
    ).limit(10) # Limit to 10 results for quick UI response

    result = await db.execute(query)
    users = result.scalars().all()

    safe_users = []
    for u in users:
        if u.id in blocked_ids:
            continue
            
        # Profile Visibility Check
        visibility = u.profile_visibility or "public"
        if visibility == "premium_only":
            continue # Hide premium-only profiles from global free search

        safe_users.append(sanitize_user_dict(u))

    return safe_users


# =====================
# ACCOUNT DEACTIVATION & DELETION
# =====================

@app.post("/account/deactivate")
async def deactivate_account_endpoint(
    data: DeactivateAccountRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    """
    Deactivate user account. User can reactivate within 30 days.
    """
    try:
        # Get user details
        user = await get_user_by_id(db, user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Deactivate account
        deactivation_result = await deactivate_account(db, user_id, reason=data.reason)
        
        if deactivation_result:
            # Send email notification
            send_deactivation_email(
                user.email,
                user.first_name,
                deactivation_result["reactivation_deadline"]
            )
            
            return {
                "status": "deactivated",
                "message": "Account deactivated successfully. You can reactivate within 30 days.",
                "reactivation_deadline": deactivation_result["reactivation_deadline"],
                "email": user.email
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deactivating account: {str(e)}")


@app.post("/account/delete")
async def delete_account_endpoint(
    data: DeleteAccountRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    """
    Permanently delete user account. Requires password verification.
    """
    try:
        # Get user
        user = await get_user_by_id(db, user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Verify password
        if not verify_password(data.password, user.password):
            raise HTTPException(status_code=401, detail="Incorrect password. Account deletion cancelled.")
        
        # Delete account
        deletion_result = await delete_account_permanently(db, user_id, reason=data.reason)
        
        if deletion_result:
            # Send email notification
            send_deletion_email(user.email, user.first_name)
            
            return {
                "status": "deleted",
                "message": "Account permanently deleted. You will be logged out shortly.",
                "email": user.email
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting account: {str(e)}")


@app.get("/account/deactivation-status")
async def get_deactivation_status(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    """
    Check if user account is deactivated and get reactivation deadline.
    """
    user = await get_user_by_id(db, user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "is_deactivated": user.is_deactivated,
        "deactivation_date": user.deactivation_date,
        "reactivation_deadline": user.reactivation_deadline,
        "is_active": user.is_active
    }

# -------------------------------------------Admin Starts Here ----------------------------------------------------- 
# =====================================================================
# 🛡️ ADMIN SYSTEM ROUTES
# =====================================================================


from crud import (
    create_admin_user,
    authenticate_admin,
    get_admin_dashboard_stats,
    get_all_users_for_admin,
    toggle_user_ban_status,
    get_all_reports_for_admin,
    get_admin_user_profile_details
)

@app.post("/admin/register", response_model=AdminOut, tags=["Admin System"])
async def register_admin(
    data: AdminCreate,
    db: AsyncSession = Depends(get_db)
):
    from models import Admin

    existing = await db.execute(
        select(Admin).where(
            Admin.username == data.username.lower()
        )
    )

    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Admin username already exists"
        )

    return await create_admin_user(db, data)


@app.post("/admin/login", tags=["Admin System"])
async def login_admin(
    data: AdminLogin,
    db: AsyncSession = Depends(get_db)
):
    admin = await authenticate_admin(
        db,
        data.username,
        data.password
    )

    if not admin:
        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials"
        )

    token = create_access_token(
        admin.id,
        role="admin"
    )

    return {
        "access_token": token,
        "admin_id": admin.id,
        "username": admin.username,
        "is_superadmin": admin.is_superadmin
    }


@app.get("/admin/dashboard/stats", tags=["Admin System"])
async def admin_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    return await get_admin_dashboard_stats(db)


@app.get("/admin/users", tags=["Admin System"])
async def admin_list_users(
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    return await get_all_users_for_admin(db)


@app.get("/admin/users/{user_id}", tags=["Admin System"])
async def admin_user_profile_details(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    return await get_admin_user_profile_details(db, user_id)


@app.post("/admin/users/{target_id}/toggle-ban", tags=["Admin System"])
async def admin_toggle_ban(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    user = await toggle_user_ban_status(db, target_id)

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    status_text = "unbanned" if user.is_active else "banned"

    return {
        "message": f"User successfully {status_text}",
        "is_active": user.is_active
    }


@app.get("/admin/reports", tags=["Admin System"])
async def admin_list_reports(
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    return await get_all_reports_for_admin(db)


@app.get("/admin/user/{user_id}/profile-overview", tags=["Admin System"])
async def admin_user_profile_overview(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_id: int = Depends(get_current_admin)
):
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalars().first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    txn_result = await db.execute(select(Transaction).where(Transaction.user_id == user_id))
    txns = txn_result.scalars().all()
    
    total_coins = sum(t.amount for t in txns if t.amount > 0)
    earned_profile = sum(t.amount for t in txns if t.amount > 0 and ("profile" in t.description.lower() or "completion" in t.description.lower()))
    earned_actions = total_coins - earned_profile
    
    return {
        "coin": {
            "total_coins": total_coins,
            "available_coins": user.coin_balance or 0,
            "earned_from_profile_completion": earned_profile,
            "earned_from_actions": earned_actions
        }
    }
# ==========================================
# ADMIN SETTINGS
# ==========================================

@app.get("/admin/settings")
async def admin_settings(
    db: AsyncSession = Depends(get_db)
):
    return await get_admin_settings_data(db)



# Safety & Moderation Pipeline Integrity Verified