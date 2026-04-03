import json
import shutil
import os
from datetime import date

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, or_, and_

from db import engine, SessionLocal
from models import Base, User, Message, Interaction
from schemas import RegisterUser, LoginUser, UserResponse, MessageCreate, UpdateUser, InteractionCreate , MatchmakerQuizParams
from crud import (
    create_user,
    authenticate_user,
    get_user_by_email,
    get_all_users,
    save_message,
    get_messages,
    get_user_by_mobile
)
from auth import create_access_token, get_current_user, verify_password

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

        # ⚠️ COMMENT THESE OUT IF YOUR DB IS ALREADY WORKING!
        # await conn.run_sync(Base.metadata.drop_all)
        # await conn.run_sync(Base.metadata.create_all)

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

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "user_id": user.id,
        "first_name": user.first_name,
    }

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

    return {"message": "Profile updated successfully"}

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
# USERS
# =====================
@app.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    return await get_all_users(db, user_id)


# =====================
# MATCHMAKING LOGIC 🔥
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

@app.get("/matchmaking/suggested")
async def get_suggested_matches(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    current_user_res = await db.execute(select(User).where(User.id == user_id))
    current_user = current_user_res.scalars().first()

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


@app.post("/chat/send")
async def send_message(
    data: MessageCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    return await save_message(db, user_id, data.receiver_id, data.message)


# =====================
# WEBSOCKET MANAGER
# =====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json(message)


manager = ConnectionManager()


# =====================
# WEBSOCKET ROUTE
# =====================
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            receiver_id = message_data.get("receiver_id")

            if receiver_id:
                await manager.send_personal_message(message_data, int(receiver_id))

    except WebSocketDisconnect:
        manager.disconnect(user_id)


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
    user_id: int = Depends(get_current_user) # Ensures the requester is logged in
):
    result = await db.execute(select(User).where(User.id == target_id))
    user = result.scalars().first()
    
    if not user:
        raise HTTPException(status_code=404, detail="Profile not found")
        
    # Strip password before sending!
    user_data = user.__dict__.copy()
    user_data.pop("_sa_instance_state", None)
    user_data.pop("password", None)
    
    return user_data


# =====================
# FREE "AI" QUIZ SEARCH
# =====================
@app.post("/ai-matchmaker/quiz-search")
async def ai_quiz_search(
    data: MatchmakerQuizParams,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user)
):
    # Start the query, excluding the user themselves
    query = select(User).where(User.id != user_id)
    
    ans = data.answers
    
    # Dynamically build the search query based on what the user answered
    if ans.get("city"):
        query = query.where(User.city.ilike(f"%{ans['city']}%"))
    if ans.get("profession"):
        query = query.where(User.profession.ilike(f"%{ans['profession']}%"))
    if ans.get("religion"):
        query = query.where(User.religion.ilike(f"%{ans['religion']}%"))
    if ans.get("caste"):
        query = query.where(User.caste.ilike(f"%{ans['caste']}%"))
    if ans.get("diet"):
        query = query.where(User.diet.ilike(f"%{ans['diet']}%"))
    if ans.get("marital_status"):
        query = query.where(User.marital_status.ilike(f"%{ans['marital_status']}%"))
    if ans.get("education"):
        query = query.where(User.education.ilike(f"%{ans['education']}%"))
    if ans.get("mother_tongue"):
        query = query.where(User.mother_tongue.ilike(f"%{ans['mother_tongue']}%"))
    if ans.get("habits"):
        query = query.where(User.habits.ilike(f"%{ans['habits']}%"))
    if ans.get("family_type"):
        query = query.where(User.family_type.ilike(f"%{ans['family_type']}%"))

    # Execute the search
    result = await db.execute(query)
    matches = result.scalars().all()
    
    # Strip passwords and format for frontend
    safe_matches = []
    for u in matches[:10]: # Return top 10 matches
        user_data = u.__dict__.copy()
        user_data.pop("_sa_instance_state", None)
        user_data.pop("password", None)
        
        # Give them an arbitrary high match percentage since they match the keywords
        user_data["match_percentage"] = 95
        safe_matches.append(user_data)
        
    return {"suggested_profiles": safe_matches}