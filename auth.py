from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
import os

# =====================
# CONFIG (ENV BASED)
# =====================
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# =====================
# PASSWORD HELPERS
# =====================
def _normalize_password(password: str) -> str:
    return password.encode("utf-8")[:72].decode("utf-8", errors="ignore")

def hash_password(password: str) -> str:
    return pwd_context.hash(_normalize_password(password))

def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_context.verify(_normalize_password(password), hashed_password)

# =====================
# JWT (WITH ROLE TRACKING)
# =====================
def create_access_token(subject_id: int, role: str = "user") -> str:
    """
    Creates a token and embeds the role. Defaults to 'user'.
    For admins, you will call: create_access_token(admin_id, role="admin")
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(subject_id),
        "role": role,  # 🔥 Tracks if this is a user or admin
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

# =====================
# REGULAR USER AUTH
# =====================
def get_current_user(token: str = Depends(oauth2_scheme)) -> int:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        role = payload.get("role")
        
        # Must be a user, not an admin
        if user_id is None or role != "user":
            raise credentials_exception
        return int(user_id)
    except JWTError:
        raise credentials_exception

# =====================
# ADMIN AUTH (NEW)
# =====================
def get_current_admin(token: str = Depends(oauth2_scheme)) -> int:
    credentials_exception = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized as admin. Access denied.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        admin_id = payload.get("sub")
        role = payload.get("role")
        
        # Reject regular users trying to access admin routes
        if admin_id is None or role != "admin":
            raise credentials_exception
        return int(admin_id)
    except JWTError:
        raise credentials_exception